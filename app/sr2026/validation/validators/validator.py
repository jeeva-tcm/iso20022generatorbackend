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
from app.sr2026.validation.delta_rules.pacs009cov_validator import Pacs009CovValidator
from app.sr2026.validation.delta_rules.pacs003_validator import Pacs003Validator
from app.sr2026.validation.delta_rules.pacs004_validator import Pacs004Validator
from app.sr2026.validation.delta_rules.pacs002_validator import Pacs002Validator
from app.sr2026.validation.delta_rules.cbpr_formal_rules import CBPRFormalRulesValidator
from app.sr2026.validation.delta_rules.camt_general_validator import CamtGeneralValidator
from app.sr2026.validation.delta_rules.camt_statement_validator import CamtStatementValidator
from app.sr2026.validation.delta_rules.camt_charges_validator import CamtChargesValidator
from app.sr2026.validation.delta_rules.pain_validator import PainValidator
from app.sr2026.validation.delta_rules.pacs008stp_validator import Pacs008StpValidator
from app.sr2026.validation.delta_rules.warning_engine import WarningEngine
from app.schemas.api_validation import ApiValidateResponse, ApiIssue

import os
import json
import re
from typing import Set, List

# Matches the "is expected : 'X'" / "is expected: 'A', 'B'." tail that
# Layer2Mixin._simplify_error_message produces for sequence-violation /
# incomplete-content XSD errors (e.g. "...One of the following elements
# is expected : 'NbOfTxs'").
_EXPECTED_TAGS_RE = re.compile(r"is expected\s*:?\s*((?:'[^']*'(?:,\s*)?)+)")


def _extract_expected_tags(message: str) -> Set[str]:
    """Pull the candidate tag name(s) out of a raw-XSD 'expected' clause."""
    tags: Set[str] = set()
    m = _EXPECTED_TAGS_RE.search(message)
    if not m:
        return tags
    for quoted in re.findall(r"'([^']*)'", m.group(1)):
        for tag in quoted.split(','):
            tag = tag.strip()
            if tag:
                tags.add(tag)
    return tags


