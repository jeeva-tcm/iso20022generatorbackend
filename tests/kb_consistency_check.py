"""
KB consistency checker — audits the per-message validation KBs against each
other and against the fixer's own canonical functions, to catch the kind of
drift that makes the auto-fix loop oscillate forever (KB says X, validator
demands Y → fixer writes X → validator re-flags → repeat).

No pytest needed:

  cd iso20022generatorbackend
  .venv/Scripts/python.exe tests/kb_consistency_check.py            # all checks
  .venv/Scripts/python.exe tests/kb_consistency_check.py -v         # show INFO too
  .venv/Scripts/python.exe tests/kb_consistency_check.py bizsvc     # filter findings

Exit code = number of ERROR findings (0 = clean), so it slots into CI next to the
golden corpus.

Checks
  E1  expected_value not in its own documented valid_values
  E2  expected_value violates its declared datatype (Date/DateTime/Decimal/UUID
      shape, or Max<N>Text length)
  E3  pacs.009 BizSvc expected_value != the fixer's canonical _cbpr_bizsvc_value
      (the value the validator enforces; mismatch = guaranteed oscillation)
  E4  MsgDefIdr expected_value family/version does not match the KB's own message
  W1  a leaf documented in several families with DISAGREEING expected_values
      (excluding tags that are legitimately message-specific)
"""
from __future__ import annotations

import json
import os
import re
import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.normpath(os.path.join(_HERE, ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from app.services.fix_suggester import _cbpr_bizsvc_value  # noqa: E402

_KB_DIR = os.path.normpath(os.path.join(_BACKEND, "app", "resources", "KB"))
_COMMON = {"ai_knowledge_base.json", "swift_mx_enterprise_llm_kb.json"}

_COLOR = sys.stdout.isatty()
RED = "\033[91m" if _COLOR else ""
YEL = "\033[93m" if _COLOR else ""
DIM = "\033[2m" if _COLOR else ""
GREEN = "\033[92m" if _COLOR else ""
RESET = "\033[0m" if _COLOR else ""

# Tags whose value is SUPPOSED to differ between message families — never flag
# these for cross-family disagreement.
_VARIANT_TAGS = {"BizSvc", "MsgDefIdr", "MsgId", "BizMsgIdr", "CreDt", "CreDtTm"}


def _family_token(filename: str) -> str:
    """'camt057_cbprplus_…json' → 'camt.057'; 'pacs009_cov_…' → 'pacs.009'."""
    m = re.match(r"(camt|pacs|pain|sese|reda|acmt)(\d{2,3})", filename)
    return f"{m.group(1)}.{m.group(2)}" if m else ""


def _msg_type_for(filename: str) -> str:
    """A representative msg_type string for _cbpr_bizsvc_value (carries variant)."""
    fam = _family_token(filename)
    if "cov" in filename:
        return f"{fam}.001.08_COV"
    if "adv" in filename:
        return f"{fam}.001.08_ADV"
    return f"{fam}.001.08"


def _violates_datatype(value: str, datatype: str) -> str:
    """Return a reason string when `value` does not fit `datatype`, else ''."""
    dt = (datatype or "").lower()
    if not value:
        return ""
    if "datetime" in dt:
        if not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", value):
            return "not an ISO DateTime"
    elif "date" in dt:
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", value):
            return "not an ISO Date"
    elif "decimal" in dt or "amount" in dt:
        if not re.match(r"^-?\d+(\.\d+)?$", value):
            return "not a decimal"
    elif "uuid" in dt:
        if not re.match(r"^[0-9a-fA-F-]{36}$", value):
            return "not a UUID"
    else:
        m = re.search(r"max(\d+)text", dt)
        if m and len(value) > int(m.group(1)):
            return f"exceeds Max{m.group(1)}Text ({len(value)} chars)"
    return ""


def _load_tags(path: str) -> list:
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception as e:
        print(f"{RED}  cannot load {os.path.basename(path)}: {e}{RESET}")
        return []
    tags = data.get("tags", [])
    return tags if isinstance(tags, list) else list(tags.values())


def main(argv: list[str]) -> int:
    verbose = "-v" in argv
    filters = [a.lower() for a in argv if not a.startswith("-")]

    errors: list[str] = []
    warns: list[str] = []
    infos: list[str] = []

    # leaf → {expected_value → set(families)}  for the cross-family check
    expected_by_leaf: dict[str, dict[str, set]] = {}

    files = sorted(f for f in os.listdir(_KB_DIR)
                   if f.endswith("_validation_kb.json")
                   and f not in _COMMON and "syntactic" not in f)

    for fn in files:
        fam = _family_token(fn)
        msg_type = _msg_type_for(fn)
        for t in _load_tags(os.path.join(_KB_DIR, fn)):
            if not isinstance(t, dict):
                continue
            leaf = t.get("xml_element") or (t.get("tag", "").split("/")[-1])
            ev = t.get("expected_value")
            vv = t.get("valid_values")
            dt = t.get("datatype")

            # E1 — expected_value must be in its own valid_values
            if ev and isinstance(vv, list) and vv and ev not in [str(x) for x in vv]:
                errors.append(f"E1 [{fn}] <{leaf}> expected_value '{ev}' "
                              f"not in valid_values {vv}")

            # E2 — expected_value must fit its datatype
            if ev and dt:
                why = _violates_datatype(str(ev), str(dt))
                if why:
                    errors.append(f"E2 [{fn}] <{leaf}> expected_value '{ev}' "
                                  f"{why} (datatype={dt})")

            # E3 — pacs.009 BizSvc against the fixer's canonical
            if leaf == "BizSvc" and ev and fam == "pacs.009":
                canon = _cbpr_bizsvc_value(msg_type, "")
                if canon and str(ev) != canon:
                    errors.append(f"E3 [{fn}] BizSvc expected_value '{ev}' != "
                                  f"canonical '{canon}' (oscillation risk)")

            # E4 — MsgDefIdr must name this message family
            if leaf == "MsgDefIdr" and ev and fam and not str(ev).startswith(fam):
                errors.append(f"E4 [{fn}] MsgDefIdr expected_value '{ev}' does "
                              f"not match message family '{fam}'")

            if ev and leaf and leaf not in _VARIANT_TAGS:
                expected_by_leaf.setdefault(leaf, {}).setdefault(str(ev), set()).add(fam)

    # W1 — same leaf, disagreeing expected_values across families
    for leaf, by_val in expected_by_leaf.items():
        if len(by_val) > 1:
            detail = "; ".join(f"'{v}'→{sorted(fams)}" for v, fams in by_val.items())
            warns.append(f"W1 <{leaf}> disagreeing expected_value across families: {detail}")

    def _show(label: str, color: str, items: list) -> None:
        if filters:
            items = [x for x in items if any(f in x.lower() for f in filters)]
        if not items:
            return
        print(f"\n{color}{label} ({len(items)}){RESET}")
        for x in items:
            print(f"  {color}- {x}{RESET}")

    print(f"\nKB consistency — {len(files)} message KB file(s)")
    _show("ERRORS", RED, errors)
    _show("WARNINGS", YEL, warns)
    if verbose:
        _show("INFO", DIM, infos)

    shown_err = len([e for e in errors
                     if not filters or any(f in e.lower() for f in filters)])
    print("\n" + ("─" * 60 if _COLOR else "-" * 60))
    if shown_err == 0:
        print(f"  {GREEN}no consistency errors{RESET} "
              f"({len(warns)} warning(s))\n")
    else:
        print(f"  {RED}{shown_err} error(s){RESET}, {len(warns)} warning(s)\n")
    return shown_err


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
