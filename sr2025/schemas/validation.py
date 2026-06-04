from datetime import datetime, timezone
from pydantic import BaseModel, Field, field_validator
from typing import List, Dict, Any, Optional

class ValidationRequest(BaseModel):
    xml_content: str
    mode: str = "Full 1-3"
    message_type: str = "Auto-detect"
    store_in_history: bool = True
    batch_id: Optional[str] = None
    file_id: Optional[str] = None
    origin: Optional[str] = "Manual Entry"

class BatchInitRequest(BaseModel):
    file_count: int

class MTConversionRequest(BaseModel):
    mt_message: str
    target_mt_type: Optional[str] = None
    store_in_history: bool = True

class IssueSchema(BaseModel):
    severity: str
    layer: int
    code: str
    path: str
    message: str
    fix_suggestion: str
    related_test: str
    line: Optional[int] = None

class ValidationResponse(BaseModel):
    validation_id: str
    timestamp: str
    status: str
    xsd_schema: str = Field(alias="schema")
    message: str
    errors: int
    warnings: int
    total_time_ms: float
    layer_status: Dict[str, Any]
    details: List[IssueSchema]
    file_id: Optional[str] = None
    batch_id: Optional[str] = None

    class Config:
        allow_population_by_field_name = True

class HistorySummary(BaseModel):
    validation_id: str
    batch_id: Optional[str] = None
    file_id: Optional[str] = None
    timestamp: Any  # Keep as Any to handle the manual conversion
    message_type: str
    status: str
    total_errors: int
    total_warnings: int
    execution_time_ms: float
    origin: Optional[str] = "Pasted"

    @field_validator('timestamp', mode='before')
    @classmethod
    def format_timestamp(cls, v):
        if isinstance(v, datetime):
            # Ensure it has UTC info and ends with Z for JS compatibility
            if v.tzinfo is None:
                v = v.replace(tzinfo=timezone.utc)
            return v.strftime("%Y-%m-%dT%H:%M:%SZ")
        return v

    class Config:
        from_attributes = True

class DashboardStats(BaseModel):
    total_audits: int
    passed_messages: int
    failed_messages: int
    validation_quality: int  # Percentage
