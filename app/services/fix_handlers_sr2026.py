"""SR2026-specific fix handlers.

The shared fix engine lives in ``fix_suggester.py`` and serves BOTH releases —
~95% of fixes are identical across SR2025 and SR2026, so they stay there.

This module is the override surface for fixes that are UNIQUE to SR2026 (e.g. the
new SR2026 delta-rule error codes from app/sr2026/.../delta_rules/) or whose
behaviour must DIFFER from SR2025. ``handle()`` gets first refusal before the
shared deterministic chain: return a FixSuggestion to take over, or None to defer
to the shared engine.

To add a handler:

    from app.services.fix_suggester import FixSuggestion

    def handle(suggester, code, msg, root, fix_hint=""):
        if code == "SR2026_NEW_RULE_CODE":
            el = ...                                   # locate the element in root
            parent = el.getparent()
            return FixSuggestion(
                suggester._xpath_of(parent),
                suggester._serialize(parent),
                suggester._serialize(fixed_parent),    # built via suggester._copy(...)
                code, msg, "high")
        return None

`suggester` is the live FixSuggester instance, so its helpers (_serialize, _copy,
_xpath_of, _parse_xml) and KB loaders (already version-scoped to SR2026 — base
sr2025 KB + sr2026 overlay) are all available.

This module is ONLY consulted when the active release is SR2026, so everything
here is inherently SR2025-safe.
"""
import re
from typing import Optional

from lxml import etree


_STTLM_MTD_CODES = {"INDA", "INGA", "COVE", "CLRG"}

# ISO 20022 PostalAddress child sequence (superset across PostalAddress24/27).
# Ctry MUST precede AdrLine.
_POSTAL_ADDR_ORDER = [
    "AdrTp", "CareOf", "Dept", "SubDept", "StrtNm", "BldgNb", "BldgNm", "Flr",
    "UnitNb", "PstBx", "Room", "PstCd", "TwnNm", "TwnLctnNm", "DstrctNm",
    "CtrySubDvsn", "Ctry", "AdrLine",
]


def _localname(el) -> str:
    return etree.QName(el.tag).localname if isinstance(el.tag, str) else ""


def _is_pacs009_adv(root) -> bool:
    """pacs.009 in the ADV variant (BizSvc adv*, LclInstrm/Prtry=ADV, or reimbursement agent)."""
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


def _t(el) -> str:
    """Trimmed text of an element (empty string when None/blank)."""
    return (el.text or "").strip() if el is not None else ""


def _first(node, xpath):
    """First node matching an xpath, else None."""
    res = node.xpath(xpath)
    return res[0] if res else None


def _sync_leaf(suggester, leaf, value: str, code: str, msg: str):
    """Surgical fix: copy `leaf`, set its text to `value`, return a FixSuggestion
    that replaces just that leaf. No structural churn — the opposite of the LLM
    fallback, which regenerated whole blocks for these cross-ref codes."""
    from app.services.fix_suggester import FixSuggestion
    original = suggester._serialize(leaf)
    fixed = suggester._copy(leaf)
    fixed.text = value
    return FixSuggestion(
        suggester._xpath_of(leaf), original, suggester._serialize(fixed),
        code, msg, "high")


def _noop(suggester, leaf, code: str, msg: str):
    """Return a no-op FixSuggestion when the issue is already resolved.
    SR2026 ownership codes must never return None (fallthrough to shared chain),
    or the shared engine will misinterpret them as missing-subtree problems and
    emit destructive whole-envelope reconstructions. A no-op is the correct
    response when an ownership code is encountered but already satisfied."""
    from app.services.fix_suggester import FixSuggestion
    frag = suggester._serialize(leaf)
    return FixSuggestion(
        suggester._xpath_of(leaf), frag, frag,
        code, msg, "high")


# Local-names of the Document GroupHeader MsgId — the single source of truth that
# BizMsgIdr (BAH) and PmtInfId (pain) must echo under the SR2026 CBPR+ rules
# (CBPR_BusinessMessageIdentifier_FormalRule / R8).
_GRP_MSGID_XP = "//*[local-name()='GrpHdr']/*[local-name()='MsgId']"

