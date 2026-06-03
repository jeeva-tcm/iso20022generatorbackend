import time
import uuid
import threading
import re
import os
import json
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Set
import zipfile
from lxml import etree

from .models import ValidationIssue, ValidationReport
from .layer1_validator import Layer1Mixin
from .layer2_validator import Layer2Mixin
from .layer3_validator import Layer3Mixin
from .layer3_timing import validateLayer3Timing
from .pacs004_validator import Pacs004Mixin
from .cbpr_json_validator import CBPRJsonSchemaMixin


class ISOValidator(Layer1Mixin, Layer2Mixin, Layer3Mixin, Pacs004Mixin, CBPRJsonSchemaMixin):

    _id_lock = threading.Lock()
    _daily_counter = 0
    _counter_date = ""

    def __init__(self, history_service=None):
        self.history_service = history_service
        # Path configuration
        base_dir = os.path.dirname(os.path.abspath(__file__))
        backend_root = os.path.normpath(os.path.join(base_dir, "../../"))
        
        self.xsd_path = os.path.join(backend_root, "xsds", "extracted")
        self.rules_path = os.path.join(backend_root, "app", "resources", "rules")
        self.codelists_path = os.path.join(backend_root, "app", "resources", "codelists")
        self.bics_path = os.path.join(backend_root, "bics")
        
        # Load Configuration
        self.config_path = os.path.join(backend_root, "app", "resources", "config.json")
        self.config = self._load_config()
        
        self.cutoff_config_path = os.path.join(backend_root, "app", "resources", "cutoff_timings.json")
        self.cutoff_config = {}
        if os.path.exists(self.cutoff_config_path):
            try:
                with open(self.cutoff_config_path, 'r') as f:
                    self.cutoff_config = json.load(f)
            except Exception as e:
                print(f"Error loading cutoff config: {e}")
        
        # Step 4 Mapping: Version to SR Version (Dynamic)
        self.sr_mapping = self.config.get("sr_versions", {
            "pacs.008.001.08": "SR2025",
            "pacs.009.001.08": "SR2025"
        })
        
        # Cache for message types
        self._message_type_cache = []
        self._last_cache_update = 0
        self._cache_duration = 3600 # 1 hour

        # Memoized XSD path lookups. _get_xsd_path() does an os.listdir() fallback
        # scan on every call (the XSD filenames are versioned, so the exact-match
        # almost always misses and the directory scan fires). The result is
        # deterministic per message_type, so cache it — this was ~57ms of
        # nt.listdir per validation batch.
        self._xsd_path_cache: Dict[str, Optional[str]] = {}
        
        # Load Reference Data
        self._ensure_xsds_extracted()
        self.supported_bics = self._load_bics()
        self.codelists = self._load_codelists()
        
        print(f"ISOValidator Initialized:")
        print(f" - XSD Path: {self.xsd_path}")
        print(f" - Config Loaded: {bool(self.config)}")
        print(f" - BICs Loaded: {len(self.supported_bics)}")

    def generate_next_id(self) -> str:
        """Generates the next sequential validation ID in format VAL{DDMMYY}{00001}"""
        today = time.strftime('%d%m%y')
        
        # 1. Try Firebase for globally unique/persistent sequence
        if self.history_service and self.history_service.enabled:
            try:
                seq = self.history_service.get_next_sequence(today)
                if seq is not None:
                    return f"VAL{today}{seq:05d}"
            except Exception as e:
                print(f"DEBUG: Firebase ID generation failed, falling back: {e}")

        # 2. Fallback to in-memory sequential counter
        with ISOValidator._id_lock:
            if ISOValidator._counter_date != today:
                ISOValidator._counter_date = today
                ISOValidator._daily_counter = 0
            ISOValidator._daily_counter += 1
            seq = ISOValidator._daily_counter
        return f"VAL{today}{seq:05d}"

    def reset_counter(self):
        """Resets the daily counter back to 0"""
        with ISOValidator._id_lock:
            ISOValidator._daily_counter = 0

    def _load_config(self) -> Dict[str, Any]:
        """Loads the dynamic configuration file"""
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, 'r') as f:
                    return json.load(f)
            except Exception as e:
                print(f"Error loading config: {e}")
        return {}

    def _load_bics(self) -> Set[str]:
        """
        Loads BIC codes from the entities.ftm.json file (JSONL format).

        Parsing the 19 MB JSONL line-by-line costs ~0.3s and is paid on every
        process (re)start — which dominates the dev `--reload` cycle. We cache
        the parsed result to a pickle keyed by the source file's (mtime, size);
        a subsequent start with an unchanged source loads it in a few ms. The
        output (the BIC set + self.bic_records) is byte-for-byte identical to a
        full parse, and any change to the source file invalidates the cache.
        """
        bics = set()
        self.bic_records = [] # Store simplified records for search
        file_path = os.path.join(self.bics_path, "entities.ftm.json")
        if not os.path.exists(file_path):
            return bics

        cache_path = os.path.join(self.bics_path, "entities.ftm.cache.pkl")
        sig = None
        try:
            st = os.stat(file_path)
            sig = (int(st.st_mtime), st.st_size)
        except OSError:
            sig = None

        # Fast path: reuse the cache when the source file is unchanged.
        if sig is not None and os.path.exists(cache_path):
            try:
                import pickle
                with open(cache_path, "rb") as cf:
                    cached = pickle.load(cf)
                if cached.get("sig") == sig:
                    self.bic_records = cached["records"]
                    return set(cached["bics"])
            except Exception as e:
                print(f"[BIC Cache] ignoring unreadable cache: {e}")

        # Slow path: parse the JSONL source.
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                for i, line in enumerate(f, 1):
                    if not line.strip(): continue
                    try:
                        data = json.loads(line)
                        props = data.get("properties", {})
                        swift_bics = props.get("swiftBic", [])
                        name = str(data.get("caption") or props.get("name", ["Unknown Bank"])[0])
                        country = str(props.get("country", [""])[0]).upper()
                        address = str(props.get("address", [""])[0])

                        for bic in swift_bics:
                            if not bic: continue
                            bic_upper = str(bic).upper()
                            bics.add(bic_upper)
                            self.bic_records.append({
                                "bic": bic_upper,
                                "name": name,
                                "country": country,
                                "address": address
                            })
                    except Exception as e:
                        print(f"Skipping line {i} in entities.ftm.json due to error: {e}")
                        continue
        except Exception as e:
            print(f"Error loading BICs from {file_path}: {e}")
            return bics

        # Write the cache for the next start (best-effort — never fatal).
        if sig is not None:
            try:
                import pickle
                tmp_path = cache_path + ".tmp"
                with open(tmp_path, "wb") as cf:
                    pickle.dump({"sig": sig, "bics": list(bics), "records": self.bic_records},
                                cf, protocol=pickle.HIGHEST_PROTOCOL)
                os.replace(tmp_path, cache_path)
            except Exception as e:
                print(f"[BIC Cache] could not write cache: {e}")
        return bics

    def reload_bics(self) -> None:
        """
        Hot-reload the BIC dataset from disk into memory without disrupting
        active search requests (BR-3, BR-7).

        Builds new data structures first, then swaps the references in a single
        Python assignment per attribute.  Because Python's GIL makes a single
        attribute assignment atomic, any concurrent call to ``search_bics`` or
        ``supported_bics`` will see either the old or the new complete dataset —
        never a partial state.
        """
        new_bics: Set[str] = set()
        new_records: List[Dict[str, Any]] = []
        file_path = os.path.join(self.bics_path, "entities.ftm.json")

        if os.path.exists(file_path):
            try:
                with open(file_path, "r", encoding="utf-8") as fh:
                    for i, line in enumerate(fh, 1):
                        if not line.strip():
                            continue
                        try:
                            data = json.loads(line)
                            props = data.get("properties", {})
                            swift_bics = props.get("swiftBic", [])
                            name = str(
                                data.get("caption")
                                or props.get("name", ["Unknown Bank"])[0]
                            )
                            country = str(props.get("country", [""])[0]).upper()
                            address = str(props.get("address", [""])[0])
                            for bic in swift_bics:
                                if not bic:
                                    continue
                                bic_upper = str(bic).upper()
                                new_bics.add(bic_upper)
                                new_records.append(
                                    {
                                        "bic": bic_upper,
                                        "name": name,
                                        "country": country,
                                        "address": address,
                                    }
                                )
                        except Exception as exc:
                            print(f"[BIC Reload] Skipping line {i}: {exc}")
                            continue
            except Exception as exc:
                print(f"[BIC Reload] Error reading file: {exc}")
                return  # Retain existing cache on read failure

        # Atomic reference swaps (GIL-safe single assignments)
        self.supported_bics = new_bics
        self.bic_records = new_records
        print(f"[BIC Reload] Cache refreshed — {len(new_bics):,} BIC codes loaded.")

    def search_bics(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Searches for BICs matching the query in either the BIC code or bank name"""
        if not query or len(query) < 2:
            return []
        
        query = query.upper()
        results = []
        for record in self.bic_records:
            # Safely check bic and name
            bic_val = record.get("bic", "")
            name_val = record.get("name", "").upper()
            if query in bic_val or query in name_val:
                results.append(record)
                if len(results) >= limit:
                    break
        return results

    def _ensure_xsds_extracted(self):
        """
        High-Performance Extraction Engine:
        Automatically unzips all XSD blueprints from the ZIP library into 
        the 'extracted' directory for instant validation readiness.
        """
        source_dir = os.path.dirname(self.xsd_path)
        if not os.path.exists(self.xsd_path):
            os.makedirs(self.xsd_path)

        if not os.path.exists(source_dir):
            return

        print(f"Auto-Syncing XSD Library...")
        import zipfile
        for filename in os.listdir(source_dir):
            if filename.endswith(".zip"):
                zip_path = os.path.join(source_dir, filename)
                try:
                    with zipfile.ZipFile(zip_path, 'r') as zf:
                        # Extract only .xsd files that don't exist yet to save time
                        for member in zf.namelist():
                            if member.endswith(".xsd"):
                                base_name = os.path.basename(member)
                                if not base_name: continue
                                
                                target_file = os.path.join(self.xsd_path, base_name)
                                if not os.path.exists(target_file):
                                    with zf.open(member) as source, open(target_file, 'wb') as target:
                                        target.write(source.read())
                except Exception as e:
                    print(f"Warning: Could not extract {filename}: {e}")

    def _load_codelists(self) -> Dict[str, Any]:
        """Loads all JSON codelists from the resource directory (Lowercased keys)"""
        lists = {}
        if not os.path.exists(self.codelists_path):
            return lists
        
        for filename in os.listdir(self.codelists_path):
            if filename.endswith(".json"):
                try:
                    with open(os.path.join(self.codelists_path, filename), 'r', encoding='utf-8-sig') as f:
                        lists[filename.replace(".json", "").lower()] = json.load(f)
                except Exception as e:
                    print(f"Error loading codelist {filename}: {e}")
                    continue
        return lists

    def get_supported_messages(self) -> List[str]:
        """
        Scans the XSD directory and ZIP files for supported message types.
        Caches results for performance.
        """
        now = time.time()
        # Reduce cache duration for more frequent updates during dev
        if self._message_type_cache and (now - self._last_cache_update < 60):
            return self._message_type_cache

        messages = set()
        
        # 1. Scan extracted directory
        if os.path.exists(self.xsd_path):
            for root, dirs, files in os.walk(self.xsd_path):
                for file in files:
                    if file.endswith(".xsd"):
                        # Ensure we handle various nesting if needed
                        msg_id = file.rsplit('.', 1)[0]
                        messages.add(msg_id)
        
        # 2. Scan ZIP files in the xsds root
        source_dir = os.path.dirname(self.xsd_path)
        if os.path.exists(source_dir):
            for filename in os.listdir(source_dir):
                if filename.endswith(".zip"):
                    zip_path = os.path.join(source_dir, filename)
                    try:
                        with zipfile.ZipFile(zip_path, 'r') as zf:
                            for name in zf.namelist():
                                if name.lower().endswith(".xsd"):
                                    base = os.path.basename(name).rsplit('.', 1)[0]
                                    if base:
                                        messages.add(base)
                    except:
                        pass
        
        if not messages:
            fallback = self.config.get("fallback_message_types", [
                "pacs.008.001.08", "pacs.009.001.08", "pacs.002.001.10"
            ])
            self._message_type_cache = sorted(fallback)
            print(f"XSD Discovery: No files found. Using {len(fallback)} fallbacks.")
        else:
            self._message_type_cache = sorted(list(messages))
            print(f"XSD Discovery: Successfully indexed {len(self._message_type_cache)} message types.")
            
        self._last_cache_update = now
        return self._message_type_cache

    async def validate(self, xml_content: str, mode: str = "Full 1-3", message_type: str = "Auto-detect", filename: Optional[str] = None, validation_id: Optional[str] = None) -> ValidationReport:
        """
        Main 10-Step Validation Flow
        """
        start_time = time.time()
        
        # 0. Detect Identity or use provided type
        if not message_type or message_type == "Auto-detect":
            detected_type = self._detect_message_type(xml_content)
        else:
            detected_type = message_type

        # Keep full version in report for UI display
        # The _get_xsd_path function will handle version-blind matching internally

        assigned_id = validation_id if validation_id else self.generate_next_id()
        report = ValidationReport(assigned_id, detected_type, mode)

        # Extract MsgId and UETR early for history and metadata
        msg_id_match = re.search(r'<MsgId>\s*([^<]+?)\s*</MsgId>', xml_content, re.IGNORECASE)
        uetr_match = re.search(r'<UETR>\s*([^<]+?)\s*</UETR>', xml_content, re.IGNORECASE)
        
        if msg_id_match:
            report.metadata["MsgId"] = msg_id_match.group(1).strip()
        if uetr_match:
            report.metadata["UETR"] = uetr_match.group(1).strip()

        try:
            # STEP 1 & 2: Safe XML Parse & Well-formedness (Layer 1)
            # STEP 3: Identity & Rejection logic is here
            try:
                if not await self._run_layer_1(xml_content, report, filename):
                    report.layer_status['2'] = {"status": "SKIPPED", "time": 0}
                    report.layer_status['3'] = {"status": "SKIPPED", "time": 0}
                    return self._finalize_report(report, start_time)
            except Exception as e:
                report.add_issue(ValidationIssue("ERROR", 1, "FATAL_L1", "/", f"Critical failure in Layer 1: {str(e)}", "Check if XML is properly formed."))
                return self._finalize_report(report, start_time)

            # Post-parsing cleanup: If type was "Unknown", try to refine from detected namespace
            try:
                if (report.message_type == "Unknown" or not report.message_type) and "Namespace" in report.metadata:
                    ns = report.metadata["Namespace"]
                    extracted = "Unknown"
                    if "xsd:" in ns:
                        extracted = ns.split("xsd:")[-1]
                    elif any(f in ns for f in ["pacs.", "camt.", "pain.", "sese.", "head."]):
                        parts = ns.split(":")
                        extracted = parts[-1]
                    
                    if extracted != "Unknown":
                        # Keep full version for display
                        report.message_type = extracted
                        detected_type = extracted
            except:
                pass # Non-critical failure

            # Final check to force COV or ADV variant on report if elements are present
            if report.message_type and report.message_type.startswith("pacs.009") and "cov" not in report.message_type.lower() and "adv" not in report.message_type.lower():
                if "<Prtry>ADV</Prtry>" in xml_content:
                    report.message_type = "pacs.009.001.08_ADV"
                    detected_type = "pacs.009.001.08_ADV"
                elif "UndrlygCstmrCdtTrf" in xml_content or "cov" in xml_content.lower():
                    report.message_type = "pacs.009.cov"
                    detected_type = "pacs.009.cov"

            # STEP 4.5: DATE VALIDATION — runs on raw XML before Layer 2
            # so past-date errors are ALWAYS reported even when XSD also fails.
            self._validate_dates_in_xml(xml_content, report, start_time)

            # STEP 4.6: ID FIELD MAX-LENGTH VALIDATION — runs on raw XML before Layer 2
            self._validate_id_lengths_in_xml(xml_content, report)

            # STEP 4.7: UETR UUID v4 FORMAT VALIDATION — runs on raw XML before Layer 2
            self._validate_uetr_in_xml(xml_content, report)

            # STEP 4.8: IBAN / BBAN ACCOUNT IDENTIFIER VALIDATION
            self._validate_account_identifiers_in_xml(xml_content, report)

            # STEP 4.9: NbOfTxs COUNT VALIDATION
            self._validate_nboftxs(xml_content, report)

            # STEP 4.10: SWIFT CHARACTER SET VALIDATION (Remittance)
            self._validate_swift_charset(xml_content, report)

            # STEP 4.11: CHARGES CURRENCY MATCH VALIDATION
            self._validate_charges_currency(xml_content, report)

            # STEP 4.12: PARTY IDENTIFICATION VALIDATION
            self._validate_party_rules(xml_content, report)

            # STEP 4.13: ADDRESS CBPR+ RULES VALIDATION
            self._validate_address_cbpr_rules(xml_content, report)

            # STEP 4.14: REMITTANCE INFORMATION RULES
            self._validate_remittance_rules(xml_content, report)

            # STEP 4.15: CLEARING SYSTEM SPECIFIC RULES
            self._validate_clearing_system_rules(xml_content, report)

            # STEP 4.16: CHARACTER SET VALIDATION (Nm, AdrLine, StrtNm, TwnNm, etc.)
            self._validate_charsets_in_xml(xml_content, report)

            # STEP 4.17: DUPLICATE IDENTIFIER VALIDATION
            self._validate_duplicate_ids(xml_content, report)

            # STEP 4.18: DUPLICATE TAG VALIDATION (Layer 3 Business Rules)
            self._validate_duplicate_tags(xml_content, report, detected_type)

            # STEP 4.19: SCHEME NAME ALLOWLIST VALIDATION (Strict Policy)
            self._validate_schme_nm_in_xml(xml_content, report)

            # Step 4.20: PRIORITY ENTITY MISMATCH CHECK (Layer 3 rule moved forward)
            # This allows the business rule to appear before Schema (Layer 2) errors.
            self.validate_entity_mismatch(xml_content, report)

            if mode != "Layer 1 only":
                try:
                    # STEP 4.21: CBPR+ DATETIME FORMAT VALIDATION
                    self._validate_cbpr_datetime(xml_content, report)
                    
                    # Global Rule: Name & Address Co-existence (CBPR+)
                    if not report.message_type.startswith("pain"):
                        self._validate_name_address_coexistence(xml_content, report)

                    # Global Rule: Empty mandatory containers (e.g. <FinInstnId/>) —
                    # caught BEFORE Layer 2 so missing identifiers fail validation even
                    # when the XSD declares all children as minOccurs="0".
                    self._validate_empty_required_containers(xml_content, report)

                    # HEAD001_MSGDEFIDR_MATCHES_PAYLOAD + BizSvc format + tz drift.
                    # XSD cannot catch this because header and payload validate against
                    # different schemas; this is a frequent integration/routing bug.
                    self._validate_apphdr_payload_match(xml_content, report)

                    # KB-driven cross-tag dependency rules (resources/KB/<family>.json),
                    # e.g. BizMsgIdr must equal GrpHdr/MsgId. Enforces documented CBPR+
                    # rules not covered elsewhere; dedup-guarded so it never double-reports.
                    self._validate_kb_dependency_rules(xml_content, report, detected_type)

                    # SWIFT CBPR+ Rule: pain.008 GrpHdr must contain FwdgAgt.
                    # Base XSD declares FwdgAgt optional, but SWIFT MyStandards
                    # promotes it to mandatory for cross-border direct debits.
                    self._validate_pain008_fwdgagt_rule(xml_content, report)

                    layer2_success = await self._run_layer_2(xml_content, report, detected_type)
                except Exception as e:
                    report.add_issue(ValidationIssue("ERROR", 2, "FATAL_L2", "/", f"Critical failure in Layer 2 (XSD): {str(e)}", "Ensure the XSD library is available."))
                    return self._finalize_report(report, start_time)

            # STEP 4.22: CBPR+ JSON Schema type-level check (folded into Layer 3)
            # Validates every leaf element (BICFI, IBAN, MmbId, Nm, addresses, etc.)
            # against the constraints in the SWIFT MyStandards JSON schema for the
            # detected message type. Runs only when a schema file is available; silently
            # no-ops otherwise so it never blocks the existing pipeline.
            try:
                self._run_cbpr_json_schema_check(xml_content, report, detected_type)
            except Exception as e:
                report.add_issue(ValidationIssue(
                    "WARNING", 3, "CBPR_JSON_FAULT", "/",
                    f"CBPR+ JSON Schema check could not run: {str(e)[:120]}",
                    "Non-fatal — the rest of the validation still runs.",
                ))

            # Only skip Layer 3 if there are errors in Layer 1 or Layer 2. 
            # Layer 3 errors (like from CBPR_JSON check) should not prevent the rest of Layer 3 from running,
            # nor should they cause themselves to be erased by the SKIPPED cleanup logic.
            l1_l2_errors = sum(1 for i in report.issues if str(getattr(i, 'layer', i.get('layer', ''))) in ['1', '2'] and getattr(i, 'severity', i.get('severity', '')) == 'ERROR')
            
            if l1_l2_errors > 0:
                report.layer_status['3'] = {"status": "SKIPPED", "time": 0}
                return self._finalize_report(report, start_time)

            # STEP 5: Canonical Normalization for Rule Execution
            try:
                canonical_data, line_map = self._normalize_message(xml_content)
            except Exception as e:
                report.add_issue(ValidationIssue("ERROR", 3, "FATAL_L3", "/", f"Failed to normalize message: {str(e)}"))
                return self._finalize_report(report, start_time)

            # STEP 5.0: Run Generic Field Library & Global Algorithms Validation
            self._run_generic_field_validation(detected_type, canonical_data, line_map, report)

            # STEP 5.1: PACS.004 SPECIALIZED VALIDATION (SR2025)
            if "pacs.004" in detected_type:
                await self._validate_pacs_004(xml_content, canonical_data, line_map, report)

            # STEP 5.1: FAIL-FAST SANCTIONS SCREENING (Dynamic)
            sanctions_config = self.config.get("sanctions", {})
            sanctioned_codes = set(sanctions_config.get("codes", []))
            sanctioned_names = set(sanctions_config.get("names", []))
            
            # Use defaults if config is empty for fallback safety
            if not sanctioned_codes: sanctioned_codes = {'AF', 'RU', 'KP'}
            if not sanctioned_names: sanctioned_names = {'russia', 'iran', 'north korea'}
            
            for path, value in canonical_data.items():
                # Check fields associated with parties (Debtor, Creditor, Agents)
                if any(x in path for x in ['Dbtr', 'Cdtr', 'InstgAgt', 'InstdAgt', 'InitgPty', 'Pty']):
                    str_val = str(value).strip().lower()
                    # Check ISO code (in Ctry tags)
                    if 'Ctry' in path and value in sanctioned_codes:
                        line_num = str(line_map.get(path, "/"))
                        report.add_issue(ValidationIssue("ERROR", 3, "SANCTIONS_BLOCKED", line_num, f"Fail-fast: Party from sanctioned country code '{value}' detected.", "Transaction rejected due to sanctions compliance."))
                        break
                    # Check names in address/name fields
                    if any(name in str_val for name in sanctioned_names):
                        # Simple keyword hit
                        hit = next(name for name in sanctioned_names if name in str_val)
                        line_num = str(line_map.get(path, "/"))
                        report.add_issue(ValidationIssue("ERROR", 3, "SANCTIONS_BLOCKED", line_num, f"Fail-fast: Party from sanctioned country '{hit.title()}' detected.", "Transaction rejected due to sanctions compliance."))
                        break
            
            if any(i['code'] == 'SANCTIONS_BLOCKED' for i in report.issues):
                 report.layer_status['3'] = {"status": "FAIL", "time": round((time.time() - start_time) * 1000, 2)}
                 return self._finalize_report(report, start_time)

            # STEP 6-9: Dynamic Rule Engine (Layers 1-3)
            # Load all rules once
            try:
                all_rules = self._load_all_rules(detected_type)
                
                if mode == "Full 1-3":
                    for layer_id in [3]:
                        self._run_dynamic_layer(layer_id, all_rules, canonical_data, line_map, report)
                        
                        # --- Apply Layer 3 Timing Validation ---
                        try:
                            if self.cutoff_config:
                                # Dynamic Defaults
                                t_conf = self.config.get("timing_defaults", {})
                                debtor_country = t_conf.get("debtor_country", "US")
                                creditor_country = t_conf.get("creditor_country", "GB")
                                debtor_sys = t_conf.get("debtor_system", "FEDWIRE")
                                creditor_sys = t_conf.get("creditor_system", "CHAPS")
                                
                                for k, v in canonical_data.items():
                                    if k.endswith('.Dbtr.PstlAdr.Ctry') or k.endswith('.InitgPty.PstlAdr.Ctry'):
                                        debtor_country = v
                                    if k.endswith('.Cdtr.PstlAdr.Ctry'):
                                        creditor_country = v
                                        
                                d_sys_conf = self.cutoff_config.get("timings", {}).get(debtor_country, {}).get("paymentSystems", {})
                                if d_sys_conf: debtor_sys = list(d_sys_conf.keys())[0]
                                c_sys_conf = self.cutoff_config.get("timings", {}).get(creditor_country, {}).get("paymentSystems", {})
                                if c_sys_conf: creditor_sys = list(c_sys_conf.keys())[0]

                                ctx = {
                                    "debtorCountry": debtor_country,
                                    "debtorPaymentSystem": debtor_sys,
                                    "creditorCountry": creditor_country,
                                    "creditorPaymentSystem": creditor_sys,
                                    "submissionTimestamp": datetime.now(timezone.utc).isoformat(),
                                    "validationMode": "STRICT"
                                }
                                
                                # Extract specific timing fields into payload
                                t_payload = {}
                                t_paths = {} # FieldName in payload -> Canonical Path in message
                                for k, v in canonical_data.items():
                                    # Fallback country detection from BIC
                                    if k.endswith('.BICFI') or k == 'BICFI':
                                        bic_val = str(v).strip().upper()
                                        if len(bic_val) >= 6:
                                            extracted_ctry = bic_val[4:6]
                                            if 'Dbtr' in k or 'InstgAgt' in k:
                                                ctx["debtorCountry"] = extracted_ctry
                                            if 'Cdtr' in k or 'InstdAgt' in k:
                                                ctx["creditorCountry"] = extracted_ctry

                                    if k.endswith('.CreDtTm') or k.endswith('.CreDt') or k in ['CreDtTm', 'CreDt']:
                                        t_payload['CreDtTm'] = v
                                        t_paths['CreDtTm'] = k
                                    elif k.endswith('.ReqdExctnDt') or k == 'ReqdExctnDt':
                                        t_payload['ReqdExctnDt'] = v
                                        t_paths['ReqdExctnDt'] = k
                                    elif k.endswith('.IntrBkSttlmDt') or k == 'IntrBkSttlmDt':
                                        t_payload['IntrBkSttlmDt'] = v
                                        t_paths['IntrBkSttlmDt'] = k
                                    elif 'MsgDefIdr' in k:
                                        t_payload['MsgDefIdr'] = v
                                        
                                timing_result = validateLayer3Timing(t_payload, ctx, self.cutoff_config)
                                for iss_dict in timing_result.get("issues", []):
                                    field_key = iss_dict.get("field", "CreDtTm") # Default to creation time if missing
                                    matched_path = t_paths.get(field_key, "/")
                                             
                                    # Fallback search if path is still /
                                    if matched_path == "/":
                                        for k in canonical_data.keys():
                                            if k.endswith(f".{field_key}") or k == field_key:
                                                matched_path = k
                                                break
                                                
                                    line = str(line_map.get(matched_path, "/"))
                                    severity = "ERROR" if iss_dict["severity"] == "FAIL" else ("WARNING" if iss_dict["severity"] == "WARN" else iss_dict["severity"])
                                    
                                    details_str = ""
                                    if "computed" in iss_dict.get("details", {}):
                                         details_str = f"Recommended Value Date: {iss_dict['details']['computed'].get('recommendedValueDate', '')}"
                                    
                                    full_msg = f"{iss_dict['message']} {details_str}".strip()
                                    report.add_issue(ValidationIssue(severity, 3, iss_dict["ruleId"], line, full_msg, details_str))
                                    
                                if timing_result.get("status") == "FAIL":
                                    report.layer_status['3'] = {"status": "❌", "time": round((time.time() - start_time) * 1000, 2)}
                        except Exception as e:
                            print(f"DEBUG Timing Validation Error: {e}")

                        # Stop if this layer failed
                        layer_status = report.layer_status.get(str(layer_id), {}).get("status")
                        if layer_status == "❌":
                            break
            except Exception as e:
                report.add_issue(ValidationIssue("WARNING", 3, "RULE_ENGINE_ERR", "/", f"Rule Engine encountered an issue: {str(e)}", "Partial validation completed."))

        except Exception as e:
            report.add_issue(ValidationIssue("ERROR", 0, "SYSTEM_ERR", "/", f"General system failure: {str(e)}"))
            import traceback; traceback.print_exc()
            
        return self._finalize_report(report, start_time)

    def _validate_dates_in_xml(self, xml_content: str, report: ValidationReport, start_time: float) -> None:
        """
        Step 4.5 — Past Date Validation
        Scans the raw XML string directly for ALL date and datetime values.
        This runs BEFORE Layer 2 so past-date errors are always reported,
        even when the XSD also finds other errors (e.g. invalid amount/IBAN).

        Supported formats:
          2026-03-02                       (XML date)
          2026-03-02T10:35:00              (XML dateTime, no tz)
          2026-03-02T10:35:00Z             (XML dateTime, UTC)
          2026-03-02T10:35:00+05:30        (XML dateTime, offset)
          2026-03-02T10:35:00.123+00:00    (XML dateTime, ms + offset)
        """
        today_date = datetime.now().date()

        # Matches tag + value pairs: <TagName>2026-02-01T10:35:00+00:00</TagName>
        # Captures: (1) tag name  (2) date/datetime value  (3) optional time+tz part
        xml_date_patt = re.compile(
            r'<([A-Za-z][A-Za-z0-9]*)>'           # opening tag
            r'\s*'
            r'(\d{4}-\d{2}-\d{2}'                  # date part  (group 2)
            r'(?:T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)?)'  # optional time+tz
            r'\s*'
            r'</\1>'                               # matching closing tag
        )

        seen = set()  # avoid duplicate errors for the EXACT same occurrence
        for m in xml_date_patt.finditer(xml_content):
            tag_name  = m.group(1)
            raw_value = m.group(2).strip()
            # Include the byte-offset so multiple occurrences of the same
            # (tag, value) pair (e.g. AppHdr/CreDtTm AND GrpHdr/CreDtTm both
            # holding the same past datetime) each get their own issue and
            # therefore their own fix suggestion.
            key = (tag_name, raw_value, m.start())
            if key in seen:
                continue
            seen.add(key)

            try:
                date_part  = raw_value[:10]          # always YYYY-MM-DD
                parsed_date = datetime.strptime(date_part, "%Y-%m-%d").date()
            except ValueError:
                continue  # not a real calendar date

            if tag_name == 'BirthDt':
                # Birth dates MUST be in the past or today, but NOT in the future
                if parsed_date > today_date:
                    try:
                        line_num = xml_content.count('\n', 0, m.start()) + 1
                    except Exception:
                        line_num = "Unknown"

                    report.add_issue(ValidationIssue(
                        "ERROR",
                        2,
                        "FUTURE_DATE_BIRTH_ERROR",
                        str(line_num),
                        f"Birth date cannot be in the future. ",
                        f"Field <{tag_name}> contains '{raw_value}', which is after today ({today_date}).",
                        f"Update <{tag_name}> to a valid past date. (Line: {line_num})"
                    ))
            elif parsed_date < today_date and tag_name != 'BirthDt':
                # Find the line number in the raw XML
                try:
                    line_num = xml_content.count('\n', 0, m.start()) + 1
                except Exception:
                    line_num = "Unknown"

                report.add_issue(ValidationIssue(
                    "ERROR",
                    2,
                    "PAST_DATE_ERROR",
                    str(line_num),
                    f"Date cannot be in the past. "
                    f"Field <{tag_name}> contains '{raw_value}', "
                    f"which is before today ({today_date}).",
                    f"Update <{tag_name}> to today ({today_date}) or a future date. "
                    f"(Line: {line_num})"
                ))

    def _validate_id_lengths_in_xml(self, xml_content: str, report: ValidationReport) -> None:
        """
        Step 4.6 — ID Field Maximum Length Validation
        Scans the raw XML string for known identifier fields and checks that
        their values do not exceed their ISO 20022-defined maximum lengths.

        This runs BEFORE Layer 2 so violations are always reported alongside
        any XSD errors (e.g. invalid amounts, IBANs, UETRs).

        Field limits enforced:
          InstrId      → max 16 chars (CBPR+ RestrictedFINXMax16Text)
          EndToEndId   → max 35 chars
          BizMsgIdr    → max 35 chars
          MsgId        → max 35 chars
          TxId         → max 35 chars
          UETR         → max 36 chars
        """
        # UETR is handled by the dedicated _validate_uetr_in_xml validator (Step 4.7)
        # which checks UUID v4 format fully, so it is excluded here to avoid
        # double-reporting.
        ID_MAX_LENGTHS = {
            "InstrId":    16,
            "EndToEndId": 35,
            "BizMsgIdr":  35,
            "MsgId":      35,
            "TxId":       35,
            "ClrSysRef":  35,
        }

        # Build one combined pattern that matches any of the tracked tags
        tag_alternation = "|".join(re.escape(t) for t in ID_MAX_LENGTHS)
        id_patt = re.compile(
            r'<(' + tag_alternation + r')>'   # opening tag  (group 1)
            r'\s*([^<]+?)\s*'                  # value        (group 2)
            r'</\1>'                           # matching closing tag
        )

        for m in id_patt.finditer(xml_content):
            tag_name   = m.group(1)
            raw_value  = m.group(2).strip()
            max_len    = ID_MAX_LENGTHS[tag_name]
            actual_len = len(raw_value)

            if actual_len > max_len:
                try:
                    line_num = xml_content.count('\n', 0, m.start()) + 1
                except Exception:
                    line_num = "Unknown"

                report.add_issue(ValidationIssue(
                    "ERROR",
                    2,
                    "ID_LENGTH_ERROR",
                    str(line_num),
                    f"Invalid length in element <{tag_name}> at line {line_num}: "
                    f"Length {actual_len} exceeds maximum allowed {max_len}.",
                    f"Shorten the value of <{tag_name}> to at most {max_len} characters. "
                    f"Current value has {actual_len} characters."
                ))

    def _validate_cbpr_datetime(self, xml_content: str, report: ValidationReport) -> None:
        r"""
        Step 4.21 — CBPR+ DateTime Format Validation
        Enforces:
          1. Timezone offset is mandatory (e.g., +00:00, +05:30)
          2. 'Z' (UTC indicator) is FORBIDDEN
          3. Milliseconds (.sss) should be removed
        Rule: .*(\+|-)((0[0-9])|(1[0-4])):[0-5][0-9]
        """
        # Match all datetime tags: <CreDt>, <CreDtTm>, <IntrBkSttlmTm>, etc.
        # CBPR+ specifically targets fields that contain DateTime
        datetime_tags = ["CreDt", "CreDtTm", "IntrBkSttlmTm", "PmtStpTm", "SttlmTmReq", "CLSTm", "TillTm", "FrTm", "RjctTm"]
        
        tag_alternation = "|".join(re.escape(t) for t in datetime_tags)
        dt_patt = re.compile(
            r'<(' + tag_alternation + r')>'   # opening tag  (group 1)
            r'\s*([^<]+?)\s*'                  # value        (group 2)
            r'</\1>'                           # matching closing tag
        )

        for m in dt_patt.finditer(xml_content):
            tag_name   = m.group(1)
            raw_value  = m.group(2).strip()
            
            # 1. Check for 'Z'
            if 'Z' in raw_value:
                line_num = xml_content.count('\n', 0, m.start()) + 1
                report.add_issue(ValidationIssue(
                    "ERROR", 2, "CBPR_DATETIME_Z_FORBIDDEN", str(line_num),
                    f"Element <{tag_name}> contains 'Z' UTC indicator which is forbidden in CBPR+.",
                    f"Replace 'Z' with an explicit timezone offset like '+00:00'."
                ))
                continue

            # 2. Check for milliseconds
            if '.' in raw_value:
                # If it's a date like 2026-03-23, ignore. But these tags are likely DateTime.
                if 'T' in raw_value:
                    line_num = xml_content.count('\n', 0, m.start()) + 1
                    report.add_issue(ValidationIssue(
                        "ERROR", 2, "CBPR_DATETIME_MILLISECONDS", str(line_num),
                        f"Element <{tag_name}> contains milliseconds which are forbidden in CBPR+.",
                        f"Remove the decimal part (e.g., '.415') from the time."
                    ))
                    continue

            # 3. Check for mandatory offset using the user-provided regex
            # Skip garbage values that aren't datetimes at all (e.g. 'with', 'abc').
            # Those are already caught by XSD validation; emitting a CBPR timezone
            # error on top produces a confusing duplicate with contradictory hints.
            if not re.match(r'^\d{4}-\d{2}-\d{2}T', raw_value):
                continue

            offset_patt = re.compile(r'.*(\+|-)((0[0-9])|(1[0-4])):[0-5][0-9]$')
            if not offset_patt.match(raw_value):
                line_num = xml_content.count('\n', 0, m.start()) + 1
                report.add_issue(ValidationIssue(
                    "ERROR", 2, "CBPR_DATETIME_NO_TIMEZONE", str(line_num),
                    f"Element <{tag_name}> is missing a mandatory timezone offset.",
                    f"Ensure the format is YYYY-MM-DDThh:mm:ss(+/-)HH:MM (e.g., +00:00)."
                ))

    def _validate_name_address_coexistence(self, xml_content: str, report: ValidationReport) -> None:
        """
        Step 4.22 — Global Name & Postal Address Co-existence (CBPR+)
        Enforces the rule: If Name <Nm> is present, Postal Address <PstlAdr> must be present.
        Applies to all party and financial institution structures.
        """
        try:
            parser = etree.XMLParser(recover=True, no_network=True, resolve_entities=False)
            root = etree.fromstring(xml_content.encode('utf-8'), parser)
        except Exception:
            return

        # Target elements that typically contain both Nm and PstlAdr
        target_tags = {
            'Dbtr', 'Cdtr', 'UltmtDbtr', 'UltmtCdtr', 'InitgPty', 
            'DbtrAgt', 'CdtrAgt', 'InstgAgt', 'InstdAgt', 'IntrmyAgt1', 
            'IntrmyAgt2', 'IntrmyAgt3', 'FinInstnId', 'BrnchId', 'Pty'
        }

        for elem in root.iter():
            if not isinstance(elem.tag, str):
                continue
            
            tag_local = elem.tag.split('}')[-1] if '}' in elem.tag else elem.tag
            if tag_local not in target_tags:
                continue

            # Check for Nm and PstlAdr children
            has_nm = False
            has_pstl_adr = False
            nm_line = elem.sourceline or 1

            for child in elem:
                if not isinstance(child.tag, str):
                    continue
                child_local = child.tag.split('}')[-1] if '}' in child.tag else child.tag
                if child_local == 'Nm':
                    has_nm = True
                    nm_line = child.sourceline or nm_line
                elif child_local == 'PstlAdr':
                    has_pstl_adr = True

            # The Rule: If <Nm> exists, <PstlAdr> MUST exist.
            if has_nm and not has_pstl_adr:
                report.add_issue(ValidationIssue(
                    "ERROR", 3, "NAME_ADDRESS_COEXISTENCE", str(nm_line),
                    "Error: Name and Address must always be present together",
                    f"The element <{tag_local}> contains a Name <Nm> but is missing a Postal Address <PstlAdr>. "
                    "For CBPR+ compliance, if a name is provided, the full postal address must also be included."
                ))
            
            # (Optional inverse) If <PstlAdr> exists, <Nm> MUST exist.
            elif has_pstl_adr and not has_nm:
                report.add_issue(ValidationIssue(
                    "ERROR", 3, "NAME_ADDRESS_COEXISTENCE", str(elem.sourceline or 1),
                    "Error: Name and Address must always be present together",
                    f"The element <{tag_local}> contains a Postal Address <PstlAdr> but is missing a Name <Nm>. "
                    "For CBPR+ compliance, if an address is provided, the name of the party must also be included."
                ))

    def _validate_empty_required_containers(self, xml_content: str, report: ValidationReport) -> None:
        """
        CBPR+ Business Rule — Empty Mandatory Containers.

        Several ISO 20022 complex types declare *all* of their children as
        ``minOccurs="0"`` (notably ``FinancialInstitutionIdentification18`` used
        for AppHdr Fr/To and every agent in the payload). XSD validation will
        therefore happily accept ``<FinInstnId></FinInstnId>`` even though such a
        message is operationally invalid — it cannot be routed because no party
        has been identified. CBPR+ network rules require at least one identifier
        to be present.

        This rule scans the message for known "must-not-be-empty" containers
        and flags any that have no element children and no text content.
        It runs BEFORE Layer 2 so the failure is recorded with severity ERROR
        and the report is marked FAIL even when the XSD itself is satisfied.
        """
        try:
            parser = etree.XMLParser(recover=True, no_network=True, resolve_entities=False)
            root = etree.fromstring(xml_content.encode('utf-8'), parser)
        except Exception:
            return

        def local(tag) -> str:
            return tag.split('}')[-1] if isinstance(tag, str) and '}' in tag else tag

        def is_effectively_empty(elem) -> bool:
            """True if elem has no element children AND no non-whitespace text."""
            for child in elem:
                if isinstance(child.tag, str):
                    return False  # at least one element child present
            text = (elem.text or "").strip()
            return not text

        # Containers that the XSD allows to be empty but CBPR+ does not.
        # value = human-friendly hint about what must be inside.
        EMPTY_NOT_ALLOWED = {
            # ── Identification containers (XSD declares every choice as optional) ──
            'FinInstnId':       "BICFI, ClrSysMmbId, LEI, Nm, or Othr",
            'FIId':              "a FinInstnId block with at least one identifier",
            'ClrSysMmbId':       "MmbId",
            'OrgId':             "AnyBIC, LEI, or Othr/Id",
            'PrvtId':            "DtAndPlcOfBirth or Othr/Id",
            'SchmeNm':           "<Cd> or <Prtry>",
            'Othr':              "an identifier value <Id>",
            'ClrSys':            "<Cd> or <Prtry>",
            # ── Postal address (all children optional in XSD) ──
            'PstlAdr':           "Ctry, TwnNm, StrtNm, BldgNb, PstCd, or AdrLine",
            # ── Payment identification & group header ──
            'PmtId':             "EndToEndId (and ideally InstrId, TxId, UETR)",
            'GrpHdr':            "MsgId, CreDtTm, NbOfTxs and SttlmInf",
            'SttlmInf':          "SttlmMtd",
            # ── Payment type info (choice containers) ──
            'PmtTpInf':          "SvcLvl, LclInstrm, CtgyPurp, InstrPrty, or ClrChanl",
            'SvcLvl':            "<Cd> or <Prtry>",
            'LclInstrm':         "<Cd> or <Prtry>",
            'CtgyPurp':          "<Cd> or <Prtry>",
            'Purp':              "<Cd> or <Prtry>",
            # ── Settlement timing ──
            'SttlmTmIndctn':     "DbtDtTm or CdtDtTm",
            'SttlmTmReq':        "CLSTm, TillTm, FrTm, or RjctTm",
            # ── Remittance & ancillary ──
            'RmtInf':            "<Ustrd> or <Strd>",
            'Strd':              "RfrdDocInf, RfrdDocAmt, CdtrRefInf, Invcr, Invcee, TaxRmt, GrnshmtRmt, or AddtlRmtInf",
            'CdtrRefInf':        "Tp or Ref",
            'RgltryRptg':        "DbtCdtRptgInd, Authrty, or Dtls",
            'Tax':               "Cdtr, Dbtr, AdmstnZn, RefNb, Mtd, TtlTaxblBaseAmt, TtlTaxAmt, Dt, SeqNb, or Rcrd",
            'TaxRcrd':           "Tp, Ctgy, CtgyDtls, DbtrSts, CertId, FrmsCd, Prd, TaxAmt, or AddtlInf",
            'Chrgs':             "Amt and Agt",
            'ChrgsInf':          "Amt and Agt",
            'ChrgsBrkdwn':       "Amt, Tp, or CdtDbtInd",
            # ── Instruction wrappers ──
            'InstrForCdtrAgt':   "Cd or InstrInf",
            'InstrForNxtAgt':    "Cd or InstrInf",
            # ── Statement / report common (camt) ──
            'Bal':               "Tp, Amt, CdtDbtInd, and Dt",
            'NtryDtls':          "TxDtls or Btch",
            'TxDtls':            "Refs, Amt, or RltdPties",
            # ── Account identification choice ──
            'AcctId':            "<IBAN> or <Othr> with an identifier",
        }

        # Party/agent wrappers — must contain *some* identifying child
        # (Nm, FinInstnId, FIId, Id) that itself isn't empty.
        AGENT_AND_PARTY = {
            'Dbtr', 'Cdtr', 'UltmtDbtr', 'UltmtCdtr', 'InitgPty',
            'DbtrAgt', 'CdtrAgt', 'InstgAgt', 'InstdAgt',
            'IntrmyAgt1', 'IntrmyAgt2', 'IntrmyAgt3',
            'PrvsInstgAgt1', 'PrvsInstgAgt2', 'PrvsInstgAgt3',
            'FwdgAgt', 'Fr', 'To',
        }

        # Account containers — their <Id> child must carry IBAN or Othr/Id.
        # The bare <Id> tag is too generic to flag globally (it appears in many
        # non-account contexts), so we walk these specific parents instead.
        ACCOUNT_CONTAINERS = {
            'DbtrAcct', 'CdtrAcct',
            'DbtrAgtAcct', 'CdtrAgtAcct',
            'IntrmyAgt1Acct', 'IntrmyAgt2Acct', 'IntrmyAgt3Acct',
            'InstgAgtAcct', 'InstdAgtAcct',
            'SttlmAcct', 'RcvgAgtAcct', 'DlvrgAgtAcct',
            'ChrgsAcct',
        }

        def has_acct_identifier(acct_elem) -> bool:
            """An account container is valid only if its <Id> carries IBAN text
            or an Othr/Id text value (or, in older XSDs, a direct text on <Id>)."""
            for descendant in acct_elem.iter():
                if descendant is acct_elem or not isinstance(descendant.tag, str):
                    continue
                d_name = local(descendant.tag)
                if d_name in ('IBAN', 'BBAN'):
                    if (descendant.text or "").strip():
                        return True
                elif d_name == 'Id':
                    # Othr/Id is the proprietary identifier; require text content
                    parent = descendant.getparent()
                    p_name = local(parent.tag) if parent is not None else ""
                    if p_name == 'Othr' and (descendant.text or "").strip():
                        return True
            return False

        # Track elements already reported by path (sourceline + name) to avoid
        # double-flagging when both the parent (e.g. Fr) and child (FinInstnId)
        # would qualify.
        reported = set()

        for elem in root.iter():
            if not isinstance(elem.tag, str):
                continue
            name = local(elem.tag)
            line = elem.sourceline or 0

            if name in EMPTY_NOT_ALLOWED and is_effectively_empty(elem):
                key = (line, name)
                if key in reported:
                    continue
                reported.add(key)
                hint = EMPTY_NOT_ALLOWED[name]
                report.add_issue(ValidationIssue(
                    "ERROR", 2, "EMPTY_REQUIRED_CONTAINER", str(line or "?"),
                    f"<{name}> is present but empty.",
                    f"Provide {hint}. Empty <{name}> is rejected by CBPR+ "
                    f"network rules even though the XSD permits it."
                ))
                continue

            if name in AGENT_AND_PARTY:
                # For pain.008, these fields might not require identification in this context
                if "pain.008" in report.message_type and name in ('Dbtr', 'Cdtr', 'UltmtDbtr', 'UltmtCdtr'):
                    continue

                # Recurse to verify at least one identifying descendant carries content
                has_meaningful = False
                for descendant in elem.iter():
                    if descendant is elem or not isinstance(descendant.tag, str):
                        continue
                    d_name = local(descendant.tag)
                    if d_name in ('BICFI', 'ClrSysMmbId', 'MmbId', 'LEI', 'Nm',
                                  'IBAN', 'Othr', 'AnyBIC', 'OrgId', 'PrvtId',
                                  'Ctry', 'TwnNm', 'StrtNm', 'AdrLine', 'Id'):
                        if (descendant.text or "").strip():
                            has_meaningful = True
                            break
                if not has_meaningful:
                    key = (line, name)
                    if key in reported:
                        continue
                    reported.add(key)
                    report.add_issue(ValidationIssue(
                        "ERROR", 2, "EMPTY_PARTY_CONTAINER", str(line or "?"),
                        f"<{name}> contains no identifying information.",
                        f"Provide at least a BIC, name, or identifier inside <{name}>. "
                        f"An empty party/agent container is not routable."
                    ))
                continue

            if name in ACCOUNT_CONTAINERS:
                # Account container present but no IBAN/BBAN/Othr identifier
                if not has_acct_identifier(elem):
                    key = (line, name)
                    if key in reported:
                        continue
                    reported.add(key)
                    report.add_issue(ValidationIssue(
                        "ERROR", 2, "EMPTY_ACCOUNT_CONTAINER", str(line or "?"),
                        f"<{name}> is present but carries no account identifier.",
                        f"Inside <{name}> provide either <Id><IBAN>...</IBAN></Id> "
                        f"or <Id><Othr><Id>...</Id></Othr></Id>. An account container "
                        f"without an identifier is not processable."
                    ))

    def _validate_apphdr_payload_match(self, xml_content: str, report: ValidationReport) -> None:
        """
        CBPR+ rule HEAD001_MSGDEFIDR_MATCHES_PAYLOAD —

        The AppHdr's <MsgDefIdr> must reference the *same* message type as the
        <Document>'s namespace. A header that claims ``pacs.008.001.13`` while the
        Document namespace is ``pacs.009.001.12`` is a common integration bug
        (often caused by routing/mapping mistakes) and produces a perfectly
        XSD-valid but operationally meaningless message. lxml's XSD validator
        cannot catch this because the header and the payload are validated
        against different schemas.

        Also checks BizSvc format ("swift.<service>.NN") and AppHdr CreDt vs
        Document CreDtTm timezone consistency (warning only).
        """
        try:
            parser = etree.XMLParser(recover=True, no_network=True, resolve_entities=False)
            root = etree.fromstring(xml_content.encode('utf-8'), parser)
        except Exception:
            return

        app_hdr = root.find(".//{*}AppHdr")
        document = root.find(".//{*}Document")
        if app_hdr is None or document is None:
            # Nothing to compare — either header or payload missing; other rules will handle it
            return

        # 1. MsgDefIdr vs Document namespace
        msg_def_idr_el = app_hdr.find(".//{*}MsgDefIdr")
        msg_def_idr = (msg_def_idr_el.text or "").strip() if msg_def_idr_el is not None else ""

        doc_ns = etree.QName(document).namespace or ""
        # Extract the trailing message identifier from the namespace
        # e.g. urn:iso:std:iso:20022:tech:xsd:pacs.008.001.13 → pacs.008.001.13
        doc_msg_id = doc_ns.split(":")[-1] if ":" in doc_ns else doc_ns

        if msg_def_idr and doc_msg_id and msg_def_idr != doc_msg_id:
            line = str(msg_def_idr_el.sourceline or "?")
            report.add_issue(ValidationIssue(
                "ERROR", 2, "HEAD001_MSGDEFIDR_MISMATCH", line,
                f"AppHdr.MsgDefIdr '{msg_def_idr}' does not match the Document namespace "
                f"'{doc_msg_id}'.",
                f"Either update <MsgDefIdr> in the AppHdr to '{doc_msg_id}', or change the "
                f"Document namespace to 'urn:iso:std:iso:20022:tech:xsd:{msg_def_idr}'. "
                f"Header and payload must reference the same ISO 20022 message definition."
            ))

        # 2. BizSvc — MANDATORY under CBPR+ / SWIFT MyStandards.
        #    The base head.001 XSD declares <BizSvc> as minOccurs="0", so a message
        #    WITHOUT it is XSD-valid yet REJECTED by MyStandards ("CreDt not
        #    expected here … expected BizSvc", because MyStandards makes BizSvc
        #    mandatory before CreDt). That is exactly the "passes in our validator
        #    but fails in MyStandards" gap — so we enforce presence as a hard error
        #    here, then validate the format when it IS present.
        biz_svc_el = app_hdr.find(".//{*}BizSvc")
        biz_svc = (biz_svc_el.text or "").strip() if biz_svc_el is not None else ""
        if not biz_svc:
            # Anchor the error to MsgDefIdr (BizSvc must immediately follow it) or
            # the AppHdr itself when MsgDefIdr is also absent. Use an /AppHdr/BizSvc
            # path (not a bare line number) so the fix-suggester's namespace-aware,
            # KB-ordered AppHdr inserter places <BizSvc> in the correct slot.
            anchor = msg_def_idr_el if msg_def_idr_el is not None else app_hdr
            line_no = (anchor.sourceline if anchor is not None else None)
            report.add_issue(ValidationIssue(
                "ERROR", 2, "HEAD001_BIZSVC_MISSING", "/AppHdr/BizSvc",
                "AppHdr.BizSvc is missing. The business service is MANDATORY under "
                "CBPR+ / SWIFT MyStandards, even though the base head.001 XSD marks it "
                "optional — a message without it is rejected by MyStandards.",
                "Add <BizSvc>swift.cbprplus.02</BizSvc> to the AppHdr, immediately after "
                "<MsgDefIdr> and before <CreDt>.",
                line=line_no,
            ))
        elif not re.match(r'^swift\.[a-z]+(\.[a-z]+)?(\.[0-9]+)?$', biz_svc):
            line = str(biz_svc_el.sourceline or "?")
            report.add_issue(ValidationIssue(
                "WARNING", 2, "HEAD001_BIZSVC_FORMAT", line,
                f"AppHdr.BizSvc '{biz_svc}' does not match the SWIFT business service "
                f"pattern 'swift.<service>.NN'.",
                "Use a recognised value such as 'swift.cbprplus.02' (CBPR+ SR2025), "
                "'swift.cbprplus.01', 'swift.hvps.01', or 'swift.csp.02'."
            ))

        # 3. Timezone consistency (warning) — both header CreDt and payload CreDtTm should
        # carry the same offset for downstream timing/cut-off calculations to be correct.
        cre_dt_el = app_hdr.find(".//{*}CreDt")
        # First CreDtTm under Document (typically in GrpHdr)
        cre_dt_tm_el = document.find(".//{*}CreDtTm")

        def _extract_offset(value: str) -> str:
            if not value:
                return ""
            # Match ±HH:MM or 'Z'
            m = re.search(r'(Z|[+-]\d{2}:\d{2})$', value)
            return m.group(1) if m else ""

        if cre_dt_el is not None and cre_dt_tm_el is not None:
            h_off = _extract_offset((cre_dt_el.text or "").strip())
            p_off = _extract_offset((cre_dt_tm_el.text or "").strip())
            if h_off and p_off and h_off != p_off:
                line = str(cre_dt_tm_el.sourceline or "?")
                report.add_issue(ValidationIssue(
                    "WARNING", 2, "HEAD001_TZ_DRIFT", line,
                    f"AppHdr CreDt timezone offset '{h_off}' differs from Document "
                    f"CreDtTm timezone offset '{p_off}'.",
                    "For consistency, use the same UTC offset in both header and payload "
                    "timestamps (e.g. both '+00:00' or both '+05:30')."
                ))

    def _validate_kb_dependency_rules(self, xml_content: str, report: ValidationReport,
                                       message_type: str) -> None:
        """
        Enforce cross-tag dependency rules documented in resources/KB/<family>.json
        that are not already covered by the dedicated validators above.

        Currently enforces:
          • CBPR_BIZ_MSG_IDR / DEP_001 — AppHdr/BizMsgIdr must equal GrpHdr/MsgId.

        The rule set is driven by the KB (we only emit a check when the KB for this
        message family actually declares the rule), and every issue is dedup-guarded
        against the existing report so it can never double-report.
        """
        try:
            from app.services.fix_suggester import _KBContext
        except Exception:
            return
        kb = _KBContext.get(message_type or "")
        if kb is None:
            return

        try:
            parser = etree.XMLParser(recover=True, no_network=True, resolve_entities=False)
            root = etree.fromstring(xml_content.encode("utf-8"), parser)
        except Exception:
            return
        if root is None:
            return

        existing_codes = {i.get("code") for i in report.issues}

        # ── BizMsgIdr == GrpHdr/MsgId ─────────────────────────────────────────
        rule_declared = (
            any(fr.get("rule_id") == "CBPR_BIZ_MSG_IDR" for fr in kb.formal_rules)
            or any("BizMsgIdr" in r.get("rule", "") and "MsgId" in r.get("rule", "")
                   for r in kb.dependency_rules)
        )
        if rule_declared and "BIZMSGIDR_NEQ_MSGID" not in existing_codes:
            biz_el = root.find(".//{*}BizMsgIdr")
            # GrpHdr/MsgId specifically — the documented counterpart. Messages
            # that carry their identifier elsewhere (e.g. camt.055 Assgnmt/Id)
            # simply aren't checked here, avoiding false positives.
            msgid_el = root.find(".//{*}GrpHdr/{*}MsgId")
            if biz_el is not None and msgid_el is not None:
                bv = (biz_el.text or "").strip()
                mv = (msgid_el.text or "").strip()
                if bv and mv and bv != mv:
                    # Emitted as Layer 2 (like the other AppHdr cross-field checks)
                    # so it surfaces even when Layer 2 schema errors are present and
                    # isn't dropped by the Layer-3 SKIPPED cleanup.
                    report.add_issue(ValidationIssue(
                        "ERROR", 2, "BIZMSGIDR_NEQ_MSGID", "/AppHdr/BizMsgIdr",
                        f"AppHdr/BizMsgIdr '{bv}' must equal GrpHdr/MsgId '{mv}'.",
                        "Set <BizMsgIdr> to the same value as GrpHdr/MsgId "
                        f"(i.e. '{mv}'). Always harvest from MsgId rather than inventing a value.",
                        line=biz_el.sourceline
                    ))

    def _validate_pain008_fwdgagt_rule(self, xml_content: str, report: ValidationReport) -> None:
        """
        SWIFT CBPR+ Rule — pain.008 GrpHdr must contain FwdgAgt.

        The base ISO 20022 XSD for pain.008.001.08 marks <FwdgAgt> as optional
        (minOccurs="0") inside <GrpHdr>, but SWIFT MyStandards / CBPR+ network
        rules promote it to mandatory because every cross-border direct-debit
        message routed over SWIFT requires the Forwarding Agent to be present
        for correlation and routing.

        When <FwdgAgt> is absent we emit the exact error message that SWIFT
        MyStandards displays so users see consistent output across tools:

            "The content of element 'GrpHdr' is not complete.
             One of the following elements is expected: 'FwdgAgt'."
        """
        if not report.message_type or "pain.008" not in report.message_type:
            return

        try:
            parser = etree.XMLParser(recover=True, no_network=True, resolve_entities=False)
            root = etree.fromstring(xml_content.encode('utf-8'), parser)
        except Exception:
            return

        grp_hdr_nodes = root.xpath("//*[local-name()='GrpHdr']")
        if not grp_hdr_nodes:
            return  # GrpHdr itself is missing — XSD layer will flag it

        grp_hdr = grp_hdr_nodes[0]
        fwdg_agt_nodes = grp_hdr.xpath("*[local-name()='FwdgAgt']")
        if fwdg_agt_nodes:
            return  # Present — nothing to flag (content-of-element check passes)

        line = str(grp_hdr.sourceline or "?")
        report.add_issue(ValidationIssue(
            "ERROR", 2, "PAIN008_FWDGAGT_MANDATORY", line,
            "The content of element 'GrpHdr' is not complete. "
            "One of the following elements is expected: 'FwdgAgt'.",
            "Add <FwdgAgt> inside <GrpHdr> with the Forwarding Agent's "
            "financial institution identification (e.g. <FinInstnId><BICFI>...</BICFI></FinInstnId>). "
            "FwdgAgt is mandatory in SWIFT CBPR+ pain.008 messages even though "
            "the base ISO 20022 XSD declares it optional."
        ))

    def _validate_uetr_in_xml(self, xml_content: str, report: ValidationReport) -> None:
        """
        Step 4.7 — UETR UUID v4 Format Validation
        Finds every <UETR> element in the raw XML and validates it against
        the full UUID v4 specification:

          Format : 8-4-4-4-12  (total 36 chars including hyphens)
          Chars  : lowercase hexadecimal (0-9, a-f) and hyphens only
          Version: third group must start with '4'  (UUID version 4)
          Variant: fourth group must start with 8, 9, a, or b

        Example valid UETR: 550e8400-e29b-41d4-a716-446655440000

        This runs BEFORE Layer 2 so UETR errors are always reported even
        when other XSD errors are present.
        """
        # Strict UUID v4 pattern: lowercase only, version=4, variant=[89ab]
        UUID_V4 = re.compile(
            r'^[0-9a-f]{8}-'      # 8 hex
            r'[0-9a-f]{4}-'       # 4 hex
            r'4[0-9a-f]{3}-'      # version 4 + 3 hex
            r'[89ab][0-9a-f]{3}-' # variant + 3 hex
            r'[0-9a-f]{12}$'      # 12 hex
        )

        # Match all <UETR>...</UETR> and <OrgnlUETR>...</OrgnlUETR> elements
        uetr_patt = re.compile(
            r'<(UETR|OrgnlUETR)>'  # opening tag (group 1)
            r'\s*([^<]+?)\s*'      # value        (group 2)
            r'</\1>'               # matching closing tag
        )

        for m in uetr_patt.finditer(xml_content):
            tag_name  = m.group(1)
            raw_value = m.group(2).strip()

            if not UUID_V4.match(raw_value):
                try:
                    line_num = xml_content.count('\n', 0, m.start()) + 1
                except Exception:
                    line_num = "Unknown"

                # Give a specific hint about what exactly is wrong
                if len(raw_value) != 36:
                    hint = f"Value has {len(raw_value)} characters; must be exactly 36."
                elif raw_value != raw_value.lower():
                    hint = "Value must use only lowercase characters."
                elif not re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
                                  raw_value.lower()):
                    hint = "Value does not follow 8-4-4-4-12 hex grouping with hyphens."
                elif raw_value[14] != '4':
                    hint = f"Third group must start with '4' (UUID v4). Found '{raw_value[14]}'."
                else:
                    hint = (f"Fourth group must start with 8, 9, a, or b (UUID v4 variant). "
                            f"Found '{raw_value[19]}'.")

                report.add_issue(ValidationIssue(
                    "ERROR",
                    2,
                    "UETR_FORMAT_ERROR",
                    str(line_num),
                    f"Invalid UETR in element <{tag_name}> at line {line_num}: "
                    f"Must be a valid UUID v4 (36-character format). "
                    f"Value: '{raw_value}'.",
                    f"{hint} "
                    f"Example of a valid UETR: 550e8400-e29b-41d4-a716-446655440000"
                ))

    def _validate_account_identifiers_in_xml(self, xml_content: str, report: ValidationReport) -> None:
        """
        Step 4.8 — IBAN / BBAN Account Identifier Validation

        For every account container element (DbtrAcct, CdtrAcct, etc.) found in
        the XML, validates the <Id> child against ISO 20022 account rules:

          1. IBAN validation  — format, country length, MOD-97 check digit, overall length (15-34)
          2. BBAN validation  — length, alphanumeric, country-specific structure (FR, ES)
          3. Mutual exclusivity — exactly one of <IBAN> or <Othr> must be present
          4. SEPA rule        — BBAN not permitted when SvcLvl/Cd = SEPA
          5. Amount validation - checks for strictly positive amounts
        """
        # ── Country-specific IBAN lengths (SWIFT IBAN Registry, Edition 2024) ─
        # ⚠️  IMPORTANT: Countries like US, CA, AU, IN, CN, JP do NOT use IBAN.
        #     The US banking system uses ABA routing numbers + account numbers.
        #     An IBAN starting with 'US' is ALWAYS INVALID — the US is not a
        #     participant in the IBAN scheme (ISO 13616 / SWIFT Registry).
        IBAN_LENGTHS = {
            # A
            'AD':24,  # Andorra
            'AE':23,  # United Arab Emirates
            'AL':28,  # Albania
            'AT':20,  # Austria
            'AZ':28,  # Azerbaijan
            # B
            'BA':20,  # Bosnia and Herzegovina
            'BE':16,  # Belgium
            'BF':28,  # Burkina Faso
            'BG':22,  # Bulgaria
            'BH':22,  # Bahrain
            'BI':27,  # Burundi
            'BJ':28,  # Benin
            'BR':29,  # Brazil
            'BY':28,  # Belarus
            # C
            'CF':27,  # Central African Republic
            'CG':27,  # Congo
            'CH':21,  # Switzerland
            'CI':28,  # Ivory Coast (Côte d'Ivoire)
            'CM':27,  # Cameroon
            'CR':22,  # Costa Rica
            'CV':25,  # Cape Verde
            'CY':28,  # Cyprus
            'CZ':24,  # Czech Republic
            # D
            'DE':22,  # Germany
            'DJ':27,  # Djibouti
            'DK':18,  # Denmark
            'DO':28,  # Dominican Republic
            'DZ':26,  # Algeria
            # E
            'EE':20,  # Estonia
            'EG':29,  # Egypt
            'ES':24,  # Spain
            # F
            'FI':18,  # Finland
            'FK':18,  # Falkland Islands
            'FO':18,  # Faroe Islands
            'FR':27,  # France
            # G
            'GA':27,  # Gabon
            'GB':22,  # United Kingdom
            'GE':22,  # Georgia
            'GI':23,  # Gibraltar
            'GL':18,  # Greenland
            'GN':26,  # Guinea
            'GQ':27,  # Equatorial Guinea
            'GR':27,  # Greece
            'GT':28,  # Guatemala
            'GW':25,  # Guinea-Bissau
            # H
            'HN':28,  # Honduras
            'HR':21,  # Croatia
            'HU':28,  # Hungary
            # I
            'IE':22,  # Ireland
            'IL':23,  # Israel
            'IQ':23,  # Iraq
            'IR':26,  # Iran
            'IS':26,  # Iceland
            'IT':27,  # Italy
            # J
            'JO':30,  # Jordan
            # K
            'KM':27,  # Comoros
            'KW':30,  # Kuwait
            'KZ':20,  # Kazakhstan
            # L
            'LB':28,  # Lebanon
            'LC':32,  # Saint Lucia
            'LI':21,  # Liechtenstein
            'LT':20,  # Lithuania
            'LU':20,  # Luxembourg
            'LV':21,  # Latvia
            'LY':25,  # Libya
            # M
            'MA':28,  # Morocco
            'MC':27,  # Monaco
            'MD':24,  # Moldova
            'ME':22,  # Montenegro
            'MG':27,  # Madagascar
            'MK':19,  # North Macedonia
            'ML':28,  # Mali
            'MN':20,  # Mongolia
            'MR':27,  # Mauritania
            'MT':31,  # Malta
            'MU':30,  # Mauritius
            'MZ':25,  # Mozambique
            # N
            'NE':28,  # Niger
            'NI':32,  # Nicaragua
            'NL':18,  # Netherlands
            'NO':15,  # Norway
            'NZ':16,  # New Zealand
            # O
            'OM':23,  # Oman
            # P
            'PK':24,  # Pakistan
            'PL':28,  # Poland
            'PS':29,  # Palestinian Territory
            'PT':25,  # Portugal
            # Q
            'QA':29,  # Qatar
            # R
            'RO':24,  # Romania
            'RS':22,  # Serbia
            'RU':33,  # Russia
            # S
            'SA':24,  # Saudi Arabia
            'SC':31,  # Seychelles
            'SD':18,  # Sudan
            'SE':24,  # Sweden
            'SI':19,  # Slovenia
            'SK':24,  # Slovakia
            'SM':27,  # San Marino
            'SN':28,  # Senegal
            'SO':23,  # Somalia
            'ST':25,  # Sao Tome and Principe
            'SV':28,  # El Salvador
            # T
            'TD':27,  # Chad
            'TG':28,  # Togo
            'TL':23,  # Timor-Leste
            'TN':24,  # Tunisia
            'TR':26,  # Turkey
            # U
            'UA':29,  # Ukraine
            # V
            'VA':22,  # Vatican City
            'VG':24,  # British Virgin Islands
            # X
            'XK':20,  # Kosovo
            # Y
            'YE':30,  # Yemen
        }
        IBAN_PATTERN = re.compile(r'^[A-Z]{2}[0-9]{2}[A-Z0-9]{1,30}$')



        # Account container tags to scan
        ACCOUNT_TAGS = [
            'DbtrAcct','CdtrAcct','IntrmyAgtAcct','InstgAgtAcct','InstdAgtAcct',
            'CdtrAgtAcct','DbtrAgtAcct','SttlmAcct','RcvgAgtAcct','DlvrgAgtAcct',
        ]

        # Amount tags to scan for positive value (simple local tag names only)
        AMOUNT_TAGS = {
            'InstdAmt', 'IntrBkSttlmAmt', 'ChrgAmt', 'Amt', 'EqvtAmt', 'TtlIntrBkSttlmAmt',
        }

        # ── Country-specific BBAN structures (SWIFT IBAN Registry / ISO 13616) ─
        # Each entry: (total_bban_length, [(segment_length, type), ...])
        # type: 'n'=numeric only, 'a'=alphabetic only, 'c'=alphanumeric
        BBAN_STRUCTURES = {
            'AL': (28, [(8,'n'),(16,'c')]),
            'AD': (20, [(4,'n'),(4,'n'),(12,'c')]),
            'AT': (16, [(5,'n'),(11,'n')]),
            'AZ': (24, [(4,'a'),(20,'c')]),
            'BH': (18, [(4,'a'),(14,'c')]),
            'BE': (12, [(3,'n'),(7,'n'),(2,'n')]),
            'BA': (16, [(3,'n'),(3,'n'),(8,'n'),(2,'n')]),
            'BR': (25, [(8,'n'),(5,'n'),(10,'n'),(1,'a'),(1,'c')]),
            'BG': (18, [(4,'a'),(4,'n'),(2,'n'),(8,'c')]),
            'CR': (18, [(4,'n'),(14,'n')]),
            'HR': (17, [(7,'n'),(10,'n')]),
            'CY': (24, [(3,'n'),(5,'n'),(16,'c')]),
            'CZ': (20, [(4,'n'),(6,'n'),(10,'n')]),
            'DK': (14, [(4,'n'),(9,'n'),(1,'n')]),
            'DO': (24, [(4,'a'),(20,'n')]),
            'EE': (16, [(2,'n'),(2,'n'),(11,'n'),(1,'n')]),
            'FI': (14, [(6,'n'),(7,'n'),(1,'n')]),
            'FO': (14, [(4,'n'),(9,'n'),(1,'n')]),
            'FR': (23, [(5,'n'),(5,'n'),(11,'c'),(2,'n')]),   # bank+branch+acct+rib_key
            'GE': (18, [(2,'a'),(16,'n')]),
            'DE': (18, [(8,'n'),(10,'n')]),
            'GI': (19, [(4,'a'),(15,'c')]),
            'GL': (14, [(4,'n'),(9,'n'),(1,'n')]),
            'GR': (23, [(3,'n'),(4,'n'),(16,'c')]),
            'GT': (24, [(4,'c'),(20,'c')]),
            'HU': (24, [(3,'n'),(4,'n'),(1,'n'),(15,'n'),(1,'n')]),
            'IS': (22, [(4,'n'),(2,'n'),(6,'n'),(10,'n')]),
            'IE': (18, [(4,'a'),(6,'n'),(8,'n')]),
            'IL': (19, [(3,'n'),(3,'n'),(13,'n')]),
            'IT': (23, [(1,'a'),(5,'n'),(5,'n'),(12,'c')]),
            'JO': (26, [(4,'a'),(4,'n'),(18,'c')]),
            'KZ': (16, [(3,'n'),(13,'c')]),
            'KW': (26, [(4,'a'),(22,'c')]),
            'LV': (17, [(4,'a'),(13,'c')]),
            'LB': (24, [(4,'n'),(20,'c')]),
            'LI': (17, [(5,'n'),(12,'c')]),
            'LT': (16, [(5,'n'),(11,'n')]),
            'LU': (16, [(3,'n'),(13,'c')]),
            'MK': (15, [(3,'n'),(10,'c'),(2,'n')]),
            'MT': (27, [(4,'a'),(5,'n'),(18,'c')]),
            'MR': (23, [(5,'n'),(5,'n'),(11,'n'),(2,'n')]),
            'MU': (26, [(4,'a'),(2,'n'),(2,'n'),(12,'n'),(3,'n'),(3,'a')]),
            'MD': (20, [(2,'c'),(18,'n')]),
            'MC': (23, [(5,'n'),(5,'n'),(11,'c'),(2,'n')]),   # same as FR
            'ME': (18, [(3,'n'),(13,'n'),(2,'n')]),
            'NL': (14, [(4,'a'),(10,'n')]),
            'NO': (11, [(4,'n'),(6,'n'),(1,'n')]),
            'PK': (20, [(4,'a'),(16,'n')]),
            'PS': (25, [(4,'a'),(21,'n')]),
            'PL': (24, [(8,'n'),(16,'n')]),
            'PT': (21, [(4,'n'),(4,'n'),(11,'n'),(2,'n')]),
            'QA': (25, [(4,'a'),(21,'c')]),
            'RO': (20, [(4,'a'),(16,'c')]),
            'SM': (23, [(1,'a'),(5,'n'),(5,'n'),(12,'c')]),
            'SA': (20, [(2,'n'),(18,'c')]),
            'RS': (18, [(3,'n'),(13,'n'),(2,'n')]),
            'SK': (20, [(4,'n'),(6,'n'),(10,'n')]),
            'SI': (15, [(5,'n'),(8,'n'),(2,'n')]),
            'ES': (20, [(4,'n'),(4,'n'),(2,'n'),(10,'n')]),   # bank+branch+ctrl+acct
            'SE': (20, [(3,'n'),(16,'n'),(1,'n')]),
            'CH': (17, [(5,'n'),(12,'c')]),
            'TN': (20, [(2,'n'),(3,'n'),(13,'n'),(2,'n')]),
            'TR': (22, [(5,'n'),(1,'n'),(16,'c')]),
            'AE': (19, [(3,'n'),(16,'n')]),
            'GB': (18, [(4,'a'),(6,'n'),(8,'n')]),
            'VG': (20, [(4,'a'),(16,'n')]),
        }

        # ── Helper: MOD 97-10 check digit verification ───────────────────────
        def _iban_mod97(iban: str) -> bool:
            rearranged = iban[4:] + iban[:4]
            numeric = ''.join(
                str(ord(c) - 55) if c.isalpha() else c
                for c in rearranged
            )
            try:
                return int(numeric) % 97 == 1
            except ValueError:
                return False

        # ── Helper: French / Monaco RIB key check ───────────────────────────
        def _fr_rib_check(bban: str) -> bool:
            """
            French BBAN = 5n(bank) + 5n(branch) + 11c(account) + 2n(rib_key)
            Standard letter → digit substitution table used by French banks.
            Expectation: 97 - ((89*bank + 15*branch + 3*acct_numeric) % 97) == rib_key
            Edge case: result of 97 maps to 0.
            """
            if len(bban) != 23:
                return True  # length already checked in segment validator
            try:
                # Letter substitution: A-Z → values per French RIB spec
                _LETTER_MAP = {
                    'A':1,'B':2,'C':3,'D':4,'E':5,'F':6,'G':7,'H':8,'I':9,
                    'J':1,'K':2,'L':3,'M':4,'N':5,'O':6,'P':7,'Q':8,'R':9,
                    'S':2,'T':3,'U':4,'V':5,'W':6,'X':7,'Y':8,'Z':9,
                }
                def to_num(s: str) -> str:
                    return ''.join(str(_LETTER_MAP[c]) if c.isalpha() else c for c in s.upper())

                bank_n   = int(to_num(bban[0:5]))
                branch_n = int(to_num(bban[5:10]))
                acct_n   = int(to_num(bban[10:21]))
                rib_key  = int(bban[21:23])

                expected = (97 - ((89 * bank_n + 15 * branch_n + 3 * acct_n) % 97)) % 97
                return expected == rib_key
            except Exception:
                return True  # don't falsely reject on unexpected parsing errors

        # ── Helper: Spanish CCC control digit check ──────────────────────────
        def _es_check_digit(bban: str) -> bool:
            """
            Spanish CCC: 4n(bank)+4n(branch)+2n(ctrl)+10n(account) = 20 chars
            ctrl[0] = check digit for bank+branch (left-padded to 10 with leading '00')
            ctrl[1] = check digit for account (10 digits)
            Algorithm: weighted sum mod 11, weights = [1,2,4,8,5,10,9,7,3,6]
            """
            if len(bban) != 20:
                return True  # length already checked in segment validator
            try:
                if not bban.isdigit():
                    return True  # character type already checked
                weights = [1, 2, 4, 8, 5, 10, 9, 7, 3, 6]

                def _cd(ten_digits: str) -> int:
                    total = sum(int(d) * w for d, w in zip(ten_digits, weights))
                    rem = total % 11
                    result = 11 - rem
                    return 0 if result == 11 else (1 if result == 10 else result)

                # Bank+branch padded to 10 chars with leading '00'
                bank_branch_10 = '00' + bban[0:4] + bban[4:8]
                ctrl           = bban[8:10]
                account_10     = bban[10:20]

                exp0 = _cd(bank_branch_10)
                exp1 = _cd(account_10)
                return ctrl == f"{exp0}{exp1}"
            except Exception:
                return True

        # ── Helper: validate a single IBAN value ─────────────────────────────
        def _validate_iban(value: str, container: str, line_num) -> list:
            errors = []
            # Strip surrounding whitespace, then remove embedded spaces (per spec: trim spaces)
            trimmed = value.strip()
            v_no_spaces = trimmed.replace(' ', '')

            # 0. Overall length check (ISO 13616 specifies 15-34 characters)
            # Use the space-stripped value for length counting
            if not (15 <= len(v_no_spaces) <= 34):
                errors.append((
                    f"Invalid account identifier in element <{container}> at line {line_num}: "
                    f"Failed IBAN/BBAN validation. "
                    f"IBAN '{value}' has length {len(v_no_spaces)} (after removing spaces), "
                    f"which is outside the valid range of 15–34 characters.",
                    f"An IBAN must be between 15 and 34 characters long (excluding spaces). "
                    f"Check for missing or extra characters."
                ))
                return errors  # Further checks are meaningless

            # 1. Uppercase + no special characters check
            # The spec requires: uppercase A-Z and digits 0-9 only (no lowercase, no specials).
            # We check on the space-stripped raw value (NOT yet uppercased) so that lowercase
            # letters are correctly rejected rather than silently accepted.
            if not IBAN_PATTERN.match(v_no_spaces):
                # Give a more specific hint for the most common violation: lowercase
                if v_no_spaces != v_no_spaces.upper():
                    hint = (
                        f"IBAN must use only UPPERCASE letters (A–Z) and digits (0–9). "
                        f"Lowercase letters are not permitted. "
                        f"Found: '{v_no_spaces}'."
                    )
                else:
                    hint = (
                        f"Correct the IBAN format. Expected: 2-letter country code, "
                        f"2 check digits, then up to 30 uppercase alphanumeric characters."
                    )
                errors.append((
                    f"Invalid account identifier in element <{container}> at line {line_num}: "
                    f"Failed IBAN/BBAN validation. "
                    f"IBAN '{value}' does not match the required pattern "
                    f"^[A-Z]{{2}}[0-9]{{2}}[A-Z0-9]{{1,30}}$ (uppercase alphanumeric only, "
                    f"no spaces or special characters).",
                    hint
                ))
                return errors  # Further checks are meaningless

            # From here on use the normalised (uppercased, space-free) value
            v = v_no_spaces.upper()

            # 2. Country code known in IBAN registry
            country = v[:2]
            if country not in IBAN_LENGTHS:
                errors.append((
                    f"Invalid account identifier in element <{container}> at line {line_num}: "
                    f"Failed IBAN/BBAN validation. "
                    f"IBAN country code '{country}' is not a recognised IBAN-issuing country.",
                    f"Use a recognised 2-letter ISO country code that participates in the IBAN scheme."
                ))
                return errors

            # 3. Exact length for country
            expected_len = IBAN_LENGTHS[country]
            if len(v) != expected_len:
                errors.append((
                    f"Invalid account identifier in element <{container}> at line {line_num}: "
                    f"Failed IBAN/BBAN validation. "
                    f"IBAN '{value}' has length {len(v)}, but {country} IBANs must be "
                    f"exactly {expected_len} characters.",
                    f"Check the IBAN for missing or extra characters. "
                    f"{country} IBANs are always {expected_len} characters long."
                ))
                return errors

            # 4. MOD 97-10 check digit verification
            if not _iban_mod97(v):
                errors.append((
                    f"Invalid account identifier in element <{container}> at line {line_num}: "
                    f"Failed IBAN/BBAN validation. "
                    f"IBAN '{value}' failed the MOD-97 check digit verification.",
                    f"The IBAN check digits (positions 3–4) are incorrect. "
                    f"Move the first 4 characters to the end, convert letters to numbers "
                    f"(A=10…Z=35), then verify the result mod 97 equals 1."
                ))
            return errors

        # ── Helper: validate BBAN value ──────────────────────────────────────
        def _validate_bban(value: str, container: str, line_num, country_code: str = '') -> list:
            """
            Validates a BBAN value against:
              - Non-empty check
              - Maximum 30-character length
              - Alphanumeric characters
              - Country-specific length & character-type structure (if country known)
              - Embedded national check digits: France RIB key, Spain CCC
            """
            errors = []
            v = value.strip()
            cc = (country_code or '').strip().upper()

            # Rule 1: must not be empty
            if not v:
                errors.append((
                    f"Invalid account identifier in element <{container}> at line {line_num}: "
                    f"Failed IBAN/BBAN validation. BBAN value is empty.",
                    "Provide a valid BBAN (domestic account number)."
                ))
                return errors

            # Rule 2: must not exceed 30 characters
            if len(v) > 30:
                errors.append((
                    f"Invalid account identifier in element <{container}> at line {line_num}: "
                    f"Failed IBAN/BBAN validation. BBAN '{v}' exceeds the maximum length of 30 characters "
                    f"(actual: {len(v)}).",
                    "Shorten the BBAN to at most 30 characters."
                ))

            # Country-specific structure validation (takes priority over generic check)
            if cc and cc in BBAN_STRUCTURES:
                bban_len, segments = BBAN_STRUCTURES[cc]

                # Exact domestic length required
                if len(v) != bban_len:
                    errors.append((
                        f"Invalid account identifier in element <{container}> at line {line_num}: "
                        f"Failed IBAN/BBAN validation. BBAN '{v}' has length {len(v)}, "
                        f"but {cc} domestic BBANs must be exactly {bban_len} characters.",
                        f"The BBAN for country {cc} must be exactly {bban_len} characters. "
                        f"Structure: " + ', '.join(f"{l}x{'numeric' if t=='n' else 'alpha' if t=='a' else 'alphanumeric'}" for l, t in segments) + '.'
                    ))
                else:
                    # Validate each segment character type
                    pos = 0
                    seg_errors = []
                    for seg_len, seg_type in segments:
                        seg = v[pos:pos + seg_len]
                        pos += seg_len
                        if seg_type == 'n' and not seg.isdigit():
                            seg_errors.append(
                                f"chars {pos - seg_len + 1}–{pos} ('{seg}') must be numeric only"
                            )
                        elif seg_type == 'a' and not seg.isalpha():
                            seg_errors.append(
                                f"chars {pos - seg_len + 1}–{pos} ('{seg}') must be alphabetic only"
                            )
                        elif seg_type == 'c' and not re.match(r'^[A-Za-z0-9]+$', seg):
                            seg_errors.append(
                                f"chars {pos - seg_len + 1}–{pos} ('{seg}') must be alphanumeric"
                            )
                    if seg_errors:
                        seg_structure = ', '.join(
                            f"{l}x{'n' if t=='n' else 'a' if t=='a' else 'c'}" for l, t in segments
                        )
                        errors.append((
                            f"Invalid account identifier in element <{container}> at line {line_num}: "
                            f"Failed IBAN/BBAN validation. BBAN '{v}' has invalid character types for country {cc}: "
                            + '; '.join(seg_errors) + '.',
                            f"{cc} BBAN structure is {bban_len} chars = [{seg_structure}] "
                            f"where n=numeric, a=alpha, c=alphanumeric."
                        ))

                    # National check digit validation (only when structure passed)
                    if not seg_errors:
                        if cc in ('FR', 'MC') and not _fr_rib_check(v):
                            errors.append((
                                f"Invalid account identifier in element <{container}> at line {line_num}: "
                                f"Failed IBAN/BBAN validation. BBAN '{v}' failed the French RIB key check "
                                f"(2-digit RIB key at positions 22–23).",
                                "Recalculate the RIB key using: 97 - ((89×bank + 15×branch + 3×account) mod 97). "
                                "Apply the French letter substitution table (A=1, B=2 … S=2, T=3 … wrapping at 9)."
                            ))
                        elif cc == 'ES' and not _es_check_digit(v):
                            errors.append((
                                f"Invalid account identifier in element <{container}> at line {line_num}: "
                                f"Failed IBAN/BBAN validation. BBAN '{v}' failed the Spanish CCC control digit check "
                                f"(digits 9–10 of the 20-digit CCC).",
                                "Recalculate the 2 control digits using the Spanish weighted-sum MOD-11 algorithm "
                                "(weights 1,2,4,8,5,10,9,7,3,6) applied to the bank/branch code and account number."
                            ))
            else:
                # Fallback: general alphanumeric check for unknown countries
                if not re.match(r'^[A-Za-z0-9]+$', v):
                    errors.append((
                        f"Invalid account identifier in element <{container}> at line {line_num}: "
                        f"Failed IBAN/BBAN validation. BBAN '{v}' contains invalid characters. "
                        f"Only alphanumeric characters (A–Z, a–z, 0–9) are allowed.",
                        "Remove spaces, hyphens, and any special characters from the BBAN value."
                    ))
            return errors

        # ── Helper: validate amount is strictly positive ─────────────────────
        def _validate_positive_amount(value: str, tag_name: str, line_num, parent_tag: str = "") -> list:
            errors = []
            try:
                amount = float(value)
                if amount <= 0 and not (amount == 0 and parent_tag == 'ChrgsInf'):
                    errors.append((
                        f"Invalid amount in element <{tag_name}> at line {line_num}: "
                        f"Amount '{value}' must be strictly positive.",
                        "Ensure the amount is greater than zero."
                    ))
            except ValueError:
                # This case should ideally be caught by XSD validation (Layer 1)
                # but we add a safeguard here.
                errors.append((
                    f"Invalid amount format in element <{tag_name}> at line {line_num}: "
                    f"Value '{value}' is not a valid number.",
                    "Provide a valid numeric amount."
                ))
            return errors

        # ── Parse XML with lxml to walk elements ─────────────────────────────
        try:
            parser = etree.XMLParser(recover=True, remove_blank_text=True)
            root   = etree.fromstring(xml_content.encode('utf-8'), parser)
        except Exception:
            return  # XML parsing handled by Layer 1

        def local(tag):
            if not isinstance(tag, str):
                return ""
            return tag.split('}')[-1] if '}' in tag else tag

        def get_text(elem, *tags):
            """Traverse child tags and return stripped text, or None."""
            node = elem
            for t in tags:
                node = next((c for c in node if local(c.tag) == t), None)
                if node is None:
                    return None
            return (node.text or '').strip() or None

        # Detect SEPA context (SvcLvl/Cd = 'SEPA' anywhere in document)
        is_sepa = any(
            (e.text or '').strip().upper() == 'SEPA'
            for e in root.iter()
            if local(e.tag) == 'Cd'
        )

        # Store country codes for BBAN validation if available
        debtor_country = None
        creditor_country = None
        for e in root.iter():
            tag = local(e.tag)
            if tag == 'Ctry':
                parent_tag = local(e.getparent().tag) if e.getparent() is not None else ''
                grandparent_tag = local(e.getparent().getparent().tag) if e.getparent() is not None and e.getparent().getparent() is not None else ''
                if 'Dbtr' in grandparent_tag or 'InitgPty' in grandparent_tag:
                    debtor_country = (e.text or '').strip().upper()
                if 'Cdtr' in grandparent_tag:
                    creditor_country = (e.text or '').strip().upper()

        # Walk all relevant elements
        for elem in root.iter():
            tag_name = local(elem.tag)
            line_num = getattr(elem, 'sourceline', 'Unknown') or 'Unknown'

            # ── Account Identifier Validation ────────────────────────────────
            if tag_name in ACCOUNT_TAGS:
                container = tag_name
                # Find <Id> child
                id_elem = next((c for c in elem if local(c.tag) == 'Id'), None)
                if id_elem is None:
                    continue

                iban_elem = next((c for c in id_elem if local(c.tag) == 'IBAN'), None)
                othr_elem = next((c for c in id_elem if local(c.tag) == 'Othr'), None)

                # ── Rule 3: Mutual exclusivity ───────────────────────────────────
                if iban_elem is not None and othr_elem is not None:
                    report.add_issue(ValidationIssue(
                        "ERROR", 3, "ACCT_MUTUAL_EXCLUSIVITY", str(line_num),
                        f"Invalid account identifier in element <{container}> at line {line_num}: "
                        f"Failed IBAN/BBAN validation. "
                        f"Both <IBAN> and <Othr> are present simultaneously. Exactly one must be used.",
                        "Remove either <IBAN> or <Othr>. ISO 20022 requires exactly one account identification method."
                    ))
                    continue

                if iban_elem is None and othr_elem is None:
                    report.add_issue(ValidationIssue(
                        "ERROR", 2, "ACCT_MISSING_ID", str(line_num),
                        f"Invalid account identifier in element <{container}> at line {line_num}: "
                        f"Neither <IBAN> nor <Othr> is present inside <Id>. ",
                        "Provide an account identification: either <IBAN> for international accounts, "
                        "or <Othr><SchmeNm><Cd>BBAN</Cd></SchmeNm></Othr> for domestic accounts."
                    ))
                    continue

                # ── IBAN path ─────────────────────────────────────────────────────
                if iban_elem is not None:
                    iban_val = (iban_elem.text or '').strip()
                    for msg, fix in _validate_iban(iban_val, container, line_num):
                        report.add_issue(ValidationIssue(
                            "ERROR", 3, "IBAN_VALIDATION_ERROR", str(line_num), msg, fix
                        ))

                # ── BBAN / Othr path ──────────────────────────────────────────────
                if othr_elem is not None:
                    othr_id  = get_text(othr_elem, 'Id')
                    scheme_cd = get_text(othr_elem, 'SchmeNm', 'Cd')

                    if scheme_cd and scheme_cd.upper() == 'BBAN':
                        # Rule 6: SEPA — BBAN not allowed
                        if is_sepa:
                            report.add_issue(ValidationIssue(
                                "ERROR", 2, "SEPA_BBAN_NOT_ALLOWED", str(line_num),
                                f"Invalid account identifier in element <{container}> at line {line_num}: "
                                f"BBAN account identification is not permitted in SEPA payments. IBAN is mandatory.",
                                "Replace the <Othr><Id>BBAN</Id> block with a valid <IBAN> element for SEPA transactions."
                            ))
                        else:
                            # Determine country for BBAN validation
                            bban_country = None
                            if 'Dbtr' in container or 'InitgPty' in container:
                                bban_country = debtor_country
                            elif 'Cdtr' in container:
                                bban_country = creditor_country

                            for msg, fix in _validate_bban(othr_id or '', container, line_num, bban_country):
                                report.add_issue(ValidationIssue(
                                    "ERROR", 2, "BBAN_VALIDATION_ERROR", str(line_num), msg, fix
                                ))

            # ── Amount Validation (strictly positive) ────────────────────────
            if tag_name in AMOUNT_TAGS:
                amount_val = (elem.text or '').strip()
                if amount_val:
                    parent_tag = local(elem.getparent().tag) if elem.getparent() is not None else ''
                    for msg, fix in _validate_positive_amount(amount_val, tag_name, line_num, parent_tag):
                        report.add_issue(ValidationIssue(
                            "ERROR", 2, "NON_POSITIVE_AMOUNT", str(line_num), msg, fix
                        ))

    def _validate_nboftxs(self, xml_content: str, report: ValidationReport) -> None:
        """
        Step 4.9 — NbOfTxs Count Validation
        Verifies that the <NbOfTxs> value matches the actual number of
        transaction elements in the message.
        """
        # Extract NbOfTxs value
        nb_match = re.search(r'<NbOfTxs>\s*(\d+)\s*</NbOfTxs>', xml_content)
        if not nb_match:
            return  # NbOfTxs not present, XSD will catch if mandatory

        declared_count = int(nb_match.group(1))

        # Count transaction elements — covers pacs.008, pacs.009, pacs.002, pain, camt
        tx_tags = ['CdtTrfTxInf', 'DrctDbtTxInf', 'TxInfAndSts', 'PmtInf']
        actual_count = 0
        for tag in tx_tags:
            count = len(re.findall(rf'<{tag}[\s>]', xml_content))
            if count > 0:
                actual_count = count
                break

        if actual_count > 0 and declared_count != actual_count:
            try:
                line_num = xml_content.count('\n', 0, nb_match.start()) + 1
            except Exception:
                line_num = "Unknown"

            report.add_issue(ValidationIssue(
                "ERROR", 2, "NBOFTXS_MISMATCH", str(line_num),
                f"NbOfTxs declares {declared_count} transaction(s) but the message "
                f"actually contains {actual_count}.",
                f"Update <NbOfTxs> to {actual_count} to match the actual number of transactions."
            ))

    def _validate_duplicate_ids(self, xml_content: str, report: ValidationReport) -> None:
        """
        Step 4.17 — Duplicate Identification Validation
        Scans for unique identifiers that should be unique within the message 
        (UETR, EndToEndId, InstrId, TxId).
        """
        id_tags = ['UETR', 'EndToEndId', 'InstrId', 'TxId', 'MsgId', 'BizMsgIdr']
        
        for tag in id_tags:
            # Pattern to find all values for a specific tag
            pattern = re.compile(rf'<{tag}>\s*([^<]+?)\s*</{tag}>', re.IGNORECASE)
            seen = {} # value -> first_line
            
            for m in pattern.finditer(xml_content):
                val = m.group(1).strip()
                if not val: continue
                
                line_num = xml_content.count('\n', 0, m.start()) + 1
                
                if val in seen:
                    prev_line = seen[val]
                    report.add_issue(ValidationIssue(
                        "ERROR", 2, "DUPLICATE_ID_VALUE", str(line_num),
                        f"Duplicate value '{val}' found for tag <{tag}>.",
                        f"The ID '{val}' appears at line {line_num} but was already used at line {prev_line}. "
                        f"Each {tag} must be unique within the message file."
                    ))
                else:
                    seen[val] = line_num

    def _validate_swift_charset(self, xml_content: str, report: ValidationReport) -> None:
        r"""
        Step 4.10 — SWIFT Character Set Validation
        Checks <Ustrd> (unstructured remittance) content for characters
        outside the permitted ISO 20022 MX character set.

        Ustrd allowed: 0-9 a-z A-Z / - ? : ( ) . , ' + space ! # $ % & * = ^ _ ` { | } ~ " ; < > @ [ \ ]
        """
        USTRD_CHARSET = re.compile(r'^[0-9a-zA-Z/\-\?:\(\)\.,\'\+ !#$%&\*=^_`\{\|\}~\x22;<>@\[\\\]]+$')

        ustrd_patt = re.compile(r'<Ustrd>\s*([^<]+?)\s*</Ustrd>')

        for m in ustrd_patt.finditer(xml_content):
            value = m.group(1).strip()
            if not value:
                continue

            if not USTRD_CHARSET.match(value):
                # Find the offending characters
                bad_chars = set(re.findall(r'[^0-9a-zA-Z/\-\?:\(\)\.,\'\+ !#$%&\*=^_`\{\|\}~\x22;<>@\[\\\]]', value))
                bad_str = ', '.join(f"'{c}'" for c in sorted(bad_chars)[:5])

                try:
                    line_num = xml_content.count('\n', 0, m.start()) + 1
                except Exception:
                    line_num = "Unknown"

                report.add_issue(ValidationIssue(
                    "WARNING", 3, "SWIFT_CHARSET_WARN", str(line_num),
                    f"Unstructured remittance at line {line_num} contains characters "
                    f"outside the permitted ISO 20022 MX character set: {bad_str}.",
                    "Allowed characters for Ustrd: letters, digits, space, and: / - ? : ( ) . , ' + ! # $ % & * = ^ _ ` {{ | }} ~ \" ; < > @ [ \\ ]. "
                    "Remove or replace any other special characters."
                ))

    def _validate_charges_currency(self, xml_content: str, report: ValidationReport) -> None:
        """
        Step 4.11 — Charges Currency Match Validation
        Verifies that <ChrgsInf><Amt Ccy="X"> uses the same currency
        as the transaction amount (<IntrBkSttlmAmt Ccy="Y">).
        """
        # Extract transaction currency
        tx_ccy_match = re.search(r'<IntrBkSttlmAmt\s+Ccy="([A-Z]{3})"', xml_content)
        if not tx_ccy_match:
            return  # No interbank settlement amount, nothing to compare

        tx_ccy = tx_ccy_match.group(1)

        # Find all charges amounts
        chrg_patt = re.compile(r'<Amt\s+Ccy="([A-Z]{3})"[^>]*>([^<]+)</Amt>')

        # Only check within <ChrgsInf> blocks
        chrg_blocks = re.finditer(r'<ChrgsInf>(.*?)</ChrgsInf>', xml_content, re.DOTALL)

        for block in chrg_blocks:
            block_content = block.group(1)
            for amt_match in chrg_patt.finditer(block_content):
                chrg_ccy = amt_match.group(1)
                if chrg_ccy != tx_ccy:
                    try:
                        line_num = xml_content.count('\n', 0, block.start() + amt_match.start()) + 1
                    except Exception:
                        line_num = "Unknown"

                    report.add_issue(ValidationIssue(
                        "ERROR", 3, "CHRG_CCY_MISMATCH", str(line_num),
                        f"Charges currency '{chrg_ccy}' does not match the transaction "
                        f"currency '{tx_ccy}'.",
                        f"Update the charges amount currency from '{chrg_ccy}' to '{tx_ccy}' "
                        f"to match the Interbank Settlement Amount currency."
                    ))

    # ISO 20022 MX character set regex for Strd / address fields (reusable)
    _SWIFT_CHARSET_RE = re.compile(r'^[0-9a-zA-Z/\-\?:\(\)\.,\'\+ !#$%&\*=^_`\{\|\}~\x22;<>@\[\\\]\r\n]+$')
    _CONTROL_CHAR_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')
    _XML_RESERVED_RE = re.compile(r'[<>&"]')

    def _validate_party_rules(self, xml_content: str, report: ValidationReport) -> None:
        """
        Step 4.12 — Party Identification Validation
        Validates all party blocks (Dbtr, Cdtr, UltmtDbtr, UltmtCdtr, InitgPty)
        for:
          1. Name presence and format (SWIFT charset, no control/HTML/newline chars)
          2. OrgId / PrvtId mutual exclusivity
          3. LEI format (20 alphanumeric)
          4. Party must have either Identification or Postal Address
        """
        try:
            parser = etree.XMLParser(recover=True, no_network=True, resolve_entities=False)
            root = etree.fromstring(xml_content.encode('utf-8'), parser)
        except Exception:
            return

        party_tags = ['Dbtr', 'Cdtr', 'UltmtDbtr', 'UltmtCdtr', 'InitgPty']

        def find_child(parent, tag_name):
            for c in parent.iter():
                if isinstance(c.tag, str) and c.tag.split('}')[-1] == tag_name:
                    return c
            return None

        for ptag in party_tags:
            for party in root.iter():
                if not isinstance(party.tag, str):
                    continue
                tag_local = party.tag.split('}')[-1] if '}' in party.tag else party.tag
                if tag_local != ptag:
                    continue

                line = party.sourceline or 1

                # --- Name validation ---
                nm_el = None
                for child in party:
                    if isinstance(child.tag, str) and child.tag.split('}')[-1] == 'Nm':
                        nm_el = child
                        break
                nm_el = find_child(party, 'Nm')

                if nm_el is not None and nm_el.text is not None:
                    name_val = nm_el.text
                    nm_line = nm_el.sourceline or line

                    # Empty or spaces only
                    if not name_val.strip():
                        report.add_issue(ValidationIssue(
                            "ERROR", 3, "PARTY_NAME_EMPTY", str(nm_line),
                            f"{ptag} name is empty or contains only spaces.",
                            f"Provide a valid name for the {ptag} party (max 140 chars)."
                        ))
                        continue

                    # Max length 140
                    if len(name_val) > 140:
                        report.add_issue(ValidationIssue(
                            "ERROR", 3, "PARTY_NAME_LENGTH", str(nm_line),
                            f"{ptag} name exceeds 140 characters ({len(name_val)} chars).",
                            "Shorten the party name to 140 characters or less."
                        ))

                    # Control characters
                    if self._CONTROL_CHAR_RE.search(name_val):
                        report.add_issue(ValidationIssue(
                            "ERROR", 3, "PARTY_NAME_CTRL_CHAR", str(nm_line),
                            f"{ptag} name contains invalid control characters.",
                            "Remove invisible control characters (ASCII 0-31) from the party name."
                        ))

                    # Newline characters
                    if '\n' in name_val or '\r' in name_val:
                        report.add_issue(ValidationIssue(
                            "WARNING", 3, "PARTY_NAME_NEWLINE", str(nm_line),
                            f"{ptag} name contains newline characters.",
                            "Remove line breaks from the party name. Use a single-line value."
                        ))

                    # XML/HTML reserved characters (unescaped)
                    if self._XML_RESERVED_RE.search(name_val):
                        report.add_issue(ValidationIssue(
                            "WARNING", 3, "PARTY_NAME_XML_CHARS", str(nm_line),
                            f"{ptag} name contains XML-reserved characters (< > & \").",
                            "Escape or remove XML-reserved characters from the party name."
                        ))

                    # SWIFT character set
                    if not self._SWIFT_CHARSET_RE.match(name_val.replace('\n', '').replace('\r', '')):
                        bad_chars = set(re.findall(r"[^a-zA-Z0-9 /\-?:().,'+\r\n]", name_val))
                        bad_str = ', '.join(f"'{c}'" for c in sorted(bad_chars)[:5])
                        report.add_issue(ValidationIssue(
                            "WARNING", 3, "PARTY_NAME_SWIFT_CHARSET", str(nm_line),
                            f"{ptag} name contains characters outside SWIFT character set: {bad_str}.",
                            "SWIFT FIN only allows: a-z A-Z 0-9 / - ? : ( ) . , ' + and space."
                        ))

                # --- OrgId / PrvtId mutual exclusivity ---
                id_el = find_child(party, 'Id')
                if id_el is not None:
                    org_id = find_child(id_el, 'OrgId')
                    prvt_id = find_child(id_el, 'PrvtId')

                    if org_id is not None and prvt_id is not None:
                        report.add_issue(ValidationIssue(
                            "ERROR", 3, "PARTY_ID_DUAL", str(id_el.sourceline or line),
                            f"{ptag} identification contains both OrgId and PrvtId.",
                            "A party must have either Organisation Identification OR Private Identification, not both."
                        ))

                    # LEI format check (20 alphanumeric)
                    lei_el = find_child(id_el, 'LEI') or find_child(party, 'LEI')
                    if lei_el is not None and lei_el.text:
                        lei_val = lei_el.text.strip()
                        if not re.match(r'^[A-Z0-9]{20}$', lei_val):
                            report.add_issue(ValidationIssue(
                                "ERROR", 3, "LEI_FORMAT", str(lei_el.sourceline or line),
                                f"Invalid LEI '{lei_val}' in {ptag}. LEI must be exactly 20 alphanumeric characters.",
                                "Correct the LEI to be exactly 20 uppercase alphanumeric characters (e.g., 7ZW8QJWVPR4P1J1KQY45)."
                            ))

                # --- Party must have either Id or PstlAdr ---
                has_id = find_child(party, 'Id') is not None
                has_addr = find_child(party, 'PstlAdr') is not None
                # Only enforce for Dbtr/Cdtr (main parties), not agents
                if ptag in ['Dbtr', 'Cdtr'] and not has_id and not has_addr:
                    if "pain.008" in report.message_type:
                        continue
                    report.add_issue(ValidationIssue(
                        "WARNING", 3, "PARTY_NO_ID_OR_ADDR", str(line),
                        f"{ptag} does not contain either an Identification or Postal Address.",
                        f"Add at least one of <Id> (with OrgId/PrvtId) or <PstlAdr> to the {ptag} block for better STP."
                    ))

    def _validate_address_cbpr_rules(self, xml_content: str, report: ValidationReport) -> None:
        """
        Step 4.13 — Address CBPR+ Rules Validation
        Validates all <PstlAdr> blocks for:
          1. Max 2 AdrLine elements (CBPR+ rule)
          2. AdrLine SWIFT character set
          3. Address fields not spaces-only
          4. No control/XML characters in address fields
          5. Structured address preferred over unstructured
          6. Field length limits (StrtNm=70, BldgNb=16, PstCd=16, TwnNm=35, CtrySubDvsn=35)
        """
        try:
            parser = etree.XMLParser(recover=True, no_network=True, resolve_entities=False)
            root = etree.fromstring(xml_content.encode('utf-8'), parser)
        except Exception:
            return

        FIELD_MAX_LENGTHS = {
            'StrtNm': 70, 'BldgNb': 16, 'BldgNm': 70, 'PstCd': 16,
            'TwnNm': 35, 'CtrySubDvsn': 35, 'Dept': 70, 'SubDept': 70,
            'Flr': 70, 'PstBx': 16, 'Room': 70
        }

        for addr in root.iter():
            if not isinstance(addr.tag, str):
                continue
            addr_local = addr.tag.split('}')[-1] if '}' in addr.tag else addr.tag
            if addr_local != 'PstlAdr':
                continue

            line = addr.sourceline or 1
            # Find the parent party name for context
            parent = addr.getparent()
            parent_name = ''
            if parent is not None and isinstance(parent.tag, str):
                parent_name = parent.tag.split('}')[-1] if '}' in parent.tag else parent.tag

            # Count AdrLine elements
            adr_lines = []
            for child in addr:
                if isinstance(child.tag, str) and child.tag.split('}')[-1] == 'AdrLine':
                    adr_lines.append(child)

            # CBPR+ max 2 AdrLine
            if len(adr_lines) > 2:
                report.add_issue(ValidationIssue(
                    "WARNING", 3, "ADDR_ADRLINE_LIMIT", str(line),
                    f"Address in {parent_name} has {len(adr_lines)} AdrLine elements. CBPR+ recommends maximum 2.",
                    "Reduce to 2 AdrLine elements or switch to structured address format."
                ))

            # Check each AdrLine
            for adr_el in adr_lines:
                if adr_el.text:
                    val = adr_el.text
                    adr_line_num = adr_el.sourceline or line

                    # AdrLine max 70
                    if len(val) > 70:
                        report.add_issue(ValidationIssue(
                            "ERROR", 3, "ADDR_ADRLINE_LENGTH", str(adr_line_num),
                            f"AdrLine in {parent_name} exceeds 70 characters ({len(val)} chars).",
                            "Shorten the address line to 70 characters or less."
                        ))

                    # Spaces only
                    if not val.strip():
                        report.add_issue(ValidationIssue(
                            "ERROR", 3, "ADDR_ADRLINE_EMPTY", str(adr_line_num),
                            f"AdrLine in {parent_name} is empty or contains only spaces.",
                            "Provide a valid address line value or remove the empty element."
                        ))
                        continue

                    # ISO 20022 MX charset
                    if not self._SWIFT_CHARSET_RE.match(val):
                        bad_chars = set(re.findall(r'[^0-9a-zA-Z/\-\?:\(\)\.,\'\+ !#$%&\*=^_`\{\|\}~\x22;<>@\[\\\]\r\n]', val))
                        bad_str = ', '.join(f"'{c}'" for c in sorted(bad_chars)[:5])
                        report.add_issue(ValidationIssue(
                            "WARNING", 3, "ADDR_ADRLINE_CHARSET", str(adr_line_num),
                            f"AdrLine in {parent_name} contains characters outside the ISO 20022 MX set: {bad_str}.",
                            "Allowed characters: letters, digits, space, and: / - ? : ( ) . , ' + ! # $ % & * = ^ _ ` {{ | }} ~ \" ; < > @ [ \\ ]."
                        ))

                    # Leading/Trailing space check
                    if val != val.strip():
                        report.add_issue(ValidationIssue(
                            "ERROR", 3, "ADDR_ADRLINE_WHITESPACE", str(adr_line_num),
                            f"AdrLine in {parent_name} contains leading or trailing spaces: '{val}'.",
                            "Remove leading and trailing whitespace from the address line. Use the 'Trim' function if necessary."
                        ))

                    # Control characters
                    if self._CONTROL_CHAR_RE.search(val):
                        report.add_issue(ValidationIssue(
                            "ERROR", 3, "ADDR_CTRL_CHAR", str(adr_line_num),
                            f"AdrLine in {parent_name} contains invalid control characters.",
                            "Remove invisible control characters from the address."
                        ))

            # Check structured fields for length and content
            has_structured = False
            has_ctry = False
            for child in addr:
                if not isinstance(child.tag, str):
                    continue
                child_local = child.tag.split('}')[-1] if '}' in child.tag else child.tag

                if child_local == 'Ctry':
                    has_ctry = True

                if child_local in FIELD_MAX_LENGTHS and child.text:
                    has_structured = True
                    val = child.text
                    max_len = FIELD_MAX_LENGTHS[child_local]
                    child_line = child.sourceline or line

                    # Length check
                    if len(val) > max_len:
                        report.add_issue(ValidationIssue(
                            "ERROR", 3, "ADDR_FIELD_LENGTH", str(child_line),
                            f"{child_local} in {parent_name} address exceeds {max_len} characters ({len(val)} chars).",
                            f"Shorten {child_local} to {max_len} characters or less."
                        ))

                    # Spaces only
                    if not val.strip():
                        report.add_issue(ValidationIssue(
                            "ERROR", 3, "ADDR_FIELD_EMPTY", str(child_line),
                            f"{child_local} in {parent_name} address is empty or contains only spaces.",
                            f"Provide a valid {child_local} value or remove the empty element."
                        ))

                    # Leading/Trailing space check
                    if val != val.strip():
                        report.add_issue(ValidationIssue(
                            "ERROR", 3, "ADDR_FIELD_WHITESPACE", str(child_line),
                            f"Address field '{child_local}' in {parent_name} contains leading or trailing spaces: '{val}'.",
                            f"Remove leading and trailing whitespace from the {child_local} field."
                        ))

                    # Control characters
                    if self._CONTROL_CHAR_RE.search(val):
                        report.add_issue(ValidationIssue(
                            "ERROR", 3, "ADDR_FIELD_CTRL", str(child_line),
                            f"{child_local} in {parent_name} contains control characters.",
                            f"Remove hidden control characters from {child_local}."
                        ))

            # CBPR+ — Country (Ctry) is mandatory in PstlAdr
            if not has_ctry:
                report.add_issue(ValidationIssue(
                    "ERROR", 3, "ADDR_CTRY_MISSING", str(line),
                    f"Country <Ctry> is missing in {parent_name} address.",
                    "Add a valid 2-character ISO country code (e.g., <Ctry>US</Ctry>) to the address block."
                ))

            # Structured preferred over unstructured (advisory)
            if len(adr_lines) > 0 and not has_structured:
                report.add_issue(ValidationIssue(
                    "WARNING", 3, "ADDR_PREFER_STRUCTURED", str(line),
                    f"Address in {parent_name} uses only AdrLine (unstructured). Structured address is preferred for CBPR+.",
                    "Consider using structured fields (StrtNm, TwnNm, Ctry, PstCd) instead of AdrLine for better STP."
                ))

    def _validate_remittance_rules(self, xml_content: str, report: ValidationReport) -> None:
        """
        Step 4.14 — Remittance Information Validation (CBPR+ SR2025)
        Validates:
          1. Ustrd max length 140
          2. Strd and Ustrd mutually exclusive
          3. CdtrRefInf SCOR validation (ISO 11649)
          4. Ustrd no control characters (FIN-X)
        """
        try:
            parser = etree.XMLParser(recover=True, no_network=True, resolve_entities=False)
            root = etree.fromstring(xml_content.encode('utf-8'), parser)
        except Exception:
            return

        # Determine if this is a pacs.009 standard vs COV
        message_type = "Unknown"
        if root.tag and '}' in root.tag:
            ns = root.tag.split('}')[0]
            if 'pacs.008' in ns: message_type = 'pacs.008'
            elif 'pacs.009' in ns: message_type = 'pacs.009'
            elif 'pacs.004' in ns: message_type = 'pacs.004'
            elif 'pacs.002' in ns: message_type = 'pacs.002'
            elif 'camt.056' in ns: message_type = 'camt.056'
            elif 'camt.029' in ns: message_type = 'camt.029'
            elif 'camt.053' in ns: message_type = 'camt.053'
            elif 'camt.052' in ns: message_type = 'camt.052'
            elif 'camt.054' in ns: message_type = 'camt.054'
            elif 'pain.001' in ns: message_type = 'pain.001'
            elif 'pain.002' in ns: message_type = 'pain.002'

        # Special logic to determine if it is pacs.009 COV
        if message_type and message_type.startswith('pacs.009'):
            # Fast check if it is COV by looking for UndrlygCstmrCdtTrf
            is_cov = False
            for _ in root.iter(f"{{{root.tag.split('}')[0]}}}UndrlygCstmrCdtTrf"):
                is_cov = True
                break
            if is_cov:
                message_type += '.cov'
        
        # Helper for ISO 11649 Creditor Reference validation
        def is_iso11649(ref: str) -> bool:
            # ISO 11649 must start with RF and have exactly 2 check digits, up to 25 chars total length.
            # E.g. RF18...
            ref = ref.replace(" ", "").upper()
            if not ref.startswith("RF") or len(ref) < 5 or len(ref) > 25:
                return False
            # Check digits calculation
            try:
                rearranged = ref[4:] + ref[:4]
                numeric_val = ""
                for char in rearranged:
                    if char.isdigit():
                        numeric_val += char
                    else:
                        numeric_val += str(ord(char) - 55)
                return int(numeric_val) % 97 == 1
            except Exception:
                return False

        for elem in root.iter():
            if not isinstance(elem.tag, str):
                continue
            ns_prefix = f"{{{elem.tag.split('}')[0]}}}" if '}' in elem.tag else ""
            tag_local = elem.tag.split('}')[-1] if '}' in elem.tag else elem.tag

            if tag_local == 'RmtInf':
                rm_ln = elem.sourceline or 1
                
                if message_type == 'pacs.009':
                    report.add_issue(ValidationIssue("ERROR", 3, "PACS009-RMT-001", str(rm_ln), "Remittance information is not permitted in standard pacs.009. Use pacs.009 COV variant."))
                    
                if message_type in ['pacs.002', 'pain.002']:
                    report.add_issue(ValidationIssue("ERROR", 3, f"{message_type.upper().replace('.', '')}-RMT-001", str(rm_ln), f"Remittance information is not permitted in {message_type} status report messages."))

                has_strd = False
                has_ustrd = False
                
                for child in elem:
                    c_tag = child.tag.split('}')[-1] if '}' in child.tag else child.tag
                    c_ln = child.sourceline or rm_ln
                    if c_tag == 'Ustrd':
                        has_ustrd = True
                        val = child.text or ""
                        
                        if len(val) > 140:
                            report.add_issue(ValidationIssue("ERROR", 3, "GLOBAL-RMT-UST-LEN", str(c_ln), f"Unstructured remittance exceeds 140 characters ({len(val)} chars)."))
                        if self._CONTROL_CHAR_RE.search(val):
                            report.add_issue(ValidationIssue("WARNING", 3, "GLOBAL-RMT-FINX", str(c_ln), "Remittance field contains characters outside the permitted FIN-X extended character set."))
                            
                    elif c_tag == 'Strd':
                        has_strd = True
                        # AddtlRmtInf validation
                        addtls = child.findall(f"{ns_prefix}AddtlRmtInf")
                        if len(addtls) > 3:
                            report.add_issue(ValidationIssue("ERROR", 3, "GLOBAL-RMT-ADDTL-OCCUR", str(c_ln), "AdditionalRemittanceInformation may only occur a maximum of 3 times per Strd block."))
                        
                        for ad in addtls:
                            ad_val = ad.text or ""
                            if len(ad_val) > 140:
                                report.add_issue(ValidationIssue("ERROR", 3, "GLOBAL-RMT-ADDTL-LEN", str(ad.sourceline or c_ln), "AdditionalRemittanceInformation must not exceed 140 characters."))
                        
                        # SCOR Creditor Reference Validation
                        cdtr_ref_inf = child.find(f"{ns_prefix}CdtrRefInf")
                        if cdtr_ref_inf is not None:
                            tp = cdtr_ref_inf.find(f"{ns_prefix}Tp")
                            if tp is not None:
                                cd_or_prtry = tp.find(f"{ns_prefix}CdOrPrtry")
                                if cd_or_prtry is not None:
                                    cd = cd_or_prtry.find(f"{ns_prefix}Cd")
                                    if cd is not None and cd.text == "SCOR":
                                        ref = cdtr_ref_inf.find(f"{ns_prefix}Ref")
                                        if ref is not None and ref.text:
                                            if not is_iso11649(ref.text):
                                                report.add_issue(ValidationIssue("ERROR", 3, "GLOBAL-RMT-SCOR", str(ref.sourceline or c_ln), "Creditor reference type SCOR must conform to ISO 11649 format."))
                        
                        # Extended fields length validation
                        invcr = child.find(f"{ns_prefix}Invcr")
                        if invcr is not None:
                            nm = invcr.find(f"{ns_prefix}Nm")
                            if nm is not None and nm.text and len(nm.text) > 140:
                                report.add_issue(ValidationIssue("ERROR", 3, "GLOBAL-RMT-INVCR-LEN", str(nm.sourceline or c_ln), "Invoicer Name must not exceed 140 characters."))
                        invcee = child.find(f"{ns_prefix}Invcee")
                        if invcee is not None:
                            nm = invcee.find(f"{ns_prefix}Nm")
                            if nm is not None and nm.text and len(nm.text) > 140:
                                report.add_issue(ValidationIssue("ERROR", 3, "GLOBAL-RMT-INVCEE-LEN", str(nm.sourceline or c_ln), "Invoicee Name must not exceed 140 characters."))
                        rfrd_doc = child.find(f"{ns_prefix}RfrdDocInf")
                        if rfrd_doc is not None:
                            nb = rfrd_doc.find(f"{ns_prefix}Nb")
                            if nb is not None and nb.text and len(nb.text) > 35:
                                report.add_issue(ValidationIssue("ERROR", 3, "GLOBAL-RMT-RFRDDOC-LEN", str(nb.sourceline or c_ln), "Referred Document Number must not exceed 35 characters."))

                if has_strd and has_ustrd:
                    report.add_issue(ValidationIssue("ERROR", 3, "GLOBAL-RMT-001", str(rm_ln), "Structured and Unstructured remittance are mutually exclusive in all CBPR+ messages.", "Remove either Strd or Ustrd from the RmtInf block."))
                
                if message_type in ['pacs.008', 'pain.001']:
                    mandate_date_str = self.config.get("validation_rules", {}).get("cbpr_plus_mandate_date", "2027-11-01T00:00:00")
                    try:
                         mandate_date = datetime.fromisoformat(mandate_date_str)
                    except:
                         mandate_date = datetime(2027, 11, 1)
                         
                    is_after_2027 = datetime.now() > mandate_date
                    if has_ustrd and not has_strd:
                         severity = "ERROR" if is_after_2027 else "WARNING"
                         report.add_issue(ValidationIssue(severity, 3, "GLOBAL-RMT-004", str(rm_ln), f"Structured remittance is mandatory in payment messages from November 2027. Currently using Unstructured (Ustrd)."))

            # --- CBPR+ Purpose & Category Purpose Validation (SR2025) ---
            if tag_local in ['Purp', 'CtgyPurp']:
                ln = elem.sourceline or 1
                type_name = "Purpose" if tag_local == 'Purp' else "Category Purpose"
                code_list_key = "purp" if tag_local == 'Purp' else "ctgypurp"
                
                cd_elem = None
                for child in elem:
                    if isinstance(child.tag, str) and child.tag.split('}')[-1] == 'Cd':
                        cd_elem = child
                        break
                
                if cd_elem is None:
                    report.add_issue(ValidationIssue("ERROR", 3, f"SR2025_{tag_local.upper()}_NO_CD", str(ln), f"{type_name} must contain a code <Cd>."))
                elif not cd_elem.text or not cd_elem.text.strip():
                    report.add_issue(ValidationIssue("ERROR", 3, f"SR2025_{tag_local.upper()}_EMPTY_CD", str(cd_elem.sourceline or ln), f"{type_name} code <Cd> cannot be empty."))
                else:
                    val = cd_elem.text.strip()
                    if code_list_key in self.codelists:
                        valid_codes = self.codelists[code_list_key].get("codes", [])
                        if val not in valid_codes:
                            report.add_issue(ValidationIssue("ERROR", 3, f"SR2025_{tag_local.upper()}_INVALID_CODE", str(cd_elem.sourceline or ln), f"Invalid {type_name} code: '{val}'."))

    def _normalize_message(self, xml_content: str) -> tuple:
        """
        Step 5: Canonical Message Creation
        Converts XML to a flat canonical JSON structure with indexed paths.
        Returns (data_map, line_map)
        """
        canonical = {}
        line_map = {}
        try:
            parser = etree.XMLParser(recover=True, remove_blank_text=True)
            root = etree.fromstring(xml_content.encode('utf-8'), parser)
            
            def get_clean_tag(tag):
                return tag.split('}')[-1] if '}' in tag else tag

            def flatten(element, path=""):
                if path:
                    line_map[path] = element.sourceline

                # 1. Attributes
                for k, v in element.attrib.items():
                    attr_name = get_clean_tag(k)
                    attr_path = f"{path}@{attr_name}" if path else f"@{attr_name}"
                    canonical[attr_path] = v
                    line_map[attr_path] = element.sourceline

                # 2. Text value
                if element.text and element.text.strip():
                    canonical[path] = element.text.strip()

                # 3. Children with indexing for repeats
                # Note: skip non-element nodes (Comments, PIs) whose .tag is a function
                element_children = [c for c in element if isinstance(c.tag, str)]
                tag_counts = {}
                for child in element_children:
                    tag = get_clean_tag(child.tag)
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1
                
                current_counts = {}
                for child in element_children:
                    tag = get_clean_tag(child.tag)
                    
                    # Construct indexed path: Tag[0], Tag[1] if multiple exist
                    if tag_counts[tag] > 1:
                        idx = current_counts.get(tag, 0)
                        indexed_tag = f"{tag}[{idx}]"
                        current_counts[tag] = idx + 1
                    else:
                        indexed_tag = tag
                        
                    new_path = f"{path}.{indexed_tag}" if path else indexed_tag
                    flatten(child, new_path)

            # ISO 20022 Messages typically contain AppHdr and Document
            # We flatten both into the same flat map for rule access
            for part in ["AppHdr", "Document"]:
                node = root.find(f".//{{*}}{part}")
                if node is not None:
                    flatten(node, part)
                    
            # If nothing found by part name, flatten the whole thing from root
            if not canonical:
                flatten(root, get_clean_tag(root.tag))
                
        except Exception as e:
            print(f"DEBUG: Normalization Error: {e}")
            
        return canonical, line_map

    def _detect_message_type(self, xml_content: str) -> str:
        """
        Robust Message Type Detection - Prioritizes Payload over Header
        """
        # Load dynamic scan limit
        scan_limit = self.config.get("app_settings", {}).get("scan_limit_chars", 10000)

        # 1. Broad Namespace Search (Handles single/double quotes)
        ns_patterns = self.config.get("validation_rules", {}).get("namespace_patterns", [
            r'xmlns[:\w]*\s*=\s*["\']urn:iso:std:iso:20022:tech:xsd:([^"\']+)["\']',
            r'xmlns[:\w]*\s*=\s*["\']urn:swift:xsd:([^"\']+)["\']'
        ])
        
        res = "Unknown"

        # 1. MsgDefIdr — authoritative explicit declaration. Check FIRST so a
        #    camt.056 message whose <Document> accidentally carries a pacs.008
        #    namespace doesn't get detected as pacs.008 and validated against the
        #    wrong XSD (producing a confusing "expected FIToFICstmrCdtTrf" error).
        match_hdr = re.search(r'<MsgDefIdr>([^<]+)</MsgDefIdr>', xml_content)
        if match_hdr:
            res = match_hdr.group(1).strip()

        # 2. Broad Namespace Search (Handles single/double quotes)
        if res == "Unknown":
            ns_patterns = self.config.get("validation_rules", {}).get("namespace_patterns", [
                r'xmlns[:\w]*\s*=\s*["\']urn:iso:std:iso:20022:tech:xsd:([^"\']+)["\']',
                r'xmlns[:\w]*\s*=\s*["\']urn:swift:xsd:([^"\']+)["\']'
            ])

            candidates = []
            for pattern in ns_patterns:
                for match in re.finditer(pattern, xml_content[:scan_limit]):
                    val = match.group(1).strip()
                    # Prioritize non-header and non-envelope types
                    if all(x not in val.lower() for x in ["head.001", "envelope", "busmsgenvlp"]):
                        res = val
                        break
                    candidates.append(val)
                if res != "Unknown": break

            # If no payload namespace, check candidates
            if res == "Unknown" and candidates:
                res = candidates[0]

        # 3. Root Tag Heuristic (e.g. <pacs.008.001.08 ...>)
        if res == "Unknown":
            match_root = re.search(r'<([a-z]{4}\.[0-9]{3}\.[0-9]{3}\.[0-9]{2})', xml_content[:2000])
            if match_root:
                res = match_root.group(1).strip()

        # 4. Family Fallback
        if res == "Unknown":
            families = self.config.get("supported_families_fallback", ["pacs.008", "pacs.009", "pain.001", "camt.053"])
            for family in families:
                if family in xml_content[:5000]: # Search first 5K for performance
                    res = family
                    break
        
        # FINAL REFINEMENT: Cover variants
        if res.startswith("pacs.009"):
            if "<Prtry>ADV</Prtry>" in xml_content:
                return "pacs.009.001.08_ADV"
            elif "UndrlygCstmrCdtTrf" in xml_content or "cov" in xml_content.lower():
                return "pacs.009.cov"
            
        return res

    def _finalize_report(self, report: ValidationReport, start_time: float) -> ValidationReport:
        """
        Runs post-processing on the report:
        1. Deduplicates issues (especially common when structural and business rules overlap).
        2. Assigns final Pass/Fail status to layers.
        3. Calculates total time.
        """
        unique_issues = []
        seen_keys = set()
        
        for issue in report.issues:
            # 0. Skip issues belonging to a SKIPPED layer
            layer_str = str(issue.get("layer", ""))
            if layer_str in report.layer_status and report.layer_status[layer_str].get("status") == "SKIPPED":
                self._decrement_counters(report, issue)
                continue

            # Create a unique signature for deduplication
            code = issue.get("code")
            line = issue.get("path")
            msg = issue.get("message")
            
            key = (issue.get("severity"), line, msg)
            
            # Special case for fuzzy matching similar errors on same line
            is_schme_err = any(x in msg.lower() for x in ["schmenm", "<cd>", "<prtry>", "scheme code"])
            is_dupe_err = "duplicate" in msg.lower() or "is duplicated" in msg.lower()

            if is_schme_err or is_dupe_err:
                duplicate_found = False
                for existing in unique_issues:
                    if str(existing.get("path")) == str(line):
                        ex_msg = existing.get("message", "").lower()
                        
                        # Case A: Both are scheme errors
                        if is_schme_err and any(x in ex_msg for x in ["schmenm", "<cd>", "<prtry>", "scheme code"]):
                             duplicate_found = True
                             break
                             
                        # Case B: Both are duplication errors
                        if is_dupe_err and ("duplicate" in ex_msg or "is duplicated" in ex_msg):
                             duplicate_found = True
                             break
                
                if duplicate_found:
                    self._decrement_counters(report, issue)
                    continue

            unique_issues.append(issue)
            seen_keys.add(key)
        
        report.issues = unique_issues

        # 2. Check all issues and update layer statuses appropriately 
        for layer_str in ["1", "2", "3"]:
            layer_errors = [i for i in report.issues if str(i.get("layer", "")) == layer_str and i.get("severity") == "ERROR"]
            if layer_str in report.layer_status and report.layer_status[layer_str]["status"] == "SKIPPED":
                continue # Preserve SKIPPED status
            elif layer_errors and layer_str in report.layer_status:
                report.layer_status[layer_str]["status"] = "❌"
            elif layer_str in report.layer_status:
                 report.layer_status[layer_str]["status"] = "✅"

        # 3. Update global report status
        if report.errors == 0:
            report.status = "PASS"
        else:
            report.status = "FAIL"

        # 4. Calculate total time
        total_layers = sum(l.get("time", 0) for l in report.layer_status.values())
        report.total_time_ms = total_layers
        return report

    def _decrement_counters(self, report, issue):
        """Helper to adjust counters when removing a duplicate issue."""
        if issue.get("severity") == "ERROR":
            report.errors = max(0, report.errors - 1)
        elif issue.get("severity") == "WARNING":
            report.warnings = max(0, report.warnings - 1)

    def _get_xsd_path(self, message_type: str) -> Optional[str]:
        """
        Locates the XSD file for the given message type.
        1. Exact Match (e.g. pacs.008.001.08.xsd)
        2. Family Fallback (e.g. pacs.008.xsd)
        3. Version Fallback (Look for highest available version e.g. .13)

        Memoized: the filesystem layout is fixed for a process lifetime, so the
        path for a given message_type is computed once and cached. The fallback
        branch does an os.listdir() scan which is comparatively expensive to
        repeat on every validation.
        """
        if not message_type or message_type == "Unknown":
            return None

        cached = self._xsd_path_cache.get(message_type)
        if cached is not None or message_type in self._xsd_path_cache:
            return cached

        result = self._resolve_xsd_path(message_type)
        self._xsd_path_cache[message_type] = result
        return result

    def _resolve_xsd_path(self, message_type: str) -> Optional[str]:
        # Special handling for internal refined types
        if message_type == "pacs.009.cov":
            message_type = "pacs.009.001.08" # Use standard version for XSD
        elif message_type in ["pacs.009.adv", "pacs.009.001.08_ADV"]:
            message_type = "pacs.009.001.08_ADV"

        # 1. Exact Match
        exact_xsd = f"{message_type}.xsd"
        exact_path = os.path.join(self.xsd_path, exact_xsd)
        if os.path.exists(exact_path):
            return exact_path

        # 2. Family Match (User's specific preference for short names)
        parts = message_type.split('.')
        family_prefix = ".".join(parts[:3]) if len(parts) >= 3 else message_type
        family_short = parts[0] + "." + parts[1] if len(parts) >= 2 else message_type

        family_xsd = f"{family_short}.xsd"
        family_path = os.path.join(self.xsd_path, family_xsd)
        if os.path.exists(family_path):
            return family_path

        # 3. Version-Blind Fallback (Highest available version in family)
        try:
            candidates = []
            for f in os.listdir(self.xsd_path):
                if f.startswith(family_prefix) and f.endswith(".xsd"):
                    candidates.append(f)

            if candidates:
                best_match = sorted(candidates, reverse=True)[0]
                fallback_path = os.path.join(self.xsd_path, best_match)
                print(f"XSD: Exact version '{message_type}' not found. Falling back to '{best_match}'.")
                return fallback_path
        except:
            pass

        return None

    def _validate_clearing_system_rules(self, xml_content: str, report: ValidationReport) -> None:
        """
        Step 4.15 — Clearing System Specific Rules
        1. TARGET2 (T2) -> Settlement Currency MUST be "EUR"
        2. CHAPS -> Transaction Currency MUST be "GBP"
        3. ClrSysRef (SR2025) -> Mandatory if clearing system used, forbidden if not.
        Uses etree for absolute reliability with namespaces and prefixes.
        """
        try:
            parser = etree.XMLParser(recover=True, no_network=True, resolve_entities=False)
            root = etree.fromstring(xml_content.encode('utf-8'), parser)
        except Exception:
            return

        def local(tag):
            return tag.split('}')[-1] if '}' in tag else tag

        # --- 1. Identify clearing systems and ClrSysRef elements ---
        active_systems = set()
        for cd in root.xpath("//*[local-name()='Cd']"):
            if cd.text:
                val = cd.text.strip().upper()
                parent = cd.getparent()
                if parent is not None and local(parent.tag) in ('ClrSysId', 'ClrSys'):
                    active_systems.add(val)
        
        # Also check ClrChanl for values like RTGS
        for chanl in root.xpath("//*[local-name()='ClrChanl']"):
            if chanl.text:
                active_systems.add(chanl.text.strip().upper())
        
        clr_ref_els = root.xpath("//*[local-name()='ClrSysRef']")
        has_clr_ref = len(clr_ref_els) > 0
        
        # --- 2. Extract Business Service/Standard (e.g. pacs.009.001.08) ---
        biz_svc = "Unknown"
        doc = root.find(".//{*}Document")
        if doc is not None and len(doc) > 0:
            biz_svc = local(doc[0].tag)

        # --- 3. ClrSysRef SPECIAL RULES (Manual Entry Scope) ---
        
        # Rule 3.1: No Empty ClrSysRef Tag
        for ref_el in clr_ref_els:
            if not ref_el.text or not ref_el.text.strip():
                report.add_issue(ValidationIssue(
                    "ERROR", 3, "CLRSYSREF_EMPTY", str(ref_el.sourceline or "Unknown"),
                    "Clearing System Reference <ClrSysRef> must NOT be empty.",
                    "Provide a valid alphanumeric reference or remove the empty tag."
                ))

        # Rule 3.2: Only one ClrSysRef per PmtId (Structural check)
        for pmt_id in root.xpath("//*[local-name()='PmtId']"):
            refs = pmt_id.xpath("./*[local-name()='ClrSysRef']")
            if len(refs) > 1:
                report.add_issue(ValidationIssue(
                    "ERROR", 3, "CLRSYSREF_DUPLICATE", str(refs[1].sourceline or pmt_id.sourceline),
                    "Only one Clearing System Reference <ClrSysRef> is allowed inside <PmtId>.",
                    "Remove the duplicate <ClrSysRef> element."
                ))

        # Rule 3.3: ClrSysRef MUST NOT be sent WITHOUT a clearing system
        # Standard Clearing Systems: T2, CHAPS, CHIPS, FED, RTGS (as per user req)
        standard_systems = {'T2', 'CHAPS', 'CHIPS', 'FED', 'RTGS'}
        has_standard_clearing = any(s in standard_systems for s in active_systems)

        if has_clr_ref and not has_standard_clearing:
             # Find the first ClrSysRef for report line number
             line = clr_ref_els[0].sourceline or "Unknown"
             report.add_issue(ValidationIssue(
                 "ERROR", 3, "CLRSYSREF_FORBIDDEN", str(line),
                 "Clearing System Reference <ClrSysRef> must NOT be sent if no active clearing system is used.",
                 "Remove <ClrSysRef> or specify a clearing system (T2, CHAPS, CHIPS, FED, or RTGS) in agent identifiers."
             ))
        
        # Rule 3.4: ClrSysRef Recommendation (Warning if missing when clearing is used)
        if has_standard_clearing and not has_clr_ref:
             # Report on PmtId or IntrBkSttlmAmt
             report.add_issue(ValidationIssue(
                 "WARNING", 3, "CLRSYSREF_RECOMMENDED", "Unknown",
                 "Clearing System Reference is recommended when a clearing system (T2/CHAPS/etc.) is used.",
                 "Consider adding <ClrSysRef> under <PmtId> for better tracking."
             ))

        # --- 4. Currency Specific Rules (Legacy) ---
        for amt in root.xpath("//*[local-name()='IntrBkSttlmAmt' or local-name()='Amt']"):
            ccy = amt.get('Ccy')
            if not ccy: continue
            ccy = ccy.strip().upper()
            line_num = amt.sourceline or "Unknown"

            # Check T2 Rule
            if 'T2' in active_systems and ccy != 'EUR':
                report.add_issue(ValidationIssue(
                    "ERROR", 3, "T2_CURRENCY_ERROR", str(line_num),
                    "T2 allows only EUR currency.",
                    f"Clearing System 'T2' detected, but currency is '{ccy}'. Change IntrBkSttlmAmt currency to 'EUR'."
                ))

            # Check CHIPS Rule
            if 'CHIPS' in active_systems and ccy != 'USD':
                report.add_issue(ValidationIssue(
                    "ERROR", 3, "CHIPS_CURRENCY_ERROR", str(line_num),
                    "CHIPS allows only USD currency.",
                    f"Clearing System 'CHIPS' detected, but currency is '{ccy}'. Change IntrBkSttlmAmt currency to 'USD'."
                ))

            # Check FED Rule
            if 'FED' in active_systems and ccy != 'USD':
                report.add_issue(ValidationIssue(
                    "ERROR", 3, "FED_CURRENCY_ERROR", str(line_num),
                    "FED allows only USD currency.",
                    f"Clearing System 'FED' detected, but currency is '{ccy}'. Change IntrBkSttlmAmt currency to 'USD'."
                ))

            # Check CHAPS Rule
            if 'CHAPS' in active_systems and ccy != 'GBP':
                report.add_issue(ValidationIssue(
                    "ERROR", 3, "CHAPS_CURRENCY_ERROR", str(line_num),
                    "Invalid Currency for CHAPS clearing system. When ClrSysId/Cd = CHAPS, the transaction currency must be GBP.",
                    f"Clearing System 'CHAPS' detected, but currency is '{ccy}'. Change IntrBkSttlmAmt currency to 'GBP'."
                ))

        # --- 5. Settlement Priority (SttlmPrty) Rules ---
        sttlm_prty_els = root.xpath("//*[local-name()='SttlmPrty']")
        
        # Rule 5.1: No Empty Tag & Valid Values
        for sp_el in sttlm_prty_els:
            if not sp_el.text or not sp_el.text.strip():
                report.add_issue(ValidationIssue(
                    "ERROR", 3, "STTLMPRTY_EMPTY", str(sp_el.sourceline or "Unknown"),
                    "Settlement Priority <SttlmPrty> must NOT be empty.",
                    "Provide HIGH or NORM or remove the empty tag."
                ))
            else:
                val = sp_el.text.strip().upper()
                if val not in ('HIGH', 'NORM'):
                    report.add_issue(ValidationIssue(
                        "ERROR", 3, "STTLMPRTY_INVALID", str(sp_el.sourceline or "Unknown"),
                        f"Invalid Settlement Priority: '{val}'. Must be HIGH or NORM.",
                        "Change value to HIGH or NORM."
                    ))
            
            # Rule 5.2: Dependency - Must be inside CdtTrfTxInf
            parent = sp_el.getparent()
            if parent is not None and local(parent.tag) != 'CdtTrfTxInf':
                report.add_issue(ValidationIssue(
                    "ERROR", 3, "STTLMPRTY_WRONG_PARENT", str(sp_el.sourceline or "Unknown"),
                    "Settlement Priority <SttlmPrty> MUST be inside <CdtTrfTxInf>.",
                    "Move <SttlmPrty> directly under <CdtTrfTxInf>."
                ))

        # Rule 5.3: Position and Uniqueness (Relative to CdtTrfTxInf)
        for tx_inf in root.xpath("//*[local-name()='CdtTrfTxInf']"):
            sp = tx_inf.xpath("./*[local-name()='SttlmPrty']")
            if len(sp) > 1:
                 report.add_issue(ValidationIssue(
                    "ERROR", 3, "STTLMPRTY_DUPLICATE", str(sp[1].sourceline or tx_inf.sourceline),
                    "Only one Settlement Priority <SttlmPrty> is allowed per transaction.",
                    "Remove the duplicate element."
                ))
            
            if sp:
                # MUST appear immediately after IntrBkSttlmDt
                prev = sp[0].getprevious()
                # Skip comments/PIs
                while prev is not None:
                    if isinstance(prev.tag, str):
                        break
                    prev = prev.getprevious()
                
                prev_tag = local(prev.tag) if prev is not None else "None"
                if prev is None or prev_tag != 'IntrBkSttlmDt':
                    report.add_issue(ValidationIssue(
                        "ERROR", 3, "STTLMPRTY_WRONG_POSITION", str(sp[0].sourceline or "Unknown"),
                        "Settlement Priority <SttlmPrty> MUST appear immediately after <IntrBkSttlmDt>.",
                        "Ensure <SttlmPrty> follows <IntrBkSttlmDt> according to ISO 20022 sequence."
                    ))

        # Rule 5.4: Business Rule - RTGS Recommendation
        if 'RTGS' in active_systems:
            has_high = any(el.text and el.text.strip().upper() == 'HIGH' for el in sttlm_prty_els)
            if not has_high:
                 report.add_issue(ValidationIssue(
                    "WARNING", 3, "RTGS_STTLMPRTY_RECOMMENDED", "Unknown",
                    "For RTGS clearing channel, Settlement Priority HIGH is recommended.",
                    "Consider setting Settlement Priority to HIGH for faster processing."
                ))



    def _validate_charsets_in_xml(self, xml_content: str, report) -> None:
        """
        STEP 4.16 - Character Set Validation for Name and Address Tags

        Checks that text fields like Nm, StrtNm, TwnNm, BldgNm, AdrLine, DstrctNm, CtrySubDvsn
        only contain safe characters: a-z A-Z 0-9 space . , ( ) ' -

        Specifically BLOCKS: & @ ! # $ % * < > ; : / ^ ~ ` | {{ }} [ ] = +
        """
        import re as _re
        from .models import ValidationIssue as _VI

        CHECKED_TAGS = {
            'Nm', 'StrtNm', 'TwnNm', 'BldgNm', 'AdrLine',
            'DstrctNm', 'CtrySubDvsn', 'TwnLctnNm', 'ClrSysRef'
        }
        # ISO 20022 MX extended character set for Strd fields
        SAFE = _re.compile(r'^[0-9a-zA-Z/\-\?:\(\)\.,\'\+ !#$%&\*=^_`\{\|\}~\x22;<>@\[\\\]]+$')
        tag_alt = "|".join(_re.escape(t) for t in CHECKED_TAGS)
        patt = _re.compile(r'<(' + tag_alt + r')>\s*([^<]+?)\s*</\1>')

        seen = set()
        for m in patt.finditer(xml_content):
            tag_name = m.group(1)
            raw_value = m.group(2) # Don't strip here yet, we need to check for spaces
            key = (tag_name, raw_value)
            if key in seen or not raw_value:
                continue
            seen.add(key)

            # 1. Leading/Trailing space check
            if raw_value != raw_value.strip():
                try:
                    line_num = xml_content.count('\n', 0, m.start()) + 1
                except Exception:
                    line_num = 'Unknown'
                report.add_issue(_VI(
                    "ERROR", 3, "WHITESPACE_ERROR", str(line_num),
                    f"Field <{tag_name}> contains leading or trailing spaces: '{raw_value}'.",
                    f"Remove leading/trailing spaces from the <{tag_name}> element. These are not permitted in ISO 20022 MX messages."
                ))

            # 2. Charset check (strip for this check specifically)
            val_to_check = raw_value.strip()
            if val_to_check and not SAFE.match(val_to_check):
                inv = sorted(set(c for c in val_to_check if not _re.match(r'[0-9a-zA-Z/\-\?:\(\)\.,\'\+ !#$%&\*=^_`\{\|\}~\x22;<>@\[\\\]]', c)))
                inv_display = ' '.join(repr(c) for c in inv)
                try:
                    line_num = xml_content.count('\n', 0, m.start()) + 1
                except Exception:
                    line_num = 'Unknown'
                report.add_issue(_VI(
                    "ERROR", 3, "INVALID_CHARSET", str(line_num),
                    f"Field <{tag_name}> contains invalid character(s): {inv_display}. "
                    f"Only ISO 20022 MX permitted characters are allowed.",
                    f"Remove or replace {inv_display} in <{tag_name}>. "
                    f"Allowed characters: letters, digits, space, and: / - ? : ( ) . , ' + ! # $ % & * = ^ _ ` {{{{ | }}}} ~ \" ; < > @ [ \\ ]."
                ))

    def _get_xpath_for_element(self, element) -> str:
        """Helper to build a simple non-indexed XPath for an lxml element"""
        path = []
        curr = element
        while curr is not None:
            if isinstance(curr.tag, str):
                tag = curr.tag.split('}')[-1] if '}' in curr.tag else curr.tag
                path.append(tag)
            curr = curr.getparent()
        return '/' + '/'.join(reversed(path))


    def _validate_duplicate_tags(self, xml_content: str, report: ValidationReport, message_type: str) -> None:
        """
        Step 4.18 — Duplicate Tag Validation
        Checks for tags that appear more than maxOccurs allowed by the schema.
        Reports as Layer 3 Business Rule as requested.
        """
        try:
            # 1. Get XSD tag info to know maxOccurs
            xsd_path = self._get_xsd_path(message_type)
            if not xsd_path:
                return
            
            tag_info = self._build_tag_info_from_xsd(xsd_path)
            if not tag_info:
                return

            # 2. Parse XML (CRITICAL: remove_blank_text must be False to preserve accurate line numbers)
            parser = etree.XMLParser(recover=True, remove_blank_text=False)
            root = etree.fromstring(xml_content.encode('utf-8'), parser)

            # 3. Traverse and check counts for children of each element
            for elem in root.iter():
                if not isinstance(elem.tag, str):
                    continue
                
                # Filter out non-element children
                children = [c for c in elem if isinstance(c.tag, str)]
                if not children:
                    continue
                    
                tag_counts = {}
                for child in children:
                    t = child.tag.split('}')[-1] if '}' in child.tag else child.tag
                    tag_counts[t] = tag_counts.get(t, 0) + 1
                
                for tag, count in tag_counts.items():
                    info = tag_info.get(tag)
                    if not info:
                        continue
                    
                    max_allowed = info.get('max', '1')
                    if max_allowed == 'unbounded':
                        continue
                        
                    try:
                        max_val = int(max_allowed)
                    except:
                        max_val = 1
                        
                    if count > max_val:
                        # Find the first child that exceeds the limit (max_val indexed instance)
                        instances = [c for c in children if (c.tag.split('}')[-1] if '}' in c.tag else c.tag) == tag]
                        offending_child = instances[max_val] if len(instances) > max_val else instances[-1]
                        line = offending_child.sourceline or elem.sourceline or 1
                        
                        parent_xpath = self._get_xpath_for_element(elem)
                        tag_xpath = f"{parent_xpath}/{tag}" if parent_xpath != "/" else f"/{tag}"
                        
                        report.add_issue(ValidationIssue(
                            "ERROR",
                            2, # Layer 2 Schema Validation
                            "DUPLICATE_TAG",
                            str(line),
                            f"Duplicate tag detected: <{tag}>",
                            f"The tag <{tag}> appears {count} times, but only {max_val} is allowed at this location ({tag_xpath})."
                        ))
        except Exception as e:
            print(f"DEBUG: Duplicate Tag Validation Error: {e}")
    def _check_lei(self, lei: str) -> str:
        """
        Validates Legal Entity Identifier (LEI) using ISO 7064 MOD 97-10.
        Returns "OK" or an error code.
        """
        lei = lei.strip().upper()
        
        # 1. Correct Length
        if not lei or len(lei) != 20: 
            return "INVALID_LENGTH"
            
        # 2. Character Set
        if not re.match(r'^[A-Z0-9]{20}$', lei): 
            return "INVALID_CHARACTERS"
            
        # 3. All Zeros check
        if lei == "0" * 20:
            return "ALL_ZEROS_INVALID"
            
        # 4. Reserved Check Digits (00, 01)
        check_digits = lei[-2:]
        if check_digits in ["00", "01"]:
            return "INVALID_CHECK_DIGITS_RESERVED"
        
        # 5. MOD 97 Checksum
        # Letters A-Z -> 10-35
        numeric_str = ""
        for char in lei:
            if '0' <= char <= '9':
                numeric_str += char
            else:
                numeric_str += str(ord(char) - 55)
        
        try:
            val = int(numeric_str)
            if val % 97 == 1:
                return "OK"
            else:
                return "CHECKSUM_MISMATCH"
        except Exception:
            return "CHECKSUM_MISMATCH"

    def _validate_schme_nm_in_xml(self, xml_content: str, report: ValidationReport) -> None:
        """
        Step 4.19 — Scheme Name Validation (Strict Policy + Structural Rules + LEI Checksum)
        Enforces:
          1. CD Allowlist ("EXCEPT VALID EVERYTHING INVALID")
          2. Mutual Exclusivity (<Cd> vs <Prtry>)
          3. Presence check (One of them must be present)
          4. Missing SchmeNm check inside Othr
          5. LEI format & checksum validation
        """
        # Default fallback lists
        valid_codes = {"LEI": "", "TXID": "", "BANK": "", "CUST": "", "COID": "", "TXNR": "", "DUNS": "", "GIIN": ""}
        invalid_map = {}
        error_labels = {
            "invalid_scheme": "Invalid scheme code in <Cd>",
            "both_cd_and_prtry": "Mutually exclusive elements conflict",
            "missing_element": "<SchmeNm> must contain either <Cd> or <Prtry>",
            "empty_prtry": "<Prtry> value must not be empty",
            "missing_schmenm": "<SchmeNm> is required when <Othr> is present"
        }
        
        # 0. Path detection to differentiate Account vs Org identifiers
        acct_othr_lines = set()
        path_map = {} # line -> path
        try:
            parser = etree.XMLParser(recover=True, no_network=True, resolve_entities=False)
            tree_root = etree.fromstring(xml_content.encode('utf-8'), parser)
            for othr in tree_root.xpath("//*[local-name()='Othr']"):
                path = self._get_xpath_for_element(othr)
                ln = othr.sourceline
                if ln:
                    if "/Acct/" in path:
                        acct_othr_lines.add(ln)
                    path_map[ln] = path
        except:
            pass

        # Try to load from dynamic codelists
        if hasattr(self, 'codelists') and 'schme_nm' in self.codelists:
            cfg = self.codelists['schme_nm'].get('schmeNm_validation', {})
            vc = {item['code'].upper(): item.get('usage', '') for item in cfg.get('valid_cd_codes', [])}
            if vc: valid_codes = vc
            
            im = {item['code'].upper(): {"reason": item['reason'], "fix": item.get('fix')} 
                  for item in cfg.get('invalid_cd_codes', [])}
            if im: invalid_map = im
            
            labels = cfg.get('errors', {})
            for k, v in labels.items():
                if v: error_labels[k] = v

        # Stage 1: Find <Othr> blocks
        othr_patt = re.compile(r'<Othr[^>]*>([\s\S]*?)</Othr>', re.IGNORECASE)
        # Stage 2: Sub-patterns
        id_patt = re.compile(r'<Id[^>]*>\s*([^<]*?)\s*</Id>', re.IGNORECASE)
        schme_patt = re.compile(r'<SchmeNm[^>]*>([\s\S]*?)</SchmeNm>', re.IGNORECASE)
        cd_patt = re.compile(r'<Cd[^>]*>\s*([^<]*?)\s*</Cd>', re.IGNORECASE)
        prtry_patt = re.compile(r'<Prtry[^>]*>\s*([^<]*?)\s*</Prtry>', re.IGNORECASE)
        
        codes_str = ", ".join(valid_codes.keys())
        
        for m_othr in othr_patt.finditer(xml_content):
            inner_othr = m_othr.group(1)
            othr_pos = m_othr.start()
            try:
                othr_line = xml_content.count('\n', 0, othr_pos) + 1
            except:
                othr_line = "Unknown"

            # 1. Identification (<Id>)
            m_id = id_patt.search(inner_othr)
            id_val = m_id.group(1).strip() if m_id else ""
            
            # 2. Scheme Name (<SchmeNm>)
            m_schme = schme_patt.search(inner_othr)
            
            # Use detected path for clearer error messages
            curr_path = path_map.get(othr_line, "/Othr")
            
            # Implementation of rules from User Step 1992
            is_forbidden_path = any(f in curr_path for f in ["/DbtrAcct/", "/CdtrAcct/"])
            is_mandatory_path = any(m in curr_path for m in [
                "/Dbtr/Id/OrgId/", "/Dbtr/Id/PrvtId/",
                "/Cdtr/Id/OrgId/", "/Cdtr/Id/PrvtId/"
            ])

            if is_forbidden_path:
                # RULE: SchmeNm is NOT Allowed for Acct identifiers
                if m_schme:
                    report.add_issue(ValidationIssue(
                        "ERROR", 3, "SCHEME_NOT_ALLOWED", str(othr_line),
                        f"{curr_path} → {error_labels.get('not_supported', '<SchmeNm> is not allowed for this identifier type.')}",
                        "Remove the <SchmeNm> block. For accounts, provide only the <Id> within <Othr>."
                    ))
                    continue
                else:
                    # Valid: Account Othr with no SchmeNm
                    continue
            
            elif is_mandatory_path:
                # RULE: SchmeNm is Mandatory for Dbtr/Cdtr Org/Prvt identifiers
                if not m_schme:
                    report.add_issue(ValidationIssue(
                        "ERROR", 3, "SCHEME_MISSING", str(othr_line),
                        f"{curr_path} → {error_labels['missing_schmenm']}",
                        "Identify the ID type using <SchmeNm>. For example: <SchmeNm><Cd>LEI</Cd></SchmeNm>."
                    ))
                    continue
            else:
                # For any other Othr blocks not specified by the user, we skip strict existence check
                # but if SchmeNm IS present, we will continue to validate its contents (Cd vs Prtry)
                if not m_schme:
                    continue

            inner_schme = m_schme.group(1)
            # Recalculate schme_pos more accurately
            schme_pos = othr_pos + m_othr.group(0).find(m_schme.group(0))
            try:
                schme_line = xml_content.count('\n', 0, schme_pos) + 1
            except:
                schme_line = "Unknown"
            
            cds = list(cd_patt.finditer(inner_schme))
            ptys = list(prtry_patt.finditer(inner_schme))
            
            # RULE 1: Mutual Exclusivity
            if len(cds) > 0 and len(ptys) > 0:
                report.add_issue(ValidationIssue(
                    "ERROR", 3, "SCHEME_CONFLICT", str(schme_line),
                    f"{curr_path} → {error_labels['both_cd_and_prtry']}",
                    "You cannot provide both <Cd> and <Prtry> in the same <SchmeNm> block. Use either a standardized code OR a proprietary name."
                ))
                continue
            
            # RULE 2: Presence Check
            if len(cds) == 0 and len(ptys) == 0:
                report.add_issue(ValidationIssue(
                    "ERROR", 3, "SCHEME_MISSING_CHILD", str(schme_line),
                    f"{curr_path} → {error_labels['missing_element']}",
                    "The <SchmeNm> block must contain either <Cd> or <Prtry>."
                ))
                continue
            
            # RULE 3: Valid <Cd> Allowlist & Identifier Validation (LEI)
            for m_cd in cds:
                val = m_cd.group(1).strip()
                val_upper = val.upper()
                
                if not val:
                     report.add_issue(ValidationIssue(
                        "ERROR", 3, "SCHEME_EMPTY_CD", str(schme_line),
                        "Missing or empty identifier code in <Cd>.",
                        f"Please provide a valid code such as: {codes_str}."
                    ))
                     continue

                # Skip the allowlist check here to let global.json handle it with descriptive messages
                # Valid codes and invalid map are still used for context but we won't add ERROR issues here for simple allowlist failures
                if val_upper == "LEI" and id_val:
                    lei_status = self._check_lei(id_val)
                    if lei_status != "OK":
                        id_match_pos = othr_pos + m_othr.group(0).find(m_id.group(0))
                        try:
                            id_line = xml_content.count('\n', 0, id_match_pos) + 1
                        except:
                            id_line = "Unknown"
                        
                        error_map = {
                            "INVALID_LENGTH": ("LEI must be exactly 20 characters.", "Correct the LEI to full 20-character length."),
                            "INVALID_CHARACTERS": ("Special characters found — only A-Z and 0-9 allowed.", "Replace special characters with valid alphanumeric characters."),
                            "ALL_ZEROS_INVALID": ("All-zero LEI is not a valid GLEIF-registered identifier.", "Use a real GLEIF-registered LEI."),
                            "INVALID_CHECK_DIGITS_RESERVED": ("Check digits '00' or '01' are reserved and not allowed.", "Valid check digit range is 02–97 only."),
                            "CHECKSUM_MISMATCH": ("Invalid LEI checksum (MOD 97 remainder is not 1).", "Ensure the LEI follows ISO 7064 MOD 97-10 rules. The last two digits must be correct check digits.")
                        }
                        msg, fix = error_map.get(lei_status, ("Invalid LEI format.", "Check the LEI structure."))
                        
                        report.add_issue(ValidationIssue(
                            "ERROR", 3, f"LEI_{lei_status}", str(id_line),
                            f"{msg} ('{id_val}')",
                            fix
                        ))

            # RULE 4: <Prtry> not empty
            for m_pty in ptys:
                val = m_pty.group(1).strip()
                if not val:
                    report.add_issue(ValidationIssue(
                        "ERROR", 3, "EMPTY_PRTRY", str(schme_line),
                        f"{curr_path} → {error_labels['empty_prtry']}",
                        "The <Prtry> tag cannot be empty. Provide a proprietary scheme description or code."
                    ))

