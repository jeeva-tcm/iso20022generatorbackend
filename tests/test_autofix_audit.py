"""
Extended auto-fix AUDIT harness — hard structural faults across 18 msg types.

Goes beyond test_autofix_harness.py (5 simple faults). This battery injects the
REAL-WORLD corruption patterns users have reported, to surface fixer gaps:

  1. multi_unclosed   — several text elements missing their close tags at once
                        (e.g. <CxlId>X\n<Case>… , <BICFI>Y\n</FinInstnId>)
  2. simple_with_child— a simple-type (Max35Text) element wrapping a child element
                        (e.g. <Id>ID-1<IBAN>…</IBAN></Id>)
  3. bare_lt          — a stray '<' before a closing tag (<\n/Fr>)
  4. anon_close       — an anonymous close tag </> instead of </Tag>
  5. mixed_struct     — missing close tag + a bad enum value together

Each fault is injected, then the iterative auto-fix runs (max 8 rounds), and the
result is validated. PASS = no introduced ERROR/CRITICAL residual after fixing.

Usage:
    cd iso20022generatorbackend
    .venv/Scripts/python tests/test_autofix_audit.py
"""
import sys, os, re, asyncio, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.bulk_generator import generate_single_xml, get_blocks_for_message
from app.services.validator import ISOValidator
from app.services.fix_suggester import fix_suggester

validator = ISOValidator()

MAX_ROUNDS = 8
_AUTOFIX_WARNING_CODES = {
    "L3_CLRSYS_CODE", "L3_ACCT_TYPE_CODE", "L3_LCLINSTRM_CODE",
    "HEAD001_BIZSVC_FORMAT", "L3_ENTRY_STATUS_CODE",
}

MSG_TYPES = [
    "pacs.008", "pacs.009", "pacs.009.adv", "pacs.009.cov",
    "pacs.004", "pacs.003", "pacs.002", "pacs.010", "pacs.010.v3",
    "camt.052", "camt.053", "camt.054", "camt.055", "camt.056", "camt.057",
    "pain.001", "pain.002", "pain.008",
]

FAULT_TYPES = ["multi_unclosed", "simple_with_child", "bare_lt", "anon_close", "mixed_struct"]

# ── injectors ────────────────────────────────────────────────────────────────

def _inject_multi_unclosed(xml: str) -> str:
    """Strip the closing tags of up to 3 text-only leaf elements (content runs
    into the next sibling's open tag). Mirrors the camt.056 user report."""
    # Find text-only elements: <Tag>text</Tag> where text has no '<'
    leaf_re = re.compile(r'<([A-Za-z][\w.\-]*)((?:\s[^<>]*)?)>([^<>]+)</\1>')
    count = 0
    out = xml
    for m in leaf_re.finditer(xml):
        # Remove the closing tag of this element
        close = f'</{m.group(1)}>'
        # Only strip if this exact close appears right after the text (single occurrence safe)
        frag = m.group(0)
        broken = frag[: -len(close)]  # drop trailing </Tag>
        out = out.replace(frag, broken, 1)
        count += 1
        if count >= 3:
            break
    return out if count else xml


def _inject_simple_with_child(xml: str) -> str:
    """Insert a child element into a simple-type text element (e.g. <MsgId>)."""
    # Target the first <MsgId>TEXT</MsgId> — MsgId is Max35Text (simple type)
    for tag in ("MsgId", "InstrId", "EndToEndId", "TxId", "NtryRef"):
        m = re.search(rf'(<{tag}>)([^<>]+)(</{tag}>)', xml)
        if m:
            inject = f'{m.group(1)}{m.group(2)}<IBAN>GB29NWBK60161331926819</IBAN>{m.group(3)}'
            return xml[:m.start()] + inject + xml[m.end():]
    return xml


def _inject_bare_lt(xml: str) -> str:
    """Insert a stray '<' before a closing tag in the AppHdr Fr block."""
    fr_m = re.search(r'(<BICFI>[^<]*)(</BICFI>\s*</FinInstnId>\s*</FIId>\s*</Fr>)', xml, re.DOTALL)
    if fr_m:
        # Replace </BICFI></FinInstnId></FIId></Fr> chain with bare '<\n/Fr>'
        return xml[:fr_m.start(2)] + "\n        <\n        /Fr>" + xml[fr_m.end(2):]
    return xml


def _inject_anon_close(xml: str) -> str:
    """Replace a leaf element's closing tag with an anonymous </>."""
    for tag in ("Nm", "TwnNm", "StrtNm", "MsgId"):
        m = re.search(rf'(<{tag}>[^<>]+)</{tag}>', xml)
        if m:
            return xml[:m.start()] + m.group(1) + "</>" + xml[m.end():]
    return xml


def _inject_mixed_struct(xml: str) -> str:
    """Missing close tag on a text leaf + a bad enum code, simultaneously."""
    out = xml
    # bad enum
    m = re.search(r'(<(?:ChrgBr|CdtDbtInd)>)([A-Z]{3,4})(</\w+>)', out)
    if m:
        out = out[:m.start(2)] + "for" + out[m.end(2):]
    # missing close on a text leaf
    m2 = re.search(r'(<(?:InstrId|MsgId|EndToEndId)>)([^<>]+)(</\w+>)', out)
    if m2:
        out = out[:m2.start(3)] + out[m2.end(3):]  # drop the close tag
    return out if out != xml else xml


