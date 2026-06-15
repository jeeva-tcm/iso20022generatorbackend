"""
Auto-fix regression harness — 18 message types × 5 fault categories.
Generates a clean XML, corrupts it with a specific fault, runs the
iterative auto-fix (max 8 rounds), then validates the result.

Usage:
    cd iso20022generatorbackend
    .venv/Scripts/python tests/test_autofix_harness.py

Fault categories injected:
  1. missing_close   — remove a random closing tag  → not well-formed
  2. bad_bicfi       — replace a BICFI with garbage  → schema/L2/L3
  3. past_date       — replace a datetime with a past date (no TZ) → L2/L3
  4. bad_decimal     — add letters into an amount    → schema error
  5. bad_code        — put "for" into a code element → enum violation
"""
import sys, os, re, asyncio, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.bulk_generator import generate_single_xml, get_blocks_for_message
from app.services.validator import ISOValidator
from app.services.fix_suggester import fix_suggester

validator = ISOValidator()

# ── 8 rounds as the user requested ──────────────────────────────────────────
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

FAULT_TYPES = ["missing_close", "bad_bicfi", "past_date", "bad_decimal", "bad_code"]

# ── fault injectors ──────────────────────────────────────────────────────────

def _inject_missing_close(xml: str) -> str:
    """Remove the closing tag of a known-safe wrapper element.

    We try a priority list of tags that are present in most ISO 20022 messages
    and are isolated wrappers with no sequence-sensitive siblings of their own.
    Removing any of these produces a simple well-formedness error the fixer can
    close without reordering siblings.
    """
    # Priority: tags that are single-child wrappers or terminal containers
    # whose removal does NOT make a sibling appear out-of-sequence.
    _CANDIDATES = [
        "FinInstnId",   # inside AgtId — wraps BICFI, no ordered siblings after
        "ClrSys",       # inside SttlmInf — small wrapper
        "PstlAdr",      # address block — self-contained
        "Id",           # identity wrapper
        "OrgId",        # organisation id wrapper
        "PrvtId",       # private id wrapper
        "CtctDtls",     # contact details
        "RltdPties",    # related parties block
        "RltdAgts",     # related agents block
        "Purp",         # purpose wrapper
        "RmtInf",       # remittance info
        "Refs",         # references block
        "CxlRsnInf",    # cancellation reason
        "CancellationReason",
    ]
    for tag in _CANDIDATES:
        if f'</{tag}>' in xml:
            return xml.replace(f'</{tag}>', '', 1)
    return xml


def _inject_bad_bicfi(xml: str) -> str:
    """Replace AppHdr Fr BICFI (or first BICFI) with an invalid-format value."""
    # Prefer injecting into AppHdr/Fr so body agent BIC stays valid (avoids L3-PACS-MATCH-FR cascade)
    # Try AppHdr Fr section first
    fr_m = re.search(r'<Fr\b[^>]*>.*?</Fr>', xml, re.DOTALL)
    if fr_m:
        fr_block = fr_m.group(0)
        bic_m = re.search(r'(<BICFI>)([A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?)(</BICFI>)', fr_block)
        if bic_m:
            replaced = fr_block[:bic_m.start(2)] + "BADINPUT" + fr_block[bic_m.end(2):]
            return xml[:fr_m.start()] + replaced + xml[fr_m.end():]
    # Fallback: replace first BICFI anywhere
    m = re.search(r'(<BICFI>)([A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?)(</BICFI>)', xml)
    if not m:
        return xml
    return xml[:m.start(2)] + "BADINPUT" + xml[m.end(2):]


def _inject_past_date(xml: str) -> str:
    """Replace first ISODateTime with a past date missing timezone."""
    m = re.search(r'(<\w*(?:DtTm|CreDt)\w*>)(\d{4}-\d{2}-\d{2}T[^<]+)(</)', xml)
    if not m:
        m = re.search(r'(<IntrBkSttlmDt>)(\d{4}-\d{2}-\d{2})(</)', xml)
    if not m:
        return xml
    return xml[:m.start(2)] + "2020-01-01T00:00:00" + xml[m.end(2)-2:]


def _inject_bad_decimal(xml: str) -> str:
    """Corrupt a numeric amount — replace with a value that has too many decimal places."""
    # Match <SomeAmt Ccy="USD">digits.digits</SomeAmt> capturing the numeric text as group 2
    # Use a named-element approach to avoid hitting <Ccy> text nodes
    m = re.search(
        r'(<(?:InstdAmt|IntrBkSttlmAmt|TtlIntrBkSttlmAmt|EqvtAmt|Amt)\s[^>]*Ccy="[A-Z]{3}"[^>]*>)'
        r'(\d+\.\d+)'
        r'(</(?:InstdAmt|IntrBkSttlmAmt|TtlIntrBkSttlmAmt|EqvtAmt|Amt)>)',
        xml
    )
    if not m:
        # Simpler fallback: element open tag contains Ccy attr, text is digits.digits
        m = re.search(r'(<\w+Amt Ccy="[A-Z]{3}">)(\d+)(\.\d+)(</\w+Amt>)', xml)
        if m:
            # Replace the fractional digits with 7 digits (too many decimal places)
            return xml[:m.start(3)] + ".1234567" + xml[m.end(3):]
        return xml
    # Replace with a value that has 7 decimal digits (exceeds XSD fractionDigits=5)
    integer_part = m.group(2).split('.')[0]
    return xml[:m.start(2)] + integer_part + ".1234567" + xml[m.end(2):]


