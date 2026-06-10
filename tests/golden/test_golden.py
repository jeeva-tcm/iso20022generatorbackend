"""
Pytest shim — exposes the golden corpus as parametrized tests IF pytest is
installed. The corpus runs fine without pytest via run.py; this just lets it
join an existing pytest run / CI when available.

  pytest tests/golden/test_golden.py
"""
import pytest

from app.services.fix_suggester import FixSuggester
from tests.golden.harness import run_case
from tests.golden.cases import CASES

_FS = FixSuggester()


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_golden_case(case):
    res = run_case(_FS, case)
    assert res.ok, "; ".join(res.failures)
