"""
SR2026 auto-fix battery — damage then repair, all 17 SR2026 message types.

Exercises the SR2026 path end-to-end:
  • fix_suggester runs with _ACTIVE_SR_VERSION = "SR2026" (uses the SR2026
    per-message validation KBs + the inherited ai_knowledge_base fixes),
  • validation uses the real SR2026Validator (not the SR2025 ISOValidator),
  • generation uses generate_single_xml(..., version="SR2026").

Two damage passes are run per message type, each measured honestly:

  PASS A — WRONG DATA (stays well-formed, so the comparison is apples-to-apples):
    injects deterministically-recoverable value defects into still-present tags —
    'Z'-suffixed datetime (CBPR+ forbids it), over-length MsgId, wrong MsgDefIdr.
    Because the doc stays well-formed, Layers 2-3 run before AND after, so
    "errors cleared" is a TRUE measure of the fixer's value-repair ability.

  PASS B — TAG REMOVAL (structural): rips ~6 tag-spans (open-only / close-only /
    both). This usually breaks well-formedness; while malformed the validator
    runs ONLY Layer 1 and reports a single syntax error, HIDING the real
    schema/business errors. So here we do NOT compare raw counts (that would be
    apples-to-oranges) — we measure whether the AI restores well-formedness and
    how many residual errors remain once Layers 2-3 can finally run.

Usage:
    cd iso20022generatorbackend
    .venv/Scripts/python.exe tests/test_autofix_sr2026.py
    .venv/Scripts/python.exe tests/test_autofix_sr2026.py -v        # dump diffs
    .venv/Scripts/python.exe tests/test_autofix_sr2026.py pacs.008  # one type
"""
import sys, os, re, asyncio, random, zlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Load .env so the LLM layer (OPENAI_API_KEY) is active — same as app/main.py at
# startup. The user asked whether the *AI* can repair the damage; without a key
# only the deterministic+KB engine runs (still a valid, weaker test).
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
except Exception:
    pass

import app.services.fix_suggester as fs
fs._set_active_sr_version("SR2026")               # <-- the whole point: SR2026 KBs

from app.services.generation.bulk_generator import generate_single_xml, get_blocks_for_message
from app.sr2026.validation.validators.validator import SR2026Validator
from app.services.fix_suggester import fix_suggester

validator = SR2026Validator()

MAX_ROUNDS = 8
TAG_SPANS = 6                                     # moderate structural damage

# Match the DOCUMENT namespace specifically — NOT the first namespace in the
# file, which is the AppHdr's head.001.001.02. Getting this wrong makes the
# validator schema-check the Document against the BAH schema (bogus SCHEMA_VAL).
_NS_RE = re.compile(
    r"<Document[^>]*\bxmlns(?::\w+)?=\"urn:iso:std:iso:20022:tech:xsd:"
    r"([a-z]+\.\d{3}\.\d{3}\.\d{2})\"")

# Generator token per SR2026 KB message type (17 types we built KBs for).
MSG_TYPES = [
    "pacs.002", "pacs.003", "pacs.004", "pacs.008",
    "pacs.009", "pacs.009.adv", "pacs.009.cov", "pacs.010",
    "camt.052", "camt.053", "camt.054", "camt.055", "camt.056", "camt.057",
    "pain.001", "pain.002", "pain.008",
]

# Codelist-validity WARNINGs whose definitively-invalid values are still safely
# auto-correctable — mirror production's _AUTOFIX_WARNING_CODES set.
_AUTOFIX_WARNING_CODES = {
    "L3_CLRSYS_CODE", "L3_ACCT_TYPE_CODE", "L3_LCLINSTRM_CODE",
    "HEAD001_BIZSVC_FORMAT", "L3_ENTRY_STATUS_CODE",
}

_TAG_RE = re.compile(r"<(/?)([A-Za-z][\w.\-]*)((?:\s[^<>]*)?)(/?)>")


