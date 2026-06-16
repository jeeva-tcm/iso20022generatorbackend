"""
/fixes/* routes — Inline Fix Suggester endpoints.

POST /fixes/suggest        → single-issue fix suggestion
POST /fixes/suggest-batch  → multi-issue fix suggestions (max 20)
POST /fixes/apply          → apply a single fix to XML
POST /fixes/apply-batch    → apply multiple fixes to XML
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
