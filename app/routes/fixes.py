"""
/fixes/* routes — Inline Fix Suggester endpoints.

POST /fixes/suggest        → single-issue fix suggestion
POST /fixes/suggest-batch  → multi-issue fix suggestions (max 20)
POST /fixes/apply          → apply a single fix to XML
POST /fixes/apply-batch    → apply multiple fixes to XML
POST /fixes/auto-fix       → iterative validate→fix→re-validate until clean
"""
from fastapi import APIRouter, HTTPException, Header

from typing import Dict, Optional

from app.schemas.fixes import (
    SuggestRequest,
    SuggestBatchRequest,
    FixSuggestionResponse,
    SuggestBatchResponse,
    ApplyRequest,
    ApplyBatchRequest,
    ApplyResponse,
    FeedbackRequest,
    FeedbackResponse,
    AutoFixRequest,
    AutoFixResponse,
)
from app.services.fix_suggester import (
    fix_suggester, fix_suggester_sr2025, fix_suggester_sr2026, FixApplyError,
)
from app.services import fix_feedback, fix_metrics

router = APIRouter(prefix="/fixes", tags=["fixes"])


def _suggester_for(version: str):
    """Pick the release-bound fix-suggester. The engine is shared; the instance
    carries the release so its version-specific handler module + version-scoped
    KB are used."""
    return fix_suggester_sr2026 if version == "SR2026" else fix_suggester_sr2025


def _suggestion_to_response(s) -> FixSuggestionResponse:
    return FixSuggestionResponse(
        xpath=s.xpath,
        original_fragment=s.original_fragment,
        fragment_xml=s.fragment_xml,
        issue_code=s.issue_code,
        issue_message=s.issue_message,
        confidence=s.confidence,
        verified=getattr(s, "verified", None),
    )


def _resolve_sr_version(header_value: Optional[str]) -> str:
    """Active SWIFT release for a fix request. Reads the same x-sr-version
    header as validation; defaults to SR2025 (optional here for back-compat —
    older clients that don't send it keep working)."""
    v = (header_value or "").strip()
    return v if v in ("SR2025", "SR2026") else "SR2025"


@router.post("/suggest", response_model=FixSuggestionResponse)
def suggest_fix(req: SuggestRequest, x_sr_version: Optional[str] = Header(default=None)):
    """Generate a fix suggestion for a single validation issue.

    Uses the verified path: the suggestion is applied to a throwaway copy and
    self-checked (well-formed + no XSD regression) before returning, so the
    `verified` flag tells the UI whether the fix actually holds up.
    """
    version = _resolve_sr_version(x_sr_version)
    issue_dict = req.issue.model_dump()
    suggestion = _suggester_for(version).suggest_verified(req.xml, issue_dict, version=version)
    return _suggestion_to_response(suggestion)


@router.post("/suggest-batch", response_model=SuggestBatchResponse)
def suggest_fix_batch(req: SuggestBatchRequest, x_sr_version: Optional[str] = Header(default=None)):
    """Generate fix suggestions for up to 20 validation issues."""
    if len(req.issues) > 20:
        raise HTTPException(status_code=400, detail="Maximum 20 issues per batch request.")
    version = _resolve_sr_version(x_sr_version)
    issues_list = [i.model_dump() for i in req.issues]
    suggestions = _suggester_for(version).suggest_batch(req.xml, issues_list, version=version)
    return SuggestBatchResponse(fixes=[_suggestion_to_response(s) for s in suggestions])


@router.post("/apply", response_model=ApplyResponse)
def apply_fix(req: ApplyRequest):
    """Apply a single XML fix (replace element at xpath with fragment_xml)."""
    try:
        new_xml = fix_suggester.apply(req.xml, req.xpath, req.fragment_xml)
        return ApplyResponse(new_xml=new_xml)
    except FixApplyError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/apply-batch", response_model=ApplyResponse)
def apply_fix_batch(req: ApplyBatchRequest):
    """Apply multiple XML fixes in reverse document order."""
    fixes_list = [f.model_dump() for f in req.fixes]
    try:
        new_xml = fix_suggester.apply_batch(req.xml, fixes_list)
        return ApplyResponse(new_xml=new_xml)
    except FixApplyError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/auto-fix", response_model=AutoFixResponse)
