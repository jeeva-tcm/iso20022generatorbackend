from typing import List, Optional

class ValidationIssue:
    def __init__(self, severity: str, code: str, path: str, message: str, line: Optional[int] = None, fix: Optional[str] = None):
        self.severity = severity  # "ERROR", "WARNING", "INFO"
        self.code = code
        self.path = path
        self.message = message
        self.line = line
        self.fix = fix

    def to_dict(self):
        return {
            "code": self.code,
            "path": self.path,
            "message": self.message,
            "line": self.line,
            "fix": self.fix
        }

class ValidationReport:
    def __init__(self):
        self.status = "PASSED"
        self.issues: List[ValidationIssue] = []

    def add_issue(self, issue: ValidationIssue):
        self.issues.append(issue)
        if issue.severity == "ERROR":
            self.status = "FAILED"
