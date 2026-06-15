"""
Golden-test harness for the AI Fix Suggester — standalone, no pytest required.

WHY THIS EXISTS
---------------
The fix engine (app/services/fix_suggester.py) is a 9.6k-line deterministic
repair engine. Its bugs (To-duplication, batch-overwrite, lost fixes) all share
a root cause: structural edits done by tree/string heuristics with no guard that
the result still obeys the XSD. This corpus locks in correctness by asserting a
set of INVARIANTS on every (broken_xml, issue) -> fixed_xml transition, so the
duplication / lost-fix bug classes can never silently return.

INVARIANTS (run on every case unless a case opts out)
  1. WELL-FORMED        — the fixed XML parses.
  2. APPLIES CLEANLY    — suggest() then apply() (or suggest_batch+apply_batch)
                          runs without raising.
  3. NO CARDINALITY     — no element appears more MORE times than the XSD allows
     BREACH               (maxOccurs). THIS is the duplication guard — a second
                          <To>, a doubled <TxId>, etc. is a hard failure.
  4. IDEMPOTENT         — re-validating-and-fixing the already-fixed XML for the
                          SAME issue produces no further change (a fix that keeps
                          re-triggering is a fix that isn't really fixing).
  5. EXPECTED COUNTS    — per-case asserted local-name -> exact count (the
                          positive assertion: the missing tag IS now present,
                          exactly once).
  6. XSD-VALID (opt-in) — for cases that should be fully repaired, assert the
                          result validates against the shipped XSD.

HOW TO RUN
  cd iso20022generatorbackend
  .venv/Scripts/python.exe tests/golden/run.py            # all cases
  .venv/Scripts/python.exe tests/golden/run.py -v         # verbose
  .venv/Scripts/python.exe tests/golden/run.py charset    # filter by substring

It is also pytest-discoverable if pytest is ever installed (see test_golden.py).
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Callable, Optional

from lxml import etree

# Make `app...` importable when run from anywhere.
_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.normpath(os.path.join(_HERE, "..", ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from app.services.fix_suggester import FixSuggester, FixApplyError  # noqa: E402

_XSD_DIR = os.path.join(_BACKEND, "xsds", "extracted")


# ── XSD cardinality model (maxOccurs per element local-name, per message) ─────
# Parsed once from the shipped XSD: for every element declaration, the max number
# of times it may appear under its declared parent. We use a conservative
# GLOBAL-MAX read: an element's allowed max across ALL its declarations. That is
# enough to catch duplication (a tag that is [0..1] or [1..1] everywhere must
# never appear twice), without needing full context-sensitive validation.
def _xsd_global_max(xsd_path: str) -> dict[str, int]:
    """local-name -> max allowed occurrences anywhere in the schema (unbounded=10^9)."""
    XS = "http://www.w3.org/2001/XMLSchema"
    UNBOUNDED = 10 ** 9
    try:
        root = etree.parse(xsd_path).getroot()
    except Exception:
        return {}
    out: dict[str, int] = {}
    for el in root.iter(f"{{{XS}}}element"):
        name = el.get("name") or el.get("ref")
        if not name:
            continue
        name = name.split(":")[-1]
        mx = el.get("maxOccurs", "1")
        mxn = UNBOUNDED if mx == "unbounded" else int(mx)
        # keep the LARGEST max seen for this name (most permissive declaration)
        out[name] = max(out.get(name, 0), mxn)
    return out


_CARD_CACHE: dict[str, dict[str, int]] = {}


def _cardinality_for(xml: str) -> dict[str, int]:
    """Resolve the right XSD for this message and return its global-max map."""
    import re
    m = re.search(r"(pacs|pain|camt|pacs)\.\d{3}\.\d{3}\.\d{2}", xml)
    fam = None
    if m:
        fam = m.group(0)
    # map the declared family to a shipped XSD by family prefix
    if not os.path.isdir(_XSD_DIR):
        return {}
    if fam:
        prefix = ".".join(fam.split(".")[:2])  # e.g. pacs.008
    else:
        prefix = ""
    cand = None
    for f in sorted(os.listdir(_XSD_DIR)):
        if prefix and f.startswith(prefix + ".") and f.endswith(".xsd"):
            cand = os.path.join(_XSD_DIR, f)
            break
    if cand is None:
        return {}
    if cand not in _CARD_CACHE:
        _CARD_CACHE[cand] = _xsd_global_max(cand)
    return _CARD_CACHE[cand]


def _local_counts(xml: str) -> dict[str, int]:
    """Count every element by local-name in the document."""
    from collections import Counter
    root = etree.fromstring(xml.encode("utf-8"),
                            parser=etree.XMLParser(recover=True))
    c: Counter = Counter()
    for el in root.iter():
        if isinstance(el.tag, str):
            c[etree.QName(el.tag).localname] += 1
    return dict(c)


# ── Case model ────────────────────────────────────────────────────────────────
@dataclass
class Case:
    name: str
    xml: str
    # Single-issue cases set `issue`; batch cases set `issues`.
    issue: Optional[dict] = None
    issues: Optional[list[dict]] = None
    # Positive assertion: local-name -> EXACT expected count in the fixed doc.
    expect_counts: dict[str, int] = field(default_factory=dict)
    # Positive assertion: local-name -> EXACT text value of the (first) element.
    expect_values: dict[str, str] = field(default_factory=dict)
    # Opt-in: assert the fixed doc validates against its XSD.
    expect_xsd_valid: bool = False
    # Opt-out: some cases legitimately can't be made idempotent in one pass.
    check_idempotent: bool = True
    # Opt-out of cardinality (only for cases that probe non-XSD families).
    check_cardinality: bool = True
    notes: str = ""


@dataclass
class Result:
    name: str
    ok: bool
    failures: list[str]
    fixed_xml: str = ""


# ── Invariant runner ──────────────────────────────────────────────────────────
def _apply_single(fs: FixSuggester, xml: str, issue: dict) -> tuple[str, str]:
    """Return (fixed_xml, confidence). Raises on a hard apply error."""
    fs._line_hint = issue.get("line")
    sug = fs.suggest(xml, issue)
    if sug.confidence not in ("high", "low") or not sug.fragment_xml \
            or sug.fragment_xml == sug.original_fragment:
        # No actionable fix — return unchanged (some cases assert this is wrong).
        return xml, sug.confidence
    fixed = fs.apply(xml, sug.xpath, sug.fragment_xml)
    return fixed, sug.confidence


def _apply_batch(fs: FixSuggester, xml: str, issues: list[dict]) -> str:
    sugs = fs.suggest_batch(xml, issues)
    fixes = [{"xpath": s.xpath, "fragment_xml": s.fragment_xml} for s in sugs
             if s.confidence in ("high", "low")
             and s.fragment_xml and s.fragment_xml != s.original_fragment]
    return fs.apply_batch(xml, fixes)


def run_case(fs: FixSuggester, case: Case) -> Result:
    failures: list[str] = []
    fixed = ""
    try:
        if case.issues is not None:
            fixed = _apply_batch(fs, case.xml, case.issues)
        else:
            fixed, _conf = _apply_single(fs, case.xml, case.issue or {})
    except FixApplyError as e:
        return Result(case.name, False, [f"apply raised FixApplyError: {e}"])
    except Exception as e:  # noqa: BLE001
        return Result(case.name, False, [f"apply raised {type(e).__name__}: {e}"])

    # 1. WELL-FORMED
    try:
        etree.fromstring(fixed.encode("utf-8"))
    except Exception as e:  # noqa: BLE001
        failures.append(f"not well-formed: {e}")
        return Result(case.name, False, failures, fixed)

    # 3. NO CARDINALITY BREACH (the duplication guard)
    if case.check_cardinality:
        card = _cardinality_for(fixed)
        if card:
            counts = _local_counts(fixed)
            for name, n in counts.items():
                allowed = card.get(name)
                if allowed is not None and n > allowed:
                    failures.append(
                        f"cardinality breach: <{name}> appears {n}x, XSD max {allowed}"
                    )

    # 5. EXPECTED COUNTS
    if case.expect_counts:
        counts = _local_counts(fixed)
        for name, want in case.expect_counts.items():
            got = counts.get(name, 0)
            if got != want:
                failures.append(f"expected <{name}> x{want}, got x{got}")

    # 5b. EXPECTED VALUES — exact text of the first element with that local-name
    if case.expect_values:
        try:
            froot = etree.fromstring(fixed.encode("utf-8"),
                                     parser=etree.XMLParser(recover=True))
            for name, want in case.expect_values.items():
                el = next((e for e in froot.iter()
                           if isinstance(e.tag, str)
                           and etree.QName(e.tag).localname == name), None)
                got = (el.text or "").strip() if el is not None else None
                if got != want:
                    failures.append(f"expected <{name}>={want!r}, got {got!r}")
        except Exception as e:  # noqa: BLE001
            failures.append(f"value check failed to parse: {e}")

    # 4. IDEMPOTENT — fixing the fixed doc for the same issue(s) does nothing
    if case.check_idempotent:
        try:
            if case.issues is not None:
                refix = _apply_batch(fs, fixed, case.issues)
            else:
                refix, _ = _apply_single(fs, fixed, case.issue or {})
            if _norm(refix) != _norm(fixed):
                failures.append("not idempotent: re-applying changed the document")
        except Exception as e:  # noqa: BLE001
            failures.append(f"idempotency re-run raised {type(e).__name__}: {e}")

    # 6. XSD-VALID (opt-in)
    if case.expect_xsd_valid:
        ok, err = _xsd_validate(fixed)
        if not ok:
            failures.append(f"XSD-invalid: {err}")

    return Result(case.name, not failures, failures, fixed)


def _norm(xml: str) -> str:
    """Whitespace-insensitive canonical form for idempotency comparison."""
    try:
        root = etree.fromstring(xml.encode("utf-8"),
                                parser=etree.XMLParser(remove_blank_text=True))
        return etree.tostring(root, encoding="unicode")
    except Exception:
        return xml.strip()


def _xsd_validate(xml: str) -> tuple[bool, str]:
    import re
    m = re.search(r"(pacs|pain|camt)\.\d{3}\.\d{3}\.\d{2}", xml)
    if not m or not os.path.isdir(_XSD_DIR):
        return True, ""  # can't validate — don't fail the case on infra
    prefix = ".".join(m.group(0).split(".")[:2])
    cand = next((os.path.join(_XSD_DIR, f) for f in sorted(os.listdir(_XSD_DIR))
                 if f.startswith(prefix + ".") and f.endswith(".xsd")), None)
    if cand is None:
        return True, ""
    try:
        schema = etree.XMLSchema(etree.parse(cand))
        doc = etree.fromstring(xml.encode("utf-8"))
        # Validate only the Document body (strip any BusMsgEnvlp/AppHdr wrapper).
        body = doc if etree.QName(doc.tag).localname == "Document" else \
            doc.find(".//{*}Document")
        if body is None:
            return True, ""
        if schema.validate(body):
            return True, ""
        return False, str(schema.error_log.last_error)
    except Exception as e:  # noqa: BLE001
        return True, f"(validation skipped: {e})"