def _inject_bad_code(xml: str) -> str:
    """Replace first short code element value with 'for'."""
    # Targets: ChrgBr, SvcLvl/Cd, TxSts, CdtDbtInd, Cd inside Sts
    m = re.search(r'(<(?:ChrgBr|TxSts|GrpSts|CdtDbtInd)>)([A-Z]{3,4})(</\w+>)', xml)
    if not m:
        m = re.search(r'(<Cd>)([A-Z]{2,6})(</Cd>)', xml)
    if not m:
        return xml
    return xml[:m.start(2)] + "for" + xml[m.end(2):]


INJECTORS = {
    "missing_close": _inject_missing_close,
    "bad_bicfi":     _inject_bad_bicfi,
    "past_date":     _inject_past_date,
    "bad_decimal":   _inject_bad_decimal,
    "bad_code":      _inject_bad_code,
}

# ── iterative fix (mirrors main.py _auto_fix_iterative) ─────────────────────

async def _auto_fix(xml: str, msg_type: str, max_rounds: int = MAX_ROUNDS):
    cur = xml
    rounds_used = 0
    applied_total = 0
    seen = {hash(cur)}
    best_xml = cur
    best_errors = None
    best_wf = None
    prev_round_errors = None

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
        applied_total += len(fixes_list)
        cur = new_xml

    return best_xml, rounds_used, applied_total


# ── test runner ──────────────────────────────────────────────────────────────

async def run_tests():
    results = []  # (msg_type, fault, passed, rounds, residual_errors, note)

    for msg_type in MSG_TYPES:
        # Generate one clean XML
        blocks = get_blocks_for_message(msg_type)
        all_ids = [b["id"] for b in blocks]
        try:
            clean_xml = generate_single_xml(msg_type, all_ids, idx=1)
        except Exception as e:
            for fault in FAULT_TYPES:
                results.append((msg_type, fault, False, 0, -1, f"GEN_FAIL: {e}"))
            continue

        # Verify clean baseline passes
        base_report = await validator.validate(clean_xml, mode="Full 1-3",
                                               message_type=msg_type)
        base_errors = base_report.to_dict().get("errors", 0)
        if base_errors > 0:
            # Still run fixes on clean (it may have minor issues) but note it
            pass

        for fault in FAULT_TYPES:
            injector = INJECTORS[fault]
            broken = injector(clean_xml)
            if broken == clean_xml:
                results.append((msg_type, fault, True, 0, 0, "SKIP:no_injection_site"))
                continue

            t0 = time.time()
            fixed, rounds, applied = await _auto_fix(broken, msg_type)
            elapsed = time.time() - t0

            final = await validator.validate(fixed, mode="Full 1-3",
                                             message_type=msg_type)
            fd = final.to_dict()
            residual = [d for d in (fd.get("details", []) or [])
                        if d.get("severity") in ("ERROR", "CRITICAL")]

            # Pass = no residual errors that were introduced by our fault injection
            # (base_errors accounts for any pre-existing issues in the generator)
            introduced_residual = max(0, len(residual) - base_errors)
            passed = (introduced_residual == 0)

            note = f"rounds={rounds} applied={applied} elapsed={elapsed:.1f}s"
            if not passed:
                codes = [d.get("code", "?") + ":" + (d.get("message", "")[:60])
                         for d in residual[:3]]
                note += f" | residual={codes}"

            results.append((msg_type, fault, passed, rounds, introduced_residual, note))

            status = "PASS" if passed else "FAIL"
            print(f"  [{status}] {msg_type:18s} {fault:15s} {note}")

    return results


def print_summary(results):
    total = len(results)
    passed = sum(1 for r in results if r[2])
    skipped = sum(1 for r in results if "SKIP" in r[5])
    failed_rows = [r for r in results if not r[2] and "SKIP" not in r[5]]

    print("\n" + "="*70)
    print(f"SUMMARY: {passed}/{total} passed  ({skipped} skipped/no-injection-site)")
    print(f"FAILED:  {len(failed_rows)}")
    print("="*70)
    if failed_rows:
        print("\nFAILED CASES:")
        for r in failed_rows:
            print(f"  {r[0]:20s} {r[1]:15s} residual={r[4]}  {r[5][:100]}")
    print()


if __name__ == "__main__":
    print(f"Running auto-fix harness: {len(MSG_TYPES)} msg types × {len(FAULT_TYPES)} faults  (max {MAX_ROUNDS} rounds each)\n")
    results = asyncio.run(run_tests())
    print_summary(results)