class SR2026Validator:
    def __init__(self, history_service=None):
        self.history_service = history_service
        self.supported_bics = self._load_bics()

    def get_supported_messages(self) -> List[str]:
        return [
            "pacs.008.001.08",
            "pacs.008.001.08_STP",
            "pacs.009.001.08",
            "pacs.009.001.08_ADV",
            "pacs.009.001.08_COV",
            "pacs.002.001.10",
            "pacs.003.001.08",
            "pacs.004.001.09",
            "pacs.010.001.03",
            "camt.029.001.09",
            "camt.052.001.08",
            "camt.053.001.08",
            "camt.054.001.08",
            "camt.056.001.08",
            "pain.001.001.09",
            "pain.002.001.10",
            "pain.008.001.08"
        ]

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
        report.message_type = message_type

        try:
            # Step 1: Layer 1 Technical well-formedness
            root_element = Layer1Validator.validate(xml_content, report)
            if root_element is None:
                # Malformed XML — schema and business-rule layers never run.
                return self._build_response(report, layers_skipped=[2, 3])

            # Step 2: Layer 2 XSD Validation
            l2_result = Layer2Validator.validate(root_element, message_type, report)
            # Catastrophic failure (None) = no schema, stop immediately.
            if l2_result is None:
                return self._build_response(report, layers_skipped=[3])

            # AppHdr structurally broken → NVR/Guidelines would duplicate same BAH errors.
            # Return now so L3 "BAH From/To BIC is missing" doesn't stack on top of L2.
            apphdr_failed = any(i.code == "APPHDR_SCHEMA_ERROR" for i in report.issues)
            if apphdr_failed:
                return self._build_response(report, layers_skipped=[3])

            # Document XSD failed but AppHdr OK → delta rules still useful, keep going.
            xsd_failed = (l2_result is False)

            # Step 2.5: Pre-Normalization XML Regex Checks (Ported from SR 2025)
            if not xsd_failed:
                PreNormalizationValidator.validate_all(xml_content, report, message_type)

                # Enforce skip logic for Pre-Normalization errors that are classified as Layer 2
                has_l2_errors = any(i.layer == 2 and i.severity == "ERROR" for i in report.issues)
                if has_l2_errors:
                    return self._build_response(report, layers_skipped=[3])

            # Step 3: Usage Guidelines
            try:
                GuidelineValidator.validate(root_element, report)
            except Exception as e:
                print(f"[SR2026] GuidelineValidator error: {e}")

            # Step 4: Network Validated Rules (NVR)
            try:
                NVREngine.validate(root_element, report)
            except Exception as e:
                print(f"[SR2026] NVREngine error: {e}")

            # Step 5: Normalize and run Layer 3 check
            # Runs even if the Document XSD failed — these are tree-walking value/business
            # checks (country codes, currencies, amounts, BIC lookups, etc.) that don't
            # depend on strict XSD structural conformance, so a single XSD error shouldn't
            # hide every other Layer 3 finding.
            try:
                canonical_data, line_map = Layer3Validator.normalize(root_element)
                Layer3Validator.validate(canonical_data, line_map, message_type, report, self.supported_bics)
            except Exception as e:
                print(f"[SR2026] Layer3Validator error: {e}")

            # Step 5.5: CBPR+ JSON Schema — type-level constraints (pattern, maxLength, enum)
            try:
                from app.sr2026.validation.validators.cbpr_json_validator import CBPRJsonValidator
                json_validator = CBPRJsonValidator()
                json_validator._run_cbpr_json_schema_check(xml_content, report, message_type)
            except Exception as e:
                print(f"[CBPR JSON] check error: {e}")

            # Step 6: Delta rules - Structured address
            try:
                AddressValidator.validate(root_element, report)
            except Exception as e:
                print(f"[SR2026] AddressValidator error: {e}")

            # Step 7: Delta rules - LEI validation
            try:
                LEIValidator.validate(root_element, report)
            except Exception as e:
                print(f"[SR2026] LEIValidator error: {e}")

            # Step 8: Delta rules - Tax block validation
            try:
                TaxValidator.validate(root_element, report)
            except Exception as e:
                print(f"[SR2026] TaxValidator error: {e}")

            # Step 9: Delta rules - New mandatory fields
            try:
                NewMandatoryFieldsValidator.validate(root_element, report)
            except Exception as e:
                print(f"[SR2026] NewMandatoryFieldsValidator error: {e}")

            # Step 9.5: Pacs.009 specific rules
            try:
                Pacs009Validator.validate(root_element, report, message_type)
            except Exception as e:
                print(f"[SR2026] Pacs009Validator error: {e}")

            # Step 9.51: Pacs.009 ADV-specific rules (pre-advice variant)
            try:
                Pacs009AdvValidator.validate(root_element, report, message_type)
            except Exception as e:
                print(f"[SR2026] Pacs009AdvValidator error: {e}")

            # Step 9.52: Pacs.009 COV-specific rules (cover payment variant)
            try:
                Pacs009CovValidator.validate(root_element, report, message_type)
            except Exception as e:
                print(f"[SR2026] Pacs009CovValidator error: {e}")

            # Step 9.6: Pacs.003 (Direct Debit) specific rules
            try:
                Pacs003Validator.validate(root_element, report, message_type)
            except Exception as e:
                print(f"[SR2026] Pacs003Validator error: {e}")

            # Step 9.7: Pacs.004 (Payment Return) specific rules
            try:
                Pacs004Validator.validate(root_element, report, message_type)
            except Exception as e:
                print(f"[SR2026] Pacs004Validator error: {e}")

            # Step 9.8: Pacs.002 (Payment Status Report) specific rules
            try:
                Pacs002Validator.validate(root_element, report, message_type)
            except Exception as e:
                print(f"[SR2026] Pacs002Validator error: {e}")

            # Step 9.9: Cross-cutting CBPR+ formal rules (all message types)
            try:
                CBPRFormalRulesValidator.validate(root_element, report, message_type)
            except Exception as e:
                print(f"[SR2026] CBPRFormalRulesValidator error: {e}")

            # Step 9.10: CAMT general rules (CopyDuplicate, PageNumber, RJCT/RJCR, NARR, slash IDs)
            try:
                CamtGeneralValidator.validate(root_element, report, message_type)
            except Exception as e:
                print(f"[SR2026] CamtGeneralValidator error: {e}")

            # Step 9.11: CAMT.053 statement page/balance complex rules
            try:
                CamtStatementValidator.validate(root_element, report, message_type)
            except Exception as e:
                print(f"[SR2026] CamtStatementValidator error: {e}")

            # Step 9.12: CAMT.105/106 charges sum/count/currency rules
            try:
                CamtChargesValidator.validate(root_element, report, message_type)
            except Exception as e:
                print(f"[SR2026] CamtChargesValidator error: {e}")

            # Step 9.13: PAIN message rules (MsgId=PmtInfoId, NbOfTxs, Originator)
            try:
                PainValidator.validate(root_element, report, message_type)
            except Exception as e:
                print(f"[SR2026] PainValidator error: {e}")

            # Step 9.14: pacs.008 STP SEPA/country-pair IBAN rules
            try:
                Pacs008StpValidator.validate(root_element, report, message_type)
            except Exception as e:
                print(f"[SR2026] Pacs008StpValidator error: {e}")

            # Step 10: Warning Engine
            try:
                WarningEngine.evaluate(root_element, report)
            except Exception as e:
                print(f"[SR2026] WarningEngine error: {e}")

        except Exception as e:
            print(f"[SR2026] Unexpected pipeline error: {e}")
            report.add_issue(ValidationIssue(
                severity="ERROR",
                layer=1,
                code="PIPELINE_ERROR",
                path="/",
                message=f"Validation pipeline encountered an unexpected error: {str(e)}",
                line=1,
                fix="Please check the XML structure and try again."
            ))

        return self._build_response(report)

    def _build_response(self, report: ValidationReport, layers_skipped: List[int] = None) -> ApiValidateResponse:
        errors = []
        warnings = []
        info = []

        # Collect every Layer-2 element that another validator has already
        # reported as missing (e.g. "Number of Transactions is missing.",
        # "AppHdr XSD Validation Error: Element 'Fr' is missing from AppHdr.").
        # Raw libxml2 sequence-violation errors ("Element 'SttlmInf' is not
        # expected here... expected 'NbOfTxs'") describe the SAME root cause
        # via the trailing 'expected' tag — drop those duplicates below.
        missing_tags = set()
        for issue in report.issues:
            if getattr(issue, "layer", 3) == 2 and issue.severity == "ERROR" and "is missing" in issue.message.lower():
                tag = issue.path.rstrip("/").split("/")[-1].lstrip("@")
                if tag:
                    missing_tags.add(tag)

        seen_issues = set()

        for issue in report.issues:
            if (getattr(issue, "layer", 3) == 2 and issue.code == "SCHEMA_VAL"
                    and "is expected" in issue.message
                    and _extract_expected_tags(issue.message) & missing_tags):
                continue

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
            info=info,
            layers_skipped=layers_skipped or []
        )
