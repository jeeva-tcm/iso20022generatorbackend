"""
AI-fix GAP AUDIT — SR2026 vs SR2025, side by side.

Applies the SAME damage to each message type under BOTH releases, runs each
release's AI fixer (correct KB + version handler module + validator), and
aggregates, PER ERROR CODE, how often the fixer leaves it unresolved. The point
is to answer: "where does the SR2026 AI fix lag SR2025, and which SR2026-only
(delta-rule) codes can't it fix yet?"

Two damage passes per message:
  A. WRONG DATA  — recoverable value defects (datetime Z, over-length MsgId,
                   wrong MsgDefIdr). Doc stays well-formed → clean per-code
                   resolved/unresolved measure.
  B. TAG REMOVAL — ~6 tag-spans (structural). Measures well-formedness recovery
                   + residual codes.

Reuses the damage/snapshot helpers from test_autofix_sr2026.py.

Usage:
    cd iso20022generatorbackend
    .venv/Scripts/python.exe tests/audit_autofix_sr2025_vs_sr2026.py
    .venv/Scripts/python.exe tests/audit_autofix_sr2025_vs_sr2026.py pacs.008
"""
import sys, os, asyncio, zlib
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
except Exception:
    pass

import app.services.fix_suggester as fs
from app.services.fix_suggester import fix_suggester
from app.services.generation.bulk_generator import generate_single_xml, get_blocks_for_message
from app.sr2025.validation.validators.engine import ISOValidator
from app.sr2026.validation.validators.validator import SR2026Validator

# reuse damage + helpers from the SR2026 harness
import importlib.util
_spec = importlib.util.spec_from_file_location(
    "h2026", os.path.join(os.path.dirname(__file__), "test_autofix_sr2026.py"))
H = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(H)

V25 = ISOValidator()
V26 = SR2026Validator()
MAX_ROUNDS = 8

MSG_TYPES = H.MSG_TYPES
_AUTOFIX_WARNING_CODES = H._AUTOFIX_WARNING_CODES


# ── per-version validation snapshots → (well_formed, [error dicts]) ───────────
async def snap25(xml, mt):
    rep = await V25.validate(xml, mode="Full 1-3", message_type=mt)
    rd = rep.to_dict()
    details = rd.get("details", []) or []
    errs = [{"code": d.get("code"), "path": d.get("path"), "message": d.get("message"),
             "severity": d.get("severity"), "line": d.get("line")}
            for d in details if d.get("severity") in ("ERROR", "CRITICAL")]
    warns = [{"code": d.get("code"), "path": d.get("path"), "message": d.get("message"),
              "severity": "WARNING", "line": d.get("line")}
             for d in details if d.get("severity") == "WARNING"]
    wf = rd.get("layer_status", {}).get("1", {}).get("status") == "✅"
    return wf, errs, warns


async def snap26(xml, mt):
    return await H.snapshot(xml, mt)


def _sigs(errs):
    return {(e["code"], e["path"]) for e in errs}


# ── version-parametrised auto-fix loop (mirrors production) ───────────────────
async def auto_fix(xml, mt, version, snap):
    fs._set_active_sr_version(version)
    cur = xml
    seen = {hash(cur)}
    best_xml, best_sigs, best_wf = cur, None, None
    prev_sigs = None
    stall = 0
    for _ in range(MAX_ROUNDS):
        wf, errs, warns = await snap(cur, mt)
        sigs = _sigs(errs)
        fixable = errs + [w for w in warns if w["code"] in _AUTOFIX_WARNING_CODES]
        resolved_vs_best = (best_sigs - sigs) if best_sigs is not None else set()
        if (best_sigs is None or (wf and not best_wf)
                or (wf == best_wf and (resolved_vs_best or sigs == best_sigs))):
            best_sigs, best_wf, best_xml = sigs, wf, cur
        if not fixable:
            break
        if wf:
            if prev_sigs is not None and not (prev_sigs - sigs):
                stall += 1
                if stall >= 2:
                    break
            else:
                stall = 0
            prev_sigs = sigs
        try:
            comp = fix_suggester.xsd_completeness_pass(cur)
            if comp != cur:
                cur = comp
        except Exception:
            pass
        try:
            sugs = fix_suggester.suggest_batch(cur, H.priority_sort(fixable)[:20], version=version)
        except Exception as e:
            print(f"    [{version} suggest_batch error] {e}")
            break
        fixes = [{"xpath": s.xpath, "fragment_xml": s.fragment_xml}
                 for s in sugs if s.confidence in ("high", "low")
                 and s.fragment_xml and s.fragment_xml != s.original_fragment]
        if not fixes:
            break
        new_xml = fix_suggester.apply_batch(cur, fixes)
        if new_xml == cur or hash(new_xml) in seen:
            break
        seen.add(hash(new_xml))
        cur = new_xml
    return best_xml


