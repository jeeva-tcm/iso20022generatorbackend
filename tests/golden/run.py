"""
Standalone runner for the golden-test corpus (no pytest needed).

  cd iso20022generatorbackend
  .venv/Scripts/python.exe tests/golden/run.py            # all cases
  .venv/Scripts/python.exe tests/golden/run.py -v         # show fixed XML on fail
  .venv/Scripts/python.exe tests/golden/run.py charset     # filter by substring

Exit code is the number of failing cases (0 = all green), so it slots into CI.
"""
from __future__ import annotations

import os
import sys

# Windows consoles default to cp1252 and choke on box-drawing / ANSI glyphs.
# Force UTF-8 on stdout/stderr where supported; fall back silently otherwise.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
_ASCII = not (sys.stdout.encoding or "").lower().startswith("utf")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.normpath(os.path.join(_HERE, "..", ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from app.services.fix_suggester import FixSuggester  # noqa: E402
from tests.golden.harness import run_case             # noqa: E402
from tests.golden.cases import CASES                   # noqa: E402

# Disable ANSI colour when not a TTY (e.g. piped / captured) to keep logs clean.
_COLOR = sys.stdout.isatty()
GREEN = "\033[92m" if _COLOR else ""
RED = "\033[91m" if _COLOR else ""
DIM = "\033[2m" if _COLOR else ""
RESET = "\033[0m" if _COLOR else ""
_BAR = ("─" if not _ASCII else "-") * 60


def main(argv: list[str]) -> int:
    verbose = "-v" in argv
    filters = [a for a in argv if not a.startswith("-")]
    cases = [c for c in CASES
             if not filters or any(f in c.name for f in filters)]

    fs = FixSuggester()
    passed = 0
    failed = 0
    print(f"\nGolden corpus - {len(cases)} case(s)\n" + _BAR)
    for case in cases:
        res = run_case(fs, case)
        if res.ok:
            passed += 1
            print(f"  {GREEN}PASS{RESET}  {case.name}")
        else:
            failed += 1
            print(f"  {RED}FAIL{RESET}  {case.name}")
            for f in res.failures:
                print(f"          {RED}- {f}{RESET}")
            if verbose and res.fixed_xml:
                print(f"{DIM}--- fixed XML ---\n{res.fixed_xml}\n---{RESET}")

    print(_BAR)
    status = f"{GREEN}all green{RESET}" if not failed else f"{RED}{failed} failing{RESET}"
    print(f"  {passed} passed · {failed} failed — {status}\n")
    return failed


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
