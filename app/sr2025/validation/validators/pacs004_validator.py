import re
import time
from typing import Dict, Any, List
from .models import ValidationIssue, ValidationReport

class Pacs004Mixin:
    """
    Dedicated strict validator for pacs.004.001.09 (PaymentReturn)
    Based on CBPR+ SR2025 Usage Guidelines.
    """

    async def _validate_pacs_004(self, xml_content: str, canonical_data: Dict[str, Any], line_map: Dict[str, int], report: ValidationReport):
        if "pacs.004" not in report.message_type:
            return

        start = time.time()
        
        # Helper: Get value or None
        def get_val(path): return canonical_data.get(path)

        def _get_line(key):
             if not isinstance(line_map, dict) or not key:
                 return None
             key = key.split('@')[0]
             def clean_path(p):
                 return re.sub(r'\[\d+\]', '', p)
             parts = key.split('.')
             while parts:
                 current_path = '.'.join(parts)
                 l = line_map.get(current_path)
                 if l:
                     return int(l)
                 l = line_map.get(clean_path(current_path))
                 if l:
                     return int(l)
                 parts.pop()
             return None

        # Override ValidationIssue constructor to automatically resolve line numbers using line_map
        from .models import ValidationIssue as OriginalValidationIssue
        def ValidationIssue(severity, layer, code, path, message, fix_suggestion="", related_test=""):
            line = _get_line(path)
            return OriginalValidationIssue(severity, layer, code, path, message, fix_suggestion, related_test, line=line)

        # --------------------------------------------------
        # 1. STRUCTURAL VALIDATIONS
        # --------------------------------------------------
        
        # GrpHdr Mandatory Fields
        gh_path = "Document.PmtRtr.GrpHdr"
        if not any(k.startswith(gh_path) for k in canonical_data.keys()):
            report.add_issue(ValidationIssue("ERROR", 3, "PACS004_GH_MISSING", "Document.PmtRtr", "GrpHdr is mandatory in pacs.004."))
        else:
            # MsgId (max 35)
            msg_id = get_val(f"{gh_path}.MsgId")
            if not msg_id or len(msg_id) > 35:
                report.add_issue(ValidationIssue("ERROR", 3, "PACS004_MSGID_INVALID", f"{gh_path}.MsgId", "GrpHdr.MsgId is mandatory and max 35 characters."))
            
            # CreDtTm (ISODateTime with timezone)
            cre_dt_tm = get_val(f"{gh_path}.CreDtTm")
            if not cre_dt_tm or not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$", cre_dt_tm):
                report.add_issue(ValidationIssue("ERROR", 3, "PACS004_CREDT_INVALID", f"{gh_path}.CreDtTm", "GrpHdr.CreDtTm must match ISO 8601 with timezone (e.g., 2023-10-27T10:00:00Z)."))
            
            # NbOfTxs = "1" (fixed)
            nb_txs = get_val(f"{gh_path}.NbOfTxs")
            if nb_txs != "1":
                 report.add_issue(ValidationIssue("ERROR", 3, "PACS004_NBTXS_INVALID", f"{gh_path}.NbOfTxs", "NbOfTxs must be exactly '1' for pacs.004 CBPR+ usage."))
            
            # SttlmInf
            if not any(k.startswith(f"{gh_path}.SttlmInf") for k in canonical_data.keys()):
                 report.add_issue(ValidationIssue("ERROR", 3, "PACS004_STTLMINF_MISSING", gh_path, "SttlmInf is mandatory in GrpHdr."))

        # TxInf Mandatory (single transaction)
        tx_path = "Document.PmtRtr.TxInf"
        # Check if multiple TxInf exist (indexing [1] would exist)
        if any(k.startswith(f"{tx_path}[1]") for k in canonical_data.keys()):
             report.add_issue(ValidationIssue("ERROR", 3, "PACS004_SINGLE_TX_ONLY", "Document.PmtRtr", "pacs.004 must contain exactly one TxInf in CBPR+."))
        
        if not any(k.startswith(tx_path) for k in canonical_data.keys()):
             report.add_issue(ValidationIssue("ERROR", 3, "PACS004_TXINF_MISSING", "Document.PmtRtr", "TxInf is mandatory."))
        else:
            # Mandatory fields in TxInf
            mandatory_tx = {
                "RtrId": "PACS004_RTRID_MISSING",
                "OrgnlInstrId": "PACS004_ORGNLINSTRID_MISSING",
                "OrgnlEndToEndId": "PACS004_ORGNLENDTOENDID_MISSING",
                "OrgnlTxId": "PACS004_ORGNLTXID_MISSING",
                "OrgnlUETR": "PACS004_ORGNLUETR_MISSING",
                "RtrdIntrBkSttlmAmt": "PACS004_RTRDAMT_MISSING",
                "IntrBkSttlmDt": "PACS004_STTLMDT_MISSING",
                "ChrgBr": "PACS004_CHRGBR_MISSING",
                "RtrChain": "PACS004_RTRCHAIN_MISSING"
            }
            for field, code in mandatory_tx.items():
                if not any(k.startswith(f"{tx_path}.{field}") for k in canonical_data.keys()):
                     report.add_issue(ValidationIssue("ERROR", 3, code, f"{tx_path}.{field}", f"{field} is mandatory in TxInf."))

            # RtrRsnInf must contain Rsn.Cd
            rsn_path = f"{tx_path}.RtrRsnInf"
            if any(k.startswith(rsn_path) for k in canonical_data.keys()):
                if not any(k.startswith(f"{rsn_path}.Rsn.Cd") for k in canonical_data.keys()):
                     report.add_issue(ValidationIssue("ERROR", 3, "PACS004_RSNCD_MISSING", rsn_path, "RtrRsnInf must contain Rsn.Cd. Proprietary (Prtry) is NOT allowed per CBPR+ guidelines."))

        # --------------------------------------------------
        # 2. FORMAT / PATTERN VALIDATIONS & 7. AMOUNT VALIDATIONS
        # --------------------------------------------------
        
        patterns = {
            "OrgnlUETR": r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$", # UUIDv4
            "BICFI": r"^[A-Z0-9]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?$",
            "LEI": r"^[A-Z0-9]{18}[0-9]{2}$",
            "IBAN": r"^[A-Z]{2}[0-9]{2}[A-Za-z0-9]{1,30}$",
            "Country": r"^[A-Z]{2}$"
        }

        # Amount Rules: Max 14 digits, 5 fractions, >= 0
        def validate_amount(val, path, msg_prefix):
             if not val: return
             try:
                 amt = float(val)
                 if amt < 0:
                     report.add_issue(ValidationIssue("ERROR", 3, "PACS004_AMT_NEGATIVE", path, f"{msg_prefix} must be >= 0."))
                 # Total digits and decimals
                 raw = str(val).replace("-", "")
                 parts = raw.split('.')
                 total_digits = len(parts[0]) + (len(parts[1]) if len(parts) > 1 else 0)
                 frac_digits = len(parts[1]) if len(parts) > 1 else 0
                 
                 if total_digits > 14:
                      report.add_issue(ValidationIssue("ERROR", 3, "PACS004_AMT_LEN", path, f"{msg_prefix} total digits ({total_digits}) exceeds maximum of 14."))
                 if frac_digits > 5:
                      report.add_issue(ValidationIssue("ERROR", 3, "PACS004_AMT_FRAC", path, f"{msg_prefix} fraction digits ({frac_digits}) exceeds maximum of 5."))
             except ValueError:
                 report.add_issue(ValidationIssue("ERROR", 3, "PACS004_AMT_NOT_NUMERIC", path, f"{msg_prefix} must be a numeric value."))

        # Exchange Rate Rules: Max 11 digits, 10 decimals
        def validate_exchange_rate(val, path):
             if not val: return
             try:
                 raw = str(val).replace("-", "")
                 parts = raw.split('.')
                 total_digits = len(parts[0]) + (len(parts[1]) if len(parts) > 1 else 0)
                 frac_digits = len(parts[1]) if len(parts) > 1 else 0
                 if total_digits > 11:
                      report.add_issue(ValidationIssue("ERROR", 3, "PACS004_XRATE_LEN", path, f"Exchange Rate total digits ({total_digits}) exceeds maximum of 11."))
                 if frac_digits > 10:
                      report.add_issue(ValidationIssue("ERROR", 3, "PACS004_XRATE_FRAC", path, f"Exchange Rate fraction digits ({frac_digits}) exceeds maximum of 10."))
             except: pass

        for path, val in canonical_data.items():
            # Pattern checks
            for field, pattern in patterns.items():
                if field in path and not re.match(pattern, str(val)):
                    report.add_issue(ValidationIssue("ERROR", 3, f"PACS004_{field}_PATTERN", path, f"Invalid format for {field}: '{val}'."))
            
            # Amount checks
            if any(x in path for x in ["Amt", "RtrdIntrBkSttlmAmt", "InstdAmt", "EqvtAmt"]):
                 if not path.endswith("@Ccy"):
                     validate_amount(val, path, "Amount field")
            
            # Exchange Rate checks
            if "XchgRate" in path:
                 validate_exchange_rate(val, path)

        # --------------------------------------------------
        # 3. ENUM VALIDATIONS & 11. CRITICAL PACS.004 RULES
        # --------------------------------------------------
        
        # ChrgBr: CRED, SHAR, SLEV allowed
        chrg_br = get_val(f"{tx_path}.ChrgBr")
        if chrg_br and chrg_br not in ["CRED", "SHAR", "SLEV"]:
             report.add_issue(ValidationIssue("ERROR", 3, "PACS004_CHRGBR_ENUM", f"{tx_path}.ChrgBr", f"Charge Bearer '{chrg_br}' is invalid. Allowed: CRED, SHAR, SLEV."))

        # GrpHdr.SttlmInf.SttlmMtd: INDA, INGA only
        sttlm_mtd = get_val(f"{gh_path}.SttlmInf.SttlmMtd")
        if sttlm_mtd and sttlm_mtd not in ["INDA", "INGA"]:
             report.add_issue(ValidationIssue("ERROR", 3, "PACS004_STTLMMTD_ENUM", f"{gh_path}.SttlmInf.SttlmMtd", f"Settlement Method '{sttlm_mtd}' is invalid. pacs.004 allows only INDA or INGA in GrpHdr. COVE is strictly forbidden here."))

        # OrglTxRef.SttlmInf.SttlmMtd: INDA, INGA, COVE
        ref_mtd = get_val(f"{tx_path}.OrgnlTxRef.SttlmInf.SttlmMtd")
        if ref_mtd and ref_mtd not in ["INDA", "INGA", "COVE"]:
             report.add_issue(ValidationIssue("ERROR", 3, "PACS004_REFMTD_ENUM", f"{tx_path}.OrgnlTxRef.SttlmInf.SttlmMtd", f"Original Settlement Method '{ref_mtd}' is invalid. Allowed: INDA, INGA, COVE."))

        # Payment Method: CHK, TRF, DD, TRA
        pmt_mtd = get_val(f"{tx_path}.OrgnlTxRef.PmtMtd")
        if pmt_mtd and pmt_mtd not in ["CHK", "TRF", "DD", "TRA"]:
             report.add_issue(ValidationIssue("ERROR", 3, "PACS004_PMTMETHOD_ENUM", f"{tx_path}.OrgnlTxRef.PmtMtd", f"Payment Method '{pmt_mtd}' is invalid for pacs.004."))

        # --------------------------------------------------
        # 4 & 5. AGENT & PARTY IDENTIFICATION RULES (Rule 1A & 1B)
        # --------------------------------------------------
        
        agent_paths = [
            f"{gh_path}.SttlmInf.InstgAgt",
            f"{gh_path}.SttlmInf.InstdAgt",
            f"{tx_path}.InstgAgt",
            f"{tx_path}.InstdAgt",
            f"{tx_path}.RtrChain.DbtrAgt",
            f"{tx_path}.RtrChain.CdtrAgt",
            f"{tx_path}.RtrChain.IntrmyAgt1",
            f"{tx_path}.RtrChain.IntrmyAgt2",
            f"{tx_path}.RtrChain.IntrmyAgt3"
        ]

        party_paths = [
            f"{tx_path}.RtrChain.Dbtr",
            f"{tx_path}.RtrChain.Cdtr",
            f"{tx_path}.RtrChain.UltmtDbtr",
            f"{tx_path}.RtrChain.UltmtCdtr",
            f"{gh_path}.InitgPty"
        ]

        def validate_agent(base_path):
             # Only validate if the agent block actually exists in the message
             if not any(k.startswith(base_path) for k in canonical_data.keys()):
                 return

             bic = get_val(f"{base_path}.FinInstnId.BICFI")
             nm = get_val(f"{base_path}.FinInstnId.Nm")
             addr = any(k.startswith(f"{base_path}.FinInstnId.PstlAdr") for k in canonical_data.keys())
             clrsys = any(k.startswith(f"{base_path}.FinInstnId.ClrSysMmbId") for k in canonical_data.keys())
             
             # Mandatory BICFI for InstgAgt and InstdAgt if they are present
             if "InstgAgt" in base_path or "InstdAgt" in base_path:
                 if not bic:
                      report.add_issue(ValidationIssue("ERROR", 3, "PACS004_AGENT_BIC_MANDATORY", base_path, "Instructing and Instructed Agents MUST have a BICFI if provided."))

             if bic:
                 if nm or addr:
                      report.add_issue(ValidationIssue("ERROR", 3, "PACS004_AGENT_RULE_1A_BIC", base_path, "If BICFI is present, Name (Nm) and Postal Address (PstlAdr) MUST NOT be present for Agents."))
             else:
                 # Check logic: (Nm + PstlAdr) OR (Nm + PstlAdr + ClrSysMmbId)
                 if not nm or not addr:
                      # Exception: ClrSysMmbId alone allowed if country check passed (difficult in flat map)
                      if not clrsys:
                           report.add_issue(ValidationIssue("ERROR", 3, "PACS004_AGENT_RULE_1A_NOBIC", base_path, "If BICFI is absent, Agent must have both Name and Postal Address."))

        def validate_party(base_path):
             # Only validate if the party block actually exists in the message
             if not any(k.startswith(base_path) for k in canonical_data.keys()):
                 return

             bic = get_val(f"{base_path}.Id.OrgId.AnyBIC") or get_val(f"{base_path}.Id.PrvtId.AnyBIC")
             nm = get_val(f"{base_path}.Nm")
             addr = any(k.startswith(f"{base_path}.PstlAdr") for k in canonical_data.keys())

             if bic:
                 if nm or addr:
                      report.add_issue(ValidationIssue("ERROR", 3, "PACS004_PARTY_RULE_1B_BIC", base_path, "If AnyBIC is present, Name (Nm) and Postal Address (PstlAdr) MUST NOT be present for Parties."))
             elif nm:
                 if not addr:
                      report.add_issue(ValidationIssue("WARNING", 3, "PACS004_PARTY_RULE_1B_ADDR_RECO", base_path, "Postal Address (PstlAdr) is recommended when Name (Nm) is provided."))

        for ap in agent_paths: validate_agent(ap)
        for pp in party_paths: validate_party(pp)

        # --------------------------------------------------
        # 6. RETURN CHAIN VALIDATIONS
        # --------------------------------------------------
        # RtrChain mandatory already checked in step 1.
        
        # Must contain Dbtr and Cdtr mandatory
        rc_path = f"{tx_path}.RtrChain"
        if any(k.startswith(rc_path) for k in canonical_data.keys()):
            if not any(k.startswith(f"{rc_path}.Dbtr") for k in canonical_data.keys()):
                 report.add_issue(ValidationIssue("ERROR", 3, "PACS004_RTRCHAIN_DBTR_MISSING", rc_path, "Return Chain must contain a Debtor (Dbtr)."))
            if not any(k.startswith(f"{rc_path}.Cdtr") for k in canonical_data.keys()):
                 report.add_issue(ValidationIssue("ERROR", 3, "PACS004_RTRCHAIN_CDTR_MISSING", rc_path, "Return Chain must contain a Creditor (Cdtr)."))

        # UltmtDbtr, UltmtCdtr, InitgPty MUST be Pty only (NOT Agt)
        # Checking this usually involves verifying they don't have FinInstnId
        for p in [f"{tx_path}.RtrChain.UltmtDbtr", f"{tx_path}.RtrChain.UltmtCdtr", f"{gh_path}.InitgPty"]:
             if any(k.startswith(f"{p}.FinInstnId") for k in canonical_data.keys()):
                  report.add_issue(ValidationIssue("ERROR", 3, "PACS004_PTY_NOT_AGT", p, "Ultimate Parties and Initiating Party must be Party (Pty) identification, not Financial Institution identification."))

        # --------------------------------------------------
        # 8 & 9. CHARSET & FIELD LENGTH VALIDATIONS
        # --------------------------------------------------
        
        # FIN-X fields validation handled by common Layer 3 if included, but added here specifically for IDs
        fin_x_patt = r"^[0-9a-zA-Z/\-\?:\(\)\.,'\+ ]+$"
        for path, val in canonical_data.items():
            if any(x in path for x in ["Id", "MsgId", "RtrId", "InstrId", "UETR"]):
                 if not re.match(fin_x_patt, str(val)):
                      report.add_issue(ValidationIssue("WARNING", 3, "PACS004_FINX_CHARSET", path, "Field contains non-FIN-X characters. CBPR+ expects FIN-X for identifiers."))
            
            # Lengths
            if "Nm" in path and len(str(val)) > 140:
                 report.add_issue(ValidationIssue("ERROR", 3, "PACS004_NM_LEN", path, "Name exceeds 140 characters."))
            if "AddtlInf" in path and len(str(val)) > 105:
                 report.add_issue(ValidationIssue("ERROR", 3, "PACS004_ADDINF_LEN", path, "Additional Information exceeds 105 characters per line."))
            if "AdrLine" in path and len(str(val)) > 70:
                 report.add_issue(ValidationIssue("ERROR", 3, "PACS004_ADRLINE_LEN", path, "Address line exceeds 70 characters."))
            if "ClrSysMmbId" in path and len(str(val)) > 28:
                 report.add_issue(ValidationIssue("ERROR", 3, "PACS004_CLRSYS_LEN", path, "Clearing System Member ID exceeds 28 characters."))

        # --------------------------------------------------
        # 10. ORIGINAL TRANSACTION REFERENCE
        # --------------------------------------------------
        ref_path = f"{tx_path}.OrgnlTxRef"
        if any(k.startswith(ref_path) for k in canonical_data.keys()):
             # Amt -> only one allowed: InstdAmt OR EqvtAmt
             has_instd = any(k.startswith(f"{ref_path}.Amt.InstdAmt") for k in canonical_data.keys())
             has_eqvt = any(k.startswith(f"{ref_path}.Amt.EqvtAmt") for k in canonical_data.keys())
             if has_instd and has_eqvt:
                  report.add_issue(ValidationIssue("ERROR", 3, "PACS004_REF_AMT_CONFLICT", ref_path, "Only one of InstdAmt or EqvtAmt is allowed in OrgnlTxRef."))
             
             # ReqdExctnDt: either Dt or DtTm (NOT both)
             has_dt = any(k.startswith(f"{ref_path}.ReqdExctnDt.Dt") for k in canonical_data.keys())
             has_dt_tm = any(k.startswith(f"{ref_path}.ReqdExctnDt.DtTm") for k in canonical_data.keys())
             if has_dt and has_dt_tm:
                  report.add_issue(ValidationIssue("ERROR", 3, "PACS004_REF_DATE_CONFLICT", ref_path, "Only one of Dt or DtTm is allowed in ReqdExctnDt."))

        # Final Layer 3 Assessment is handled by the main validator
        pass
