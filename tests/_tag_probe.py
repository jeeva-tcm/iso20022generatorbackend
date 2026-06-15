"""
Probe: remove an opening tag or a closing tag from a leaf element and see if
auto-fix recovers, across pacs/camt/pain message types.

Usage: .venv/Scripts/python tests/_tag_probe.py
"""
import sys, os, re, asyncio, io
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from app.services.bulk_generator import generate_single_xml, get_blocks_for_message
from app.services.validator import ISOValidator
from app.services.fix_suggester import fix_suggester

validator = ISOValidator()

MSG_TYPES = [
    "pacs.008", "pacs.003", "pacs.004", "pacs.009", "pacs.009.cov", "pacs.010",
    "camt.054", "pain.001", "pain.008",
]


def remove_closing_tag(xml: str, tag: str) -> str | None:
    m = re.search(rf'(<{tag}>)([^<>]+)(</{tag}>)', xml)
    if not m:
        return None
    return xml[:m.start(3)] + xml[m.end(3):]


def remove_opening_tag(xml: str, tag: str) -> str | None:
    m = re.search(rf'(<{tag}>)([^<>]+)(</{tag}>)', xml)
    if not m:
        return None
    return xml[:m.start(1)] + xml[m.end(1):]


async def auto_fix(xml: str, msg_type: str, max_rounds=8):
    cur = xml
    for rnd in range(max_rounds):
        report = await validator.validate(cur, mode="Full 1-3", message_type=msg_type or "Auto-detect")
        rd = report.to_dict()
        details = rd.get("details", []) or []
        wf = rd.get("layer_status", {}).get("1", {}).get("status")
        errors = [d for d in details if d.get("severity") in ("ERROR", "CRITICAL")]
        if not errors:
            return cur, rnd, [], wf
        try:
            suggestions = fix_suggester.suggest_batch(cur, errors[:20])
        except Exception as e:
            return cur, rnd, [f"suggest_batch error: {e}"], wf
        fixes_list = [
            {"xpath": s.xpath, "fragment_xml": s.fragment_xml}
            for s in suggestions
            if s.confidence in ("high", "low")
            and s.fragment_xml and s.fragment_xml != s.original_fragment
        ]
        if not fixes_list:
            return cur, rnd, [f"{e['code']}: {e['message']}" for e in errors], wf
        new_xml = fix_suggester.apply_batch(cur, fixes_list)
        if new_xml == cur:
            return cur, rnd, [f"{e['code']}: {e['message']}" for e in errors], wf
        cur = new_xml
    report = await validator.validate(cur, mode="Full 1-3", message_type=msg_type or "Auto-detect")
    rd = report.to_dict()
    errors = [d for d in (rd.get("details") or []) if d.get("severity") in ("ERROR", "CRITICAL")]
    wf = rd.get("layer_status", {}).get("1", {}).get("status")
    return cur, max_rounds, [f"{e['code']}: {e['message']}" for e in errors], wf


async def main():
    leaf_tags = ["EndToEndId", "Nm", "BICFI", "Ustrd"]
    for msg_type in MSG_TYPES:
        blocks = get_blocks_for_message(msg_type)
        ids = [b["id"] for b in blocks]
        try:
            clean = generate_single_xml(msg_type, ids, idx=1)
        except Exception as e:
            print(f"{msg_type}: GEN_FAIL {e}")
            continue

        for tag in leaf_tags:
            for kind, fn in (("close", remove_closing_tag), ("open", remove_opening_tag)):
                broken = fn(clean, tag)
                if broken is None:
                    continue
                fixed, rounds, residual, wf = await auto_fix(broken, msg_type)
                status = "OK" if not residual else "RESIDUAL: " + "; ".join(residual[:2])
                print(f"{msg_type}: {tag} {kind} -> rounds={rounds} wf={wf} {status}")


if __name__ == "__main__":
    asyncio.run(main())
