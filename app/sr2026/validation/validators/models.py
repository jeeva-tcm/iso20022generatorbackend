from typing import List, Optional

class ValidationIssue:
    def __init__(self, severity: str, *args, **kwargs):
        self.severity = severity  # "ERROR", "WARNING", "INFO"
        self.layer = 3
        
        if len(args) >= 3 and isinstance(args[0], int):
            # Signature: (severity, layer, code, path, message, fix_suggestion, line)
            self.layer = args[0]
            self.code = args[1]
            self.path = args[2]
            self.message = args[3]
            self.fix = args[4] if len(args) > 4 else kwargs.get("fix", kwargs.get("fix_suggestion", ""))
            self.line = args[5] if len(args) > 5 else kwargs.get("line", None)
        else:
            # Standard signature: (severity, code, path, message, line, fix)
            self.code = args[0] if len(args) > 0 else kwargs.get("code", "")
            self.path = args[1] if len(args) > 1 else kwargs.get("path", "")
            self.message = args[2] if len(args) > 2 else kwargs.get("message", "")
            self.line = args[3] if len(args) > 3 else kwargs.get("line", None)
            self.fix = args[4] if len(args) > 4 else kwargs.get("fix", None)
            self.layer = kwargs.get("layer", 3)

        # Normalize line number if it was passed as path (when path is numeric)
        if isinstance(self.path, str) and self.path.isdigit() and (self.line is None or self.line == 1 or self.line == "Unknown"):
            self.line = int(self.path)
            self.path = ""

        # Normalize line to int
        if self.line is not None:
            if isinstance(self.line, str):
                if self.line.isdigit():
                    self.line = int(self.line)
                else:
                    self.line = 1
            elif not isinstance(self.line, int):
                self.line = 1
        else:
            self.line = 1

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