async def auto_fix(req: AutoFixRequest, x_sr_version: Optional[str] = Header(default=None)):
    """Iteratively validate → suggest → apply until the message is clean (or no
    further actionable fix can be made).  Returns the repaired XML plus stats.

    This is what the UI calls when every visible issue is selected — it handles
    cascading errors that a single-pass batch cannot resolve because each fix
    may expose new issues (e.g. fixing an empty AppHdr reveals BizSvc mismatch).
    """
    import re as _re
    from app.sr2026.validation.validators.validator import SR2026Validator

    version = _resolve_sr_version(x_sr_version)
    suggester = _suggester_for(version)

    # Auto-detect message type from namespace URI when not supplied
    msg_type = req.message_type or "Auto-detect"
    if msg_type == "Auto-detect":
        ns_matches = _re.findall(
            r'xmlns(?::\w+)?=["\']urn:iso:std:iso:20022:tech:xsd:'
            r'([a-z]{4}\.\d{3}\.\d{3}\.\d{2})["\']',
            req.xml,
        )
        msg_type = "pacs.008.001.08"
        for _m in ns_matches:
            if not _m.startswith("head."):
                msg_type = _m
                break

    _validator = SR2026Validator()
    current_xml = req.xml
    total_fixes = 0
    rounds_done = 0
    MAX_ROUNDS = 5

    for _ in range(MAX_ROUNDS):
        try:
            result = await _validator.validate(current_xml, msg_type)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Validation error: {e}")

        errors = result.errors  # List[ApiIssue], severity == "ERROR"
        if not errors:
            break

        issues = [
            {
                "severity": e.severity,
                "layer": e.layer,
                "code": e.code,
                "path": e.path,
                "message": e.message,
                "fix_suggestion": e.fix or "",
                "related_test": "",
                "line": e.line,
            }
            for e in errors[:20]
        ]

        suggestions = suggester.suggest_batch(current_xml, issues, version=version)
        fixes = [
            {"xpath": s.xpath, "fragment_xml": s.fragment_xml}
            for s in suggestions
            if s.confidence in ("high", "low")
            and s.xpath
            and s.fragment_xml
            and s.fragment_xml != s.original_fragment
        ]

        if not fixes:
            break  # no actionable fix found — stop to avoid infinite loop

        try:
            current_xml = suggester.apply_batch(current_xml, fixes)
            total_fixes += len(fixes)
            rounds_done += 1
        except FixApplyError:
            break

    # Final validation to report remaining errors to the UI
    try:
        final = await _validator.validate(current_xml, msg_type)
        remaining = final.errors
    except Exception:
        remaining = []

    return AutoFixResponse(
        new_xml=current_xml,
        rounds=rounds_done,
        fixes_applied=total_fixes,
        remaining_errors=len(remaining),
        remaining_details=[
            {
                "severity": e.severity,
                "code": e.code,
                "message": e.message,
                "path": e.path,
            }
            for e in remaining
        ],
    )


@router.post("/feedback", response_model=FeedbackResponse)
def fix_feedback_record(req: FeedbackRequest):
    """Record a user's accept / edit / reject of a suggested fix.

    Accepted and edited fixes are surfaced back to the LLM fallback as
    high-signal context for the same (issue_code, tag) next time — so the
    engine learns the shapes users actually want. Best-effort: a storage
    failure returns ok=false rather than erroring the request.
    """
    ok = fix_feedback.record_feedback(
        issue_code=req.issue_code,
        tag=req.tag,
        original_fragment=req.original_fragment,
        fragment_xml=req.fragment_xml,
        action=req.action,
        edited_xml=req.edited_xml,
    )
    return FeedbackResponse(ok=ok)


@router.get("/metrics")
def fix_metrics_snapshot() -> Dict[str, int]:
    """Read-only counters: confidence buckets, verify pass/fail, LLM
    invocations + cache hit/miss, self-consistency outcomes."""
    return fix_metrics.snapshot()
