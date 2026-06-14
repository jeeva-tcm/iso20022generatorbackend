"""
CAMT / PAIN SR2026 Cross-Cutting Formal Rules
=============================================
Implements the SR2026 field-level changes documented in the CAMT/PAIN
difference docs that apply across multiple messages. These supplement the
message-specific validators (CamtGeneralValidator, CamtStatementValidator,
CamtChargesValidator, PainValidator) and the XSD layer.

Rules (per SR2026 diff docs):
  - MsgDefIdr fixed value per message
  - BizMsgIdr must equal GroupHeader/MessageIdentification (CBPR_BusinessMessageIdentifier_FormalRule)
  - ClearingSystemIdentification mandatory when ClearingSystemMemberIdentification present
  - SchemeName mandatory when Other is used under OrganisationId / PrivateId
  - Proxy/Type mandatory when Proxy is present
  - OrganisationId/PrivateId Other: max 2 occurrences (pain), multiplicity caps
  - RemittanceInformation/Unstructured: max 1 (pain.001)
"""

from lxml import etree
from app.sr2026.validation.validators.models import ValidationIssue, ValidationReport

# MsgDefIdr fixed value per message (SR2026)
_MSGDEFIDR = {
    "camt.052": "camt.052.001.08",
    "camt.053": "camt.053.001.08",
    "camt.054": "camt.054.001.08",
    "camt.055": "camt.055.001.08",
    "camt.056": "camt.056.001.08",
    "camt.057": "camt.057.001.06",
    "pain.001": "pain.001.001.09",
    "pain.002": "pain.002.001.10",
    "pain.008": "pain.008.001.08",
}

# GroupHeader path differs per message; we locate MsgId generically below.


def _t(node) -> str:
    return (node.text or "").strip() if node is not None else ""


def _add(report: ValidationReport, severity: str, code: str, path: str,
         message: str, fix: str, line: int = 1, layer: int = 3):
    report.add_issue(ValidationIssue(
        severity=severity, code=code, layer=layer,
        path=path, message=message, fix=fix, line=line or 1,
    ))


