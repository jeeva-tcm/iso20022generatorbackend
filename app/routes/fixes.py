"""
/fixes/* routes — Inline Fix Suggester endpoints.

POST /fixes/suggest        → single-issue fix suggestion
POST /fixes/suggest-batch  → multi-issue fix suggestions (max 20)
POST /fixes/apply          → apply a single fix to XML
POST /fixes/apply-batch    → apply multiple fixes to XML
"""
from fastapi import APIRouter, HTTPException

from app.schemas.fixes import (
    SuggestRequest,
    SuggestBatchRequest,
    FixSuggestionResponse,
    SuggestBatchResponse,
    ApplyRequest,
    ApplyBatchRequest,
    ApplyResponse,
)
from app.services.fix_suggester import fix_suggester, FixApplyError

router = APIRouter(prefix="/fixes", tags=["fixes"])


def _suggestion_to_response(s) -> FixSuggestionResponse:
    return FixSuggestionResponse(
        xpath=s.xpath,
        original_fragment=s.original_fragment,
        fragment_xml=s.fragment_xml,
        issue_code=s.issue_code,
        issue_message=s.issue_message,
        confidence=s.confidence,
    )


@router.post("/suggest", response_model=FixSuggestionResponse)
def suggest_fix(req: SuggestRequest):
    """Generate a fix suggestion for a single validation issue."""
    issue_dict = req.issue.model_dump()
    suggestion = fix_suggester.suggest(req.xml, issue_dict)
    return _suggestion_to_response(suggestion)


@router.post("/suggest-batch", response_model=SuggestBatchResponse)
def suggest_fix_batch(req: SuggestBatchRequest):
    """Generate fix suggestions for up to 20 validation issues."""
    if len(req.issues) > 20:
        raise HTTPException(status_code=400, detail="Maximum 20 issues per batch request.")
    issues_list = [i.model_dump() for i in req.issues]
    suggestions = fix_suggester.suggest_batch(req.xml, issues_list)
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
