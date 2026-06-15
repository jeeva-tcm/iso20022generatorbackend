"""
Forensics on the BROKEN heavy-damage cases.

For each failing message type: regenerate the SAME damage (stable crc32 seed),
list exactly which tags were ripped (and how), run the auto-fix, then show the
residual error + the broken/fixed XML so we can see what defeated the fixer.

Usage:
    cd iso20022generatorbackend
    .venv/Scripts/python.exe tests/diag_heavy_failures.py
"""
import asyncio
import zlib

import test_autofix_heavy as H
from app.services.bulk_generator import generate_single_xml, get_blocks_for_message

BROKEN = ["pacs.004", "pacs.002", "pacs.010", "camt.052", "pain.002"]


async def diag(msg_type: str):
    blocks = get_blocks_for_message(msg_type)
    clean = generate_single_xml(msg_type, [b["id"] for b in blocks], idx=1)
    broken, stats = H.heavy_damage(clean, seed=zlib.crc32(msg_type.encode()))

    brk_wf, brk_err, _ = await H.snapshot(broken, msg_type)
    fixed, rounds = await H.auto_fix(broken, msg_type)
    fix_wf, fix_err, _ = await H.snapshot(fixed, msg_type)

    print("\n" + "#" * 80)
    print(f"# {msg_type}   removed={stats['removed']} "
          f"({stats['both']} both / {stats['open']} open-only / {stats['close']} close-only)"
          f"   rounds={rounds}")
    print("#" * 80)

    print("\nRemoved tags (element : how):")
    for name, mode in stats["detail"]:
        tag = {"both": "<x>…</x>", "open": "<x>  (open only)", "close": "</x>  (close only)"}[mode]
        print(f"    {name:20s} {tag}")

    print(f"\nBROKEN -> well-formed={brk_wf}  errors={len(brk_err)}")
    print(f"FIXED  -> well-formed={fix_wf}  errors={len(fix_err)}")

    print("\nResidual error(s) blocking repair:")
    for d in fix_err[:5]:
        print(f"    [{d.get('code','?')}] {(d.get('message','') or '')[:140]}")

    # The first well-formedness error usually names the tag the parser choked on.
    print("\n--- BROKEN XML (first 1400 chars) ---")
    print(broken[:1400])
    print("\n--- FIXED XML (first 1400 chars) ---")
    print(fixed[:1400])


async def main():
    for mt in BROKEN:
        await diag(mt)


if __name__ == "__main__":
    asyncio.run(main())