# ── priority sort (copied verbatim from app/main.py, dependency-free) ────────
def priority_sort(issues):
    _P0 = {"XML_SYNTAX", "XML_WELLFORMED", "STRUCTURE_ERROR", "MALFORMED_XML",
           "UNCLOSED_TAG", "PARSE_ERROR"}
    _P1 = {"MISSING_MANDATORY_FIELD", "MISSING_UETR", "CBPR_MANDATORY_FIELD",
           "MANDATORY_FIELD_MISSING", "MISSING_FIELD", "CBPR_R3",
           "PACS010_AGENTS_REQUIRED", "L3-MANDATORY-PAYMENT-PARTIES",
           "L3-PAIN-MANDATORY-PARTIES", "L3-PACS-MATCH-FR", "L3-PACS-MATCH-TO"}
    _P2 = {"NOT_EXPECTED", "SEQUENCE", "WRONG_ORDER", "WRONG_POSITION",
           "STTLMPRTY_WRONG_PARENT", "PACS010_ELEMENT_FORBIDDEN"}

    def _prio(issue):
        code = str(issue.get("code", ""))
        if code in _P0:
            return 0
        if code in _P1 or code.startswith(("L3-MANDATORY", "L3-PAIN", "L3-PACS", "PACS010")):
            return 1
        if code in _P2:
            return 2
        return 3
    return sorted(issues, key=_prio)


def msg_type_of(xml: str) -> str:
    m = _NS_RE.search(xml)
    return m.group(1) if m else ""


# ── wrong-data injectors (operate on clean, well-formed XML) ─────────────────
def inject_wrong_data(xml: str):
    """Corrupt values in still-present tags with DETERMINISTICALLY-RECOVERABLE
    defects (format / length / wrong-known-value). Returns (xml, applied_labels).

    Deliberately NOT injected: invalid BIC / currency *values*. Those have no
    inferrable correct value, so a conservative fixer rightly leaves them
    untouched — injecting them only produces permanent residual noise."""
    applied = []

    def sub_once(pattern, repl, label, flags=0):
        nonlocal xml
        new, n = re.subn(pattern, repl, xml, count=1, flags=flags)
        if n:
            xml = new
            applied.append(label)

    # 1. 'Z'-suffixed datetime — CBPR+ forbids the Z form / requires +HH:MM.
    sub_once(r"(<CreDtTm>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\+00:00(</CreDtTm>)",
             r"\1Z\2", "datetime_z")
    # 2. Over-length id (>35) in the first MsgId — fixer truncates to 35.
    sub_once(r"<MsgId>[^<]{1,34}</MsgId>",
             "<MsgId>" + ("X" * 42) + "</MsgId>", "msgid_too_long")
    # 3. Wrong MsgDefIdr value in the BAH — expected value derivable from the
    #    message type, so the fixer can restore it.
    sub_once(r"<MsgDefIdr>[^<]+</MsgDefIdr>",
             "<MsgDefIdr>WRONG.MSG.IDR.01</MsgDefIdr>", "bad_msgdefidr")
    return xml, applied


# ── tag-span remover (open-only / close-only / both; root protected) ─────────
def remove_tags(xml: str, seed: int, target: int = TAG_SPANS):
    rng = random.Random(seed)
    spans = [{
        "start": m.start(), "end": m.end(), "name": m.group(2),
        "closing": m.group(1) == "/", "self": m.group(4) == "/",
    } for m in _TAG_RE.finditer(xml)]

    root_open_idx = next(
        (i for i, s in enumerate(spans) if not s["closing"] and not s["self"]), None)

    stack, elements = [], []
    for i, s in enumerate(spans):
        if s["self"]:
            continue
        if not s["closing"]:
            stack.append(i)
        elif stack:
            oi = stack.pop()
            elements.append((oi, i, spans[oi]["name"]))
    elements = [e for e in elements if e[0] != root_open_idx]
    rng.shuffle(elements)

    kill = set()
    stats = {"removed": 0, "both": 0, "open": 0, "close": 0}
    for oi, ci, _name in elements:
        if stats["removed"] >= target:
            break
        mode = rng.choice(["both", "open", "close", "both", "open", "close"])
        if mode == "both":
            kill |= {oi, ci}; stats["removed"] += 2; stats["both"] += 1
        elif mode == "open":
            kill.add(oi); stats["removed"] += 1; stats["open"] += 1
        else:
            kill.add(ci); stats["removed"] += 1; stats["close"] += 1

    out = xml
    for s in sorted((spans[i] for i in kill), key=lambda s: s["start"], reverse=True):
        out = out[:s["start"]] + out[s["end"]:]
    return out, stats


