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
# ─────────────────────────────────────────────────────────────────────────────
# 8. RMTINF MUTUAL EXCLUSIVITY — GLOBAL-RMT-001 / CBPR_R34. <Strd> and <Ustrd>
#    cannot coexist in CBPR+. Fix removes <Strd>, keeps <Ustrd>. Previously this
#    code had no deterministic handler and silently fell through to the
#    (unavailable) LLM fallback for pacs.008/pacs.003/pain.001/pain.008.
# ─────────────────────────────────────────────────────────────────────────────
CASES.append(Case(
    name="rmtinf/strd_and_ustrd_mutually_exclusive",
    xml=_pacs008(
        '      <PmtId><EndToEndId>E2E-1</EndToEndId><TxId>TX-1</TxId></PmtId>\n'
        '      <IntrBkSttlmAmt Ccy="USD">100.00</IntrBkSttlmAmt>\n'
        '      <RmtInf>\n'
        '        <Ustrd>Invoice 12345</Ustrd>\n'
        '        <Strd><CdtrRefInf><Ref>REF12345</Ref></CdtrRefInf></Strd>\n'
        '      </RmtInf>'
    ),
    issue={
        "path": "/",
        "code": "GLOBAL-RMT-001",
        "message": "Structured and Unstructured remittance are mutually "
                   "exclusive in all CBPR+ messages.",
        "fix_suggestion": "Remove either Strd or Ustrd from the RmtInf block.",
    },
    expect_counts={"RmtInf": 1, "Ustrd": 1, "Strd": 0},
    check_idempotent=True,
    notes="Strd removed, Ustrd kept; re-fixing the result is a no-op.",
))

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

# ─────────────────────────────────────────────────────────────────────────────
# 9. CHOICE OVER-POPULATION — AccountIdentification4Choice (<Id> under *Acct)
#    permits EXACTLY ONE of <IBAN>/<Othr>. A mid-message edit left BOTH, so the
#    validator flags the 2nd member "not expected at this position" and the fixer
#    used to DECLINE (never guess choice-member removal) → stuck forever.
#    Guard: collapse to the VALID member (IBAN passes MOD-97 → keep IBAN, drop
#    Othr); single IBAN survives, Othr gone, and re-fixing is a no-op.
# ─────────────────────────────────────────────────────────────────────────────
CASES.append(Case(
    name="choice/acct_id_iban_and_othr_collapse",
    xml=_pacs008(
        '      <PmtId><EndToEndId>E2E-1</EndToEndId><TxId>TX-1</TxId></PmtId>\n'
        '      <IntrBkSttlmAmt Ccy="USD">100.00</IntrBkSttlmAmt>\n'
        '      <DbtrAcct><Id>\n'
        '        <IBAN>GB29NWBK60161331926819</IBAN>\n'
        '        <Othr><Id>ACCT-1</Id></Othr>\n'
        '      </Id></DbtrAcct>'
    ),
    issue={
        "path": "/Document/FIToFICstmrCdtTrf/CdtTrfTxInf/DbtrAcct/Id/Othr",
        "code": "SCHEMA_VAL",
        "message": "The element 'Othr' is not expected at this position. Either "
                   "it is not allowed here or a mandatory field is missing "
                   "before it.",
        "fix_suggestion": "",
    },
    expect_counts={"IBAN": 1, "Othr": 0, "DbtrAcct": 1},
    expect_values={"IBAN": "GB29NWBK60161331926819"},
    check_idempotent=True,
    notes="IBAN+Othr coexist under account Id — keep valid IBAN, drop Othr.",
))

# ─────────────────────────────────────────────────────────────────────────────
# 10. CBPR+ CTRLSUM FORBIDDEN — GrpHdr/CtrlSum is not part of the CBPR+
#     pacs.009 GroupHeader93 subset. MyStandards expects NbOfTxs → SttlmInf
#     directly. Our validator used to pass it (base XSD allows optional CtrlSum),
#     making us disagree with MyStandards. Guard: validator raises
#     CBPR_CTRLSUM_FORBIDDEN; fixer removes CtrlSum at high confidence;
#     re-fixing the result is a no-op (idempotent).
# ─────────────────────────────────────────────────────────────────────────────
PACS009_NS = "urn:iso:std:iso:20022:tech:xsd:pacs.009.001.08"

