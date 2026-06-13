"""
SR2026 camt.053 (BankToCustomerStatement) Complex Rules
=========================================================
Implements FormalRules with page/balance-type constraints.

  R8:  PageNumber > 1 → exactly 1 Balance with code OPBD + SubType INTM
  R9:  LastPageIndicator=True → exactly 1 Balance with code CLBD, SubType != INTM (if present)
  R10: Balance code CLAV → max 1 occurrence per statement
  R11: PageNumber = 1 → exactly 1 Balance with code OPBD, SubType != INTM (if present)
  R12: LastPageIndicator=False → exactly 1 Balance with code CLBD + SubType INTM
  R13: PageNumber must be > 0
"""

from lxml import etree
from app.sr2026.validation.validators.models import ValidationIssue, ValidationReport


def _t(node) -> str:
    return (node.text or "").strip() if node is not None else ""


def _add(report: ValidationReport, severity: str, code: str, path: str,
         message: str, fix: str, line: int = 1):
    report.add_issue(ValidationIssue(
        severity=severity, code=code, layer=3,
        path=path, message=message, fix=fix, line=line or 1,
    ))


def _balance_codes_subtypes(stmt: etree._Element) -> list:
    """Return list of (code, subtype_code) tuples for all Balance elements."""
    results = []
    for bal in stmt.xpath("*[local-name()='Bal']"):
        cd_nodes = bal.xpath("*[local-name()='Tp']/*[local-name()='CdOrPrtry']/*[local-name()='Cd']")
        sub_nodes = bal.xpath("*[local-name()='Tp']/*[local-name()='SubTp']/*[local-name()='Cd']")
        cd = _t(cd_nodes[0]) if cd_nodes else ""
        sub = _t(sub_nodes[0]) if sub_nodes else ""
        results.append((cd, sub, bal.sourceline or 1))
    return results


