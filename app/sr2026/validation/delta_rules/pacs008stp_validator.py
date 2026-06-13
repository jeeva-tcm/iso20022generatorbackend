"""
SR2026 pacs.008.001.08 STP Validator
=====================================
Implements SEPA/country-pair FormalRules that apply only to the
pacs.008 STP (Straight-Through Processing) variant.

  R12: CBPR_Debtor_Creditor_IBAN_FormalRule
       If both DebtorAgent and CreditorAgent BICs are in SEPA countries,
       then Debtor/Account and Creditor/Account must both be IBAN.

  R15: CBPR_Debtor_Creditor_IT/VA_FormalRule
       IT ↔ VA country pair: Debtor and Creditor must be identified by IBAN.

  R16: CBPR_Debtor_Creditor_FR/MC_FormalRule
       FR ↔ MC country pair: Debtor and Creditor must be identified by IBAN.

  R17: CBPR_Debtor_Creditor_ES/AD_FormalRule
       ES ↔ AD country pair: Debtor and Creditor must be identified by IBAN.

  R18: CBPR_Debtor_Creditor_IT/SM_FormalRule
       IT ↔ SM country pair: Debtor and Creditor must be identified by IBAN.

  R14: CBPR_CRED_FormalRule (STP variant)
       ChrgBr=CRED → ChargesInformation mandatory.
       (Already handled by cross-cutting CBPRFormalRulesValidator; listed here for completeness.)

SEPA country codes (used for R12):
AT, BE, BG, BV, CY, CZ, DE, DK, EE, ES, FI, FR, GB, GF, GI, GP, GR, HR, HU,
IE, IS, IT, LI, LT, LU, LV, MQ, MT, NL, NO, PL, PT, RE, RO, SE, SI, SK, SM,
VA, MC, AD, PM, YT
"""

import re
from lxml import etree
from app.sr2026.validation.validators.models import ValidationIssue, ValidationReport

_IBAN_RE = re.compile(r'^[A-Z]{2}[0-9]{2}[A-Z0-9]{1,30}$')

# SEPA country codes — BIC positions 5-6 (zero-based) identify the country
_SEPA_COUNTRIES = {
    "AT", "BE", "BG", "BV", "CY", "CZ", "DE", "DK", "EE", "ES", "FI",
    "FR", "GB", "GF", "GI", "GP", "GR", "HR", "HU", "IE", "IS", "IT",
    "LI", "LT", "LU", "LV", "MQ", "MT", "NL", "NO", "PL", "PT", "RE",
    "RO", "SE", "SI", "SK", "SM", "VA", "MC", "AD", "PM", "YT",
}

# Domestic country pairs (R15–R18)
_DOMESTIC_PAIRS = [
    ({"IT", "VA"}, "R15", "IT/VA"),
    ({"FR", "MC"}, "R16", "FR/MC"),
    ({"ES", "AD"}, "R17", "ES/AD"),
    ({"IT", "SM"}, "R18", "IT/SM"),
]


def _t(node) -> str:
    return (node.text or "").strip() if node is not None else ""


def _add(report: ValidationReport, severity: str, code: str, path: str,
         message: str, fix: str, line: int = 1):
    report.add_issue(ValidationIssue(
        severity=severity, code=code, layer=3,
        path=path, message=message, fix=fix, line=line or 1,
    ))


def _bic_country(bic: str) -> str:
    """Extract 2-char country code from BIC (chars 4-5, 0-indexed)."""
    bic = bic.upper().strip() if bic else ""
    return bic[4:6] if len(bic) >= 6 else ""


def _account_is_iban(account_elem: etree._Element) -> bool:
    """Check if an Account element has an IBAN identifier."""
    iban_nodes = account_elem.xpath(
        "*[local-name()='Id']/*[local-name()='IBAN']"
    )
    if not iban_nodes:
        return False
    return bool(_IBAN_RE.match(_t(iban_nodes[0]).upper()))