CASES.append(Case(
    name="cbpr_forbidden/pacs009_ctrlsum_in_grphdr",
    xml=(
        '<BusMsgEnvlp xmlns="urn:swift:xsd:envelope">\n'
        f'  <Document xmlns="{PACS009_NS}">\n'
        '    <FICdtTrf>\n'
        '      <GrpHdr>\n'
        '        <MsgId>MSG-GC-001</MsgId>\n'
        '        <CreDtTm>2026-06-11T09:00:00+00:00</CreDtTm>\n'
        '        <NbOfTxs>1</NbOfTxs>\n'
        '        <CtrlSum>1000.00</CtrlSum>\n'
        '        <SttlmInf><SttlmMtd>INGA</SttlmMtd></SttlmInf>\n'
        '      </GrpHdr>\n'
        '    </FICdtTrf>\n'
        '  </Document>\n'
        '</BusMsgEnvlp>'
    ),
    issue={
        "path": "/BusMsgEnvlp/Document/FICdtTrf/GrpHdr/CtrlSum",
        "code": "CBPR_CTRLSUM_FORBIDDEN",
        "message": "GrpHdr/CtrlSum is not permitted in CBPR+ pacs.009. "
                   "Remove <CtrlSum> — MyStandards expects NbOfTxs followed "
                   "directly by SttlmInf.",
        "fix_suggestion": "Remove <CtrlSum>.",
    },
    expect_counts={"CtrlSum": 0, "NbOfTxs": 1, "SttlmInf": 1},
    check_cardinality=False,
    check_idempotent=True,
    notes="CtrlSum removed from GrpHdr; NbOfTxs and SttlmInf untouched.",
))

# ─────────────────────────────────────────────────────────────────────────────
# 11. BULK ENVELOPE PATH RESOLUTION — a <BulkMessages> file mixing an enveloped
#     message (<BusMsgEnvlp><AppHdr/><Document/>) with a bare <Document>. The
#     validator emits an envelope-agnostic path (Document.…/PmtId/UETR) for a
#     UETR missing in the ENVELOPED (first) message. The path walker used to look
#     only at DIRECT children of <BulkMessages>, so it never reached the Document
#     nested under <BusMsgEnvlp> and instead resolved against the bare second
#     message — which already had a UETR — yielding a no-op the auto-fixer
#     silently dropped (the "this message is not getting fixed" report).
#     Guard: the fix must land in the enveloped message (UETR count 1 → 2), and
#     re-fixing is a no-op.
# ─────────────────────────────────────────────────────────────────────────────
PACS010_NS = "urn:iso:std:iso:20022:tech:xsd:pacs.010.001.03"


def _pacs010_drctdbt(msg_id: str, pmtid_inner: str) -> str:
    return (
        f'    <FIDrctDbt>\n'
        f'      <GrpHdr><MsgId>{msg_id}</MsgId>'
        f'<CreDtTm>2026-06-10T10:00:00+00:00</CreDtTm><NbOfTxs>1</NbOfTxs></GrpHdr>\n'
        f'      <CdtInstr>\n'
        f'        <CdtId>{msg_id}-C</CdtId>\n'
        f'        <DrctDbtTxInf>\n'
        f'          <PmtId>{pmtid_inner}</PmtId>\n'
        f'          <IntrBkSttlmAmt Ccy="GBP">100.00</IntrBkSttlmAmt>\n'
        f'          <Dbtr><FinInstnId><BICFI>DEUTDEFFXXX</BICFI></FinInstnId></Dbtr>\n'
        f'          <DbtrAgt><FinInstnId><BICFI>DEUTDEFFXXX</BICFI></FinInstnId></DbtrAgt>\n'
        f'        </DrctDbtTxInf>\n'
        f'      </CdtInstr>\n'
        f'    </FIDrctDbt>\n'
    )


