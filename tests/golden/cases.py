"""
Golden-test case table for the AI Fix Suggester.

Each Case is a (broken_xml, issue[s], expectations) tuple. The harness asserts
the invariants (well-formed, no cardinality breach, idempotent, expected counts,
optional XSD-valid). Add a case here whenever a new bug is fixed — that is what
stops the bug class from silently returning.

Conventions
  • XML uses the SHIPPED xsd minor versions (pacs.008.001.13, pain.001.001.12,
    camt.054.001.13) so expect_xsd_valid cases can validate.
  • Issue dicts use the keys suggest() reads: path, code, message, fix_suggestion, line.
  • Counts in expect_counts are EXACT (the anti-duplication positive assertion).
"""
from tests.golden.harness import Case

PACS008_NS = "urn:iso:std:iso:20022:tech:xsd:pacs.008.001.13"
HEAD_NS = "urn:iso:std:iso:20022:tech:xsd:head.001.001.02"


# A minimal-but-valid pacs.008 transaction body. Kept small on purpose; cases
# delete/break one thing at a time from a known-good baseline.
def _pacs008(cdttrf_inner: str) -> str:
    return (
        f'<Document xmlns="{PACS008_NS}">\n'
        f'  <FIToFICstmrCdtTrf>\n'
        f'    <GrpHdr>\n'
        f'      <MsgId>MSG-20260610-001</MsgId>\n'
        f'      <CreDtTm>2026-06-10T10:00:00+00:00</CreDtTm>\n'
        f'      <NbOfTxs>1</NbOfTxs>\n'
        f'      <SttlmInf><SttlmMtd>INGA</SttlmMtd></SttlmInf>\n'
        f'    </GrpHdr>\n'
        f'    <CdtTrfTxInf>\n{cdttrf_inner}\n'
        f'    </CdtTrfTxInf>\n'
        f'  </FIToFICstmrCdtTrf>\n'
        f'</Document>'
    )


# A BusMsgEnvlp + AppHdr wrapper (for the AppHdr Fr/To duplication cases).
def _envlp(apphdr_inner: str) -> str:
    return (
        f'<BusMsgEnvlp>\n'
        f'  <AppHdr xmlns="{HEAD_NS}">\n{apphdr_inner}\n'
        f'  </AppHdr>\n'
        f'</BusMsgEnvlp>'
    )


CASES: list[Case] = []

# ─────────────────────────────────────────────────────────────────────────────
# 1. MISSING TAG — single missing mandatory child of PmtId (the TxId class).
#    Regression guard: fix inserts TxId exactly once, never duplicates EndToEndId.
# ─────────────────────────────────────────────────────────────────────────────
CASES.append(Case(
    name="missing_tag/pmtid_txid_single",
    xml=_pacs008(
        '      <PmtId><EndToEndId>E2E-1</EndToEndId></PmtId>\n'
        '      <IntrBkSttlmAmt Ccy="USD">100.00</IntrBkSttlmAmt>'
    ),
    issue={
        "path": "/Document/FIToFICstmrCdtTrf/CdtTrfTxInf/PmtId/TxId",
        "code": "HEADER_VAL",
        "message": "content of element 'PmtId' is not complete. One of the "
                   "following elements is expected: 'TxId'",
        "fix_suggestion": "",
    },
    expect_counts={"TxId": 1, "EndToEndId": 1, "PmtId": 1},
    notes="Single missing child — must appear once; sibling not duplicated.",
))

# ─────────────────────────────────────────────────────────────────────────────
# 2. BATCH OVERLAP (ancestor + descendant) — the lost-fix / duplicate class.
#    Two issues: one targets CdtTrfTxInf (ancestor), one targets PmtId (child).
#    Regression guard: BOTH land exactly once; neither wipes nor doubles.
# ─────────────────────────────────────────────────────────────────────────────
CASES.append(Case(
    name="batch_overlap/ancestor_first_chrgbr_then_txid",
    xml=_pacs008(
        '      <PmtId><EndToEndId>E2E-1</EndToEndId></PmtId>\n'
        '      <IntrBkSttlmAmt Ccy="USD">100.00</IntrBkSttlmAmt>'
    ),
    issues=[
        {"path": "/Document/FIToFICstmrCdtTrf/CdtTrfTxInf/ChrgBr",
         "code": "HEADER_VAL",
         "message": "content of element 'CdtTrfTxInf' is not complete. One of "
                    "the following elements is expected: 'ChrgBr'",
         "fix_suggestion": ""},
        {"path": "/Document/FIToFICstmrCdtTrf/CdtTrfTxInf/PmtId/TxId",
         "code": "HEADER_VAL",
         "message": "content of element 'PmtId' is not complete. One of the "
                    "following elements is expected: 'TxId'",
         "fix_suggestion": ""},
    ],
    expect_counts={"TxId": 1, "ChrgBr": 1, "EndToEndId": 1, "PmtId": 1},
    notes="Ancestor suggested first; descendant fix must survive apply_batch.",
))