# ── validation snapshot via SR2026Validator ──────────────────────────────────
async def snapshot(xml: str, mt: str):
    resp = await validator.validate(xml, mt)
    wf = 2 not in (resp.layers_skipped or [])          # L2 skipped ⇒ malformed
    errors = [{"code": e.code, "path": e.path, "message": e.message,
               "severity": getattr(e, "severity", "ERROR"), "line": getattr(e, "line", None)}
              for e in resp.errors]
    warns = [{"code": w.code, "path": w.path, "message": w.message,
              "severity": "WARNING", "line": getattr(w, "line", None)}
             for w in resp.warnings]
    return wf, errors, warns


def _sigs(errors):
    return {(d["code"], d["path"]) for d in errors}


# ── iterative auto-fix (mirrors main.py _auto_fix_iterative, SR2026 validator) ─
async def auto_fix(xml: str, mt: str, max_rounds: int = MAX_ROUNDS):
    cur = xml
    seen = {hash(cur)}
    best_xml, best_sigs, best_wf = cur, None, None
    prev_sigs = None
    stall = 0
    rounds = 0

    for _ in range(max_rounds):
        wf, errors, warns = await snapshot(cur, mt)
        sigs = _sigs(errors)
        fixable = errors + [w for w in warns if w["code"] in _AUTOFIX_WARNING_CODES]

        resolved_vs_best = (best_sigs - sigs) if best_sigs is not None else set()
        if (best_sigs is None or (wf and not best_wf)
                or (wf == best_wf and (resolved_vs_best or sigs == best_sigs))):
            best_sigs, best_wf, best_xml = sigs, wf, cur

        if not fixable:
            break
        # diminishing-returns: a well-formed round that resolves none of the
        # previous well-formed round's signatures twice in a row → stop.
        if wf:
            if prev_sigs is not None and not (prev_sigs - sigs):
                stall += 1
                if stall >= 2:
                    break
            else:
                stall = 0
            prev_sigs = sigs

        rounds += 1
        try:
            comp = fix_suggester.xsd_completeness_pass(cur)
            if comp != cur:
                cur = comp
        except Exception:
            pass

        try:
            suggestions = fix_suggester.suggest_batch(
                cur, priority_sort(fixable)[:20], version="SR2026")
        except Exception as e:
            print(f"    [suggest_batch error] {e}")
            break

        fixes = [{"xpath": s.xpath, "fragment_xml": s.fragment_xml}
                 for s in suggestions
                 if s.confidence in ("high", "low")
                 and s.fragment_xml and s.fragment_xml != s.original_fragment]
        if not fixes:
            break

        new_xml = fix_suggester.apply_batch(cur, fixes)
        sig = hash(new_xml)
        if new_xml == cur or sig in seen:
            break
        seen.add(sig)
        cur = new_xml

    return best_xml, rounds


