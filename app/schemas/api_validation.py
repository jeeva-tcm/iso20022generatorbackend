from pydantic import BaseModel
from typing import List, Optional

class ApiIssue(BaseModel):
    code: str
    path: str
    message: str
    line: Optional[int] = None
    fix: Optional[str] = None

class ApiValidateRequest(BaseModel):
    version: str
    messageType: str
    xml: str

class ApiValidateResponse(BaseModel):
    status: str  # "PASSED" or "FAILED"
    errors: List[ApiIssue] = []
    warnings: List[ApiIssue] = []
    info: List[ApiIssue] = []
