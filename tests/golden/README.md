# Golden-test corpus — AI Fix Suggester

Regression safety net for `app/services/fix_suggester.py`. Each case is a
`(broken_xml, issue[s]) -> fixed_xml` transition, asserted against a set of
**invariants** that lock in correctness and stop the duplication / lost-fix /
value-corruption bug classes from silently returning.

## Run it

```bash
cd iso20022generatorbackend
.venv/Scripts/python.exe tests/golden/run.py          # all cases
.venv/Scripts/python.exe tests/golden/run.py -v       # print fixed XML on failure
.venv/Scripts/python.exe tests/golden/run.py charset  # filter by name substring
```

Exit code = number of failing cases (0 = green), so it drops into CI directly.
No pytest required. If pytest is installed it also runs via
`pytest tests/golden/test_golden.py`.

## Invariants (asserted on every case)

| # | Invariant | Catches |
|---|-----------|---------|
| 1 | **Well-formed** — fixed XML parses | fixes that emit broken XML |
| 2 | **Applies cleanly** — suggest+apply doesn't raise | crashes on edge inputs |
| 3 | **No cardinality breach** — no element exceeds its XSD `maxOccurs` | the **duplication** bug class (doubled `<To>`, `<TxId>`, …) |
| 4 | **Idempotent** — re-fixing the fixed doc changes nothing | re-trigger loops; **value corruption** on already-correct fields |
| 5 | **Expected counts** — asserted local-name → exact count | missing tag not inserted / inserted twice |
| 6 | **XSD-valid** (opt-in per case) | structurally-incomplete repairs |

## Files

- `harness.py` — invariant runner + XSD cardinality model. No case data.
- `cases.py` — the case table. **Add a case here whenever a bug is fixed.**
- `run.py` — standalone CLI runner.
- `test_golden.py` — pytest shim (optional).

## Bugs this corpus has already caught

- **Datetime value corruption** — `_extract_literal_from_fix` paired the closing
  quote of one literal with the opening quote of the next in prose like
  `Replace 'Z' with '+00:00'`, yielding the filler word `with` as the "value"
  and overwriting a valid datetime. Found by invariant #4 (idempotency).

## Adding a case

```python
from tests.golden.harness import Case

CASES.append(Case(
    name="category/short_descriptive_name",
    xml=<broken xml string>,
    issue={"path": ..., "code": ..., "message": ..., "fix_suggestion": ...},
    # or issues=[...] for a batch case
    expect_counts={"SomeTag": 1},      # exact counts after the fix
    expect_xsd_valid=True,             # opt-in, only if the fix fully repairs
))
```

Use the shipped XSD minor versions in fixture XML (`pacs.008.001.13`,
`pain.001.001.12`, `camt.054.001.13`) so `expect_xsd_valid` cases can validate.
