import json

kb = {
  "domain": "SWIFT MX CBPR+ pacs.003.001.08",
  "version": "SR2025",
  "last_updated": "2026-05-28",
  "purpose": "Context KB for pacs.003 FIToFICustomerDirectDebit validation. For each XML tag: possible errors, affected tags, and all possible fixes. Based on CBPR+ SR2025 Usage Guideline (pacs.003.001.08) and SWIFT MX Enterprise KB.",
  "reference_documents": [
    "CBPRPlus-pacs.003.001.08_FIToFICustomerDirectDebit CBPRPlus SR2025 (Combined), 21 March 2025",
    "SWIFT MX Enterprise LLM KB v2.0"
  ],
  "iso_20022_rules": [
    {"rule_id": "R1", "name": "RelatedPresentWhenCopyDupl", "description": "If CopyDuplicate is present, Related MUST be present.", "error_code": "H00001", "severity": "Warning", "error_text": "Element Related is missing"},
    {"rule_id": "R4", "name": "InstructedAgentRule", "description": "If GrpHdr/InstdAgt is present, then DrctDbtTxInf/InstdAgt is not allowed.", "error_code": "X00008", "severity": "Fatal", "error_text": "Invalid message content for instructed agent."},
    {"rule_id": "R5", "name": "InstructingAgentRule", "description": "If GrpHdr/InstgAgt is present, then DrctDbtTxInf/InstgAgt is not allowed.", "error_code": "X00007", "severity": "Fatal", "error_text": "Invalid message content for instructing agent."},
    {"rule_id": "R6", "name": "TotalInterbankSettlementAmountRule", "description": "If GrpHdr/TtlIntrBkSttlmAmt is present, all DrctDbtTxInf/IntrBkSttlmAmt must use the same currency.", "error_code": "X00042", "severity": "Fatal"},
    {"rule_id": "R7", "name": "TotalInterbankSettlementAmountAndSumRule", "description": "If GrpHdr/TtlIntrBkSttlmAmt is present, it must equal the sum of all DrctDbtTxInf/IntrBkSttlmAmt.", "error_code": "X00043", "severity": "Fatal"},
    {"rule_id": "R8", "name": "GroupHeaderInterbankSettlementDateRule", "description": "If GrpHdr/IntrBkSttlmDt is present, DrctDbtTxInf/IntrBkSttlmDt is not allowed.", "error_code": "X00045", "severity": "Fatal"},
    {"rule_id": "R9", "name": "TransactionInterbankSettlementDateRule", "description": "If GrpHdr/IntrBkSttlmDt is not present, DrctDbtTxInf/IntrBkSttlmDt MUST be present.", "error_code": "X00290", "severity": "Fatal"},
    {"rule_id": "R10", "name": "PaymentTypeInformationRule", "description": "If GrpHdr/PmtTpInf is present, DrctDbtTxInf/PmtTpInf is not allowed.", "error_code": "X00009", "severity": "Fatal"},
    {"rule_id": "R11", "name": "NumberOfTransactionsAndDirectDebitsRule", "description": "GrpHdr/NbOfTxs must equal the number of DrctDbtTxInf occurrences.", "error_code": "X00062", "severity": "Fatal"},
    {"rule_id": "R12", "name": "MandateRelatedInformationRule", "description": "If DrctDbtTx is present, MndtRltdInf MUST be present and contain MndtId and DtOfSgntr.", "error_code": "X00310", "severity": "Fatal"}
  ],
  "cbpr_plus_formal_rules": [
    {"rule_id": "CBPR_PRIORITY", "name": "CBPR_Priority_Instruction_Priority_FormalRule", "description": "AppHdr/Priority must equal DrctDbtTxInf/PmtTpInf/InstrPrty when both are present."},
    {"rule_id": "CBPR_FR_TO_1", "name": "CBPR_From_To_Instructing_Instructed_Agent_BIC_1", "description": "AppHdr/Fr BIC must match InstgAgt BIC and AppHdr/To BIC must match InstdAgt BIC, except when CpyDplct = COPY or CODU."},
    {"rule_id": "CBPR_FR_TO_2", "name": "CBPR_From_To_Instructing_Instructed_Agent_BIC_2", "description": "AppHdr/Fr BIC must match InstgAgt BIC and AppHdr/To BIC must match InstdAgt BIC when CpyDplct is absent."},
    {"rule_id": "CBPR_AGENT_BICFI_EXCLUSIVE", "name": "CBPR_Agent_BICFI_Rule", "description": "If BICFI is present inside FinInstnId, then Nm and PstlAdr are NOT allowed in the same FinInstnId block."},
    {"rule_id": "CBPR_AGENT_NM_PSTLADR", "name": "CBPR_Agent_Name_Postal_Address_FormalRule", "description": "If Nm is present in FinInstnId, PstlAdr must also be present, and vice versa."},
    {"rule_id": "CBPR_CHARSET", "name": "CBPR_Character_Set_Usage_TextualRule", "description": "All proprietary/text fields (excl. Nm/Adr/RmtInf) limited to FIN-X charset. Nm/Adr/RmtInf extended with !#$&%*=^_{|}~\";<>@[\\] (< as &lt;, > as &gt;)."},
    {"rule_id": "CBPR_BIZ_SVC", "name": "CBPR_Business_Service_Usage_TextualRule", "description": "AppHdr/BizSvc must be 'swift.cbprplus.03' (SR2025). This element is mandatory (min occurrence changed to 1)."},
    {"rule_id": "CBPR_BIZ_MSG_IDR", "name": "CBPR_Business_Message_Identifier_TextualRule", "description": "AppHdr/BizMsgIdr must equal GrpHdr/MsgId."},
    {"rule_id": "CBPR_DATETIME_NO_Z", "name": "CBPR_DateTime_Format", "description": "DateTime fields must use YYYY-MM-DDTHH:MM:SS+HH:MM format. Z suffix is forbidden. Milliseconds are forbidden."},
    {"rule_id": "CBPR_SINGLE_TX", "name": "Single Transaction Only", "description": "CBPR+ allows single transactions only per message."},
    {"rule_id": "CBPR_BRANCH_REMOVED", "name": "BrnchId Removed", "description": "BranchIdentification is removed from InstgAgt, InstdAgt, DbtrAgt, CdtrAgt, IntrmyAgt1/2/3, PrvsInstgAgt1/2/3 and settlement agents."},
    {"rule_id": "CBPR_MNDT_REQUIRED", "name": "CBPR_Mandate_Related_Information_TextualRule", "description": "MndtRltdInf/MndtId and MndtRltdInf/DtOfSgntr are required when DrctDbtTx is present under CBPR+."},
    {"rule_id": "CBPR_CDTR_SCHME_ID", "name": "CBPR_Creditor_Scheme_Identification_TextualRule", "description": "DrctDbtTx/CdtrSchmeId identifies the creditor under a direct debit scheme; Id and SchmeNm must be consistent when present."}
  ],
  "tags": [

    {
      "tag": "AppHdr",
      "xml_element": "AppHdr",
      "xpath": "/AppHdr",
      "occurrence": "[1..1]",
      "mandatory": True,
      "description": "Business Application Header V02. Mandatory for all CBPR+ messages.",
      "errors": [
        {
          "error_id": "APPHDR_MISSING",
          "error_code": "SCHEMA_ERR",
          "severity": "Fatal",
          "description": "AppHdr element is absent; CBPR+ mandates BusinessApplicationHeaderV02.",
          "affected_tags": ["AppHdr"],
          "possible_fixes": [
            "Insert a complete <AppHdr> block before the <Document> element.",
            "Ensure AppHdr contains Fr, To, BizMsgIdr, MsgDefIdr, BizSvc, CreDt, and optionally Prty."
          ]
        }
      ]
    },

    {
      "tag": "AppHdr/Fr",
      "xml_element": "Fr",
      "xpath": "/AppHdr/Fr",
      "occurrence": "[1..1]",
      "mandatory": True,
      "description": "Sending MessagingEndpoint. Must contain BIC8 or BIC11 when exchanged on SWIFT.",
      "errors": [
        {
          "error_id": "APPHDR_FR_MISSING",
          "error_code": "SCHEMA_ERR",
          "severity": "Fatal",
          "description": "Fr element is absent from AppHdr.",
          "affected_tags": ["AppHdr/Fr"],
          "possible_fixes": [
            "Insert <Fr><FIId><FinInstnId><BICFI>{InstgAgt_BICFI}</BICFI></FinInstnId></FIId></Fr> inside AppHdr.",
            "Harvest the BICFI value from DrctDbtTxInf/InstgAgt/FinInstnId/BICFI."
          ]
        },
        {
          "error_id": "APPHDR_FR_NEQ_INSTGAGT",
          "error_code": "CBPR_FR_TO_1",
          "severity": "Fatal",
          "description": "AppHdr/Fr BICFI does not match DrctDbtTxInf/InstgAgt BICFI (when CpyDplct is absent or not COPY/CODU).",
          "affected_tags": ["AppHdr/Fr/FIId/FinInstnId/BICFI", "DrctDbtTxInf/InstgAgt/FinInstnId/BICFI"],
          "possible_fixes": [
            "Read InstgAgt/FinInstnId/BICFI from the transaction block and set AppHdr/Fr/FIId/FinInstnId/BICFI to the same value.",
            "Alternatively update InstgAgt/FinInstnId/BICFI to match AppHdr/Fr if Fr is the authoritative source."
          ]
        },
        {
          "error_id": "APPHDR_FR_INVALID_BIC",
          "error_code": "INVALID_BICFI",
          "severity": "Fatal",
          "description": "AppHdr/Fr contains an invalid or malformed BIC.",
          "affected_tags": ["AppHdr/Fr/FIId/FinInstnId/BICFI"],
          "possible_fixes": [
            "Replace with a valid BICFI: 8 or 11 uppercase alphanumeric characters following ISO 9362.",
            "Pattern: [A-Z]{6}[A-Z2-9][A-NP-Z0-9]([A-Z0-9]{3})?"
          ]
        }
      ]
    },

    {
      "tag": "AppHdr/To",
      "xml_element": "To",
      "xpath": "/AppHdr/To",
      "occurrence": "[1..1]",
      "mandatory": True,
      "description": "Receiving MessagingEndpoint. Must contain BIC8 or BIC11 on SWIFT.",
      "errors": [
        {
          "error_id": "APPHDR_TO_MISSING",
          "error_code": "SCHEMA_ERR",
          "severity": "Fatal",
          "description": "To element is absent from AppHdr.",
          "affected_tags": ["AppHdr/To"],
          "possible_fixes": [
            "Insert <To><FIId><FinInstnId><BICFI>{InstdAgt_BICFI}</BICFI></FinInstnId></FIId></To> inside AppHdr.",
            "Harvest the BICFI from DrctDbtTxInf/InstdAgt/FinInstnId/BICFI."
          ]
        },
        {
          "error_id": "APPHDR_TO_NEQ_INSTDAGT",
          "error_code": "CBPR_FR_TO_1",
          "severity": "Fatal",
          "description": "AppHdr/To BICFI does not match DrctDbtTxInf/InstdAgt BICFI.",
          "affected_tags": ["AppHdr/To/FIId/FinInstnId/BICFI", "DrctDbtTxInf/InstdAgt/FinInstnId/BICFI"],
          "possible_fixes": [
            "Read InstdAgt/FinInstnId/BICFI from the transaction block and set AppHdr/To/FIId/FinInstnId/BICFI to match.",
            "Alternatively update InstdAgt/BICFI to match AppHdr/To if To is authoritative."
          ]
        }
      ]
    },

    {
      "tag": "AppHdr/BizMsgIdr",
      "xml_element": "BizMsgIdr",
      "xpath": "/AppHdr/BizMsgIdr",
      "occurrence": "[1..1]",
      "mandatory": True,
      "datatype": "Max35Text",
      "max_length": 35,
      "description": "Unique identifier of the Business Message. Must equal GrpHdr/MsgId.",
      "errors": [
        {
          "error_id": "BIZMSGIDR_MISSING",
          "error_code": "MISSING_MANDATORY_FIELD",
          "severity": "Fatal",
          "description": "BizMsgIdr is absent from AppHdr.",
          "affected_tags": ["AppHdr/BizMsgIdr"],
          "possible_fixes": [
            "Copy the value of GrpHdr/MsgId and insert as <BizMsgIdr>{MsgId_value}</BizMsgIdr>.",
            "If MsgId is also absent, generate a unique Max35Text value."
          ]
        },
        {
          "error_id": "BIZMSGIDR_NEQ_MSGID",
          "error_code": "BIZMSGIDR_EQ_GRPHDR_MSGID",
          "severity": "Fatal",
          "description": "AppHdr/BizMsgIdr does not equal Document/GrpHdr/MsgId.",
          "affected_tags": ["AppHdr/BizMsgIdr", "GrpHdr/MsgId"],
          "possible_fixes": [
            "Read GrpHdr/MsgId and set BizMsgIdr to the same value.",
            "If BizMsgIdr is the source of truth, update GrpHdr/MsgId to match BizMsgIdr.",
            "Never invent a new value — always harvest from the other field."
          ]
        },
        {
          "error_id": "BIZMSGIDR_TOO_LONG",
          "error_code": "ID_LENGTH_ERROR",
          "severity": "Fatal",
          "description": "BizMsgIdr exceeds 35 characters.",
          "affected_tags": ["AppHdr/BizMsgIdr"],
          "possible_fixes": [
            "Truncate the value to a maximum of 35 characters.",
            "Ensure the truncated value remains meaningful and unique.",
            "Synchronize the truncated value back to GrpHdr/MsgId."
          ]
        }
      ]
    },

    {
      "tag": "AppHdr/MsgDefIdr",
      "xml_element": "MsgDefIdr",
      "xpath": "/AppHdr/MsgDefIdr",
      "occurrence": "[1..1]",
      "mandatory": True,
      "datatype": "Max35Text",
      "description": "Message Definition Identifier. Must exactly match the Document namespace.",
      "expected_value": "pacs.003.001.08",
      "errors": [
        {
          "error_id": "MSGDEFIDR_MISSING",
          "error_code": "MISSING_MANDATORY_FIELD",
          "severity": "Fatal",
          "description": "MsgDefIdr is absent from AppHdr.",
          "affected_tags": ["AppHdr/MsgDefIdr"],
          "possible_fixes": [
            "Insert <MsgDefIdr>pacs.003.001.08</MsgDefIdr> inside AppHdr."
          ]
        },
        {
          "error_id": "MSGDEFIDR_MISMATCH",
          "error_code": "MSGDEFIDR_MISMATCH",
          "severity": "Fatal",
          "description": "AppHdr/MsgDefIdr does not match the Document xmlns namespace identifier.",
          "affected_tags": ["AppHdr/MsgDefIdr"],
          "possible_fixes": [
            "Read the xmlns attribute from the Document root element (e.g. urn:iso:std:iso:20022:tech:xsd:pacs.003.001.08).",
            "Extract the message identifier portion and set MsgDefIdr to 'pacs.003.001.08'.",
            "Ensure no trailing spaces or version mismatches."
          ]
        }
      ]
    },

    {
      "tag": "AppHdr/BizSvc",
      "xml_element": "BizSvc",
      "xpath": "/AppHdr/BizSvc",
      "occurrence": "[1..1]",
      "mandatory": True,
      "note": "Minimum occurrence changed to 1 by CBPR+ SR2025 guideline.",
      "expected_value": "swift.cbprplus.03",
      "description": "Business Service identifier. Mandatory under CBPR+ SR2025; must be 'swift.cbprplus.03'.",
      "errors": [
        {
          "error_id": "BIZSVC_MISSING",
          "error_code": "MISSING_MANDATORY_FIELD",
          "severity": "Fatal",
          "description": "BizSvc is absent; it is mandatory under CBPR+ SR2025.",
          "affected_tags": ["AppHdr/BizSvc"],
          "possible_fixes": [
            "Insert <BizSvc>swift.cbprplus.03</BizSvc> inside AppHdr."
          ]
        },
        {
          "error_id": "BIZSVC_WRONG_VALUE",
          "error_code": "CBPR_BIZ_SVC",
          "severity": "Fatal",
          "description": "BizSvc contains an incorrect value (e.g. 'swift.cbprplus.02' from older release).",
          "affected_tags": ["AppHdr/BizSvc"],
          "possible_fixes": [
            "Replace value with 'swift.cbprplus.03' as mandated by SR2025.",
            "Note: SR2024 and earlier used 'swift.cbprplus.02'; upgrade to .03 for SR2025."
          ]
        }
      ]
    },

    {
      "tag": "AppHdr/CreDt",
      "xml_element": "CreDt",
      "xpath": "/AppHdr/CreDt",
      "occurrence": "[1..1]",
      "mandatory": True,
      "datatype": "ISONormalisedDateTime",
      "description": "Creation date/time of the Business Message. Must use YYYY-MM-DDTHH:MM:SS+HH:MM format; Z and milliseconds forbidden under CBPR+.",
      "errors": [
        {
          "error_id": "CREDT_MISSING",
          "error_code": "MISSING_MANDATORY_FIELD",
          "severity": "Fatal",
          "description": "AppHdr/CreDt is absent.",
          "affected_tags": ["AppHdr/CreDt"],
          "possible_fixes": [
            "Insert <CreDt>{current datetime in YYYY-MM-DDTHH:MM:SS+00:00 format}</CreDt> inside AppHdr."
          ]
        },
        {
          "error_id": "CREDT_Z_SUFFIX",
          "error_code": "CBPR_DATETIME_Z_FORBIDDEN",
          "severity": "Fatal",
          "description": "AppHdr/CreDt uses 'Z' suffix which is forbidden under CBPR+.",
          "affected_tags": ["AppHdr/CreDt"],
          "possible_fixes": [
            "Replace trailing 'Z' with '+00:00'."
          ]
        },
        {
          "error_id": "CREDT_MILLISECONDS",
          "error_code": "CBPR_DATETIME_Z_FORBIDDEN",
          "severity": "Fatal",
          "description": "AppHdr/CreDt contains milliseconds which are forbidden under CBPR+.",
          "affected_tags": ["AppHdr/CreDt"],
          "possible_fixes": [
            "Remove millisecond component."
          ]
        },
        {
          "error_id": "CREDT_PAST_DATE",
          "error_code": "PAST_DATE_ERROR",
          "severity": "Warning",
          "description": "AppHdr/CreDt is in the past beyond tolerance window.",
          "affected_tags": ["AppHdr/CreDt"],
          "possible_fixes": [
            "Replace with the current datetime in YYYY-MM-DDTHH:MM:SS+00:00 format."
          ]
        }
      ]
    },

    {
      "tag": "AppHdr/Prty",
      "xml_element": "Prty",
      "xpath": "/AppHdr/Prty",
      "occurrence": "[0..1]",
      "mandatory": False,
      "valid_values": ["NORM", "HIGH"],
      "description": "Priority of the Business Message. If present, must match DrctDbtTxInf/PmtTpInf/InstrPrty.",
      "errors": [
        {
          "error_id": "PRTY_INVALID_CODE",
          "error_code": "SCHEMENAME_INVALID",
          "severity": "Fatal",
          "description": "AppHdr/Prty contains a value not in the allowed code list.",
          "affected_tags": ["AppHdr/Prty"],
          "possible_fixes": [
            "Replace with 'NORM' (normal) or 'HIGH' (high priority).",
            "Default to 'NORM' if priority is not specifically determined."
          ]
        },
        {
          "error_id": "PRTY_NEQ_INSTRPRTY",
          "error_code": "CBPR_PRIORITY",
          "severity": "Fatal",
          "description": "AppHdr/Prty does not match DrctDbtTxInf/PmtTpInf/InstrPrty.",
          "affected_tags": ["AppHdr/Prty", "DrctDbtTxInf/PmtTpInf/InstrPrty"],
          "possible_fixes": [
            "Set AppHdr/Prty to the same value as DrctDbtTxInf/PmtTpInf/InstrPrty.",
            "Alternatively set DrctDbtTxInf/PmtTpInf/InstrPrty to match AppHdr/Prty."
          ]
        }
      ]
    },

    {
      "tag": "GrpHdr/MsgId",
      "xml_element": "MsgId",
      "xpath": "/Document/FIToFICstmrDrctDbt/GrpHdr/MsgId",
      "occurrence": "[1..1]",
      "mandatory": True,
      "datatype": "Max35Text",
      "max_length": 35,
      "description": "Unique message identifier. Must equal AppHdr/BizMsgIdr. No spaces.",
      "errors": [
        {
          "error_id": "MSGID_MISSING",
          "error_code": "MISSING_MANDATORY_FIELD",
          "severity": "Fatal",
          "description": "GrpHdr/MsgId is absent.",
          "affected_tags": ["GrpHdr/MsgId", "AppHdr/BizMsgIdr"],
          "possible_fixes": [
            "Insert <MsgId>{unique identifier up to 35 chars}</MsgId> as first child of GrpHdr.",
            "Ensure the same value is in AppHdr/BizMsgIdr."
          ]
        },
        {
          "error_id": "MSGID_TOO_LONG",
          "error_code": "ID_LENGTH_ERROR",
          "severity": "Fatal",
          "description": "GrpHdr/MsgId exceeds 35 characters.",
          "affected_tags": ["GrpHdr/MsgId"],
          "possible_fixes": [
            "Truncate to maximum 35 characters preserving meaningful prefix.",
            "Synchronize truncated value to AppHdr/BizMsgIdr."
          ]
        },
        {
          "error_id": "MSGID_DUPLICATE",
          "error_code": "DUPLICATE_MSGID",
          "severity": "Fatal",
          "description": "GrpHdr/MsgId already exists in the receiving system (duplicate message).",
          "affected_tags": ["GrpHdr/MsgId"],
          "possible_fixes": [
            "Generate a new unique MsgId by appending current date and incrementing counter.",
            "Check sending system for duplicate submission logic."
          ]
        },
        {
          "error_id": "MSGID_INVALID_CHARS",
          "error_code": "INVALID_CHARSETS",
          "severity": "Fatal",
          "description": "MsgId contains characters outside the FIN-X character set or contains spaces.",
          "affected_tags": ["GrpHdr/MsgId"],
          "possible_fixes": [
            "Remove spaces from MsgId.",
            "Replace special/accented characters with ASCII equivalents.",
            "Allowed: A-Z a-z 0-9 / - ? : ( ) . , ' + (no leading or trailing spaces)."
          ]
        }
      ]
    },

    {
      "tag": "GrpHdr/CreDtTm",
      "xml_element": "CreDtTm",
      "xpath": "/Document/FIToFICstmrDrctDbt/GrpHdr/CreDtTm",
      "occurrence": "[1..1]",
      "mandatory": True,
      "datatype": "ISODateTime",
      "description": "Creation date and time. Format: YYYY-MM-DDTHH:MM:SS+HH:MM. Z suffix and milliseconds forbidden under CBPR+.",
      "errors": [
        {
          "error_id": "CREDTTM_MISSING",
          "error_code": "MISSING_MANDATORY_FIELD",
          "severity": "Fatal",
          "description": "GrpHdr/CreDtTm is absent.",
          "affected_tags": ["GrpHdr/CreDtTm"],
          "possible_fixes": [
            "Insert <CreDtTm>{current datetime as YYYY-MM-DDTHH:MM:SS+00:00}</CreDtTm>."
          ]
        },
        {
          "error_id": "CREDTTM_Z_SUFFIX",
          "error_code": "CBPR_DATETIME_Z_FORBIDDEN",
          "severity": "Fatal",
          "description": "CreDtTm uses 'Z' suffix; forbidden under CBPR+.",
          "affected_tags": ["GrpHdr/CreDtTm"],
          "possible_fixes": [
            "Replace 'Z' with '+00:00'."
          ]
        },
        {
          "error_id": "CREDTTM_MILLISECONDS",
          "error_code": "CBPR_DATETIME_Z_FORBIDDEN",
          "severity": "Fatal",
          "description": "CreDtTm contains milliseconds; forbidden under CBPR+.",
          "affected_tags": ["GrpHdr/CreDtTm"],
          "possible_fixes": [
            "Strip milliseconds."
          ]
        },
        {
          "error_id": "CREDTTM_PAST_DATE",
          "error_code": "PAST_DATE_ERROR",
          "severity": "Warning",
          "description": "CreDtTm is too far in the past.",
          "affected_tags": ["GrpHdr/CreDtTm"],
          "possible_fixes": [
            "Replace with current datetime: YYYY-MM-DDTHH:MM:SS+00:00."
          ]
        }
      ]
    },

    {
      "tag": "GrpHdr/NbOfTxs",
      "xml_element": "NbOfTxs",
      "xpath": "/Document/FIToFICstmrDrctDbt/GrpHdr/NbOfTxs",
      "occurrence": "[1..1]",
      "mandatory": True,
      "datatype": "Max15NumericText",
      "max_length": 15,
      "description": "Number of transactions. Must equal actual count of DrctDbtTxInf blocks. CBPR+ allows single transactions only (value = 1).",
      "errors": [
        {
          "error_id": "NBOFTXS_MISSING",
          "error_code": "MISSING_MANDATORY_FIELD",
          "severity": "Fatal",
          "description": "GrpHdr/NbOfTxs is absent.",
          "affected_tags": ["GrpHdr/NbOfTxs"],
          "possible_fixes": [
            "Count the actual number of DrctDbtTxInf blocks and insert <NbOfTxs>{count}</NbOfTxs>.",
            "For CBPR+ single-transaction messages, value should be '1'."
          ]
        },
        {
          "error_id": "NBOFTXS_MISMATCH",
          "error_code": "NBOFTXS_MISMATCH",
          "severity": "Fatal",
          "description": "GrpHdr/NbOfTxs does not equal the actual number of DrctDbtTxInf blocks. Error code X00062.",
          "affected_tags": ["GrpHdr/NbOfTxs"],
          "possible_fixes": [
            "Count all DrctDbtTxInf elements in the message and set NbOfTxs to that count.",
            "For CBPR+, ensure only one DrctDbtTxInf block exists and NbOfTxs = 1."
          ]
        },
        {
          "error_id": "NBOFTXS_MULTI_TX",
          "error_code": "CBPR_SINGLE_TX",
          "severity": "Fatal",
          "description": "CBPR+ requires single transactions only; multiple DrctDbtTxInf blocks found.",
          "affected_tags": ["GrpHdr/NbOfTxs"],
          "possible_fixes": [
            "Split the message into separate pacs.003 messages, one per transaction.",
            "Remove extra DrctDbtTxInf blocks if they are duplicates."
          ]
        },
        {
          "error_id": "NBOFTXS_NON_NUMERIC",
          "error_code": "ID_LENGTH_ERROR",
          "severity": "Fatal",
          "description": "NbOfTxs contains non-numeric characters.",
          "affected_tags": ["GrpHdr/NbOfTxs"],
          "possible_fixes": [
            "Replace with a numeric integer string (max 15 digits)."
          ]
        }
      ]
    },

    {
      "tag": "GrpHdr/CtrlSum",
      "xml_element": "CtrlSum",
      "xpath": "/Document/FIToFICstmrDrctDbt/GrpHdr/CtrlSum",
      "occurrence": "[0..1]",
      "mandatory": False,
      "datatype": "DecimalNumber",
      "description": "Control sum. If present, must equal the sum of all DrctDbtTxInf/IntrBkSttlmAmt values.",
      "errors": [
        {
          "error_id": "CTRLSUM_MISMATCH",
          "error_code": "CTRL_SUM_MISMATCH",
          "severity": "Fatal",
          "description": "CtrlSum does not equal the sum of all IntrBkSttlmAmt values.",
          "affected_tags": ["GrpHdr/CtrlSum", "DrctDbtTxInf/IntrBkSttlmAmt"],
          "possible_fixes": [
            "Sum all IntrBkSttlmAmt values and set CtrlSum to the result.",
            "If only one transaction (CBPR+), CtrlSum should equal the single IntrBkSttlmAmt.",
            "Remove CtrlSum if not required to avoid mismatch errors."
          ]
        },
        {
          "error_id": "CTRLSUM_INVALID_FORMAT",
          "error_code": "INVALID_CHARSETS",
          "severity": "Fatal",
          "description": "CtrlSum contains non-numeric or invalid decimal value.",
          "affected_tags": ["GrpHdr/CtrlSum"],
          "possible_fixes": [
            "Use a valid decimal number format.",
            "Maximum 18 digits total with up to 5 fraction digits."
          ]
        }
      ]
    },

    {
      "tag": "GrpHdr/SttlmInf/SttlmMtd",
      "xml_element": "SttlmMtd",
      "xpath": "/Document/FIToFICstmrDrctDbt/GrpHdr/SttlmInf/SttlmMtd",
      "occurrence": "[1..1]",
      "mandatory": True,
      "valid_values": ["INDA", "INGA", "COVE", "CLRG"],
      "preferred": "INGA",
      "description": "Settlement method. INGA = settled on books of instructed agent (CBPR+ preferred).",
      "errors": [
        {
          "error_id": "STTLMMTD_MISSING",
          "error_code": "MISSING_MANDATORY_FIELD",
          "severity": "Fatal",
          "description": "SttlmMtd is absent from SttlmInf.",
          "affected_tags": ["GrpHdr/SttlmInf/SttlmMtd"],
          "possible_fixes": [
            "Insert <SttlmMtd>INGA</SttlmMtd> as first child of SttlmInf."
          ]
        },
        {
          "error_id": "STTLMMTD_INVALID_CODE",
          "error_code": "CHRGBR_INVALID",
          "severity": "Fatal",
          "description": "SttlmMtd contains a value not in the allowed codelist.",
          "affected_tags": ["GrpHdr/SttlmInf/SttlmMtd"],
          "possible_fixes": [
            "Replace with one of: INDA, INGA, COVE, CLRG.",
            "Use INGA for standard CBPR+ interbank settlement."
          ]
        }
      ]
    },

    {
      "tag": "GrpHdr/InstgAgt",
      "xml_element": "InstgAgt",
      "xpath": "/Document/FIToFICstmrDrctDbt/GrpHdr/InstgAgt",
      "occurrence": "[0..1]",
      "mandatory": False,
      "description": "GrpHdr-level instructing agent. If present, DrctDbtTxInf/InstgAgt must be absent (R5).",
      "errors": [
        {
          "error_id": "GRPHDR_INSTGAGT_CONFLICT",
          "error_code": "X00007",
          "severity": "Fatal",
          "description": "Both GrpHdr/InstgAgt and DrctDbtTxInf/InstgAgt are present; this violates R5.",
          "affected_tags": ["GrpHdr/InstgAgt", "DrctDbtTxInf/InstgAgt"],
          "possible_fixes": [
            "Remove GrpHdr/InstgAgt if agent is already specified at transaction level.",
            "Remove DrctDbtTxInf/InstgAgt if agent is specified at group level."
          ]
        }
      ]
    },

    {
      "tag": "GrpHdr/InstdAgt",
      "xml_element": "InstdAgt",
      "xpath": "/Document/FIToFICstmrDrctDbt/GrpHdr/InstdAgt",
      "occurrence": "[0..1]",
      "mandatory": False,
      "description": "GrpHdr-level instructed agent. If present, DrctDbtTxInf/InstdAgt must be absent (R4).",
      "errors": [
        {
          "error_id": "GRPHDR_INSTDAGT_CONFLICT",
          "error_code": "X00008",
          "severity": "Fatal",
          "description": "Both GrpHdr/InstdAgt and DrctDbtTxInf/InstdAgt are present; this violates R4.",
          "affected_tags": ["GrpHdr/InstdAgt", "DrctDbtTxInf/InstdAgt"],
          "possible_fixes": [
            "Remove GrpHdr/InstdAgt if agent is specified at transaction level.",
            "Remove DrctDbtTxInf/InstdAgt if agent is specified at group level."
          ]
        }
      ]
    },

    {
      "tag": "DrctDbtTxInf",
      "xml_element": "DrctDbtTxInf",
      "xpath": "/Document/FIToFICstmrDrctDbt/DrctDbtTxInf",
      "occurrence": "[1..n]",
      "mandatory": True,
      "note": "CBPR+ restricts to a single occurrence (NbOfTxs = 1).",
      "description": "Direct Debit Transaction Information. Root transaction block of the FIToFICustomerDirectDebit message. Contains payment identification, settlement amount, mandate information, debtor/creditor parties, agents, and remittance.",
      "errors": [
        {
          "error_id": "DRCTDBTTXINF_MISSING",
          "error_code": "MISSING_MANDATORY_FIELD",
          "severity": "Fatal",
          "description": "DrctDbtTxInf block is absent from FIToFICstmrDrctDbt.",
          "affected_tags": ["DrctDbtTxInf"],
          "possible_fixes": [
            "Insert a complete <DrctDbtTxInf> block containing PmtId, IntrBkSttlmAmt, ChrgBr, DrctDbtTx, Cdtr, CdtrAgt, Dbtr, DbtrAcct and DbtrAgt."
          ]
        },
        {
          "error_id": "DRCTDBTTXINF_MULTIPLE",
          "error_code": "CBPR_SINGLE_TX",
          "severity": "Fatal",
          "description": "Multiple DrctDbtTxInf blocks present; CBPR+ permits a single transaction only.",
          "affected_tags": ["DrctDbtTxInf", "GrpHdr/NbOfTxs"],
          "possible_fixes": [
            "Retain a single DrctDbtTxInf block and set GrpHdr/NbOfTxs to 1.",
            "Split additional transactions into separate pacs.003 messages."
          ]
        }
      ]
    },

    {
      "tag": "DrctDbtTxInf/PmtId",
      "xml_element": "PmtId",
      "xpath": "/Document/FIToFICstmrDrctDbt/DrctDbtTxInf/PmtId",
      "occurrence": "[1..1]",
      "mandatory": True,
      "description": "Payment identification block. Contains InstrId, EndToEndId, TxId, and UETR (mandatory under CBPR+).",
      "errors": [
        {
          "error_id": "PMTID_MISSING",
          "error_code": "MISSING_MANDATORY_FIELD",
          "severity": "Fatal",
          "description": "PmtId block is absent from DrctDbtTxInf.",
          "affected_tags": ["DrctDbtTxInf/PmtId"],
          "possible_fixes": [
            "Insert a complete <PmtId> block containing InstrId, EndToEndId, TxId, and UETR."
          ]
        }
      ]
    },

    {
      "tag": "DrctDbtTxInf/PmtId/EndToEndId",
      "xml_element": "EndToEndId",
      "xpath": "/Document/FIToFICstmrDrctDbt/DrctDbtTxInf/PmtId/EndToEndId",
      "occurrence": "[1..1]",
      "mandatory": True,
      "datatype": "Max35Text",
      "max_length": 35,
      "description": "End-to-end identification assigned by the initiating party (creditor). Must be passed through the chain unchanged. 'NOTPROVIDED' is allowed.",
      "errors": [
        {
          "error_id": "ENDTOENDID_MISSING",
          "error_code": "MISSING_MANDATORY_FIELD",
          "severity": "Fatal",
          "description": "EndToEndId is absent from PmtId.",
          "affected_tags": ["DrctDbtTxInf/PmtId/EndToEndId"],
          "possible_fixes": [
            "Insert <EndToEndId>NOTPROVIDED</EndToEndId> if the value is unknown.",
            "Otherwise insert <EndToEndId>{originator's end-to-end reference, max 35 chars}</EndToEndId>."
          ]
        },
        {
          "error_id": "ENDTOENDID_TOO_LONG",
          "error_code": "ID_LENGTH_ERROR",
          "severity": "Fatal",
          "description": "EndToEndId exceeds 35 characters.",
          "affected_tags": ["DrctDbtTxInf/PmtId/EndToEndId"],
          "possible_fixes": [
            "Truncate to 35 characters maximum.",
            "Use 'NOTPROVIDED' if the original reference cannot be preserved within length limit."
          ]
        }
      ]
    },

    {
      "tag": "DrctDbtTxInf/PmtId/TxId",
      "xml_element": "TxId",
      "xpath": "/Document/FIToFICstmrDrctDbt/DrctDbtTxInf/PmtId/TxId",
      "occurrence": "[1..1]",
      "mandatory": True,
      "datatype": "Max35Text",
      "max_length": 35,
      "description": "Transaction identification assigned by the instructing agent. Unique reference for the direct debit transaction.",
      "errors": [
        {
          "error_id": "TXID_MISSING",
          "error_code": "MISSING_MANDATORY_FIELD",
          "severity": "Fatal",
          "description": "TxId is absent from PmtId.",
          "affected_tags": ["DrctDbtTxInf/PmtId/TxId"],
          "possible_fixes": [
            "Insert <TxId>{unique transaction reference, max 35 chars}</TxId>."
          ]
        },
        {
          "error_id": "TXID_TOO_LONG",
          "error_code": "ID_LENGTH_ERROR",
          "severity": "Fatal",
          "description": "TxId exceeds 35 characters.",
          "affected_tags": ["DrctDbtTxInf/PmtId/TxId"],
          "possible_fixes": [
            "Truncate to 35 characters maximum preserving the unique prefix."
          ]
        }
      ]
    },

    {
      "tag": "DrctDbtTxInf/PmtId/UETR",
      "xml_element": "UETR",
      "xpath": "/Document/FIToFICstmrDrctDbt/DrctDbtTxInf/PmtId/UETR",
      "occurrence": "[1..1]",
      "mandatory": True,
      "datatype": "UUIDv4Identifier",
      "length": 36,
      "pattern": "[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
      "description": "Unique End-to-End Transaction Reference. UUID v4 format. Mandatory under CBPR+ for pacs.003.",
      "errors": [
        {
          "error_id": "UETR_MISSING",
          "error_code": "MISSING_UETR",
          "severity": "Fatal",
          "description": "UETR is absent from PmtId; mandatory under CBPR+.",
          "affected_tags": ["DrctDbtTxInf/PmtId/UETR"],
          "possible_fixes": [
            "Insert <UETR>{valid UUID v4}</UETR> after TxId in PmtId.",
            "Generate a new UUID v4: lowercase hex, format xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx where y in {8,9,a,b}."
          ]
        },
        {
          "error_id": "UETR_FORMAT_ERROR",
          "error_code": "UETR_FORMAT_ERROR",
          "severity": "Fatal",
          "description": "UETR is not a valid UUID v4 (wrong format, uppercase, wrong version digit, invalid variant).",
          "affected_tags": ["DrctDbtTxInf/PmtId/UETR"],
          "possible_fixes": [
            "Ensure UETR is lowercase hex in format xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx.",
            "The 13th character (version) must be '4'.",
            "The 17th character (variant) must be 8, 9, a, or b.",
            "Total length must be exactly 36 characters including hyphens.",
            "Replace with a freshly generated UUID v4 if existing value is corrupt."
          ]
        },
        {
          "error_id": "UETR_UPPERCASE",
          "error_code": "UETR_FORMAT_ERROR",
          "severity": "Fatal",
          "description": "UETR contains uppercase hexadecimal characters; must be lowercase.",
          "affected_tags": ["DrctDbtTxInf/PmtId/UETR"],
          "possible_fixes": [
            "Convert all hex characters in UETR to lowercase."
          ]
        }
      ]
    },

    {
      "tag": "DrctDbtTxInf/IntrBkSttlmAmt",
      "xml_element": "IntrBkSttlmAmt",
      "xpath": "/Document/FIToFICstmrDrctDbt/DrctDbtTxInf/IntrBkSttlmAmt",
      "occurrence": "[1..1]",
      "mandatory": True,
      "datatype": "ActiveCurrencyAndAmount",
      "description": "Interbank settlement amount collected from the debtor with currency attribute (Ccy). Amount > 0. ISO 4217 currency code.",
      "errors": [
        {
          "error_id": "INTRBKSTTLMAMT_MISSING",
          "error_code": "MISSING_MANDATORY_FIELD",
          "severity": "Fatal",
          "description": "IntrBkSttlmAmt element is absent.",
          "affected_tags": ["DrctDbtTxInf/IntrBkSttlmAmt"],
          "possible_fixes": [
            "Insert <IntrBkSttlmAmt Ccy=\"{ISO4217_currency}\">{amount}</IntrBkSttlmAmt>.",
            "Ensure Ccy attribute is a valid 3-letter ISO 4217 code (e.g. USD, EUR, GBP)."
          ]
        },
        {
          "error_id": "INTRBKSTTLMAMT_MISSING_CCY",
          "error_code": "INVALID_CHARSETS",
          "severity": "Fatal",
          "description": "IntrBkSttlmAmt is present but the Ccy attribute is missing.",
          "affected_tags": ["DrctDbtTxInf/IntrBkSttlmAmt"],
          "possible_fixes": [
            "Add Ccy attribute with a valid ISO 4217 3-letter currency code."
          ]
        },
        {
          "error_id": "INTRBKSTTLMAMT_INVALID_CCY",
          "error_code": "INVALID_CHARSETS",
          "severity": "Fatal",
          "description": "IntrBkSttlmAmt Ccy attribute is not a valid ISO 4217 currency code.",
          "affected_tags": ["DrctDbtTxInf/IntrBkSttlmAmt"],
          "possible_fixes": [
            "Replace Ccy attribute with a valid 3-letter ISO 4217 code (e.g. USD, EUR, GBP, JPY).",
            "Currency codes are always 3 uppercase letters."
          ]
        },
        {
          "error_id": "INTRBKSTTLMAMT_ZERO_OR_NEGATIVE",
          "error_code": "INVALID_CHARSETS",
          "severity": "Fatal",
          "description": "IntrBkSttlmAmt amount is zero or negative.",
          "affected_tags": ["DrctDbtTxInf/IntrBkSttlmAmt"],
          "possible_fixes": [
            "Replace with a positive decimal amount greater than zero.",
            "Maximum: 18 digits total, 5 fraction digits."
          ]
        },
        {
          "error_id": "TTLINTRBKSTTLMAMT_CCY_MISMATCH",
          "error_code": "X00042",
          "severity": "Fatal",
          "description": "IntrBkSttlmAmt currency differs from GrpHdr/TtlIntrBkSttlmAmt currency.",
          "affected_tags": ["DrctDbtTxInf/IntrBkSttlmAmt", "GrpHdr/TtlIntrBkSttlmAmt"],
          "possible_fixes": [
            "Ensure all IntrBkSttlmAmt currencies match the TtlIntrBkSttlmAmt currency.",
            "Remove TtlIntrBkSttlmAmt from GrpHdr if currency alignment is not possible."
          ]
        }
      ]
    },

    {
      "tag": "DrctDbtTxInf/IntrBkSttlmDt",
      "xml_element": "IntrBkSttlmDt",
      "xpath": "/Document/FIToFICstmrDrctDbt/DrctDbtTxInf/IntrBkSttlmDt",
      "occurrence": "[0..1]",
      "mandatory": True,
      "note": "Mandatory at transaction level when GrpHdr/IntrBkSttlmDt is absent (R9). Cannot coexist with GrpHdr/IntrBkSttlmDt (R8).",
      "datatype": "ISODate",
      "description": "Interbank settlement date. ISO 8601 YYYY-MM-DD. Must be today or a future business day.",
      "errors": [
        {
          "error_id": "INTRBKSTTLMDT_MISSING",
          "error_code": "X00290",
          "severity": "Fatal",
          "description": "IntrBkSttlmDt is absent from DrctDbtTxInf and GrpHdr/IntrBkSttlmDt is also absent; violates R9.",
          "affected_tags": ["DrctDbtTxInf/IntrBkSttlmDt"],
          "possible_fixes": [
            "Insert <IntrBkSttlmDt>{today's date as YYYY-MM-DD}</IntrBkSttlmDt> in DrctDbtTxInf.",
            "Alternatively insert IntrBkSttlmDt at GrpHdr level; if so, remove it from DrctDbtTxInf."
          ]
        },
        {
          "error_id": "INTRBKSTTLMDT_PAST_DATE",
          "error_code": "PAST_DATE_ERROR",
          "severity": "Fatal",
          "description": "IntrBkSttlmDt is in the past.",
          "affected_tags": ["DrctDbtTxInf/IntrBkSttlmDt"],
          "possible_fixes": [
            "Replace with today's date (YYYY-MM-DD) or the next valid business day.",
            "Do NOT use 'Z', time components, or offsets — date only."
          ]
        },
        {
          "error_id": "INTRBKSTTLMDT_BOTH_LEVELS",
          "error_code": "X00045",
          "severity": "Fatal",
          "description": "IntrBkSttlmDt is present at both GrpHdr and DrctDbtTxInf level; violates R8.",
          "affected_tags": ["GrpHdr/IntrBkSttlmDt", "DrctDbtTxInf/IntrBkSttlmDt"],
          "possible_fixes": [
            "Remove DrctDbtTxInf/IntrBkSttlmDt if GrpHdr/IntrBkSttlmDt is the intended location.",
            "Remove GrpHdr/IntrBkSttlmDt if DrctDbtTxInf/IntrBkSttlmDt is the intended location."
          ]
        },
        {
          "error_id": "INTRBKSTTLMDT_WRONG_FORMAT",
          "error_code": "PAST_DATE_ERROR",
          "severity": "Fatal",
          "description": "IntrBkSttlmDt is not in YYYY-MM-DD format (e.g. includes time, Z suffix, or slashes).",
          "affected_tags": ["DrctDbtTxInf/IntrBkSttlmDt"],
          "possible_fixes": [
            "Use date-only format YYYY-MM-DD with no time component."
          ]
        }
      ]
    },

    {
      "tag": "DrctDbtTxInf/ReqdColltnDt",
      "xml_element": "ReqdColltnDt",
      "xpath": "/Document/FIToFICstmrDrctDbt/DrctDbtTxInf/ReqdColltnDt",
      "occurrence": "[0..1]",
      "mandatory": False,
      "datatype": "ISODate",
      "description": "Requested Collection Date. Date on which the creditor requests the amount to be collected from the debtor. ISO 8601 YYYY-MM-DD.",
      "errors": [
        {
          "error_id": "REQDCOLLTNDT_WRONG_FORMAT",
          "error_code": "PAST_DATE_ERROR",
          "severity": "Fatal",
          "description": "ReqdColltnDt is not in YYYY-MM-DD format (e.g. includes time, Z suffix, or slashes).",
          "affected_tags": ["DrctDbtTxInf/ReqdColltnDt"],
          "possible_fixes": [
            "Use date-only format YYYY-MM-DD with no time component."
          ]
        },
        {
          "error_id": "REQDCOLLTNDT_PAST_DATE",
          "error_code": "PAST_DATE_ERROR",
          "severity": "Warning",
          "description": "ReqdColltnDt is in the past.",
          "affected_tags": ["DrctDbtTxInf/ReqdColltnDt"],
          "possible_fixes": [
            "Replace with today's date (YYYY-MM-DD) or a future collection date."
          ]
        }
      ]
    },

    {
      "tag": "DrctDbtTxInf/ChrgBr",
      "xml_element": "ChrgBr",
      "xpath": "/Document/FIToFICstmrDrctDbt/DrctDbtTxInf/ChrgBr",
      "occurrence": "[1..1]",
      "mandatory": True,
      "valid_values": ["SLEV", "SHAR", "CRED", "DEBT"],
      "preferred": "SLEV",
      "description": "Charge bearer. Specifies who bears the charges. SLEV = follow service level (CBPR+ default).",
      "errors": [
        {
          "error_id": "CHRGBR_MISSING",
          "error_code": "MISSING_MANDATORY_FIELD",
          "severity": "Fatal",
          "description": "ChrgBr is absent from DrctDbtTxInf.",
          "affected_tags": ["DrctDbtTxInf/ChrgBr"],
          "possible_fixes": [
            "Insert <ChrgBr>SLEV</ChrgBr> in DrctDbtTxInf (CBPR+ default).",
            "Position ChrgBr after XchgRate/InstdAmt and before ChrgsInf per XSD element order."
          ]
        },
        {
          "error_id": "CHRGBR_INVALID_CODE",
          "error_code": "CHRGBR_INVALID",
          "severity": "Fatal",
          "description": "ChrgBr contains an invalid code.",
          "affected_tags": ["DrctDbtTxInf/ChrgBr"],
          "possible_fixes": [
            "Replace with one of: SLEV, SHAR, CRED, DEBT.",
            "Default CBPR+ value is SLEV."
          ]
        },
        {
          "error_id": "CHRGBR_CRED_WITHOUT_CHRGSINF",
          "error_code": "CHRGSINF_CCY_MISMATCH",
          "severity": "Fatal",
          "description": "ChrgBr = CRED but ChrgsInf block is absent.",
          "affected_tags": ["DrctDbtTxInf/ChrgBr", "DrctDbtTxInf/ChrgsInf"],
          "possible_fixes": [
            "Insert a ChrgsInf block with Amt and Agt elements when ChrgBr = CRED.",
            "Alternatively change ChrgBr to SLEV to avoid the requirement."
          ]
        }
      ]
    },

    {
      "tag": "DrctDbtTxInf/InstdAmt",
      "xml_element": "InstdAmt",
      "xpath": "/Document/FIToFICstmrDrctDbt/DrctDbtTxInf/InstdAmt",
      "occurrence": "[0..1]",
      "mandatory": False,
      "datatype": "ActiveOrHistoricCurrencyAndAmount",
      "description": "Instructed amount. Required when XchgRate is present. Currency may differ from IntrBkSttlmAmt.",
      "errors": [
        {
          "error_id": "INSTDAMT_MISSING_WHEN_XCHGRATE",
          "error_code": "XCHGRATE_REQUIRED",
          "severity": "Fatal",
          "description": "XchgRate is present but InstdAmt is absent; or InstdAmt currency differs from IntrBkSttlmAmt currency but XchgRate is missing.",
          "affected_tags": ["DrctDbtTxInf/InstdAmt", "DrctDbtTxInf/XchgRate"],
          "possible_fixes": [
            "When InstdAmt.Ccy != IntrBkSttlmAmt.Ccy, insert <XchgRate>{rate}</XchgRate> in DrctDbtTxInf.",
            "When currencies are the same, remove XchgRate entirely."
          ]
        },
        {
          "error_id": "INSTDAMT_ZERO_OR_NEGATIVE",
          "error_code": "INVALID_CHARSETS",
          "severity": "Fatal",
          "description": "InstdAmt is zero or negative.",
          "affected_tags": ["DrctDbtTxInf/InstdAmt"],
          "possible_fixes": [
            "Set InstdAmt to a positive decimal amount greater than zero."
          ]
        }
      ]
    },

    {
      "tag": "DrctDbtTxInf/XchgRate",
      "xml_element": "XchgRate",
      "xpath": "/Document/FIToFICstmrDrctDbt/DrctDbtTxInf/XchgRate",
      "occurrence": "[0..1]",
      "mandatory": False,
      "datatype": "BaseOneRate",
      "description": "Exchange rate. Required when InstdAmt currency != IntrBkSttlmAmt currency. Forbidden when currencies are the same.",
      "errors": [
        {
          "error_id": "XCHGRATE_REQUIRED",
          "error_code": "XCHGRATE_REQUIRED",
          "severity": "Fatal",
          "description": "InstdAmt currency differs from IntrBkSttlmAmt currency but XchgRate is absent.",
          "affected_tags": ["DrctDbtTxInf/XchgRate", "DrctDbtTxInf/InstdAmt", "DrctDbtTxInf/IntrBkSttlmAmt"],
          "possible_fixes": [
            "Insert <XchgRate>{rate}</XchgRate> inside DrctDbtTxInf after InstdAmt.",
            "Provide the actual exchange rate if known."
          ]
        },
        {
          "error_id": "XCHGRATE_FORBIDDEN",
          "error_code": "XCHGRATE_FORBIDDEN",
          "severity": "Fatal",
          "description": "InstdAmt currency equals IntrBkSttlmAmt currency but XchgRate is present.",
          "affected_tags": ["DrctDbtTxInf/XchgRate"],
          "possible_fixes": [
            "Remove the XchgRate element entirely when currencies are the same."
          ]
        },
        {
          "error_id": "XCHGRATE_ZERO_OR_NEGATIVE",
          "error_code": "INVALID_CHARSETS",
          "severity": "Fatal",
          "description": "XchgRate value is zero or negative.",
          "affected_tags": ["DrctDbtTxInf/XchgRate"],
          "possible_fixes": [
            "Replace with a positive decimal exchange rate greater than zero."
          ]
        }
      ]
    },

    {
      "tag": "DrctDbtTxInf/DrctDbtTx",
      "xml_element": "DrctDbtTx",
      "xpath": "/Document/FIToFICstmrDrctDbt/DrctDbtTxInf/DrctDbtTx",
      "occurrence": "[0..1]",
      "mandatory": True,
      "note": "Mandatory under CBPR+ direct debit usage. Container for MndtRltdInf, CdtrSchmeId, PreNtfctnId and PreNtfctnDt.",
      "description": "Direct Debit Transaction. Carries the mandate and creditor scheme details that authorise collection from the debtor.",
      "errors": [
        {
          "error_id": "DRCTDBTTX_MISSING",
          "error_code": "MISSING_MANDATORY_FIELD",
          "severity": "Fatal",
          "description": "DrctDbtTx block is absent from DrctDbtTxInf.",
          "affected_tags": ["DrctDbtTxInf/DrctDbtTx"],
          "possible_fixes": [
            "Insert a <DrctDbtTx> block containing MndtRltdInf (with MndtId and DtOfSgntr) and CdtrSchmeId."
          ]
        },
        {
          "error_id": "DRCTDBTTX_MISSING_MNDTRLTDINF",
          "error_code": "X00310",
          "severity": "Fatal",
          "description": "DrctDbtTx is present but MndtRltdInf is absent; violates R12.",
          "affected_tags": ["DrctDbtTxInf/DrctDbtTx/MndtRltdInf"],
          "possible_fixes": [
            "Insert <MndtRltdInf><MndtId>{mandate id}</MndtId><DtOfSgntr>{YYYY-MM-DD}</DtOfSgntr></MndtRltdInf> inside DrctDbtTx."
          ]
        }
      ]
    },

    {
      "tag": "DrctDbtTxInf/DrctDbtTx/MndtRltdInf",
      "xml_element": "MndtRltdInf",
      "xpath": "/Document/FIToFICstmrDrctDbt/DrctDbtTxInf/DrctDbtTx/MndtRltdInf",
      "occurrence": "[0..1]",
      "mandatory": True,
      "note": "Mandatory when DrctDbtTx is present (R12 / CBPR_MNDT_REQUIRED).",
      "description": "Mandate Related Information. Provides the mandate reference and date of signature authorising the direct debit collection.",
      "errors": [
        {
          "error_id": "MNDTRLTDINF_MISSING",
          "error_code": "X00310",
          "severity": "Fatal",
          "description": "MndtRltdInf is absent while DrctDbtTx is present.",
          "affected_tags": ["DrctDbtTxInf/DrctDbtTx/MndtRltdInf"],
          "possible_fixes": [
            "Insert <MndtRltdInf><MndtId>{mandate id}</MndtId><DtOfSgntr>{YYYY-MM-DD}</DtOfSgntr></MndtRltdInf> inside DrctDbtTx."
          ]
        }
      ]
    },

    {
      "tag": "DrctDbtTxInf/DrctDbtTx/MndtRltdInf/MndtId",
      "xml_element": "MndtId",
      "xpath": "/Document/FIToFICstmrDrctDbt/DrctDbtTxInf/DrctDbtTx/MndtRltdInf/MndtId",
      "occurrence": "[0..1]",
      "mandatory": True,
      "note": "Required under CBPR+ when MndtRltdInf is present.",
      "datatype": "Max35Text",
      "max_length": 35,
      "description": "Mandate Identification. Unique reference of the mandate signed by the debtor authorising the creditor to collect.",
      "errors": [
        {
          "error_id": "MNDTID_MISSING",
          "error_code": "MISSING_MANDATORY_FIELD",
          "severity": "Fatal",
          "description": "MndtId is absent from MndtRltdInf.",
          "affected_tags": ["DrctDbtTxInf/DrctDbtTx/MndtRltdInf/MndtId"],
          "possible_fixes": [
            "Insert <MndtId>{mandate reference, max 35 chars}</MndtId> as first child of MndtRltdInf."
          ]
        },
        {
          "error_id": "MNDTID_TOO_LONG",
          "error_code": "ID_LENGTH_ERROR",
          "severity": "Fatal",
          "description": "MndtId exceeds 35 characters.",
          "affected_tags": ["DrctDbtTxInf/DrctDbtTx/MndtRltdInf/MndtId"],
          "possible_fixes": [
            "Truncate MndtId to maximum 35 characters preserving the unique reference."
          ]
        },
        {
          "error_id": "MNDTID_INVALID_CHARS",
          "error_code": "INVALID_CHARSETS",
          "severity": "Fatal",
          "description": "MndtId contains characters outside the FIN-X character set.",
          "affected_tags": ["DrctDbtTxInf/DrctDbtTx/MndtRltdInf/MndtId"],
          "possible_fixes": [
            "Allowed: A-Z a-z 0-9 / - ? : ( ) . , ' + (no leading or trailing spaces).",
            "Replace special/accented characters with ASCII equivalents."
          ]
        }
      ]
    },

    {
      "tag": "DrctDbtTxInf/DrctDbtTx/MndtRltdInf/DtOfSgntr",
      "xml_element": "DtOfSgntr",
      "xpath": "/Document/FIToFICstmrDrctDbt/DrctDbtTxInf/DrctDbtTx/MndtRltdInf/DtOfSgntr",
      "occurrence": "[0..1]",
      "mandatory": True,
      "note": "Required under CBPR+ when MndtRltdInf is present.",
      "datatype": "ISODate",
      "description": "Date of Signature. Date on which the debtor signed the direct debit mandate. ISO 8601 YYYY-MM-DD; must not be a future date.",
      "errors": [
        {
          "error_id": "DTOFSGNTR_MISSING",
          "error_code": "MISSING_MANDATORY_FIELD",
          "severity": "Fatal",
          "description": "DtOfSgntr is absent from MndtRltdInf.",
          "affected_tags": ["DrctDbtTxInf/DrctDbtTx/MndtRltdInf/DtOfSgntr"],
          "possible_fixes": [
            "Insert <DtOfSgntr>{mandate signature date as YYYY-MM-DD}</DtOfSgntr> inside MndtRltdInf."
          ]
        },
        {
          "error_id": "DTOFSGNTR_WRONG_FORMAT",
          "error_code": "PAST_DATE_ERROR",
          "severity": "Fatal",
          "description": "DtOfSgntr is not in YYYY-MM-DD format (e.g. includes time, Z suffix, or slashes).",
          "affected_tags": ["DrctDbtTxInf/DrctDbtTx/MndtRltdInf/DtOfSgntr"],
          "possible_fixes": [
            "Use date-only format YYYY-MM-DD with no time component."
          ]
        },
        {
          "error_id": "DTOFSGNTR_FUTURE_DATE",
          "error_code": "PAST_DATE_ERROR",
          "severity": "Fatal",
          "description": "DtOfSgntr is in the future; a mandate cannot be signed after the collection.",
          "affected_tags": ["DrctDbtTxInf/DrctDbtTx/MndtRltdInf/DtOfSgntr"],
          "possible_fixes": [
            "Replace with the actual mandate signature date, which must be today or in the past."
          ]
        }
      ]
    },

    {
      "tag": "DrctDbtTxInf/DrctDbtTx/CdtrSchmeId",
      "xml_element": "CdtrSchmeId",
      "xpath": "/Document/FIToFICstmrDrctDbt/DrctDbtTxInf/DrctDbtTx/CdtrSchmeId",
      "occurrence": "[0..1]",
      "mandatory": False,
      "description": "Creditor Scheme Identification. Identifies the creditor under a direct debit scheme via Id/PrvtId/Othr/Id and SchmeNm. Id and SchmeNm must be consistent when present.",
      "errors": [
        {
          "error_id": "CDTRSCHMEID_ID_MISSING",
          "error_code": "MISSING_EXPECTED_ELEMENT",
          "severity": "Fatal",
          "description": "CdtrSchmeId is present but the scheme identifier (Id/PrvtId/Othr/Id) is absent.",
          "affected_tags": ["DrctDbtTxInf/DrctDbtTx/CdtrSchmeId/Id/PrvtId/Othr/Id"],
          "possible_fixes": [
            "Insert <Id><PrvtId><Othr><Id>{creditor scheme identifier}</Id></Othr></PrvtId></Id> inside CdtrSchmeId.",
            "Or remove the empty CdtrSchmeId block."
          ]
        },
        {
          "error_id": "CDTRSCHMEID_SCHMENM_INCONSISTENT",
          "error_code": "SCHEMENAME_INVALID",
          "severity": "Fatal",
          "description": "CdtrSchmeId/Id/PrvtId/Othr/SchmeNm Cd and Prtry are both present; mutually exclusive.",
          "affected_tags": ["DrctDbtTxInf/DrctDbtTx/CdtrSchmeId/Id/PrvtId/Othr/SchmeNm/Cd", "DrctDbtTxInf/DrctDbtTx/CdtrSchmeId/Id/PrvtId/Othr/SchmeNm/Prtry"],
          "possible_fixes": [
            "Remove SchmeNm/Prtry if SchmeNm/Cd is the correct choice.",
            "Remove SchmeNm/Cd if SchmeNm/Prtry is the correct choice."
          ]
        }
      ]
    },

    {
      "tag": "DrctDbtTxInf/InstgAgt",
      "xml_element": "InstgAgt",
      "xpath": "/Document/FIToFICstmrDrctDbt/DrctDbtTxInf/InstgAgt",
      "occurrence": "[0..1]",
      "mandatory": True,
      "note": "Mandatory at transaction level when absent at GrpHdr level. BICFI mandatory on SWIFT.",
      "description": "Instructing agent. Must have valid BICFI. Must match AppHdr/Fr.",
      "errors": [
        {
          "error_id": "INSTGAGT_MISSING",
          "error_code": "MISSING_MANDATORY_FIELD",
          "severity": "Fatal",
          "description": "InstgAgt is absent from DrctDbtTxInf (and not present in GrpHdr).",
          "affected_tags": ["DrctDbtTxInf/InstgAgt"],
          "possible_fixes": [
            "Insert <InstgAgt><FinInstnId><BICFI>{instructing_bic}</BICFI></FinInstnId></InstgAgt> in DrctDbtTxInf.",
            "Use the same BIC as AppHdr/Fr."
          ]
        },
        {
          "error_id": "INSTGAGT_INVALID_BIC",
          "error_code": "INVALID_BICFI",
          "severity": "Fatal",
          "description": "InstgAgt/FinInstnId/BICFI is invalid or malformed.",
          "affected_tags": ["DrctDbtTxInf/InstgAgt/FinInstnId/BICFI"],
          "possible_fixes": [
            "Replace with a valid BICFI: 8 or 11 uppercase alphanumeric characters.",
            "Pattern: [A-Z]{6}[A-Z2-9][A-NP-Z0-9]([A-Z0-9]{3})?"
          ]
        },
        {
          "error_id": "INSTGAGT_NM_PSTLADR_WITH_BIC",
          "error_code": "BICFI_EXCLUSIVE",
          "severity": "Fatal",
          "description": "CBPR+: BICFI is present but Nm and/or PstlAdr are also present in the same FinInstnId.",
          "affected_tags": ["DrctDbtTxInf/InstgAgt/FinInstnId/Nm", "DrctDbtTxInf/InstgAgt/FinInstnId/PstlAdr"],
          "possible_fixes": [
            "Remove Nm and PstlAdr from FinInstnId when BICFI is present.",
            "BICFI takes precedence; Nm/PstlAdr are not allowed alongside BICFI under CBPR+."
          ]
        },
        {
          "error_id": "INSTGAGT_EMPTY_FININSTNID",
          "error_code": "EMPTY_FININSTNID",
          "severity": "Fatal",
          "description": "FinInstnId inside InstgAgt is present but completely empty.",
          "affected_tags": ["DrctDbtTxInf/InstgAgt/FinInstnId"],
          "possible_fixes": [
            "Add a BICFI child element to FinInstnId.",
            "If no BICFI is available, provide Nm and PstlAdr (not both)."
          ]
        },
        {
          "error_id": "INSTGAGT_BRNCHID_PRESENT",
          "error_code": "CBPR_BRANCH_REMOVED",
          "severity": "Fatal",
          "description": "BrnchId is present in InstgAgt; removed under CBPR+ SR2025.",
          "affected_tags": ["DrctDbtTxInf/InstgAgt/BrnchId"],
          "possible_fixes": [
            "Remove the BrnchId element from InstgAgt entirely."
          ]
        }
      ]
    },

    {
      "tag": "DrctDbtTxInf/InstdAgt",
      "xml_element": "InstdAgt",
      "xpath": "/Document/FIToFICstmrDrctDbt/DrctDbtTxInf/InstdAgt",
      "occurrence": "[0..1]",
      "mandatory": True,
      "note": "Mandatory at transaction level when absent at GrpHdr level.",
      "description": "Instructed agent. Must have valid BICFI. Must match AppHdr/To.",
      "errors": [
        {
          "error_id": "INSTDAGT_MISSING",
          "error_code": "MISSING_MANDATORY_FIELD",
          "severity": "Fatal",
          "description": "InstdAgt is absent from DrctDbtTxInf (and not present in GrpHdr).",
          "affected_tags": ["DrctDbtTxInf/InstdAgt"],
          "possible_fixes": [
            "Insert <InstdAgt><FinInstnId><BICFI>{instructed_bic}</BICFI></FinInstnId></InstdAgt> in DrctDbtTxInf.",
            "Use the same BIC as AppHdr/To."
          ]
        },
        {
          "error_id": "INSTDAGT_INVALID_BIC",
          "error_code": "INVALID_BICFI",
          "severity": "Fatal",
          "description": "InstdAgt/FinInstnId/BICFI is invalid or malformed.",
          "affected_tags": ["DrctDbtTxInf/InstdAgt/FinInstnId/BICFI"],
          "possible_fixes": [
            "Replace with a valid BICFI: 8 or 11 uppercase alphanumeric characters."
          ]
        },
        {
          "error_id": "INSTDAGT_NM_PSTLADR_WITH_BIC",
          "error_code": "BICFI_EXCLUSIVE",
          "severity": "Fatal",
          "description": "CBPR+: BICFI is present but Nm and/or PstlAdr are also in the same FinInstnId.",
          "affected_tags": ["DrctDbtTxInf/InstdAgt/FinInstnId/Nm", "DrctDbtTxInf/InstdAgt/FinInstnId/PstlAdr"],
          "possible_fixes": [
            "Remove Nm and PstlAdr from InstdAgt/FinInstnId when BICFI is present."
          ]
        },
        {
          "error_id": "INSTDAGT_BRNCHID_PRESENT",
          "error_code": "CBPR_BRANCH_REMOVED",
          "severity": "Fatal",
          "description": "BrnchId is present in InstdAgt; removed under CBPR+ SR2025.",
          "affected_tags": ["DrctDbtTxInf/InstdAgt/BrnchId"],
          "possible_fixes": [
            "Remove the BrnchId element from InstdAgt entirely."
          ]
        }
      ]
    },

    {
      "tag": "DrctDbtTxInf/CdtrAgt",
      "xml_element": "CdtrAgt",
      "xpath": "/Document/FIToFICstmrDrctDbt/DrctDbtTxInf/CdtrAgt",
      "occurrence": "[1..1]",
      "mandatory": True,
      "description": "Creditor's financial institution (collecting agent). BICFI mandatory on SWIFT. Nm/PstlAdr optional when BICFI absent. BrnchId removed.",
      "errors": [
        {
          "error_id": "CDTRAGT_MISSING",
          "error_code": "MISSING_MANDATORY_FIELD",
          "severity": "Fatal",
          "description": "CdtrAgt is absent from DrctDbtTxInf.",
          "affected_tags": ["DrctDbtTxInf/CdtrAgt"],
          "possible_fixes": [
            "Insert <CdtrAgt><FinInstnId><BICFI>{creditor_bank_bic}</BICFI></FinInstnId></CdtrAgt>."
          ]
        },
        {
          "error_id": "CDTRAGT_INVALID_BIC",
          "error_code": "INVALID_BICFI",
          "severity": "Fatal",
          "description": "CdtrAgt/FinInstnId/BICFI is invalid or malformed.",
          "affected_tags": ["DrctDbtTxInf/CdtrAgt/FinInstnId/BICFI"],
          "possible_fixes": [
            "Replace with a valid 8 or 11 character BICFI.",
            "Pattern: [A-Z]{6}[A-Z2-9][A-NP-Z0-9]([A-Z0-9]{3})?"
          ]
        },
        {
          "error_id": "CDTRAGT_NM_WITHOUT_PSTLADR",
          "error_code": "CBPR_AGENT_NM_PSTLADR",
          "severity": "Fatal",
          "description": "CdtrAgt/FinInstnId has Nm but PstlAdr is absent; Nm and PstlAdr must always be present together.",
          "affected_tags": ["DrctDbtTxInf/CdtrAgt/FinInstnId/Nm", "DrctDbtTxInf/CdtrAgt/FinInstnId/PstlAdr"],
          "possible_fixes": [
            "Add <PstlAdr><AdrLine>{address}</AdrLine><Ctry>{ISO2_country}</Ctry></PstlAdr> to FinInstnId.",
            "Or use BICFI instead of Nm/PstlAdr."
          ]
        },
        {
          "error_id": "CDTRAGT_PSTLADR_WITHOUT_NM",
          "error_code": "CBPR_AGENT_NM_PSTLADR",
          "severity": "Fatal",
          "description": "CdtrAgt/FinInstnId has PstlAdr but Nm is absent.",
          "affected_tags": ["DrctDbtTxInf/CdtrAgt/FinInstnId/Nm", "DrctDbtTxInf/CdtrAgt/FinInstnId/PstlAdr"],
          "possible_fixes": [
            "Add <Nm>{institution name, max 140 chars}</Nm> to FinInstnId.",
            "Or remove PstlAdr and use BICFI instead."
          ]
        },
        {
          "error_id": "CDTRAGT_BRNCHID_PRESENT",
          "error_code": "CBPR_BRANCH_REMOVED",
          "severity": "Fatal",
          "description": "BrnchId present in CdtrAgt; removed under CBPR+ SR2025.",
          "affected_tags": ["DrctDbtTxInf/CdtrAgt/BrnchId"],
          "possible_fixes": [
            "Remove the BrnchId element from CdtrAgt entirely."
          ]
        }
      ]
    },

    {
      "tag": "DrctDbtTxInf/Cdtr",
      "xml_element": "Cdtr",
      "xpath": "/Document/FIToFICstmrDrctDbt/DrctDbtTxInf/Cdtr",
      "occurrence": "[1..1]",
      "mandatory": True,
      "description": "Creditor (party collecting the direct debit). At least Nm or Id required. Name required for AML/sanctions screening.",
      "errors": [
        {
          "error_id": "CDTR_MISSING",
          "error_code": "MISSING_MANDATORY_FIELD",
          "severity": "Fatal",
          "description": "Cdtr block is absent from DrctDbtTxInf.",
          "affected_tags": ["DrctDbtTxInf/Cdtr"],
          "possible_fixes": [
            "Insert a <Cdtr> block containing Nm and PstlAdr at minimum.",
            "<Cdtr><Nm>{name}</Nm><PstlAdr><AdrLine>{addr}</AdrLine><Ctry>{CC}</Ctry></PstlAdr></Cdtr>"
          ]
        },
        {
          "error_id": "CDTR_NM_TOO_LONG",
          "error_code": "ID_LENGTH_ERROR",
          "severity": "Fatal",
          "description": "Cdtr/Nm exceeds 140 characters.",
          "affected_tags": ["DrctDbtTxInf/Cdtr/Nm"],
          "possible_fixes": [
            "Truncate to maximum 140 characters."
          ]
        },
        {
          "error_id": "CDTR_PSTLADR_MISSING_CTRY",
          "error_code": "PSTLADR_MISSING_CTRY",
          "severity": "Fatal",
          "description": "Cdtr/PstlAdr is present but Ctry is missing.",
          "affected_tags": ["DrctDbtTxInf/Cdtr/PstlAdr/Ctry"],
          "possible_fixes": [
            "Add <Ctry>{ISO 3166-1 alpha-2 code}</Ctry> inside Cdtr/PstlAdr."
          ]
        },
        {
          "error_id": "CDTR_SANCTIONS_FLAG",
          "error_code": "SANCTIONS_BLOCKED",
          "severity": "Fatal",
          "description": "Cdtr name, country, or BIC matches a sanctioned entity.",
          "affected_tags": ["DrctDbtTxInf/Cdtr/Nm", "DrctDbtTxInf/Cdtr/PstlAdr/Ctry"],
          "possible_fixes": [
            "Escalate to compliance team; do not process.",
            "Replace sanctioned country code or name with compliant values where permitted."
          ]
        }
      ]
    },

    {
      "tag": "DrctDbtTxInf/CdtrAcct",
      "xml_element": "CdtrAcct",
      "xpath": "/Document/FIToFICstmrDrctDbt/DrctDbtTxInf/CdtrAcct",
      "occurrence": "[1..1]",
      "mandatory": True,
      "description": "Creditor's account to be credited with the collected amount. IBAN preferred. Currency must match IntrBkSttlmAmt if present.",
      "errors": [
        {
          "error_id": "CDTRACCT_MISSING",
          "error_code": "MISSING_MANDATORY_FIELD",
          "severity": "Fatal",
          "description": "CdtrAcct is absent from DrctDbtTxInf.",
          "affected_tags": ["DrctDbtTxInf/CdtrAcct"],
          "possible_fixes": [
            "Insert <CdtrAcct><Id><IBAN>{valid IBAN}</IBAN></Id></CdtrAcct>."
          ]
        },
        {
          "error_id": "CDTRACCT_INVALID_IBAN",
          "error_code": "IBAN_INVALID",
          "severity": "Fatal",
          "description": "CdtrAcct/Id/IBAN fails format or MOD97 checksum validation.",
          "affected_tags": ["DrctDbtTxInf/CdtrAcct/Id/IBAN"],
          "possible_fixes": [
            "Replace with a valid IBAN (ISO 13616, max 34 chars, MOD97 check digits valid)."
          ]
        },
        {
          "error_id": "CDTRACCT_IBAN_AND_OTHR",
          "error_code": "IBAN_XOR_OTHR",
          "severity": "Fatal",
          "description": "Both IBAN and Othr are present in CdtrAcct/Id; mutually exclusive.",
          "affected_tags": ["DrctDbtTxInf/CdtrAcct/Id/IBAN", "DrctDbtTxInf/CdtrAcct/Id/Othr"],
          "possible_fixes": [
            "Remove Othr if IBAN is the correct identifier.",
            "Remove IBAN if Othr is the correct identifier."
          ]
        },
        {
          "error_id": "CDTRACCT_CCY_MISMATCH",
          "error_code": "CHRGSINF_CCY_MISMATCH",
          "severity": "Warning",
          "description": "CdtrAcct/Ccy does not match IntrBkSttlmAmt currency.",
          "affected_tags": ["DrctDbtTxInf/CdtrAcct", "DrctDbtTxInf/IntrBkSttlmAmt"],
          "possible_fixes": [
            "Remove the Ccy attribute from CdtrAcct if it conflicts.",
            "Or align CdtrAcct/Ccy with IntrBkSttlmAmt/@Ccy."
          ]
        }
      ]
    },

    {
      "tag": "DrctDbtTxInf/Dbtr",
      "xml_element": "Dbtr",
      "xpath": "/Document/FIToFICstmrDrctDbt/DrctDbtTxInf/Dbtr",
      "occurrence": "[1..1]",
      "mandatory": True,
      "description": "Debtor (party being debited under the mandate). At least Nm or Id required. PstlAdr recommended if Nm present.",
      "errors": [
        {
          "error_id": "DBTR_MISSING",
          "error_code": "MISSING_MANDATORY_FIELD",
          "severity": "Fatal",
          "description": "Dbtr block is absent from DrctDbtTxInf.",
          "affected_tags": ["DrctDbtTxInf/Dbtr"],
          "possible_fixes": [
            "Insert a <Dbtr> block containing at minimum <Nm>{name}</Nm> and <PstlAdr><AdrLine>{addr}</AdrLine><Ctry>{CC}</Ctry></PstlAdr>."
          ]
        },
        {
          "error_id": "DBTR_NM_TOO_LONG",
          "error_code": "ID_LENGTH_ERROR",
          "severity": "Fatal",
          "description": "Dbtr/Nm exceeds 140 characters.",
          "affected_tags": ["DrctDbtTxInf/Dbtr/Nm"],
          "possible_fixes": [
            "Truncate to 140 characters maximum."
          ]
        },
        {
          "error_id": "DBTR_INVALID_CHARS",
          "error_code": "INVALID_CHARSETS",
          "severity": "Fatal",
          "description": "Dbtr/Nm contains invalid characters.",
          "affected_tags": ["DrctDbtTxInf/Dbtr/Nm"],
          "possible_fixes": [
            "Nm/Adr fields support extended charset: A-Z a-z 0-9 / - ? : ( ) . , ' + SPACE and !#$&%*=^_{|}~\";<>@[\\].",
            "Replace < with &lt; and > with &gt;."
          ]
        },
        {
          "error_id": "DBTR_PSTLADR_MISSING_CTRY",
          "error_code": "PSTLADR_MISSING_CTRY",
          "severity": "Fatal",
          "description": "Dbtr/PstlAdr is present but Ctry is missing.",
          "affected_tags": ["DrctDbtTxInf/Dbtr/PstlAdr/Ctry"],
          "possible_fixes": [
            "Add <Ctry>{ISO 3166-1 alpha-2 code}</Ctry> inside PstlAdr."
          ]
        },
        {
          "error_id": "DBTR_PSTLADR_MISSING_TWNNM",
          "error_code": "PSTLADR_MISSING_TWNNM",
          "severity": "Fatal",
          "description": "Dbtr/PstlAdr uses structured form (no AdrLine) but TwnNm is absent.",
          "affected_tags": ["DrctDbtTxInf/Dbtr/PstlAdr/TwnNm"],
          "possible_fixes": [
            "Add <TwnNm>{city name}</TwnNm> inside PstlAdr.",
            "Or use AdrLine for unstructured address."
          ]
        },
        {
          "error_id": "DBTR_ANYBIC_WITH_NM",
          "error_code": "BICFI_EXCLUSIVE",
          "severity": "Fatal",
          "description": "Dbtr/Id/OrgId/AnyBIC is present but Nm and/or PstlAdr are also present; AnyBIC takes precedence and Nm/PstlAdr are not allowed.",
          "affected_tags": ["DrctDbtTxInf/Dbtr/Nm", "DrctDbtTxInf/Dbtr/Id/OrgId/AnyBIC"],
          "possible_fixes": [
            "Remove Nm and PstlAdr from Dbtr when AnyBIC is present in OrgId.",
            "Or remove AnyBIC and retain Nm/PstlAdr."
          ]
        }
      ]
    },

    {
      "tag": "DrctDbtTxInf/DbtrAcct",
      "xml_element": "DbtrAcct",
      "xpath": "/Document/FIToFICstmrDrctDbt/DrctDbtTxInf/DbtrAcct",
      "occurrence": "[1..1]",
      "mandatory": True,
      "description": "Debtor's account to be debited. Identified by IBAN (preferred) or Othr; not both simultaneously.",
      "errors": [
        {
          "error_id": "DBTRACCT_MISSING",
          "error_code": "MISSING_MANDATORY_FIELD",
          "severity": "Fatal",
          "description": "DbtrAcct is absent from DrctDbtTxInf.",
          "affected_tags": ["DrctDbtTxInf/DbtrAcct"],
          "possible_fixes": [
            "Insert <DbtrAcct><Id><IBAN>{valid IBAN}</IBAN></Id></DbtrAcct>."
          ]
        },
        {
          "error_id": "DBTRACCT_INVALID_IBAN",
          "error_code": "IBAN_INVALID",
          "severity": "Fatal",
          "description": "DbtrAcct/Id/IBAN fails ISO 13616 format or MOD97 checksum validation.",
          "affected_tags": ["DrctDbtTxInf/DbtrAcct/Id/IBAN"],
          "possible_fixes": [
            "Replace with a valid IBAN: 2-letter country code + 2 check digits + BBAN (max 34 chars total).",
            "Verify MOD97 check digit computation."
          ]
        },
        {
          "error_id": "DBTRACCT_IBAN_AND_OTHR",
          "error_code": "IBAN_XOR_OTHR",
          "severity": "Fatal",
          "description": "Both IBAN and Othr are present in DbtrAcct/Id; mutually exclusive.",
          "affected_tags": ["DrctDbtTxInf/DbtrAcct/Id/IBAN", "DrctDbtTxInf/DbtrAcct/Id/Othr"],
          "possible_fixes": [
            "Remove Othr if IBAN is the correct identifier.",
            "Remove IBAN if Othr is the correct identifier."
          ]
        }
      ]
    },

    {
      "tag": "DrctDbtTxInf/DbtrAgt",
      "xml_element": "DbtrAgt",
      "xpath": "/Document/FIToFICstmrDrctDbt/DrctDbtTxInf/DbtrAgt",
      "occurrence": "[1..1]",
      "mandatory": True,
      "description": "Debtor's financial institution (debited agent). Must have valid BICFI under CBPR+. BrnchId removed.",
      "errors": [
        {
          "error_id": "DBTRAGT_MISSING",
          "error_code": "MISSING_MANDATORY_FIELD",
          "severity": "Fatal",
          "description": "DbtrAgt is absent from DrctDbtTxInf.",
          "affected_tags": ["DrctDbtTxInf/DbtrAgt"],
          "possible_fixes": [
            "Insert <DbtrAgt><FinInstnId><BICFI>{debtor_bank_bic}</BICFI></FinInstnId></DbtrAgt>."
          ]
        },
        {
          "error_id": "DBTRAGT_INVALID_BIC",
          "error_code": "INVALID_BICFI",
          "severity": "Fatal",
          "description": "DbtrAgt/FinInstnId/BICFI is invalid.",
          "affected_tags": ["DrctDbtTxInf/DbtrAgt/FinInstnId/BICFI"],
          "possible_fixes": [
            "Replace with a valid 8 or 11 character BIC.",
            "DBTRAGT_NEQ_CDTRAGT: DbtrAgt BIC must differ from CdtrAgt BIC."
          ]
        },
        {
          "error_id": "DBTRAGT_EQUALS_CDTRAGT",
          "error_code": "DBTRAGT_NEQ_CDTRAGT",
          "severity": "Warning",
          "description": "DbtrAgt BICFI equals CdtrAgt BICFI — loopback collection warning.",
          "affected_tags": ["DrctDbtTxInf/DbtrAgt/FinInstnId/BICFI", "DrctDbtTxInf/CdtrAgt/FinInstnId/BICFI"],
          "possible_fixes": [
            "Verify that the debtor and creditor are not at the same institution.",
            "Use different BICs for DbtrAgt and CdtrAgt."
          ]
        },
        {
          "error_id": "DBTRAGT_NM_WITH_BIC",
          "error_code": "BICFI_EXCLUSIVE",
          "severity": "Fatal",
          "description": "CBPR+: Nm and/or PstlAdr present alongside BICFI in DbtrAgt/FinInstnId.",
          "affected_tags": ["DrctDbtTxInf/DbtrAgt/FinInstnId"],
          "possible_fixes": [
            "Remove Nm and PstlAdr from DbtrAgt/FinInstnId when BICFI is present."
          ]
        },
        {
          "error_id": "DBTRAGT_BRNCHID_PRESENT",
          "error_code": "CBPR_BRANCH_REMOVED",
          "severity": "Fatal",
          "description": "BrnchId present in DbtrAgt; removed under CBPR+ SR2025.",
          "affected_tags": ["DrctDbtTxInf/DbtrAgt/BrnchId"],
          "possible_fixes": [
            "Remove the BrnchId element from DbtrAgt entirely."
          ]
        }
      ]
    },

    {
      "tag": "DrctDbtTxInf/PmtTpInf",
      "xml_element": "PmtTpInf",
      "xpath": "/Document/FIToFICstmrDrctDbt/DrctDbtTxInf/PmtTpInf",
      "occurrence": "[0..1]",
      "mandatory": False,
      "description": "Payment type information. If present at GrpHdr, must NOT be present at DrctDbtTxInf level (R10).",
      "errors": [
        {
          "error_id": "PMTTPINF_BOTH_LEVELS",
          "error_code": "X00009",
          "severity": "Fatal",
          "description": "PmtTpInf is present at both GrpHdr and DrctDbtTxInf level; violates R10.",
          "affected_tags": ["GrpHdr/PmtTpInf", "DrctDbtTxInf/PmtTpInf"],
          "possible_fixes": [
            "Remove DrctDbtTxInf/PmtTpInf if GrpHdr/PmtTpInf is present.",
            "Remove GrpHdr/PmtTpInf if DrctDbtTxInf/PmtTpInf is the intended location."
          ]
        },
        {
          "error_id": "INSTRPRTY_INVALID_CODE",
          "error_code": "SCHEMENAME_INVALID",
          "severity": "Fatal",
          "description": "PmtTpInf/InstrPrty contains a value not in the allowed codelist.",
          "affected_tags": ["DrctDbtTxInf/PmtTpInf/InstrPrty"],
          "possible_fixes": [
            "Replace with NORM or HIGH.",
            "Default: NORM."
          ]
        },
        {
          "error_id": "SEQTP_INVALID_CODE",
          "error_code": "SCHEMENAME_INVALID",
          "severity": "Fatal",
          "description": "PmtTpInf/SeqTp contains a value not in the allowed direct debit sequence codelist.",
          "affected_tags": ["DrctDbtTxInf/PmtTpInf/SeqTp"],
          "possible_fixes": [
            "Replace with one of: FRST (first), RCUR (recurring), FNAL (final), OOFF (one-off)."
          ]
        },
        {
          "error_id": "INSTRPRTY_NEQ_APPHDR_PRTY",
          "error_code": "CBPR_PRIORITY",
          "severity": "Fatal",
          "description": "PmtTpInf/InstrPrty does not match AppHdr/Prty.",
          "affected_tags": ["DrctDbtTxInf/PmtTpInf/InstrPrty", "AppHdr/Prty"],
          "possible_fixes": [
            "Set InstrPrty to the same value as AppHdr/Prty.",
            "Alternatively, set AppHdr/Prty to match InstrPrty."
          ]
        }
      ]
    },

    {
      "tag": "DrctDbtTxInf/RmtInf",
      "xml_element": "RmtInf",
      "xpath": "/Document/FIToFICstmrDrctDbt/DrctDbtTxInf/RmtInf",
      "occurrence": "[0..1]",
      "mandatory": False,
      "description": "Remittance information. Ustrd and Strd are mutually exclusive within the same RmtInf block.",
      "errors": [
        {
          "error_id": "RMTINF_USTRD_AND_STRD",
          "error_code": "DUPLICATE_TAG",
          "severity": "Fatal",
          "description": "Both Ustrd and Strd are present in the same RmtInf block; they are mutually exclusive.",
          "affected_tags": ["DrctDbtTxInf/RmtInf/Ustrd", "DrctDbtTxInf/RmtInf/Strd"],
          "possible_fixes": [
            "Remove Strd if unstructured remittance is preferred.",
            "Remove Ustrd if structured remittance is preferred."
          ]
        },
        {
          "error_id": "RMTINF_USTRD_TOO_LONG",
          "error_code": "ID_LENGTH_ERROR",
          "severity": "Fatal",
          "description": "Ustrd text exceeds 140 characters per occurrence.",
          "affected_tags": ["DrctDbtTxInf/RmtInf/Ustrd"],
          "possible_fixes": [
            "Truncate each Ustrd occurrence to maximum 140 characters.",
            "Maximum 4 Ustrd occurrences allowed."
          ]
        },
        {
          "error_id": "RMTINF_STRD_REF_TOO_LONG",
          "error_code": "ID_LENGTH_ERROR",
          "severity": "Fatal",
          "description": "Strd/CdtrRefInf/Ref exceeds 35 characters.",
          "affected_tags": ["DrctDbtTxInf/RmtInf/Strd/CdtrRefInf/Ref"],
          "possible_fixes": [
            "Truncate Ref to maximum 35 characters."
          ]
        },
        {
          "error_id": "RMTINF_INVALID_CHARS",
          "error_code": "INVALID_CHARSETS",
          "severity": "Fatal",
          "description": "RmtInf contains characters outside the extended charset.",
          "affected_tags": ["DrctDbtTxInf/RmtInf/Ustrd"],
          "possible_fixes": [
            "RmtInf supports extended charset including !#$&%*=^_{|}~\";<>@[\\].",
            "Replace < with &lt; and > with &gt;.",
            "Remove unsupported characters."
          ]
        }
      ]
    },

    {
      "tag": "DrctDbtTxInf/Purp",
      "xml_element": "Purp",
      "xpath": "/Document/FIToFICstmrDrctDbt/DrctDbtTxInf/Purp",
      "occurrence": "[0..1]",
      "mandatory": False,
      "description": "Purpose of the direct debit. Cd (ISO 20022 code) and Prtry are mutually exclusive.",
      "errors": [
        {
          "error_id": "PURP_INVALID_CODE",
          "error_code": "SCHEMENAME_INVALID",
          "severity": "Fatal",
          "description": "Purp/Cd is not in the ISO 20022 external Purpose code list.",
          "affected_tags": ["DrctDbtTxInf/Purp/Cd"],
          "possible_fixes": [
            "Use a valid ISO 20022 Purpose code: e.g. GDDS (goods), SUPP (supplier), SALA (salary), TAXS (tax), TREA (treasury).",
            "Alternatively use Purp/Prtry with a proprietary code (max 35 chars)."
          ]
        },
        {
          "error_id": "PURP_CD_AND_PRTRY",
          "error_code": "DUPLICATE_TAG",
          "severity": "Fatal",
          "description": "Both Purp/Cd and Purp/Prtry are present; mutually exclusive.",
          "affected_tags": ["DrctDbtTxInf/Purp/Cd", "DrctDbtTxInf/Purp/Prtry"],
          "possible_fixes": [
            "Remove Prtry if Cd is the correct choice.",
            "Remove Cd if Prtry is the correct choice."
          ]
        }
      ]
    },

    {
      "tag": "DrctDbtTxInf/IntrmyAgt1",
      "xml_element": "IntrmyAgt1",
      "xpath": "/Document/FIToFICstmrDrctDbt/DrctDbtTxInf/IntrmyAgt1",
      "occurrence": "[0..1]",
      "mandatory": False,
      "description": "First intermediary agent. BrnchId removed. IntrmyAgt2 cannot exist without IntrmyAgt1.",
      "errors": [
        {
          "error_id": "INTRMYAGT2_WITHOUT_INTRMYAGT1",
          "error_code": "MISSING_EXPECTED_ELEMENT",
          "severity": "Fatal",
          "description": "IntrmyAgt2 is present but IntrmyAgt1 is absent.",
          "affected_tags": ["DrctDbtTxInf/IntrmyAgt1", "DrctDbtTxInf/IntrmyAgt2"],
          "possible_fixes": [
            "Insert IntrmyAgt1 before IntrmyAgt2.",
            "Or remove IntrmyAgt2 if only one intermediary is needed."
          ]
        },
        {
          "error_id": "INTRMYAGT1_INVALID_BIC",
          "error_code": "INVALID_BICFI",
          "severity": "Fatal",
          "description": "IntrmyAgt1/FinInstnId/BICFI is invalid.",
          "affected_tags": ["DrctDbtTxInf/IntrmyAgt1/FinInstnId/BICFI"],
          "possible_fixes": [
            "Replace with a valid 8 or 11 character BIC."
          ]
        },
        {
          "error_id": "INTRMYAGT1_BRNCHID_PRESENT",
          "error_code": "CBPR_BRANCH_REMOVED",
          "severity": "Fatal",
          "description": "BrnchId present in IntrmyAgt1; removed under CBPR+ SR2025.",
          "affected_tags": ["DrctDbtTxInf/IntrmyAgt1/BrnchId"],
          "possible_fixes": [
            "Remove the BrnchId element from IntrmyAgt1 entirely."
          ]
        },
        {
          "error_id": "INTRMYAGT3_WITHOUT_INTRMYAGT2",
          "error_code": "MISSING_EXPECTED_ELEMENT",
          "severity": "Fatal",
          "description": "IntrmyAgt3 is present but IntrmyAgt2 is absent.",
          "affected_tags": ["DrctDbtTxInf/IntrmyAgt2", "DrctDbtTxInf/IntrmyAgt3"],
          "possible_fixes": [
            "Insert IntrmyAgt2 before IntrmyAgt3.",
            "Or remove IntrmyAgt3 if only two intermediaries are needed."
          ]
        }
      ]
    },

    {
      "tag": "DrctDbtTxInf/ChrgsInf",
      "xml_element": "ChrgsInf",
      "xpath": "/Document/FIToFICstmrDrctDbt/DrctDbtTxInf/ChrgsInf",
      "occurrence": "[0..n]",
      "mandatory": False,
      "description": "Charges information. Required when ChrgBr = CRED. Must contain Agt when present.",
      "errors": [
        {
          "error_id": "CHRGSINF_MISSING_WHEN_CHRGBR_CRED",
          "error_code": "CHRGBR_CRED_REQUIRES_CHRGSINF",
          "severity": "Fatal",
          "description": "ChrgBr = CRED but ChrgsInf is absent.",
          "affected_tags": ["DrctDbtTxInf/ChrgsInf", "DrctDbtTxInf/ChrgBr"],
          "possible_fixes": [
            "Insert <ChrgsInf><Amt Ccy=\"{CCY}\">0.00</Amt><Agt><FinInstnId><BICFI>{BIC}</BICFI></FinInstnId></Agt></ChrgsInf>."
          ]
        },
        {
          "error_id": "CHRGSINF_MISSING_AGT",
          "error_code": "CHRGSINF_REQUIRES_AGT",
          "severity": "Fatal",
          "description": "ChrgsInf is present but the Agt element is absent.",
          "affected_tags": ["DrctDbtTxInf/ChrgsInf/Agt"],
          "possible_fixes": [
            "Add <Agt><FinInstnId><BICFI>{agent_BIC}</BICFI></FinInstnId></Agt> inside ChrgsInf."
          ]
        },
        {
          "error_id": "CHRGSINF_CCY_MISMATCH",
          "error_code": "CHRGSINF_CCY_MISMATCH",
          "severity": "Fatal",
          "description": "ChrgsInf/Amt currency does not match IntrBkSttlmAmt currency.",
          "affected_tags": ["DrctDbtTxInf/ChrgsInf/Amt", "DrctDbtTxInf/IntrBkSttlmAmt"],
          "possible_fixes": [
            "Update ChrgsInf/Amt/@Ccy to match IntrBkSttlmAmt/@Ccy."
          ]
        }
      ]
    }
  ],
  "cross_tag_dependency_rules": [
    {"rule_id": "DEP_001", "rule": "AppHdr/BizMsgIdr must equal GrpHdr/MsgId", "affected_tags": ["AppHdr/BizMsgIdr", "GrpHdr/MsgId"], "fix": "Harvest MsgId from GrpHdr and set BizMsgIdr to same value or vice versa."},
    {"rule_id": "DEP_002", "rule": "AppHdr/Fr/BICFI must equal DrctDbtTxInf/InstgAgt/BICFI (unless CpyDplct=COPY/CODU)", "affected_tags": ["AppHdr/Fr", "DrctDbtTxInf/InstgAgt"], "fix": "Harvest InstgAgt BICFI and set AppHdr/Fr to match."},
    {"rule_id": "DEP_003", "rule": "AppHdr/To/BICFI must equal DrctDbtTxInf/InstdAgt/BICFI", "affected_tags": ["AppHdr/To", "DrctDbtTxInf/InstdAgt"], "fix": "Harvest InstdAgt BICFI and set AppHdr/To to match."},
    {"rule_id": "DEP_004", "rule": "GrpHdr/NbOfTxs must equal count of DrctDbtTxInf blocks", "affected_tags": ["GrpHdr/NbOfTxs"], "fix": "Count DrctDbtTxInf elements and update NbOfTxs."},
    {"rule_id": "DEP_005", "rule": "GrpHdr/CtrlSum must equal sum of all IntrBkSttlmAmt", "affected_tags": ["GrpHdr/CtrlSum", "DrctDbtTxInf/IntrBkSttlmAmt"], "fix": "Sum IntrBkSttlmAmt values and update CtrlSum."},
    {"rule_id": "DEP_006", "rule": "AppHdr/Prty must equal PmtTpInf/InstrPrty when both present", "affected_tags": ["AppHdr/Prty", "DrctDbtTxInf/PmtTpInf/InstrPrty"], "fix": "Align both to same value; default NORM."},
    {"rule_id": "DEP_007", "rule": "GrpHdr/InstgAgt and DrctDbtTxInf/InstgAgt are mutually exclusive (R5)", "affected_tags": ["GrpHdr/InstgAgt", "DrctDbtTxInf/InstgAgt"], "fix": "Remove from one level; keep at the other."},
    {"rule_id": "DEP_008", "rule": "GrpHdr/InstdAgt and DrctDbtTxInf/InstdAgt are mutually exclusive (R4)", "affected_tags": ["GrpHdr/InstdAgt", "DrctDbtTxInf/InstdAgt"], "fix": "Remove from one level; keep at the other."},
    {"rule_id": "DEP_009", "rule": "GrpHdr/PmtTpInf and DrctDbtTxInf/PmtTpInf are mutually exclusive (R10)", "affected_tags": ["GrpHdr/PmtTpInf", "DrctDbtTxInf/PmtTpInf"], "fix": "Remove from one level; keep at the other."},
    {"rule_id": "DEP_010", "rule": "IntrBkSttlmDt must be at either GrpHdr or DrctDbtTxInf, not both (R8/R9)", "affected_tags": ["GrpHdr/IntrBkSttlmDt", "DrctDbtTxInf/IntrBkSttlmDt"], "fix": "Keep at one level only."},
    {"rule_id": "DEP_011", "rule": "XchgRate required iff InstdAmt.Ccy != IntrBkSttlmAmt.Ccy", "affected_tags": ["DrctDbtTxInf/XchgRate", "DrctDbtTxInf/InstdAmt", "DrctDbtTxInf/IntrBkSttlmAmt"], "fix": "Add XchgRate when currencies differ; remove when same."},
    {"rule_id": "DEP_012", "rule": "IntrmyAgt2 requires IntrmyAgt1; IntrmyAgt3 requires IntrmyAgt2", "affected_tags": ["DrctDbtTxInf/IntrmyAgt1", "DrctDbtTxInf/IntrmyAgt2", "DrctDbtTxInf/IntrmyAgt3"], "fix": "Insert missing predecessor agent(s)."},
    {"rule_id": "DEP_013", "rule": "If FinInstnId/BICFI present, then Nm and PstlAdr MUST NOT be present in same block", "affected_tags": ["FinInstnId/BICFI", "FinInstnId/Nm", "FinInstnId/PstlAdr"], "fix": "Remove Nm and PstlAdr when BICFI is present."},
    {"rule_id": "DEP_014", "rule": "If Nm present in FinInstnId, PstlAdr must also be present and vice versa", "affected_tags": ["FinInstnId/Nm", "FinInstnId/PstlAdr"], "fix": "Add the missing counterpart; or use BICFI."},
    {"rule_id": "DEP_015", "rule": "BrnchId is removed from all agent elements under CBPR+ SR2025", "affected_tags": ["InstgAgt/BrnchId", "InstdAgt/BrnchId", "DbtrAgt/BrnchId", "CdtrAgt/BrnchId", "IntrmyAgt1/BrnchId", "IntrmyAgt2/BrnchId", "IntrmyAgt3/BrnchId"], "fix": "Remove all BrnchId elements from agent blocks."},
    {"rule_id": "DEP_016", "rule": "If DrctDbtTx is present, MndtRltdInf MUST be present with MndtId and DtOfSgntr (R12)", "affected_tags": ["DrctDbtTxInf/DrctDbtTx", "DrctDbtTxInf/DrctDbtTx/MndtRltdInf/MndtId", "DrctDbtTxInf/DrctDbtTx/MndtRltdInf/DtOfSgntr"], "fix": "Insert MndtRltdInf with MndtId and DtOfSgntr inside DrctDbtTx."},
    {"rule_id": "DEP_017", "rule": "MndtRltdInf/DtOfSgntr must be today or in the past and not later than IntrBkSttlmDt/ReqdColltnDt", "affected_tags": ["DrctDbtTxInf/DrctDbtTx/MndtRltdInf/DtOfSgntr", "DrctDbtTxInf/IntrBkSttlmDt", "DrctDbtTxInf/ReqdColltnDt"], "fix": "Set DtOfSgntr to the actual mandate signature date (<= collection/settlement date)."},
    {"rule_id": "DEP_018", "rule": "DbtrAgt BICFI should differ from CdtrAgt BICFI (loopback collection warning)", "affected_tags": ["DrctDbtTxInf/DbtrAgt/FinInstnId/BICFI", "DrctDbtTxInf/CdtrAgt/FinInstnId/BICFI"], "fix": "Use different BICs for DbtrAgt and CdtrAgt unless an on-us collection is intended."}
  ],
  "tag_insertion_order": {
    "GrpHdr": ["MsgId", "CreDtTm", "BtchBookg", "NbOfTxs", "CtrlSum", "TtlIntrBkSttlmAmt", "IntrBkSttlmDt", "SttlmInf", "PmtTpInf", "InstgAgt", "InstdAgt"],
    "DrctDbtTxInf": ["PmtId", "PmtTpInf", "IntrBkSttlmAmt", "IntrBkSttlmDt", "SttlmPrty", "SttlmTmIndctn", "InstdAmt", "XchgRate", "ChrgBr", "ChrgsInf", "ReqdColltnDt", "DrctDbtTx", "Cdtr", "CdtrAcct", "CdtrAgt", "CdtrAgtAcct", "UltmtCdtr", "InitgPty", "InstgAgt", "InstdAgt", "IntrmyAgt1", "IntrmyAgt1Acct", "IntrmyAgt2", "IntrmyAgt2Acct", "IntrmyAgt3", "IntrmyAgt3Acct", "Dbtr", "DbtrAcct", "DbtrAgt", "DbtrAgtAcct", "UltmtDbtr", "Purp", "RgltryRptg", "Tax", "RltdRmtInf", "RmtInf", "SplmtryData"],
    "PmtId": ["InstrId", "EndToEndId", "TxId", "ClrSysRef", "UETR"],
    "DrctDbtTx": ["MndtRltdInf", "CdtrSchmeId", "PreNtfctnId", "PreNtfctnDt"],
    "MndtRltdInf": ["MndtId", "DtOfSgntr", "AmdmntInd", "AmdmntInfDtls", "ElctrncSgntr", "FrstColltnDt", "FnlColltnDt", "Frqcy", "Rsn", "TrckgDays"],
    "AppHdr": ["Fr", "To", "BizMsgIdr", "MsgDefIdr", "BizSvc", "CreDt", "CpyDplct", "PssblDplct", "Prty", "Sgntr", "Rltd"]
  }
}

print(json.dumps(kb, indent=2, ensure_ascii=False))