INJECTORS = {
    "multi_unclosed":    _inject_multi_unclosed,
    "simple_with_child": _inject_simple_with_child,
    "bare_lt":           _inject_bare_lt,
    "anon_close":        _inject_anon_close,
    "mixed_struct":      _inject_mixed_struct,
}

# ── iterative fix (mirrors main.py _auto_fix_iterative) ─────────────────────

async def _auto_fix(xml: str, msg_type: str, max_rounds: int = MAX_ROUNDS):
    cur = xml
    seen = {hash(cur)}
    best_xml = cur
    best_errors = None
    best_wf = None
    prev_round_errors = None
    rounds_used = 0

    for _ in range(max_rounds):
        report = await validator.validate(cur, mode="Full 1-3",
                                          message_type=msg_type or "Auto-detect")
        rd = report.to_dict()
        details = rd.get("details", []) or []
        errors = [d for d in details if d.get("severity") in ("ERROR", "CRITICAL")]
        fixable = errors + [
            d for d in details
            if d.get("severity") == "WARNING" and d.get("code") in _AUTOFIX_WARNING_CODES
        ]
        cur_wf = rd.get("layer_status", {}).get("1", {}).get("status") == "✅"
        if (best_errors is None
                or (cur_wf and not best_wf)
                or (cur_wf == best_wf and len(errors) <= best_errors)):
            best_errors = len(errors)
            best_wf = cur_wf
            best_xml = cur
        if not fixable:
            break
        if cur_wf:
            if prev_round_errors is not None and len(errors) >= prev_round_errors:
                break
            prev_round_errors = len(errors)
        rounds_used += 1
        try:
            suggestions = fix_suggester.suggest_batch(cur, fixable[:20])
        except Exception as e:
            print(f"    [suggest_batch error] {e}")
            break
        fixes_list = [
            {"xpath": s.xpath, "fragment_xml": s.fragment_xml}
            for s in suggestions
            if s.confidence in ("high", "low")
            and s.fragment_xml and s.fragment_xml != s.original_fragment
        ]
        if not fixes_list:
            break
        new_xml = fix_suggester.apply_batch(cur, fixes_list)
        sig = hash(new_xml)
        if new_xml == cur or sig in seen:
            break
        seen.add(sig)
        cur = new_xml
    return best_xml, rounds_used


async def run_tests():
    results = []
    for msg_type in MSG_TYPES:
        blocks = get_blocks_for_message(msg_type)
        all_ids = [b["id"] for b in blocks]
        try:
            clean_xml = generate_single_xml(msg_type, all_ids, idx=1)
        except Exception as e:
            for fault in FAULT_TYPES:
                results.append((msg_type, fault, False, -1, f"GEN_FAIL: {e}"))
            continue
        base_report = await validator.validate(clean_xml, mode="Full 1-3", message_type=msg_type)
        base_errors = base_report.to_dict().get("errors", 0)

        for fault in FAULT_TYPES:
            broken = INJECTORS[fault](clean_xml)
            if broken == clean_xml:
                results.append((msg_type, fault, True, 0, "SKIP:no_injection_site"))
                continue
            fixed, rounds = await _auto_fix(broken, msg_type)
            final = await validator.validate(fixed, mode="Full 1-3", message_type=msg_type)
            fd = final.to_dict()
            residual = [d for d in (fd.get("details", []) or [])
                        if d.get("severity") in ("ERROR", "CRITICAL")]
            introduced = max(0, len(residual) - base_errors)
            passed = (introduced == 0)
            note = f"rounds={rounds}"
            if not passed:
                codes = [d.get("code", "?") + ":" + (d.get("message", "")[:55])
                         for d in residual[:3]]
                note += f" | residual={codes}"
            results.append((msg_type, fault, passed, introduced, note))
            status = "PASS" if passed else "FAIL"
            print(f"  [{status}] {msg_type:14s} {fault:18s} {note}")
    return results


def print_summary(results):
    total = len(results)
    passed = sum(1 for r in results if r[2])
    skipped = sum(1 for r in results if "SKIP" in r[4])
    failed = [r for r in results if not r[2] and "SKIP" not in r[4]]
    print("\n" + "=" * 72)
    print(f"AUDIT SUMMARY: {passed}/{total} passed  ({skipped} skipped/no-site)")
    print(f"FAILED: {len(failed)}")
    print("=" * 72)
    for r in failed:
        print(f"  {r[0]:14s} {r[1]:18s} introduced={r[3]}  {r[4][:110]}")
    print()


if __name__ == "__main__":
    print(f"Extended audit: {len(MSG_TYPES)} types × {len(FAULT_TYPES)} hard faults (max {MAX_ROUNDS} rounds)\n")
    results = asyncio.run(run_tests())
    print_summary(results)
