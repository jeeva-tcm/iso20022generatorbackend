"""SR2025-specific fix handlers.

The shared fix engine lives in ``fix_suggester.py`` and serves BOTH releases —
~95% of fixes (charset repair, missing-field insertion, codelist replacement,
element removal, the XSD-sequence engine, HEADER_VAL / L3_SVCLVL / PACS010 / …)
are identical across SR2025 and SR2026, so they stay there.

This module is the override surface for fixes that are UNIQUE to SR2025 or whose
behaviour must DIFFER from SR2026. ``handle()`` gets first refusal before the
shared deterministic chain: return a FixSuggestion to take over, or None to defer
to the shared engine.

`suggester` is the live FixSuggester instance, so its helpers (_serialize, _copy,
_xpath_of, _parse_xml) and KB loaders (already version-scoped to SR2025) are all
available.
"""
import re
from typing import Optional

from lxml import etree


_STTLM_MTD_CODES = {"INDA", "INGA", "COVE", "CLRG"}

# ISO 20022 PostalAddress child sequence (superset across PostalAddress24/27 —
# CareOf/UnitNb simply don't appear in the older type). Ctry MUST precede AdrLine.
_POSTAL_ADDR_ORDER = [
    "AdrTp", "CareOf", "Dept", "SubDept", "StrtNm", "BldgNb", "BldgNm", "Flr",
    "UnitNb", "PstBx", "Room", "PstCd", "TwnNm", "TwnLctnNm", "DstrctNm",
    "CtrySubDvsn", "Ctry", "AdrLine",
]


def _localname(el) -> str:
    return etree.QName(el.tag).localname if isinstance(el.tag, str) else ""


def _is_pacs009_adv(root) -> bool:
    """A pacs.009 message in the ADV (advice) variant. ADV is signalled by
    BizSvc 'swift.cbprplus.adv*', LclInstrm/Prtry = 'ADV', or the presence of a
    reimbursement agent (Instg/Instd/ThrdRmbrsmntAgt) — which only ADV carries."""
    is_009 = adv = False
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        if "pacs.009" in (etree.QName(el.tag).namespace or ""):
            is_009 = True
        ln = _localname(el)
        if ln == "BizSvc" and "adv" in (el.text or "").lower():
            adv = True
        elif ln == "Prtry" and (el.text or "").strip().upper() == "ADV":
            adv = True
        elif ln in ("InstgRmbrsmntAgt", "InstdRmbrsmntAgt", "ThrdRmbrsmntAgt"):
            adv = True
    return is_009 and adv


def handle(suggester, code: str, msg: str, root, fix_hint: str = "") -> Optional[object]:
    # ── pacs.009 ADV → SttlmMtd MUST be COVE ─────────────────────────────────
    # The advice variant settles via cover (COVE) with a reimbursement agent;
    # INDA/INGA/CLRG are wrong here. The shared fixer picks a generic settlement
    # code from the codelist/KB ('preferred: INGA'), so force COVE for ADV.
    sttlm = next((el for el in root.iter()
                  if _localname(el) == "SttlmMtd"), None)
    if sttlm is not None:
        cur = (sttlm.text or "").strip().upper()
        if cur != "COVE":
            mlow = (msg or "")
            # Fire only when the error is about the settlement method: it names
            # SttlmMtd / "settlement method", or it is the enum error correcting
            # this element's value (current code + a settlement-code allow-list).
            is_sttlmmtd_err = (
                "SttlmMtd" in mlow
                or ("settlement" in mlow.lower() and "method" in mlow.lower())
                or (cur and cur in mlow and any(c in mlow for c in _STTLM_MTD_CODES))
            )
            if is_sttlmmtd_err and _is_pacs009_adv(root):
                from app.services.fix_suggester import FixSuggestion
                _orig = suggester._serialize(sttlm)
                _new = suggester._copy(sttlm)
                _new.text = "COVE"
                return FixSuggestion(suggester._xpath_of(sttlm), _orig,
                                     suggester._serialize(_new), code, msg, "high")

    # ── PstlAdr child out of XSD sequence (e.g. Ctry after AdrLine) ──────────
    # "Unexpected field 'Ctry'. … wrong place …" means a PostalAddress child is
    # out of the schema order. Reorder the offending PstlAdr to _POSTAL_ADDR_ORDER
    # (Ctry must precede AdrLine). Deterministic — only the child order changes.
    m = msg or ""
    _order_err = (code in ("SCHEMA_VAL", "WRONG_ORDER", "SEQUENCE", "NOT_EXPECTED")
                  and any(k in m.lower() for k in
                          ("wrong place", "wrong order", "unexpected field", "not expected")))
    _tgt_m = re.search(r"'([A-Za-z]\w*)'", m)
    _tgt = _tgt_m.group(1) if _tgt_m else None
    if _order_err and _tgt in _POSTAL_ADDR_ORDER:
        _rank = {t: i for i, t in enumerate(_POSTAL_ADDR_ORDER)}
        for _pa in root.iter():
            if _localname(_pa) != "PstlAdr":
                continue
            _kids = [c for c in _pa if isinstance(c.tag, str)]
            _lns = [_localname(c) for c in _kids]
            if _tgt not in _lns:
                continue
            _ranks = [_rank.get(ln, 10_000) for ln in _lns]
            if _ranks == sorted(_ranks):
                continue  # this PstlAdr is already ordered
            from app.services.fix_suggester import FixSuggestion
            _orig = suggester._serialize(_pa)
            _new = suggester._copy(_pa)
            _newkids = [c for c in _new if isinstance(c.tag, str)]
            _newkids.sort(key=lambda c: _rank.get(_localname(c), 10_000))  # stable
            for c in list(_new):
                if isinstance(c.tag, str):
                    _new.remove(c)
            for c in _newkids:
                _new.append(c)
            if suggester._serialize(_new) != _orig:
                return FixSuggestion(suggester._xpath_of(_pa), _orig,
                                     suggester._serialize(_new), code, msg, "high")

    return None