class Pacs008StpValidator:
    """pacs.008 STP SEPA / country-pair IBAN rule validation."""

    @classmethod
    def validate(cls, root: etree._Element, report: ValidationReport, message_type: str):
        msg = (message_type or "").lower()
        if "pacs.008" not in msg or "stp" not in msg:
            return

        for tx in root.xpath("//*[local-name()='CdtTrfTxInf']"):
            cls._validate_tx(tx, report)

    @classmethod
    def _validate_tx(cls, tx: etree._Element, report: ValidationReport):
        src = tx.sourceline or 1

        # Extract debtor/creditor agent BICs
        dbtr_agt_bic_nodes = tx.xpath(
            "*[local-name()='DbtrAgt']//*[local-name()='BICFI']"
        )
        cdtr_agt_bic_nodes = tx.xpath(
            "*[local-name()='CdtrAgt']//*[local-name()='BICFI']"
        )

        dbtr_bic = _t(dbtr_agt_bic_nodes[0]) if dbtr_agt_bic_nodes else ""
        cdtr_bic = _t(cdtr_agt_bic_nodes[0]) if cdtr_agt_bic_nodes else ""

        dbtr_country = _bic_country(dbtr_bic)
        cdtr_country = _bic_country(cdtr_bic)

        # Account elements
        dbtr_acct_nodes = tx.xpath("*[local-name()='DbtrAcct']")
        cdtr_acct_nodes = tx.xpath("*[local-name()='CdtrAcct']")

        # R12: Both BICs in SEPA → both accounts must be IBAN
        if (dbtr_country in _SEPA_COUNTRIES and cdtr_country in _SEPA_COUNTRIES
                and dbtr_country and cdtr_country):
            if dbtr_acct_nodes and not _account_is_iban(dbtr_acct_nodes[0]):
                _add(report, "ERROR", "SEPA_DEBTOR_ACCOUNT_MUST_BE_IBAN",
                     "//CdtTrfTxInf/DbtrAcct/Id/IBAN",
                     f"DebtorAgent BIC '{dbtr_bic}' and CreditorAgent BIC '{cdtr_bic}' are both "
                     f"in SEPA countries. Debtor account must be an IBAN "
                     "(CBPR_Debtor_Creditor_IBAN_FormalRule R12).",
                     "Provide an IBAN in <DbtrAcct><Id><IBAN>...</IBAN></Id></DbtrAcct>.",
                     dbtr_acct_nodes[0].sourceline or src)

            if cdtr_acct_nodes and not _account_is_iban(cdtr_acct_nodes[0]):
                _add(report, "ERROR", "SEPA_CREDITOR_ACCOUNT_MUST_BE_IBAN",
                     "//CdtTrfTxInf/CdtrAcct/Id/IBAN",
                     f"DebtorAgent BIC '{dbtr_bic}' and CreditorAgent BIC '{cdtr_bic}' are both "
                     f"in SEPA countries. Creditor account must be an IBAN "
                     "(CBPR_Debtor_Creditor_IBAN_FormalRule R12).",
                     "Provide an IBAN in <CdtrAcct><Id><IBAN>...</IBAN></Id></CdtrAcct>.",
                     cdtr_acct_nodes[0].sourceline or src)

        # R15–R18: Domestic country pairs
        both_countries = {dbtr_country, cdtr_country} - {""}
        for pair_set, rule_ref, pair_label in _DOMESTIC_PAIRS:
            if not both_countries or not both_countries.issubset(pair_set):
                continue
            # Both agents are in this domestic pair — IBAN required
            if dbtr_acct_nodes and not _account_is_iban(dbtr_acct_nodes[0]):
                _add(report, "ERROR", f"DOMESTIC_{pair_label.replace('/','_')}_DEBTOR_IBAN",
                     "//CdtTrfTxInf/DbtrAcct/Id/IBAN",
                     f"DebtorAgent BIC '{dbtr_bic}' and CreditorAgent BIC '{cdtr_bic}' are in the "
                     f"{pair_label} domestic country pair. Debtor account must be an IBAN "
                     f"(CBPR_Debtor_Creditor_{pair_label}_FormalRule {rule_ref}).",
                     "Provide an IBAN in <DbtrAcct><Id><IBAN>...</IBAN></Id></DbtrAcct>.",
                     dbtr_acct_nodes[0].sourceline or src)

            if cdtr_acct_nodes and not _account_is_iban(cdtr_acct_nodes[0]):
                _add(report, "ERROR", f"DOMESTIC_{pair_label.replace('/','_')}_CREDITOR_IBAN",
                     "//CdtTrfTxInf/CdtrAcct/Id/IBAN",
                     f"DebtorAgent BIC '{dbtr_bic}' and CreditorAgent BIC '{cdtr_bic}' are in the "
                     f"{pair_label} domestic country pair. Creditor account must be an IBAN "
                     f"(CBPR_Debtor_Creditor_{pair_label}_FormalRule {rule_ref}).",
                     "Provide an IBAN in <CdtrAcct><Id><IBAN>...</IBAN></Id></CdtrAcct>.",
                     cdtr_acct_nodes[0].sourceline or src)