CASES.append(Case(
    name="batch_overlap/descendant_first_txid_then_chrgbr",
    xml=_pacs008(
        '      <PmtId><EndToEndId>E2E-1</EndToEndId></PmtId>\n'
        '      <IntrBkSttlmAmt Ccy="USD">100.00</IntrBkSttlmAmt>'
    ),
    issues=[
        {"path": "/Document/FIToFICstmrCdtTrf/CdtTrfTxInf/PmtId/TxId",
         "code": "HEADER_VAL",
         "message": "content of element 'PmtId' is not complete. One of the "
                    "following elements is expected: 'TxId'",
         "fix_suggestion": ""},
        {"path": "/Document/FIToFICstmrCdtTrf/CdtTrfTxInf/ChrgBr",
         "code": "HEADER_VAL",
         "message": "content of element 'CdtTrfTxInf' is not complete. One of "
                    "the following elements is expected: 'ChrgBr'",
         "fix_suggestion": ""},
    ],
    expect_counts={"TxId": 1, "ChrgBr": 1, "EndToEndId": 1, "PmtId": 1},
    notes="Reverse order of the above — order must not change the outcome.",
))

# ─────────────────────────────────────────────────────────────────────────────
# 3. BATCH same-parent, two children — both must land once.
# ─────────────────────────────────────────────────────────────────────────────
CASES.append(Case(
    name="batch_overlap/same_parent_instrid_and_txid",
    xml=_pacs008(
        '      <PmtId><EndToEndId>E2E-1</EndToEndId></PmtId>\n'
        '      <IntrBkSttlmAmt Ccy="USD">100.00</IntrBkSttlmAmt>'
    ),
    issues=[
        {"path": "/Document/FIToFICstmrCdtTrf/CdtTrfTxInf/PmtId/InstrId",
         "code": "HEADER_VAL",
         "message": "content of element 'PmtId' is not complete. One of the "
                    "following elements is expected: 'InstrId'",
         "fix_suggestion": ""},
        {"path": "/Document/FIToFICstmrCdtTrf/CdtTrfTxInf/PmtId/TxId",
         "code": "HEADER_VAL",
         "message": "content of element 'PmtId' is not complete. One of the "
                    "following elements is expected: 'TxId'",
         "fix_suggestion": ""},
    ],
    expect_counts={"InstrId": 1, "TxId": 1, "EndToEndId": 1, "PmtId": 1},
))

# ─────────────────────────────────────────────────────────────────────────────
# 4. CHARSET — forbidden special chars in a free-text Nm leaf must be stripped.
#    pacs KB charset additions guard. Result keeps exactly one <Nm>.
# ─────────────────────────────────────────────────────────────────────────────
CASES.append(Case(
    name="charset/dbtr_nm_special_chars_stripped",
    xml=_pacs008(
        '      <PmtId><EndToEndId>E2E-1</EndToEndId><TxId>TX-1</TxId>'
        '<UETR>4a1a0945-5772-409a-83ba-240e666e0267</UETR></PmtId>\n'
        '      <IntrBkSttlmAmt Ccy="USD">100.00</IntrBkSttlmAmt>\n'
        '      <Dbtr><Nm>Acme @#$ Corp!</Nm></Dbtr>'
    ),
    issue={
        "path": "/Document/FIToFICstmrCdtTrf/CdtTrfTxInf/Dbtr/Nm",
        "code": "INVALID_CHARSETS",
        "message": "Dbtr/Nm contains invalid characters.",
        "fix_suggestion": "",
    },
    expect_counts={"Nm": 1, "Dbtr": 1},
    check_idempotent=True,
    notes="@#$! must be removed; Nm stays single and non-empty.",
))

# ─────────────────────────────────────────────────────────────────────────────
# 5. DATETIME — Z suffix forbidden under CBPR+. Single CreDtTm, fixed in place.
# ─────────────────────────────────────────────────────────────────────────────
CASES.append(Case(
    name="datetime/credttm_z_suffix",
    xml=_pacs008(
        '      <PmtId><EndToEndId>E2E-1</EndToEndId><TxId>TX-1</TxId></PmtId>\n'
        '      <IntrBkSttlmAmt Ccy="USD">100.00</IntrBkSttlmAmt>'
    ).replace("2026-06-10T10:00:00+00:00", "2026-06-10T10:00:00Z"),
    issue={
        "path": "/Document/FIToFICstmrCdtTrf/GrpHdr/CreDtTm",
        "code": "CBPR_DATETIME_NO_Z",
        "message": "CreDtTm uses 'Z' suffix; forbidden under CBPR+.",
        "fix_suggestion": "Replace 'Z' with '+00:00'.",
    },
    expect_counts={"CreDtTm": 1},
    notes="Value-fix in place; element count unchanged.",
))

