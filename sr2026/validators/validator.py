from sr2026.validators.models import ValidationReport, ValidationIssue
from sr2026.validators.layer1 import Layer1Validator
from sr2026.validators.layer2 import Layer2Validator
from sr2026.validators.layer3 import Layer3Validator
from sr2026.nvr.nvr_engine import NVREngine
from sr2026.usage_guidelines.guideline_validator import GuidelineValidator
from sr2026.delta_rules.address_validator import AddressValidator
from sr2026.delta_rules.lei_validator import LEIValidator
from sr2026.delta_rules.tax_validator import TaxValidator
from sr2026.delta_rules.new_mandatory_fields import NewMandatoryFieldsValidator
from sr2026.delta_rules.warning_engine import WarningEngine
from app.schemas.api_validation import ApiValidateResponse, ApiIssue

class SR2026Validator:
    def __init__(self, history_service=None):
        self.history_service = history_service

    async def validate(self, xml_content: str, message_type: str) -> ApiValidateResponse:
        report = ValidationReport()
        
        # Step 1: Layer 1 Technical well-formedness
        root_element = Layer1Validator.validate(xml_content, report)
        if root_element is None:
            return self._build_response(report)
            
        # Step 2: Layer 2 XSD Validation
        Layer2Validator.validate(root_element, message_type, report)

        # Step 3: Usage Guidelines
        GuidelineValidator.validate(root_element, report)

        # Step 4: Network Validated Rules (NVR)
        NVREngine.validate(root_element, report)

        # Step 5: Normalize and run Layer 3 check
        canonical_data, line_map = Layer3Validator.normalize(root_element)
        Layer3Validator.validate(canonical_data, line_map, message_type, report)

        # Step 6: Delta rules - Structured address
        AddressValidator.validate(root_element, report)

        # Step 7: Delta rules - LEI validation
        LEIValidator.validate(root_element, report)

        # Step 8: Delta rules - Tax block validation
        TaxValidator.validate(root_element, report)

        # Step 9: Delta rules - New mandatory fields
        NewMandatoryFieldsValidator.validate(root_element, report)

        # Step 10: Warning Engine
        WarningEngine.evaluate(root_element, report)

        return self._build_response(report)

    def _build_response(self, report: ValidationReport) -> ApiValidateResponse:
        errors = []
        warnings = []
        info = []
        
        # Deduplicate issues to keep the response clean
        seen_issues = set()
        
        for issue in report.issues:
            # Create a signature
            sig = (issue.severity, issue.code, issue.path, issue.message, issue.line)
            if sig in seen_issues:
                continue
            seen_issues.add(sig)

            api_issue = ApiIssue(
                code=issue.code,
                path=issue.path,
                message=issue.message,
                line=issue.line,
                fix=issue.fix or ""
            )
            
            if issue.severity == "ERROR":
                errors.append(api_issue)
            elif issue.severity == "WARNING":
                warnings.append(api_issue)
            else:
                info.append(api_issue)
                
        status = "FAILED" if errors else "PASSED"
        
        return ApiValidateResponse(
            status=status,
            errors=errors,
            warnings=warnings,
            info=info
        )
