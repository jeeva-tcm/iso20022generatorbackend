from datetime import datetime, timezone

import re
from typing import Optional

class ValidationIssue:
    def __init__(self, severity: str, layer: int, code: str, path: str, message: str, fix_suggestion: str = "", related_test: str = "", line: Optional[int] = None):
        self.severity = severity
        self.layer = layer
        self.code = code
        self.path = path
        self.message = message
        self.fix_suggestion = fix_suggestion
        self.related_test = related_test
        self.line = line

        # Dynamic backward compatibility check: if line is not explicitly provided,
        # but path is numeric, extract it as the line number.
        if self.line is None and path:
            trimmed_path = str(path).strip()
            if trimmed_path.isdigit():
                self.line = int(trimmed_path)
                self.path = "/"
            elif "Line" in trimmed_path:
                match = re.search(r'\b\d+\b', trimmed_path)
                if match:
                    self.line = int(match.group(0))
                    self.path = "/"

    def to_dict(self):
        return {
            "severity": self.severity,
            "layer": self.layer,
            "code": self.code,
            "path": self.path,
            "message": self.message,
            "fix_suggestion": self.fix_suggestion,
            "related_test": self.related_test,
            "line": self.line
        }

class ValidationReport:
    def __init__(self, validation_id: str, message_type: str, mode: str):
        self.validation_id = validation_id
        self.timestamp = datetime.now(timezone.utc).isoformat()
        self.message_type = message_type
        self.mode = mode
        self.status = "PASS"
        self.schema_version = "Unknown"
        self.errors = 0
        self.warnings = 0
        self.total_time_ms = 0
        self.layer_status = {}
        self.issues = []
        self.metadata = {}

    def add_issue(self, issue: ValidationIssue):
        self.issues.append(issue.to_dict())
        if issue.severity in ["ERROR", "CRITICAL"]:
            self.errors += 1
            self.status = "FAIL"
        elif issue.severity == "WARNING":
            self.warnings += 1
            # User Request: Treat WARNING as PASS
            # if self.status != "FAIL":
            #     self.status = "WARNING"

    def to_dict(self):
        # Step 9: Generate Validation Report Format
        return {
            "validation_id": self.validation_id,
            "timestamp": self.timestamp,
            "status": self.status,
            "schema": self.schema_version,
            "message": self.message_type,
            "errors": self.errors,
            "warnings": self.warnings,
            "total_time_ms": round(self.total_time_ms, 2),
            "layer_status": self.layer_status,
            "details": self.issues,
            "metadata": self.metadata
        }
