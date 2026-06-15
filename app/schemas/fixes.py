"""Pydantic schemas for the /fixes/* endpoints."""
from pydantic import BaseModel
from typing import List, Optional


class IssueRef(BaseModel):
    severity: str = ""
    layer: int = 0
    code: str = ""
    path: str = ""
    message: str = ""
    fix_suggestion: str = ""
    related_test: str = ""
    line: Optional[int] = None


class SuggestRequest(BaseModel):
    xml: str
    issue: IssueRef


class SuggestBatchRequest(BaseModel):
    xml: str
    issues: List[IssueRef]


class FixSuggestionResponse(BaseModel):
    xpath: str
    original_fragment: str
    fragment_xml: str
    issue_code: str
    issue_message: str
    confidence: str  # 'high' | 'low' | 'unavailable' | 'resolved'
    # Closed-loop self-check (set on single /suggest only): True=verified safe,
    # False=fix regressed/failed, None=not judged. Optional so existing clients
    # that don't read it are unaffected.
    verified: Optional[bool] = None


class SuggestBatchResponse(BaseModel):
    fixes: List[FixSuggestionResponse]


class FeedbackRequest(BaseModel):
    issue_code: str = ""
    tag: str = ""
    original_fragment: str = ""
    fragment_xml: str = ""
    action: str = ""               # 'accept' | 'edit' | 'reject'
    edited_xml: Optional[str] = None


class FeedbackResponse(BaseModel):
    ok: bool


class ApplyRequest(BaseModel):
    xml: str
    xpath: str
    fragment_xml: str


class ApplyBatchFix(BaseModel):
    xpath: str
    fragment_xml: str


class ApplyBatchRequest(BaseModel):
    xml: str
    fixes: List[ApplyBatchFix]


class ApplyResponse(BaseModel):
    new_xml: str


class AutoFixRequest(BaseModel):
    xml: str
    message_type: str = "Auto-detect"


class AutoFixResponse(BaseModel):
    new_xml: str
    rounds: int                 # validate→fix iterations performed
    fixes_applied: int          # total fixes applied across all rounds
    remaining_errors: int       # ERROR-severity issues still present after fixing
    remaining_details: List[dict] = []  # the residual issues (for the UI)