CASES.append(Case(
    name="bulk_envelope/uetr_lands_in_enveloped_message",
    xml=(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<BulkMessages>\n'
        '  <BusMsgEnvlp>\n'
        f'    <AppHdr xmlns="{HEAD_NS}"><BizMsgIdr>MSG-1</BizMsgIdr>'
        '<MsgDefIdr>pacs.010.001.03</MsgDefIdr></AppHdr>\n'
        f'    <Document xmlns="{PACS010_NS}">\n'
        + _pacs010_drctdbt(
            "MSG-1",
            '<InstrId>INSTR-1</InstrId><EndToEndId>E2E-1</EndToEndId><TxId>TX-1</TxId>',
        )
        + '    </Document>\n'
        '  </BusMsgEnvlp>\n'
        f'  <Document xmlns="{PACS010_NS}">\n'
        + _pacs010_drctdbt(
            "MSG-2",
            '<InstrId>INSTR-2</InstrId><EndToEndId>E2E-2</EndToEndId><TxId>TX-2</TxId>'
            '<UETR>19df0b83-c8ce-4fa6-86d4-afad734faed2</UETR>',
        )
        + '  </Document>\n'
        '</BulkMessages>'
    ),
    issue={
        "path": "Document.FIDrctDbt.CdtInstr.DrctDbtTxInf.PmtId.UETR",
        "code": "PACS010_DRCTDBT_REQUIRED",
        "message": "UETR, EndToEndId, IntrBkSttlmAmt, Dbtr and DbtrAgt are "
                   "mandatory in pacs.010 DrctDbtTxInf under CBPR+.",
        "fix_suggestion": "Add the missing element inside <DrctDbtTxInf> "
                          "(Dbtr/DbtrAgt use FinInstnId/BICFI; UETR is a UUID v4).",
        "line": 7,  # enveloped message's PmtId — disambiguates to the 1st message
    },
    expect_counts={"UETR": 2},  # was 1 (bare msg only); fix adds one to the 1st
    check_cardinality=False,    # multi-message bulk — elements repeat per message
    check_idempotent=True,
    notes="Enveloped Document nested under BusMsgEnvlp must be reachable; fix "
          "lands in the message that's actually missing UETR, not the bare one.",
))

# ─────────────────────────────────────────────────────────────────────────────
# 13. STRAY TEXT RE-WRAP — children's open+close tags ripped, values left as
#     bare text inside element-only <PmtId>. The fix must RE-WRAP each orphan
#     line into the missing XSD child at that sequence position (InstrId,
#     EndToEndId, TxId) — preserving user data — instead of stripping the text
#     and refilling with placeholders.
# ─────────────────────────────────────────────────────────────────────────────
CASES.append(Case(
    name="stray_text/pmtid_values_rewrapped_not_discarded",
    xml=_pacs008(
        '      <PmtId>\n'
        '        INSTR-RIPPED-1\n'
        '        E2E-RIPPED-2\n'
        '        TX-RIPPED-3\n'
        '        <UETR>2c01f532-1a16-4f93-88fc-664e897f7bc6</UETR>\n'
        '      </PmtId>\n'
        '      <IntrBkSttlmAmt Ccy="USD">100.00</IntrBkSttlmAmt>'
    ),
    issue={
        "path": "4",
        "code": "SCHEMA_VAL",
        "message": "Validation error: Field 'PmtId': Character content other "
                   "than whitespace is not allowed because the content type "
                   "is 'element-only'.",
        "fix_suggestion": "",
        "line": 4,
    },
    expect_counts={"PmtId": 1, "InstrId": 1, "EndToEndId": 1, "TxId": 1, "UETR": 1},
    expect_values={"InstrId": "INSTR-RIPPED-1",
                   "EndToEndId": "E2E-RIPPED-2",
                   "TxId": "TX-RIPPED-3"},
    notes="Orphaned text values must be re-wrapped into the missing children "
          "in XSD sequence order — zero data loss.",
))

# ─────────────────────────────────────────────────────────────────────────────
# 14. SIMPLE-LEAF ARTIFACT CLEANUP — a dummy <IBAN> was injected INSIDE the
#     simple-type leaf Othr/Id (generic-<Id> collision by an earlier bad fix).
#     The fix must remove the artifact children, keep the leaf's real text,
#     and never touch legitimate IBANs elsewhere.
# ─────────────────────────────────────────────────────────────────────────────
CASES.append(Case(
    name="simple_leaf/iban_artifact_inside_othr_id_removed",
    xml=_pacs008(
        '      <PmtId><EndToEndId>E2E-1</EndToEndId></PmtId>\n'
        '      <IntrBkSttlmAmt Ccy="USD">100.00</IntrBkSttlmAmt>\n'
        '      <DbtrAgtAcct>\n'
        '        <Id>\n'
        '          <Othr>\n'
        '            <Id>ACCT-REAL-1<IBAN>GB29NWBK60161331926819</IBAN></Id>\n'
        '          </Othr>\n'
        '        </Id>\n'
        '      </DbtrAgtAcct>\n'
        '      <CdtrAcct>\n'
        '        <Id><IBAN>SE1328019045529161587750</IBAN></Id>\n'
        '      </CdtrAcct>'
    ),
    issue={
        "path": "9",
        "code": "SCHEMA_VAL",
        "message": "Tag <IBAN> is duplicated.",
        "fix_suggestion": "The tag <IBAN> appears more than once in this "
                          "section. Remove the extra copy and keep only one.",
        "line": 9,
    },
    expect_counts={"IBAN": 1, "Othr": 1},
    expect_values={"IBAN": "SE1328019045529161587750"},
    check_cardinality=False,  # probes a non-XSD duplicate-detection family
    notes="Artifact IBAN inside simple-type Othr/Id removed; ACCT text and the "
          "legitimate account IBAN preserved.",
))