class CamtStatementValidator:
    """camt.053 page/balance complex rule validation."""

    @classmethod
    def validate(cls, root: etree._Element, report: ValidationReport, message_type: str):
        if "camt.053" not in (message_type or "").lower():
            return
        for stmt in root.xpath("//*[local-name()='Stmt']"):
            cls._validate_stmt(stmt, report)

    @classmethod
    def _validate_stmt(cls, stmt: etree._Element, report: ValidationReport):
        src = stmt.sourceline or 1
        pgntn = stmt.xpath("*[local-name()='StmtPgntn']")
        if not pgntn:
            return
        pg = pgntn[0]

        pg_nb_nodes = pg.xpath("*[local-name()='PgNb']")
        last_pg_nodes = pg.xpath("*[local-name()='LastPgInd']")
        pg_nb = _t(pg_nb_nodes[0]) if pg_nb_nodes else ""
        last_pg = _t(last_pg_nodes[0]).lower() if last_pg_nodes else ""

        balances = _balance_codes_subtypes(stmt)

        # R13: PageNumber > 0
        ZERO_VALS = {"0", "00", "000", "0000", "00000"}
        if pg_nb in ZERO_VALS:
            _add(report, "ERROR", "INVALID_PAGE_NUMBER",
                 "//Stmt/StmtPgntn/PgNb",
                 f"PageNumber '{pg_nb}' is not allowed. Must be > 0 "
                 "(CBPR_PageNumber_FormalRule R13).",
                 "Set PageNumber to a positive integer (e.g. 1).",
                 pg_nb_nodes[0].sourceline if pg_nb_nodes else src)

        # R10: CLAV balance → max 1 occurrence
        clav_count = sum(1 for cd, _, _ in balances if cd == "CLAV")
        if clav_count > 1:
            _add(report, "ERROR", "MULTIPLE_CLAV_BALANCE",
                 "//Stmt/Bal/Tp/CdOrPrtry/Cd",
                 f"Balance code 'CLAV' appears {clav_count} times. "
                 "Only one occurrence is allowed per statement "
                 "(CBPR_BalanceTypeCode_FormalRule R10).",
                 "Remove duplicate CLAV balance entries.",
                 src)

        try:
            pg_nb_int = int(pg_nb)
        except (ValueError, TypeError):
            pg_nb_int = None

        # R8: PageNumber > 1 → exactly 1 Balance OPBD with SubType INTM
        if pg_nb_int is not None and pg_nb_int > 1:
            opbd_intm = [(cd, sub, ln) for cd, sub, ln in balances
                         if cd == "OPBD" and sub == "INTM"]
            if len(opbd_intm) != 1:
                _add(report, "ERROR", "MISSING_OPBD_INTM_BALANCE",
                     "//Stmt/Bal",
                     f"PageNumber is {pg_nb_int} (> 1) but there is not exactly one Balance "
                     "with code 'OPBD' and SubType 'INTM'. "
                     "Interim opening balance is required for continuation pages "
                     "(CBPR_Page_Number_2_FormalRule R8).",
                     "Add a Balance block: <Bal><Tp><CdOrPrtry><Cd>OPBD</Cd></CdOrPrtry>"
                     "<SubTp><Cd>INTM</Cd></SubTp></Tp>...</Bal>",
                     src)

        # R11: PageNumber = 1 → exactly 1 Balance OPBD, SubType != INTM (if SubType present)
        if pg_nb_int == 1:
            opbd = [(cd, sub, ln) for cd, sub, ln in balances if cd == "OPBD"]
            invalid_opbd = [x for x in opbd if x[1] == "INTM"]
            if len(opbd) != 1:
                _add(report, "ERROR", "MISSING_OPBD_BALANCE",
                     "//Stmt/Bal",
                     "PageNumber is 1 but there is not exactly one Balance with code 'OPBD'. "
                     "Opening balance is required for the first page "
                     "(CBPR_Page_Number_1_FormalRule R11).",
                     "Add a Balance block with code OPBD for the opening balance.",
                     src)
            elif invalid_opbd:
                _add(report, "ERROR", "OPBD_INVALID_SUBTYPE",
                     "//Stmt/Bal/Tp/SubTp/Cd",
                     "PageNumber is 1 and Balance OPBD has SubType 'INTM'. "
                     "On page 1, SubType for OPBD must not be INTM "
                     "(CBPR_Page_Number_1_FormalRule R11).",
                     "Remove SubType INTM from the OPBD balance on the first page.",
                     invalid_opbd[0][2])

        # R9: LastPageIndicator=True → exactly 1 Balance CLBD, SubType != INTM
        if last_pg in ("1", "true"):
            clbd = [(cd, sub, ln) for cd, sub, ln in balances if cd == "CLBD"]
            intm_clbd = [x for x in clbd if x[1] == "INTM"]
            if len(clbd) != 1:
                _add(report, "ERROR", "MISSING_CLBD_BALANCE",
                     "//Stmt/Bal",
                     "LastPageIndicator is 'True' but there is not exactly one Balance "
                     "with code 'CLBD'. Closing balance is required on the last page "
                     "(CBPR_Last_Page_Indicator_1_FormalRule R9).",
                     "Add a Balance block with code CLBD for the closing balance.",
                     src)
            elif intm_clbd:
                _add(report, "ERROR", "CLBD_INVALID_SUBTYPE",
                     "//Stmt/Bal/Tp/SubTp/Cd",
                     "LastPageIndicator is 'True' and Balance CLBD has SubType 'INTM'. "
                     "On the last page, SubType for CLBD must not be INTM "
                     "(CBPR_Last_Page_Indicator_1_FormalRule R9).",
                     "Remove SubType INTM from the CLBD balance on the last page.",
                     intm_clbd[0][2])

        # R12: LastPageIndicator=False → exactly 1 Balance CLBD with SubType INTM
        if last_pg in ("0", "false"):
            clbd_intm = [(cd, sub, ln) for cd, sub, ln in balances
                         if cd == "CLBD" and sub == "INTM"]
            if len(clbd_intm) != 1:
                _add(report, "ERROR", "MISSING_CLBD_INTM_BALANCE",
                     "//Stmt/Bal",
                     "LastPageIndicator is 'False' but there is not exactly one Balance "
                     "with code 'CLBD' and SubType 'INTM'. "
                     "Interim closing balance is required for continuation pages "
                     "(CBPR_Last_Page_Indicator_2_FormalRule R12).",
                     "Add a Balance block: <Bal><Tp><CdOrPrtry><Cd>CLBD</Cd></CdOrPrtry>"
                     "<SubTp><Cd>INTM</Cd></SubTp></Tp>...</Bal>",
                     src)