class CamtPainSR2026Rules:
    """Cross-cutting SR2026 rules for CAMT and PAIN messages."""

    @classmethod
    def validate(cls, root: etree._Element, report: ValidationReport, message_type: str):
        msg = (message_type or "").lower()
        if not (msg.startswith("camt.") or msg.startswith("pain.")):
            return
        cls._check_msgdefidr(root, report, msg)
        cls._check_bizmsgidr_equals_msgid(root, report, msg)
        cls._check_clrsysid_when_member(root, report)
        cls._check_schemename_when_other(root, report)
        cls._check_proxy_type(root, report)
        if msg.startswith("pain."):
            cls._check_pain_multiplicity(root, report)
            cls._check_pain_party_address(root, report)
            cls._check_pain_structured_remittance(root, report)

    # ── MsgDefIdr fixed value ────────────────────────────────────────────────
    @classmethod
    def _check_msgdefidr(cls, root, report, msg):
        expected = next((v for k, v in _MSGDEFIDR.items() if k in msg), None)
        if not expected:
            return
        hdr = root.xpath("//*[local-name()='AppHdr']")
        if not hdr:
            return
        nodes = hdr[0].xpath("*[local-name()='MsgDefIdr']")
        if not nodes:
            return
        val = _t(nodes[0])
        if val and val != expected:
            _add(report, "ERROR", "INVALID_MSG_DEF_IDR", "//AppHdr/MsgDefIdr",
                 f"MessageDefinitionIdentifier '{val}' is invalid. SR2026 fixes it to '{expected}'.",
                 f"Set <MsgDefIdr>{expected}</MsgDefIdr> in AppHdr.",
                 nodes[0].sourceline or 1)

    # ── BizMsgIdr == GrpHdr/MsgId ────────────────────────────────────────────
    @classmethod
    def _check_bizmsgidr_equals_msgid(cls, root, report, msg):
        hdr = root.xpath("//*[local-name()='AppHdr']")
        if not hdr:
            return
        biz = hdr[0].xpath("*[local-name()='BizMsgIdr']")
        if not biz or not _t(biz[0]):
            return
        # GroupHeader/MessageIdentification in the Document body
        doc_msgid = root.xpath(
            "//*[local-name()='Document']//*[local-name()='GrpHdr']/*[local-name()='MsgId']"
        )
        if not doc_msgid:
            return
        if _t(biz[0]) != _t(doc_msgid[0]):
            _add(report, "ERROR", "BIZ_MSG_IDR_MISMATCH", "//AppHdr/BizMsgIdr",
                 f"BusinessMessageIdentifier '{_t(biz[0])}' must equal GroupHeader/MessageIdentification "
                 f"'{_t(doc_msgid[0])}' (CBPR_BusinessMessageIdentifier_FormalRule, SR2026).",
                 "Set AppHdr/BizMsgIdr to the same value as Document GroupHeader/MsgId.",
                 biz[0].sourceline or 1)

    # ── ClrSysId mandatory when ClrSysMmbId present ──────────────────────────
    @classmethod
    def _check_clrsysid_when_member(cls, root, report):
        for mmb in root.xpath("//*[local-name()='ClrSysMmbId']"):
            if not mmb.xpath("*[local-name()='ClrSysId']"):
                _add(report, "ERROR", "MISSING_CLR_SYS_ID",
                     "//ClrSysMmbId/ClrSysId",
                     "ClearingSystemIdentification is mandatory when "
                     "ClearingSystemMemberIdentification is present (SR2026).",
                     "Add <ClrSysId><Cd>...</Cd></ClrSysId> inside <ClrSysMmbId> before <MmbId>.",
                     mmb.sourceline or 1)

    # ── SchemeName mandatory when Other used under OrgId / PrvtId ─────────────
    @classmethod
    def _check_schemename_when_other(cls, root, report):
        for parent_tag in ("OrgId", "PrvtId"):
            for parent in root.xpath(f"//*[local-name()='{parent_tag}']"):
                for othr in parent.xpath("*[local-name()='Othr']"):
                    # Only when Othr carries an Id (a real identification)
                    if not othr.xpath("*[local-name()='Id']"):
                        continue
                    if not othr.xpath("*[local-name()='SchmeNm']"):
                        _add(report, "ERROR", "MISSING_SCHEME_NAME",
                             f"//{parent_tag}/Othr/SchmeNm",
                             f"SchemeName is mandatory when <Othr> is used under {parent_tag} (SR2026).",
                             "Add <SchmeNm><Cd>...</Cd></SchmeNm> inside <Othr>.",
                             othr.sourceline or 1)

    # ── Proxy/Type mandatory when Proxy present ──────────────────────────────
    @classmethod
    def _check_proxy_type(cls, root, report):
        for prxy in root.xpath("//*[local-name()='Prxy']"):
            # Proxy with an Id but no Type
            if prxy.xpath("*[local-name()='Id']") and not prxy.xpath("*[local-name()='Tp']"):
                _add(report, "ERROR", "MISSING_PROXY_TYPE",
                     "//Prxy/Tp",
                     "Proxy/Type is mandatory when a Proxy is present (SR2026).",
                     "Add <Tp><Cd>...</Cd></Tp> inside <Prxy> before <Id>.",
                     prxy.sourceline or 1)

    # Note: camt.052 TtlNtries structure (NumberAndSumOfTransactions4) is fully
    # validated by the XSD (Layer 2) — all children are optional and CdtDbtInd lives
    # inside TtlNetNtry, so no extra delta rule is needed here.

    # ── PAIN: party PostalAddress requires TownName + Country (SR2026) ─────────
    @classmethod
    def _check_pain_party_address(cls, root, report):
        # SR2026 made TownName + Country mandatory for party/agent postal addresses.
        # Conditional on PstlAdr being present (structured-address requirement).
        for pstl in root.xpath("//*[local-name()='PstlAdr']"):
            has_twn = bool(pstl.xpath("*[local-name()='TwnNm']") and _t(pstl.xpath("*[local-name()='TwnNm']")[0]))
            has_ctry = bool(pstl.xpath("*[local-name()='Ctry']") and _t(pstl.xpath("*[local-name()='Ctry']")[0]))
            # AdrLine-only (unstructured) addresses are still tolerated; only flag when
            # structured detail is used without TwnNm/Ctry, mirroring CBPR structured rule.
            has_adrline = bool(pstl.xpath("*[local-name()='AdrLine']"))
            if not has_adrline:
                if not has_twn:
                    _add(report, "ERROR", "MISSING_TOWN_NAME", "//PstlAdr/TwnNm",
                         "PostalAddress/TownName is mandatory in SR2026 (structured address).",
                         "Add <TwnNm> to the postal address.", pstl.sourceline or 1)
                if not has_ctry:
                    _add(report, "ERROR", "MISSING_COUNTRY", "//PstlAdr/Ctry",
                         "PostalAddress/Country is mandatory in SR2026 (structured address).",
                         "Add <Ctry> (ISO 3166 2-letter) to the postal address.", pstl.sourceline or 1)

    # ── PAIN: Structured remittance requires CreditorReference/Reference ──────
    @classmethod
    def _check_pain_structured_remittance(cls, root, report):
        for cdtr_ref in root.xpath("//*[local-name()='Strd']/*[local-name()='CdtrRefInf']"):
            if not (cdtr_ref.xpath("*[local-name()='Ref']") and _t(cdtr_ref.xpath("*[local-name()='Ref']")[0])):
                _add(report, "ERROR", "MISSING_CDTR_REF",
                     "//Strd/CdtrRefInf/Ref",
                     "CreditorReferenceInformation/Reference is mandatory when present in "
                     "Structured remittance (SR2026).",
                     "Add <Ref> inside <CdtrRefInf>.", cdtr_ref.sourceline or 1)

    # ── PAIN multiplicity caps ───────────────────────────────────────────────
    @classmethod
    def _check_pain_multiplicity(cls, root, report):
        # RmtInf/Ustrd max 1 (pain.001)
        for rmt in root.xpath("//*[local-name()='RmtInf']"):
            ustrd = rmt.xpath("*[local-name()='Ustrd']")
            if len(ustrd) > 1:
                _add(report, "ERROR", "USTRD_MAX_OCCURRENCE",
                     "//RmtInf/Ustrd",
                     f"RemittanceInformation/Unstructured has {len(ustrd)} occurrences; "
                     "SR2026 caps it at 1.",
                     "Reduce <Ustrd> to a single occurrence.",
                     ustrd[1].sourceline or rmt.sourceline or 1)
        # OrgId/PrvtId Other max 2
        for parent_tag in ("OrgId", "PrvtId"):
            for parent in root.xpath(f"//*[local-name()='{parent_tag}']"):
                othrs = parent.xpath("*[local-name()='Othr']")
                if len(othrs) > 2:
                    _add(report, "ERROR", "OTHR_MAX_OCCURRENCE",
                         f"//{parent_tag}/Othr",
                         f"{parent_tag}/Other has {len(othrs)} occurrences; SR2026 caps it at 2.",
                         "Reduce <Othr> to at most 2 occurrences.",
                         othrs[2].sourceline or parent.sourceline or 1)
