"""
HEAVY-damage auto-fix harness — destroy then repair, all 18 message types.

Goes far beyond test_autofix_harness.py (1 tag) and test_autofix_audit.py (1-3
tags). For each message type this generates a clean full XML, then RIPS OUT
~25 tag-spans at once — a deliberate mix of:
    • both   — open AND close tag of an element removed
    • open   — only the OPENING tag removed (orphan close + text)
    • close  — only the CLOSING tag removed (content bleeds into next sibling)
Only the root element (Document / BusMsgEnvlp) is protected so the doc is still
identifiable. Everything else is fair game. This produces severe well-formedness
+ structural collapse — the worst-case the user asked to stress.

Then the iterative auto-fix (max 8 rounds, same loop as production main.py) runs
and we report, per type:
    broken : well-formed? / error count
    fixed  : well-formed? / error count / residual codes
    delta  : how many errors the AI cleared / rounds used

Usage:
    cd iso20022generatorbackend
    .venv/Scripts/python.exe tests/test_autofix_heavy.py
    .venv/Scripts/python.exe tests/test_autofix_heavy.py -v        # dump diffs
    .venv/Scripts/python.exe tests/test_autofix_heavy.py pacs.008  # one type
"""
import sys, os, re, asyncio, time, random, zlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Load .env so the LLM fallback (OPENAI_API_KEY) is active — same as the app does
# at startup in app/main.py. Without this the harness only exercises the
# deterministic engine; the user asked for the *AI* to repair the damage.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
except Exception:
    pass

from app.services.bulk_generator import generate_single_xml, get_blocks_for_message
from app.services.validator import ISOValidator
from app.services.fix_suggester import fix_suggester

validator = ISOValidator()

MAX_ROUNDS = 8
TARGET_SPANS = 25  # tag-spans to remove per message (lands in the 20-30 band)

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

_TAG_RE = re.compile(r"<(/?)([A-Za-z][\w.\-]*)((?:\s[^<>]*)?)(/?)>")


# ── heavy damage injector ────────────────────────────────────────────────────
def heavy_damage(xml: str, seed: int, target: int = TARGET_SPANS):
    """Remove ~`target` tag-spans, mixing open-only / close-only / both.

    Returns (broken_xml, stats) where stats = {removed, both, open, close}.
    """
    rng = random.Random(seed)

    spans = []
    for m in _TAG_RE.finditer(xml):
        spans.append({
            "start": m.start(), "end": m.end(),
            "name": m.group(2),
            "closing": m.group(1) == "/",
            "self": m.group(4) == "/",
        })

    # Protected root = first real opening tag (Document or BusMsgEnvlp).
    root_open_idx = next(
        (i for i, s in enumerate(spans) if not s["closing"] and not s["self"]),
        None,
    )

    # Pair opens→closes via a stack (input is well-formed) to get element units.
    stack, elements = [], []
    for i, s in enumerate(spans):
        if s["self"]:
            continue
        if not s["closing"]:
            stack.append(i)
        elif stack:
            oi = stack.pop()
            elements.append((oi, i, spans[oi]["name"]))

    # Drop the protected root element from the candidate pool.
    elements = [e for e in elements if e[0] != root_open_idx]
    rng.shuffle(elements)

    kill = set()
    stats = {"removed": 0, "both": 0, "open": 0, "close": 0, "detail": []}
    for oi, ci, name in elements:
        if stats["removed"] >= target:
            break
        mode = rng.choice(["both", "open", "close", "both", "open", "close"])
        if mode == "both":
            kill.add(oi); kill.add(ci); stats["removed"] += 2; stats["both"] += 1
        elif mode == "open":
            kill.add(oi); stats["removed"] += 1; stats["open"] += 1
        else:
            kill.add(ci); stats["removed"] += 1; stats["close"] += 1
        stats["detail"].append((name, mode))

    # Delete in reverse offset order so earlier offsets stay valid.
    out = xml
    for s in sorted((spans[i] for i in kill), key=lambda s: s["start"], reverse=True):
        out = out[:s["start"]] + out[s["end"]:]
    return out, stats


# ── validation snapshot ──────────────────────────────────────────────────────
async def snapshot(xml: str, msg_type: str):
    report = await validator.validate(xml, mode="Full 1-3", message_type=msg_type)
    rd = report.to_dict()
    details = rd.get("details", []) or []
    errors = [d for d in details if d.get("severity") in ("ERROR", "CRITICAL")]
    wf = rd.get("layer_status", {}).get("1", {}).get("status") == "✅"
    return wf, errors, rd