# ── run one (message, version) through both damage passes ─────────────────────
async def run_one(token, version, snap):
    blocks = [b["id"] for b in get_blocks_for_message(token)]
    clean = generate_single_xml(token, blocks, idx=1, version=version)
    mt = H.msg_type_of(clean) or token
    _, base_err, _ = await snap(clean, mt)
    base = _sigs(base_err)

    # A) wrong data
    dirty, _labels = H.inject_wrong_data(clean)
    _, wd_err, _ = await snap(dirty, mt)
    wd_fixed = await auto_fix(dirty, mt, version, snap)
    _, wdf_err, _ = await snap(wd_fixed, mt)
    injected = _sigs(wd_err) - base
    wd_remaining = injected & _sigs(wdf_err)           # injected, not fixed
    wd_collateral = _sigs(wdf_err) - _sigs(wd_err) - base

    # B) tag removal
    broken, _st = H.remove_tags(clean, seed=zlib.crc32(token.encode()))
    brk_wf, _brk_err, _ = await snap(broken, mt)
    tr_fixed = await auto_fix(broken, mt, version, snap)
    trf_wf, trf_err, _ = await snap(tr_fixed, mt)
    tr_residual = _sigs(trf_err) - base

    return {
        "mt": mt, "base": base,
        "wd_injected": injected, "wd_remaining": wd_remaining, "wd_collateral": wd_collateral,
        "tr_brk_wf": brk_wf, "tr_fix_wf": trf_wf, "tr_residual": tr_residual,
    }


async def main(only=None):
    # aggregates
    agg = {v: {"wd_inj": 0, "wd_res": 0, "wd_coll": 0,
               "wf_ok": 0, "n": 0,
               "unresolved": defaultdict(int)} for v in ("SR2025", "SR2026")}
    rows = []
    for token in MSG_TYPES:
        if only and only not in token:
            continue
        rec = {"token": token}
        for version, snap in (("SR2025", snap25), ("SR2026", snap26)):
            r = await run_one(token, version, snap)
            a = agg[version]
            a["n"] += 1
            a["wd_inj"] += len(r["wd_injected"])
            a["wd_res"] += len(r["wd_injected"]) - len(r["wd_remaining"])
            a["wd_coll"] += len(r["wd_collateral"])
            a["wf_ok"] += 1 if r["tr_fix_wf"] else 0
            for code, _p in (r["wd_remaining"] | r["tr_residual"] | r["wd_collateral"]):
                a["unresolved"][code] += 1
            rec[version] = r
        rows.append(rec)
        # per-message line
        s25, s26 = rec["SR2025"], rec["SR2026"]
        print(f"  {token:12s}  "
              f"SR2025[wd {len(s25['wd_injected'])-len(s25['wd_remaining'])}/{len(s25['wd_injected'])} "
              f"coll {len(s25['wd_collateral'])} wf {'Y' if s25['tr_fix_wf'] else 'N'} resid {len(s25['tr_residual'])}]  "
              f"SR2026[wd {len(s26['wd_injected'])-len(s26['wd_remaining'])}/{len(s26['wd_injected'])} "
              f"coll {len(s26['wd_collateral'])} wf {'Y' if s26['tr_fix_wf'] else 'N'} resid {len(s26['tr_residual'])}]")

    # ── summary ──
    print("\n" + "=" * 86)
    print("AI-FIX GAP AUDIT — SR2025 vs SR2026")
    for v in ("SR2025", "SR2026"):
        a = agg[v]
        print(f"  {v}:  wrong-data resolved {a['wd_res']}/{a['wd_inj']}  "
              f"collateral {a['wd_coll']}  |  tag-removal wf-restored {a['wf_ok']}/{a['n']}")
    print("=" * 86)

    # ── per-code unresolved comparison ──
    all_codes = set(agg["SR2025"]["unresolved"]) | set(agg["SR2026"]["unresolved"])
    print(f"\n  {'unresolved error code':40s} {'SR2025':>7} {'SR2026':>7}  flag")
    for code in sorted(all_codes, key=lambda c: (-agg['SR2026']['unresolved'].get(c, 0), c)):
        c25 = agg["SR2025"]["unresolved"].get(code, 0)
        c26 = agg["SR2026"]["unresolved"].get(code, 0)
        flag = ""
        if c26 and not c25:
            flag = "SR2026-ONLY GAP"
        elif c26 > c25:
            flag = "SR2026 WORSE"
        print(f"  {code or '?':40s} {c25:>7} {c26:>7}  {flag}")
    print()


if __name__ == "__main__":
    only = next((a for a in sys.argv[1:] if not a.startswith("-")), None)
    ai = "ON" if os.getenv("OPENAI_API_KEY", "").strip() else "OFF (deterministic+KB only)"
    print(f"LLM layer: {ai}")
    print(f"Auditing {len(MSG_TYPES)} message types under SR2025 + SR2026 "
          f"(wrong-data + tag-removal)\n")
    asyncio.run(main(only=only))