# ─────────────────────────────────────────────────────────────────────────────
# 6. APPHDR DEDUP — the original To-duplication bug. <To> nested inside Fr/FIId
#    plus a real <To> at AppHdr level. After normalize there must be exactly one
#    Fr and one To. (No XSD validation — partial header fragment.)
# ─────────────────────────────────────────────────────────────────────────────
CASES.append(Case(
    name="apphdr_dedup/to_nested_in_fr_fiid",
    xml=_envlp(
        '    <Fr>\n'
        '      <FIId>\n'
        '        <FinInstnId><BICFI>SOGESESSXXX</BICFI></FinInstnId>\n'
        '        <To><FIId><FinInstnId><BICFI>DEUTDEFFXXX</BICFI></FinInstnId></FIId></To>\n'
        '        <BizMsgIdr>MSG-1</BizMsgIdr>\n'
        '        <MsgDefIdr>pacs.008.001.08</MsgDefIdr>\n'
        '        <BizSvc>swift.cbprplus.02</BizSvc>\n'
        '        <CreDt>2026-06-10T10:00:00+00:00</CreDt>\n'
        '      </FIId>\n'
        '    </Fr>\n'
        '    <To><FIId><FinInstnId><BICFI>DEUTSESSXXX</BICFI></FinInstnId></FIId></To>\n'
        '    <BizMsgIdr>MSG-1</BizMsgIdr>\n'
        '    <MsgDefIdr>pacs.008.001.08</MsgDefIdr>\n'
        '    <CreDt>2026-06-10T10:00:00+00:00</CreDt>'
    ),
    issue={
        "path": "/AppHdr",
        "code": "SCHEMA_VAL",
        "message": "AppHdr structure is malformed",
        "fix_suggestion": "",
    },
    expect_counts={"Fr": 1, "To": 1},
    check_cardinality=False,  # head.001 fragment — no message XSD to map
    check_idempotent=True,
    notes="The To-duplication regression: To must collapse to exactly one.",
))

# ─────────────────────────────────────────────────────────────────────────────
# 7. BIZSVC VERSION — camt.057 requires 'swift.cbprplus.03' (validator CBPR_R8,
#    ERROR). The KB once declared '.02', so the fixer produced '.02' and the
#    auto-fix loop oscillated forever. Guard: a wrong '.02' is corrected to '.03'
#    and the corrected value is IDEMPOTENT (re-fixing '.03' is a no-op).
# ─────────────────────────────────────────────────────────────────────────────
CASES.append(Case(
    name="bizsvc/camt057_must_be_cbprplus_03",
    xml=(
        '<BusMsgEnvlp>\n'
        f'  <AppHdr xmlns="{HEAD_NS}">\n'
        '    <Fr><FIId><FinInstnId><BICFI>DEUTDEFFXXX</BICFI></FinInstnId></FIId></Fr>\n'
        '    <To><FIId><FinInstnId><BICFI>CHASUS33XXX</BICFI></FinInstnId></FIId></To>\n'
        '    <BizMsgIdr>MSG-1</BizMsgIdr>\n'
        '    <MsgDefIdr>camt.057.001.08</MsgDefIdr>\n'
        '    <BizSvc>swift.cbprplus.02</BizSvc>\n'
        '    <CreDt>2026-06-10T10:00:00+00:00</CreDt>\n'
        '  </AppHdr>\n'
        '  <Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.057.001.08">\n'
        '    <NtfctnToRcv><GrpHdr><MsgId>MSG-1</MsgId>'
        '<CreDtTm>2026-06-10T10:00:00+00:00</CreDtTm></GrpHdr></NtfctnToRcv>\n'
        '  </Document>\n'
        '</BusMsgEnvlp>'
    ),
    issue={
        "path": "/AppHdr/BizSvc",
        "code": "CBPR_R8",
        "message": "AppHdr <BizSvc> must be 'swift.cbprplus.03'.",
        "fix_suggestion": "Set <BizSvc>swift.cbprplus.03</BizSvc> in AppHdr.",
    },
    expect_counts={"BizSvc": 1},
    expect_values={"BizSvc": "swift.cbprplus.03"},
    check_cardinality=False,  # mixed head.001 + camt body — skip XSD card map
    check_idempotent=True,
    notes="camt.057 BizSvc must resolve to .03 (matches validator CBPR_R8).",
))