# ── iterative auto-fix (mirrors main.py _auto_fix_iterative) ─────────────────
async def auto_fix(xml: str, msg_type: str, max_rounds: int = MAX_ROUNDS):
    cur = xml
    seen = {hash(cur)}
    best_xml, best_errors, best_wf = cur, None, None
    prev_round_errors = None
    rounds_used = 0

    for _ in range(max_rounds):
        wf, errors, rd = await snapshot(cur, msg_type)
        details = rd.get("details", []) or []
        fixable = errors + [
            d for d in details
            if d.get("severity") == "WARNING" and d.get("code") in _AUTOFIX_WARNING_CODES
        ]
        if (best_errors is None
                or (wf and not best_wf)
                or (wf == best_wf and len(errors) <= best_errors)):
            best_errors, best_wf, best_xml = len(errors), wf, cur

        if not fixable:
            break
        if wf:
            if prev_round_errors is not None and len(errors) >= prev_round_errors:
                break
            prev_round_errors = len(errors)

        rounds_used += 1
        try:
            suggestions = fix_suggester.suggest_batch(cur, fixable[:20])
        except Exception as e:  # noqa: BLE001
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


# ── runner ───────────────────────────────────────────────────────────────────
async def run_tests(only=None, verbose=False):
    rows = []
    for msg_type in MSG_TYPES:
        if only and only not in msg_type:
            continue
        blocks = get_blocks_for_message(msg_type)
        all_ids = [b["id"] for b in blocks]
        try:
            clean_xml = generate_single_xml(msg_type, all_ids, idx=1)
        except Exception as e:  # noqa: BLE001
            rows.append((msg_type, None))
            print(f"  [GEN_FAIL] {msg_type}: {e}")
            continue

        base_wf, base_errors, _ = await snapshot(clean_xml, msg_type)
        broken, stats = heavy_damage(clean_xml, seed=zlib.crc32(msg_type.encode()))
        brk_wf, brk_errors, _ = await snapshot(broken, msg_type)

        t0 = time.time()
        fixed, rounds = await auto_fix(broken, msg_type)
        elapsed = time.time() - t0
        fix_wf, fix_errors, fd = await snapshot(fixed, msg_type)

        residual = [
            (d.get("code", "?"), (d.get("message", "") or "")[:55])
            for d in fix_errors[:4]
        ]
        introduced = max(0, len(fix_errors) - len(base_errors))
        rec = {
            "type": msg_type,
            "base_err": len(base_errors),
            "removed": stats["removed"],
            "mix": f"{stats['both']}b/{stats['open']}o/{stats['close']}c",
            "brk_wf": brk_wf, "brk_err": len(brk_errors),
            "fix_wf": fix_wf, "fix_err": len(fix_errors),
            "introduced": introduced,
            "rounds": rounds, "elapsed": elapsed,
            "residual": residual,
            "fully_clean": introduced == 0 and fix_wf,
        }
        rows.append((msg_type, rec))

        verdict = "CLEAN" if rec["fully_clean"] else ("WF-OK" if fix_wf else "BROKEN")
        print(f"  [{verdict:6s}] {msg_type:13s} "
              f"removed={stats['removed']:>2} ({rec['mix']:>9})  "
              f"broken: wf={'Y' if brk_wf else 'N'} err={len(brk_errors):>2}  "
              f"->  fixed: wf={'Y' if fix_wf else 'N'} err={len(fix_errors):>2}  "
              f"rounds={rounds} {elapsed:4.1f}s")
        if not rec["fully_clean"] and residual:
            for code, msg in residual:
                print(f"             residual {code}: {msg}")
        if verbose:
            print("\n----- BROKEN -----\n" + broken[:1500])
            print("\n----- FIXED -----\n" + fixed[:1500] + "\n")

    return rows


def print_summary(rows):
    real = [r for _, r in rows if r is not None]
    clean = sum(1 for r in real if r["fully_clean"])
    wf_ok = sum(1 for r in real if r["fix_wf"])
    total = len(real)
    print("\n" + "=" * 78)
    print(f"HEAVY-DAMAGE SUMMARY  ({TARGET_SPANS} tag-spans removed/msg, "
          f"max {MAX_ROUNDS} rounds)")
    print(f"  fully repaired (well-formed + 0 introduced errors) : {clean}/{total}")
    print(f"  well-formedness restored                           : {wf_ok}/{total}")
    print("=" * 78)
    # error-reduction table
    print(f"\n  {'type':13s} {'removed':>7} {'brk_err':>7} {'fix_err':>7} "
          f"{'cleared':>7} {'rounds':>6}  result")
    for r in real:
        cleared = r["brk_err"] - r["fix_err"]
        res = "CLEAN" if r["fully_clean"] else ("WF-OK" if r["fix_wf"] else "BROKEN")
        print(f"  {r['type']:13s} {r['removed']:>7} {r['brk_err']:>7} "
              f"{r['fix_err']:>7} {cleared:>7} {r['rounds']:>6}  {res}")
    print()


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    verbose = "-v" in sys.argv
    only = args[0] if args else None
    _ai = "ON" if os.getenv("OPENAI_API_KEY", "").strip() else "OFF (deterministic only)"
    print(f"LLM fallback: {_ai}")
    print(f"HEAVY auto-fix battery: {len(MSG_TYPES)} types, "
          f"~{TARGET_SPANS} tag-spans ripped per msg "
          f"(open-only / close-only / both)\n")
    rows = asyncio.run(run_tests(only=only, verbose=verbose))
    print_summary(rows)
