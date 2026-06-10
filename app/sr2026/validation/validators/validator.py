from app.sr2026.validation.validators.models import ValidationReport, ValidationIssue
from app.sr2026.validation.validators.layer1 import Layer1Validator
from app.sr2026.validation.validators.pre_normalization import PreNormalizationValidator
from app.sr2026.validation.validators.layer2 import Layer2Validator
from app.sr2026.validation.validators.layer3 import Layer3Validator
from app.sr2026.validation.nvr.nvr_engine import NVREngine
from app.sr2026.validation.usage_guidelines.guideline_validator import GuidelineValidator
from app.sr2026.validation.delta_rules.address_validator import AddressValidator
from app.sr2026.validation.delta_rules.lei_validator import LEIValidator
from app.sr2026.validation.delta_rules.tax_validator import TaxValidator
from app.sr2026.validation.delta_rules.new_mandatory_fields import NewMandatoryFieldsValidator
from app.sr2026.validation.delta_rules.pacs009_validator import Pacs009Validator
from app.sr2026.validation.delta_rules.pacs009adv_validator import Pacs009AdvValidator
from app.sr2026.validation.delta_rules.pacs003_validator import Pacs003Validator
from app.sr2026.validation.delta_rules.warning_engine import WarningEngine
from app.schemas.api_validation import ApiValidateResponse, ApiIssue

import os
import json
from typing import Set

class SR2026Validator:
    def __init__(self, history_service=None):
        self.history_service = history_service
        self.supported_bics = self._load_bics()

    def _load_bics(self) -> Set[str]:
        bics = set()
        # Add mock BICs for testing
        bics.update(["BOFAUS3NXXX", "BARCGB2DXXX", "AAAAUS3NXXX", "ZZZZGB2DXXX"])
        
        base_dir = os.path.dirname(os.path.abspath(__file__))
        backend_root = os.path.normpath(os.path.join(base_dir, "../../"))
        file_path = os.path.join(backend_root, "bics", "entities.ftm.json")
        
        if os.path.exists(file_path):
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    for line in f:
                        if not line.strip(): continue
                        try:
                            record = json.loads(line)
                            props = record.get("properties", {})
                            swift_bics = props.get("swiftBic", [])
                            for bic in swift_bics:
                                if bic: bics.add(str(bic).upper())
                        except Exception:
                            pass
            except Exception as e:
                print(f"Error loading BICs: {e}")
        return bics

    async def validate(self, xml_content: str, message_type: str) -> ApiValidateResponse:
        report = ValidationReport()
        report.message_type = message_type # set message type for rules that need it
        
        # Step 1: Layer 1 Technical well-formedness
        root_element = Layer1Validator.validate(xml_content, report)
        if root_element is None:
            return self._build_response(report)
            
        # Step 1.5: Pre-Normalization XML Regex Checks (Ported from SR 2025)
        PreNormalizationValidator.validate_all(xml_content, report, message_type)
        
        # Step 2: Layer 2 XSD Validation
        Layer2Validator.validate(root_element, message_type, report)

        # Step 3: Usage Guidelines
        GuidelineValidator.validate(root_element, report)

        # Step 4: Network Validated Rules (NVR)
        NVREngine.validate(root_element, report)

        # Step 5: Normalize and run Layer 3 check
        canonical_data, line_map = Layer3Validator.normalize(root_element)
        Layer3Validator.validate(canonical_data, line_map, message_type, report, self.supported_bics)
        
        # Step 5.5: CBPR+ JSON Schema (Layer 4 in SR 2025)
        try:
            from app.sr2026.validation.validators.cbpr_json_validator import CBPRJsonValidator
            json_validator = CBPRJsonValidator()
            json_validator._run_cbpr_json_schema_check(root_element, message_type, report)
        except Exception as e:
            print(f"Error running CBPR JSON check: {e}")

        # Step 6: Delta rules - Structured address
        AddressValidator.validate(root_element, report)

        # Step 7: Delta rules - LEI validation
        LEIValidator.validate(root_element, report)

        # Step 8: Delta rules - Tax block validation
        TaxValidator.validate(root_element, report)

        # Step 9: Delta rules - New mandatory fields
        NewMandatoryFieldsValidator.validate(root_element, report)

        # Step 9.5: Pacs.009 specific rules
        Pacs009Validator.validate(root_element, report, message_type)

        # Step 9.51: Pacs.009 ADV-specific rules (pre-advice variant)
        Pacs009AdvValidator.validate(root_element, report, message_type)

        # Step 9.6: Pacs.003 (Direct Debit) specific rules — SR2026 only
        Pacs003Validator.validate(root_element, report, message_type)

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
                severity=issue.severity,
                layer=getattr(issue, "layer", 3),
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