# ─────────────────────────────────────────────────────────────────────────────
# 15. STRAY AGENT FRAGMENT — a bare <BICFI> stranded directly under
#     CdtTrfTxInf (its agent + FinInstnId wrappers both ripped) is reported as
#     a cross-parent duplicate. The fix must DEEP-WRAP it into the first free
#     agent slot (PrvsInstgAgt1 → FinInstnId → BICFI), not delete it and not
#     dedupe a legitimate BICFI elsewhere.
# ─────────────────────────────────────────────────────────────────────────────
CASES.append(Case(
    name="stray_fragment/bare_bicfi_deep_wrapped_into_agent",
    xml=_pacs008(
        '      <PmtId><EndToEndId>E2E-1</EndToEndId></PmtId>\n'
        '      <IntrBkSttlmAmt Ccy="USD">100.00</IntrBkSttlmAmt>\n'
        '      <ChrgBr>DEBT</ChrgBr>\n'
        '      <BICFI>DBSSSESSXXX</BICFI>\n'
        '      <InstgAgt><FinInstnId><BICFI>ABNASESSXXX</BICFI></FinInstnId></InstgAgt>'
    ),
    issue={
        "path": "13",
        "code": "SCHEMA_VAL",
        "message": "Tag <BICFI> is duplicated.",
        "fix_suggestion": "The tag <BICFI> appears more than once in this "
                          "section. Remove the extra copy and keep only one.",
        "line": 13,
    },
    expect_counts={"BICFI": 2, "PrvsInstgAgt1": 1},
    expect_values={"PrvsInstgAgt1": ""},
    check_cardinality=False,  # stray element probes a non-XSD shape
    notes="Bare BICFI wrapped as PrvsInstgAgt1/FinInstnId/BICFI; value "
          "DBSSSESSXXX preserved; InstgAgt untouched.",
))

# ─────────────────────────────────────────────────────────────────────────────
# 16. CHOICE-PARENT STRAY (cross-message) — camt.055 Assgne is a Pty|Agt
#     CHOICE; a bare <FinInstnId> left by a ripped <Agt> wrapper must be
#     wrapped INTO <Agt>, not "fixed" by inserting a placeholder <Pty> next to
#     it (KB local-name collision), and not deleted. Also guards the
#     issue-path-aware candidate selection: the path names Assgne, and the
#     same-named FinInstnId under Assgnr/Agt must NOT be touched.
# ─────────────────────────────────────────────────────────────────────────────
CAMT055_NS = "urn:iso:std:iso:20022:tech:xsd:camt.055.001.12"

CASES.append(Case(
    name="choice_wrap/camt055_assgne_bare_fininstnid_into_agt",
    xml=(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<Document xmlns="{CAMT055_NS}">\n'
        '  <CstmrPmtCxlReq>\n'
        '    <Assgnmt>\n'
        '      <Id>ASGN-1</Id>\n'
        '      <Assgnr><Agt><FinInstnId><BICFI>RBOSNOKKXXX</BICFI></FinInstnId></Agt></Assgnr>\n'
        '      <Assgne><FinInstnId><BICFI>UBSWNOKKXXX</BICFI></FinInstnId></Assgne>\n'
        '      <CreDtTm>2026-06-12T10:00:00+00:00</CreDtTm>\n'
        '    </Assgnmt>\n'
        '  </CstmrPmtCxlReq>\n'
        '</Document>'
    ),
    issue={
        "path": "/Document/CstmrPmtCxlReq/Assgnmt/Assgne/FinInstnId",
        "code": "SCHEMA_VAL",
        "message": "The element 'FinInstnId' is not expected here. Either it "
                   "is not allowed in this specification, or another mandatory "
                   "element is missing before this position.",
        "fix_suggestion": "",
        "line": 7,
    },
    expect_counts={"FinInstnId": 2, "Agt": 2, "BICFI": 2, "Pty": 0},
    expect_values={"BICFI": "RBOSNOKKXXX"},
    check_cardinality=False,
    notes="Bare FinInstnId in the Assgne CHOICE wrapped into Agt; no Pty "
          "placeholder injected; Assgnr untouched.",
))