# Transaction-block local-names counted by the SR2026 NbOfTxs rules (mirrors
# new_mandatory_fields.py: CdtTrfTxInf / DrctDbtTxInf / TxInf — also catches the
# per-PmtInf CdtTrfTxInf/DrctDbtTxInf of pain.001/008 via the descendant axis).
# CdtInstr is the pacs.010 transaction container.
_TX_BLOCK_XP = ("//*[local-name()='CdtTrfTxInf' or local-name()='DrctDbtTxInf' "
                "or local-name()='TxInf' or local-name()='CdtInstr']")
_GRP_NBOFTXS_XP = "//*[local-name()='GrpHdr']/*[local-name()='NbOfTxs']"
# Codes that require NbOfTxs == "1" (single-transaction messages) rather than the
# actual block count: pacs.004 and pacs.009 ADV/COV.
_NBOFTXS_FIXED_ONE = {"INVALID_NB_OF_TXS"}
_NBOFTXS_COUNT = {"NB_OF_TXS_COUNT_MISMATCH", "NB_OF_TXS_MISMATCH", "SR2026-GRP-003"}


def handle(suggester, code: str, msg: str, root, fix_hint: str = "") -> Optional[object]:
    if root is None:
        return None

    # ── SCHEMA_VAL: XSD validator mis-treats NbOfTxs as an enumerated code ────
    # The schema layer raises "Invalid code '<n>' for field 'NbOfTxs'." for any
    # value it doesn't recognize, even though NbOfTxs is a plain count, not a
    # code list. Every generated SR2026 message is single-transaction, so the
    # deterministic fix is always "1" — never re-count transaction blocks here.
    _msg_l = msg.lower()
    if code == "SCHEMA_VAL" and "nboftxs" in _msg_l and "invalid code" in _msg_l:
        nb_els = root.xpath(_GRP_NBOFTXS_XP)
        if not nb_els:
            return None
        nb = nb_els[0]
        lh = getattr(suggester, "_line_hint", None)
        if lh is not None and len(nb_els) > 1:
            candidates = [e for e in nb_els if (e.sourceline or 0) <= lh]
            nb = max(candidates, key=lambda e: e.sourceline or 0) if candidates \
                else min(nb_els, key=lambda e: abs((e.sourceline or 0) - lh))
        if _t(nb) != "1":
            return _sync_leaf(suggester, nb, "1", code, msg)
        return None

    # ── ID cross-reference sync ──────────────────────────────────────────────
    # SR2026 cross-field rules require an identifier to EQUAL
    # GroupHeader/MessageIdentification. The shared engine has no handler for the
    # SR2026 codes, so they fell through to the LLM, which regenerated whole
    # blocks (one BizMsgIdr "fix" added +1008 bytes), cascading into
    # DUPLICATE_ID_VALUE / NbOfTxs / DEPRECATED_UNSTRUCTURED_ADDRESS collateral.
    # A surgical value copy resolves each with zero structural change.

    # BizMsgIdr (BAH) must equal Document GrpHdr/MsgId.
    if code in ("MESSAGE_ID_MISMATCH", "BIZ_MSG_IDR_MISMATCH"):
        msgid = _first(root, "//*[local-name()='Document']" + _GRP_MSGID_XP)
        biz = _first(root, "//*[local-name()='AppHdr']/*[local-name()='BizMsgIdr']")
        if msgid is not None and biz is not None and _t(msgid) and _t(biz) != _t(msgid):
            return _sync_leaf(suggester, biz, _t(msgid), code, msg)
        return None

    # PmtInf/PmtInfId (pain.001/008) must equal GrpHdr/MsgId. Fix the first
    # still-mismatched occurrence; suggest_batch re-runs on the rolled-forward
    # XML so multiple PmtInf blocks are each handled across the batch.
    if code == "PMT_INF_ID_MISMATCH":
        msgid = _first(root, _GRP_MSGID_XP)
        if msgid is None or not _t(msgid):
            return None
        for pmtinf in root.xpath("//*[local-name()='PmtInf']"):
            pid = _first(pmtinf, "*[local-name()='PmtInfId']")
            if pid is not None and _t(pid) != _t(msgid):
                return _sync_leaf(suggester, pid, _t(msgid), code, msg)
        return None

    # pain.002: OrgnlPmtInfId must equal OrgnlGrpInfAndSts/OrgnlMsgId.
    if code == "ORIG_PMT_INF_ID_MISMATCH":
        omsgid = _first(root, "//*[local-name()='OrgnlGrpInfAndSts']/*[local-name()='OrgnlMsgId']")
        if omsgid is None or not _t(omsgid):
            return None
        for pmtsts in root.xpath("//*[local-name()='OrgnlPmtInfAndSts']"):
            pid = _first(pmtsts, "*[local-name()='OrgnlPmtInfId']")
            if pid is not None and _t(pid) != _t(omsgid):
                return _sync_leaf(suggester, pid, _t(omsgid), code, msg)
        return None

    # ── NbOfTxs ↔ actual transaction-block count ─────────────────────────────
    # SR2026 raises several codes when GrpHdr/NbOfTxs disagrees with reality. The
    # shared engine has no handler for the SR2026 codes (SR2025 resolves its own
    # equivalent), so they went unfixed. Surgical: set NbOfTxs to the count of
    # transaction blocks — or to "1" for the single-transaction message types
    # (pacs.004 / pacs.009 ADV/COV) whose rule mandates exactly one.
    # CRITICAL: For ownership codes in _NBOFTXS_COUNT, NEVER return None (which
    # causes fallthrough to the shared chain's missing-subtree handler, emitting
    # destructive whole-envelope reconstructions). Always return either _sync_leaf
    # (when value needs fixing) or _noop (when already correct).
    if code in _NBOFTXS_FIXED_ONE or code in _NBOFTXS_COUNT:
        nb = _first(root, _GRP_NBOFTXS_XP)
        if nb is None:
            return None   # NbOfTxs absent — defer to the shared mandatory-insert
        # pacs.010 is always a single-instruction message in CBPR+; count-based
        # re-computation is unreliable on lxml-recovered XML (duplicate CdtInstr
        # blocks appear after recovery). Force "1" for pacs.010 like pacs.004.
        _is_pacs010 = any("pacs.010" in (etree.QName(e.tag).namespace or "")
                          for e in root.iter() if isinstance(e.tag, str))
        target = "1" if (code in _NBOFTXS_FIXED_ONE or _is_pacs010) \
            else str(len(root.xpath(_TX_BLOCK_XP)))
        if _t(nb) != target:
            return _sync_leaf(suggester, nb, target, code, msg)
        # Value is already correct, but this is an ownership code (shared chain
        # doesn't know about it). Return a no-op instead of None to prevent
        # fallthrough to the destructive missing-subtree handler.
        if code in _NBOFTXS_COUNT:
            return _noop(suggester, nb, code, msg)
        return None

    # ── MISSING_COUNTRY / MISSING_TOWN_NAME — insert Ctry or TwnNm into PstlAdr ─
    # SR2026 makes Ctry and TwnNm mandatory in every structured PostalAddress block.
    # When tag-removal strips one, insert a safe default value at the correct
    # XSD position (before AdrLine, after PstCd/TwnLctnNm for Ctry).
    if code in ("MISSING_COUNTRY", "MISSING_TOWN_NAME"):
        _insert_tag = "Ctry" if code == "MISSING_COUNTRY" else "TwnNm"
        _insert_val = "US" if code == "MISSING_COUNTRY" else "New York"
        _rank = {t: i for i, t in enumerate(_POSTAL_ADDR_ORDER)}
        for _pa in root.iter():
            if _localname(_pa) != "PstlAdr":
                continue
            _kids = [c for c in _pa if isinstance(c.tag, str)]
            _lns = [_localname(c) for c in _kids]
            if _insert_tag in _lns:
                continue  # already present in this block
            from app.services.fix_suggester import FixSuggestion
            _orig = suggester._serialize(_pa)
            _new = suggester._copy(_pa)
            # Determine namespace from a sibling element
            _ns = ""
            for _k in _new:
                if isinstance(_k.tag, str) and "{" in _k.tag:
                    _ns = etree.QName(_k.tag).namespace
                    break
            _tag = f"{{{_ns}}}{_insert_tag}" if _ns else _insert_tag
            _el = etree.SubElement(_new, _tag)
            _el.text = _insert_val
            # Re-sort children into canonical XSD order
            _newkids = [c for c in _new if isinstance(c.tag, str)]
            _newkids.sort(key=lambda c: _rank.get(_localname(c), 10_000))
            for c in list(_new):
                if isinstance(c.tag, str):
                    _new.remove(c)
            for c in _newkids:
                _new.append(c)
            _ser = suggester._serialize(_new)
            if _ser != _orig:
                return FixSuggestion(suggester._xpath_of(_pa), _orig, _ser, code, msg, "high")

    # ── MISSING_CLBD_BALANCE / MISSING_OPBD_BALANCE — insert balance block ───
    # SR2026 camt.053 (and camt.052/054) require a CLBD balance on the last page
    # and OPBD on the first. When removed, clone an existing Bal block, change the
    # type code, and append/prepend in the Stmt container.
    if code in ("MISSING_CLBD_BALANCE", "MISSING_OPBD_BALANCE",
                "MISSING_CLBD_INTM_BALANCE", "MISSING_OPBD_INTM_BALANCE"):
        _need_cd   = "CLBD" if "CLBD" in code else "OPBD"
        _need_intm = "INTM" in code
        for _stmt in root.iter():
            if not isinstance(_stmt.tag, str) or _localname(_stmt) != "Stmt":
                continue
            _bals = [c for c in _stmt if isinstance(c.tag, str) and _localname(c) == "Bal"]
            if not _bals:
                continue
            # Check if required balance already present
            def _bal_cd(b):
                cd = b.xpath("*[local-name()='Tp']/*[local-name()='CdOrPrtry']/*[local-name()='Cd']")
                return (cd[0].text or "").strip() if cd else ""
            def _bal_sub(b):
                sub = b.xpath("*[local-name()='Tp']/*[local-name()='SubTp']/*[local-name()='Cd']")
                return (sub[0].text or "").strip() if sub else ""
            _existing = [(b, _bal_cd(b), _bal_sub(b)) for b in _bals]
            if any(cd == _need_cd and (_need_intm == (sub == "INTM"))
                   for _, cd, sub in _existing):
                continue  # already present
            from app.services.fix_suggester import FixSuggestion
            _orig_stmt = suggester._serialize(_stmt)
            _new_stmt = suggester._copy(_stmt)
            # Clone the first existing Bal as template
            _tmpl = _new_stmt.xpath("*[local-name()='Bal']")[0]
            _new_bal = suggester._copy(_tmpl)
            # Set the balance type code
            _cd_el = _new_bal.xpath("*[local-name()='Tp']/*[local-name()='CdOrPrtry']/*[local-name()='Cd']")
            if _cd_el:
                _cd_el[0].text = _need_cd
            # Set or remove SubTp
            _tp_el = _new_bal.xpath("*[local-name()='Tp']")
            if _tp_el:
                _sub_els = _tp_el[0].xpath("*[local-name()='SubTp']")
                _ns2 = etree.QName(_tp_el[0].tag).namespace or ""
                if _need_intm and not _sub_els:
                    _sub = etree.SubElement(_tp_el[0], f"{{{_ns2}}}SubTp" if _ns2 else "SubTp")
                    _sub_cd = etree.SubElement(_sub, f"{{{_ns2}}}Cd" if _ns2 else "Cd")
                    _sub_cd.text = "INTM"
                elif not _need_intm and _sub_els:
                    _tp_el[0].remove(_sub_els[0])
            # Append after existing Bal blocks (before Ntry)
            _last_bal = _new_stmt.xpath("*[local-name()='Bal']")[-1]
            _last_bal.addnext(_new_bal)
            _ser = suggester._serialize(_new_stmt)
            if _ser != _orig_stmt:
                return FixSuggestion(suggester._xpath_of(_stmt), _orig_stmt, _ser, code, msg, "high")

    # ── CRED_MISSING_CHARGES_INFO — insert ChrgsInf when ChrgBr=CRED ─────────
    # SR2026 CBPR+ rule: when ChargeBearer is CRED, a ChrgsInf block is mandatory.
    # Insert a minimal ChrgsInf (zero amount, same currency as settlement amount)
    # immediately after ChrgBr in each qualifying transaction block.
    if code == "CRED_MISSING_CHARGES_INFO":
        _tx_tags = {"CdtTrfTxInf", "DrctDbtTxInf", "TxInf"}
        for _tx in root.iter():
            if not isinstance(_tx.tag, str) or _localname(_tx) not in _tx_tags:
                continue
            _chrg_br = _first(_tx, "*[local-name()='ChrgBr']")
            if _chrg_br is None or _t(_chrg_br) != "CRED":
                continue
            if _tx.xpath("*[local-name()='ChrgsInf']"):
                continue  # already present
            # Harvest settlement currency
            _ccy = "USD"
            for _amt_el in _tx.iter():
                if isinstance(_amt_el.tag, str) and _localname(_amt_el) in (
                        "IntrBkSttlmAmt", "InstdAmt", "TtlIntrBkSttlmAmt", "Amt"):
                    _c = _amt_el.get("Ccy")
                    if _c:
                        _ccy = _c
                        break
            from app.services.fix_suggester import FixSuggestion
            _orig_tx = suggester._serialize(_tx)
            _new_tx = suggester._copy(_tx)
            _ns3 = etree.QName(_tx.tag).namespace or ""
            def _tag3(n): return f"{{{_ns3}}}{n}" if _ns3 else n
            _chrgs = etree.Element(_tag3("ChrgsInf"))
            _amt3 = etree.SubElement(_chrgs, _tag3("Amt"))
            _amt3.set("Ccy", _ccy)
            _amt3.text = "0.00"
            _agt3 = etree.SubElement(_chrgs, _tag3("Agt"))
            _fii3 = etree.SubElement(_agt3, _tag3("FinInstnId"))
            _bic3 = etree.SubElement(_fii3, _tag3("BICFI"))
            _bic3.text = "DEUTDEFFXXX"
            # Insert after ChrgBr in the copy
            _new_chrg_br = _new_tx.xpath("*[local-name()='ChrgBr']")
            if _new_chrg_br:
                _new_chrg_br[0].addnext(_chrgs)
            else:
                _new_tx.append(_chrgs)
            _ser = suggester._serialize(_new_tx)
            if _ser != _orig_tx:
                return FixSuggestion(suggester._xpath_of(_tx), _orig_tx, _ser, code, msg, "high")

    # ── pacs.010 SCHEMA_VAL CdtId: orphaned elements at FIDrctDbt level ────────
    # After XML recovery from truncation, elements that belong inside CdtInstr
    # (CdtId, InstgAgt, InstdAgt, Cdtr, CdtrAcct, DrctDbtTxInf) get duplicated
    # at FIDrctDbt level. The XSD raises SCHEMA_VAL on CdtId as "unexpected" at
    # FIDrctDbt level. Fix: remove those orphans from FIDrctDbt since they're
    # already correctly placed inside CdtInstr.
    if code == "SCHEMA_VAL" and "cdtid" in msg.lower() and "unexpected" in msg.lower():
        _fidbt_tags = {"CdtId", "InstgAgt", "InstdAgt", "Cdtr", "CdtrAcct", "DrctDbtTxInf"}
        for _fidbt in root.iter():
            if not isinstance(_fidbt.tag, str) or _localname(_fidbt) != "FIDrctDbt":
                continue
            _cdt_instrs = _fidbt.xpath("*[local-name()='CdtInstr']")
            if not _cdt_instrs:
                continue
            # Only remove orphans when CdtInstr already has CdtId inside it
            _cdt_inside = _cdt_instrs[0].xpath("*[local-name()='CdtId']")
            if not _cdt_inside:
                continue
            # Find direct FIDrctDbt children that are orphans (not GrpHdr/CdtInstr)
            _orphans = [c for c in _fidbt if isinstance(c.tag, str)
                        and _localname(c) in _fidbt_tags]
            if not _orphans:
                continue
            from app.services.fix_suggester import FixSuggestion
            _orig = suggester._serialize(_fidbt)
            _new = suggester._copy(_fidbt)
            for _orph in _new.xpath("*[local-name()='CdtId' or local-name()='InstgAgt' or "
                                    "local-name()='InstdAgt' or local-name()='Cdtr' or "
                                    "local-name()='CdtrAcct' or local-name()='DrctDbtTxInf']"):
                # Only remove if CdtInstr is present (already validated above)
                if _new.xpath("*[local-name()='CdtInstr']"):
                    _new.remove(_orph)
            _ser = suggester._serialize(_new)
            if _ser != _orig:
                return FixSuggestion(suggester._xpath_of(_fidbt), _orig, _ser, code, msg, "high")

    # ── MISSING_UNDRLYG_CSTMR_CDT_TRF — insert minimal UndrlygCstmrCdtTrf ───
    # pacs.009 COV requires UndrlygCstmrCdtTrf inside each CdtTrfTxInf.
    # When tag-removal strips it, insert a minimal placeholder with Dbtr/Cdtr Nm.
    if code == "MISSING_UNDRLYG_CSTMR_CDT_TRF":
        for _cdt in root.iter():
            if not isinstance(_cdt.tag, str) or _localname(_cdt) != "CdtTrfTxInf":
                continue
            if _cdt.xpath("*[local-name()='UndrlygCstmrCdtTrf']"):
                continue
            from app.services.fix_suggester import FixSuggestion
            _orig = suggester._serialize(_cdt)
            _new = suggester._copy(_cdt)
            _ns5 = etree.QName(_cdt.tag).namespace or ""
            def _t5(n): return f"{{{_ns5}}}{n}" if _ns5 else n
            _u = etree.Element(_t5("UndrlygCstmrCdtTrf"))
            _dbtr = etree.SubElement(_u, _t5("Dbtr"))
            _dbtrnm = etree.SubElement(_dbtr, _t5("Nm"))
            _dbtrnm.text = "Debtor Name"
            _cdtr = etree.SubElement(_u, _t5("Cdtr"))
            _cdtrnm = etree.SubElement(_cdtr, _t5("Nm"))
            _cdtrnm.text = "Creditor Name"
            _new.append(_u)
            _ser = suggester._serialize(_new)
            if _ser != _orig:
                return FixSuggestion(suggester._xpath_of(_cdt), _orig, _ser, code, msg, "high")

    # ── pacs.009 ADV → SttlmMtd MUST be COVE ────────────────────────────────
    sttlm = next((el for el in root.iter()
                  if _localname(el) == "SttlmMtd"), None)
    if sttlm is not None:
        cur = (sttlm.text or "").strip().upper()
        if cur != "COVE":
            mlow = (msg or "")
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

    # ── PstlAdr child out of XSD sequence (e.g. Ctry after AdrLine) ─────────
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
                continue
            from app.services.fix_suggester import FixSuggestion
            _orig = suggester._serialize(_pa)
            _new = suggester._copy(_pa)
            _newkids = [c for c in _new if isinstance(c.tag, str)]
            _newkids.sort(key=lambda c: _rank.get(_localname(c), 10_000))
            for c in list(_new):
                if isinstance(c.tag, str):
                    _new.remove(c)
            for c in _newkids:
                _new.append(c)
            if suggester._serialize(_new) != _orig:
                return FixSuggestion(suggester._xpath_of(_pa), _orig,
                                     suggester._serialize(_new), code, msg, "high")

    # No SR2026-specific handler for other codes — defer to the shared engine.
    return None