# ── runner ────────────────────────────────────────────────────────────────────
async def run(only=None, verbose=False):
    rows = []
    for token in MSG_TYPES:
        if only and only not in token:
            continue
        try:
            blocks = [b["id"] for b in get_blocks_for_message(token)]
            clean = generate_single_xml(token, blocks, idx=1, version="SR2026")
        except Exception as e:
            print(f"  [GEN_FAIL] {token}: {e}")
            rows.append((token, None)); continue

        mt = msg_type_of(clean) or token
        _, base_err, _ = await snapshot(clean, mt)
        base = _sigs(base_err)        # pre-existing errors in the clean generated XML

        # ── PASS A — wrong-data only (well-formed throughout) ─────────────────
        # Signature-based AND baseline-subtracted so the metric reflects ONLY the
        # injected defects, not pre-existing generator/validator noise:
        #   resolved   = injected errors the fix cleared,
        #   remaining  = injected errors still present,
        #   introduced = NEW errors the repair created (collateral).
        dirty, labels = inject_wrong_data(clean)
        _, wd_err, _ = await snapshot(dirty, mt)
        wd_fixed, wd_rounds = await auto_fix(dirty, mt)
        _, wdf_err, _ = await snapshot(wd_fixed, mt)
        before, after = _sigs(wd_err), _sigs(wdf_err)
        injected = before - base
        wd_injected = len(injected)
        wd_resolved = len(injected - after)
        wd_remaining = len(injected & after)
        wd_introduced = len(after - before - base)

        # ── PASS B — tag removal (structural) ─────────────────────────────────
        broken, tstats = remove_tags(clean, seed=zlib.crc32(token.encode()))
        brk_wf, brk_err, _ = await snapshot(broken, mt)
        tr_fixed, tr_rounds = await auto_fix(broken, mt)
        trf_wf, trf_err, _ = await snapshot(tr_fixed, mt)
        tr_residual = len(_sigs(trf_err) - base)   # residual beyond clean baseline

        rec = {
            "token": token, "mt": mt, "base_err": len(base_err),
            # pass A (signature-based)
            "wd_labels": labels, "wd_injected": wd_injected,
            "wd_resolved": wd_resolved, "wd_introduced": wd_introduced,
            "wd_remaining": wd_remaining, "wd_rounds": wd_rounds,
            "wd_residual": [(d["code"], (d["message"] or "")[:48]) for d in wdf_err[:3]],
            # pass B
            "removed": tstats["removed"],
            "mix": f"{tstats['both']}b/{tstats['open']}o/{tstats['close']}c",
            "brk_wf": brk_wf, "tr_wf": trf_wf, "tr_residual": max(0, tr_residual),
            "tr_rounds": tr_rounds,
            "tr_residual_codes": [(d["code"], (d["message"] or "")[:48]) for d in trf_err[:3]],
        }
        rows.append((token, rec))

        coll = f" +{wd_introduced} collateral" if wd_introduced else ""
        print(f"  {token:12s} "
              f"WRONG-DATA: injected={wd_injected} resolved={wd_resolved} "
              f"remain={wd_remaining}{coll}   "
              f"TAGS: removed={tstats['removed']}({rec['mix']}) "
              f"wf {('N' if not brk_wf else 'Y')}->{('Y' if trf_wf else 'N')} residual={max(0,tr_residual)}")
        if verbose:
            for c, m in rec["wd_residual"]:
                print(f"        wrong-data residual {c}: {m}")
            for c, m in rec["tr_residual_codes"]:
                print(f"        tag-removal residual {c}: {m}")
    return rows


def summary(rows):
    real = [r for _, r in rows if r]
    tot = len(real)
    wd_clean = sum(1 for r in real if r["wd_remaining"] == 0 and r["wd_introduced"] == 0)
    wd_inj = sum(r["wd_injected"] for r in real)
    wd_res = sum(r["wd_resolved"] for r in real)
    wd_coll = sum(r["wd_introduced"] for r in real)
    wf_restored = sum(1 for r in real if r["tr_wf"])
    tr_clean = sum(1 for r in real if r["tr_wf"] and r["tr_residual"] == 0)

    print("\n" + "=" * 88)
    print("SR2026 AUTO-FIX SUMMARY")
    print(f"  PASS A — wrong data (recoverable value defects):")
    print(f"     injected value-errors RESOLVED    : {wd_res}/{wd_inj}")
    print(f"     collateral errors INTRODUCED      : {wd_coll}")
    print(f"     messages fully clean (no remain, no collateral) : {wd_clean}/{tot}")
    print(f"  PASS B — tag removal (structural damage):")
    print(f"     well-formedness restored          : {wf_restored}/{tot}")
    print(f"     fully clean (wf + 0 residual)     : {tr_clean}/{tot}")
    print("=" * 88)
    print(f"\n  {'type':12s} | {'inj':>3} {'resolved':>8} {'remain':>6} {'collat':>6} | "
          f"{'tags':>4} {'wf':>5} {'residual':>8}")
    for r in real:
        wf = ("N->Y" if r["tr_wf"] else "N->N") if not r["brk_wf"] else ("Y->Y" if r["tr_wf"] else "Y->N")
        print(f"  {r['token']:12s} | {r['wd_injected']:>3} {r['wd_resolved']:>8} "
              f"{r['wd_remaining']:>6} {r['wd_introduced']:>6} | {r['removed']:>4} {wf:>5} "
              f"{r['tr_residual']:>8}")
    print()


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    verbose = "-v" in sys.argv
    only = args[0] if args else None
    ai = "ON" if os.getenv("OPENAI_API_KEY", "").strip() else "OFF (deterministic+KB only)"
    print(f"SR version : SR2026 (KBs: app/resources/KB/sr2026/)")
    print(f"LLM layer  : {ai}")
    print(f"Battery    : {len(MSG_TYPES)} SR2026 message types — "
          f"Pass A wrong-data + Pass B {TAG_SPANS} tag-spans\n")
    rows = asyncio.run(run(only=only, verbose=verbose))
    summary(rows)
