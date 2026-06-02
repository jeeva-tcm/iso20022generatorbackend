import json
import re
import time
import os
from datetime import datetime
from typing import Dict, Any, List
from .models import ValidationIssue, ValidationReport

class Layer3Mixin:
    rules_path: str
    codelists: Dict[str, list]
    config: Dict[str, Any]
    supported_bics: List[str]
    def _normalize_message(self, xml_content: str) -> tuple[Dict[str, Any], Dict[str, int]]: ...

    def _load_generic_config(self):
        """Loads the new global algorithms and field library."""
        config = {"algorithms": {}, "fields": {}, "messages": {}}
        try:
            for name in ["algorithms", "fields", "message_definitions"]:
                path = os.path.join(self.rules_path, f"{name}.json")
                if os.path.exists(path):
                    with open(path, "r", encoding='utf-8-sig') as f:
                        data = json.load(f)
                        if name == "message_definitions":
                            config["messages"] = data.get("messages", {})
                        else:
                            config[name] = data.get(name, {})
        except Exception as e:
            print(f"Error loading generic config: {e}")
        return config

    def _load_all_rules(self, message_type: str) -> List[Dict[str, Any]]:
        """
        Loads global rules + family rules + message-specific rules.
        """
        rules = []
        
        # 1. Load Global
        global_file = os.path.join(self.rules_path, "global.json")
        if os.path.exists(global_file):
            try:
                with open(global_file, "r", encoding='utf-8-sig') as f:
                    rules.extend(json.load(f))
            except Exception as e: 
                print(f"Error loading global rules: {e}")

        # 2. Load CBPR Common (universal cross-message rules: BAH, BICFI exclusivity, AnyBIC exclusivity)
        cbpr_common_file = os.path.join(self.rules_path, "cbpr_common.json")
        if os.path.exists(cbpr_common_file) and not message_type.startswith("pain"):
            try:
                with open(cbpr_common_file, "r", encoding='utf-8-sig') as f:
                    rules.extend(json.load(f))
            except Exception as e:
                print(f"Error loading CBPR common rules: {e}")

        parts = message_type.split(".")

        # 3. Load Family Level (e.g., pacs.json)
        if len(parts) >= 1:
            family = parts[0]
            family_file = os.path.join(self.rules_path, f"{family}.json")
            if os.path.exists(family_file):
                try:
                    with open(family_file, "r", encoding='utf-8-sig') as f:
                        rules.extend(json.load(f))
                except Exception as e: 
                    print(f"Error loading family rules: {e}")

        # 4. Load Message Specific (e.g., pacs.008.json)
        # Try pacs.008 first, then also load full variant files such as
        # pacs.009.001.08_ADV.json when present.
        specific_name = ".".join(parts[:2]) if len(parts) >= 2 else message_type
        specific_file = os.path.join(self.rules_path, f"{specific_name}.json")
        
        # If not found, try full name just in case (e.g. pacs.008.001.08.json - rare but possible)
        if not os.path.exists(specific_file):
             specific_file = os.path.join(self.rules_path, f"{message_type}.json")

        if os.path.exists(specific_file):
            try:
                with open(specific_file, "r", encoding='utf-8-sig') as f:
                    rules.extend(json.load(f))
            except Exception as e: 
                print(f"Error loading specific rules: {e}")

        full_rule_names = [message_type]
        if message_type == "pacs.009.adv":
            full_rule_names.append("pacs.009.001.08_ADV")
        if message_type == "pacs.009.cov":
            full_rule_names.append("pacs.009.001.08_COV")

        for full_name in full_rule_names:
            full_specific_file = os.path.join(self.rules_path, f"{full_name}.json")
            if full_specific_file == specific_file or not os.path.exists(full_specific_file):
                continue
            try:
                with open(full_specific_file, "r", encoding='utf-8-sig') as f:
                    rules.extend(json.load(f))
            except Exception as e:
                print(f"Error loading full specific rules: {e}")
            
        return rules

    def _run_dynamic_layer(self, layer_id: int, rules: List[Dict[str, Any]], data: Dict[str, Any], line_map: Dict[str, int], report: ValidationReport):
        """
        Executes all rules assigned to a specific layer.
        """
        start = time.time()
        
        # Filter rules for the current layer
        layer_rules = [r for r in rules if r.get("layer") == layer_id]
        
        # Use cached codelists (Layer 3 always needs them)
        codelists = self.codelists

        for rule in layer_rules:
            if rule.get("type") == "bic":
                print(f"DEBUG LAYER3: Executing BIC rule. len(data)={len(data)}")
                issues_before = len(report.issues)
            self._execute_rule_logic(rule, data, line_map, codelists, report)
            if rule.get("type") == "bic":
                print(f"DEBUG LAYER3: BIC rule done. Issues added: {len(report.issues) - issues_before}")
        
        # Assessment for layer dashboard
        success = not any(
            isinstance(i, dict) and i.get('layer') == layer_id and i.get('severity') == "ERROR"
            for i in report.issues
        )
        report.layer_status[str(layer_id)] = {
            "status": "PASS" if success else "FAIL", 
            "time": round((time.time() - start) * 1000, 2)
        }

    def _run_generic_field_validation(self, message_type: str, data: Dict[str, Any], line_map: Dict[str, int], report: ValidationReport):
        """
        Executes validation based on the Generic Field Library and Global Algorithms.
        This acts as Layer 2.5 (Syntax + Semantic).
        """
        start = time.time()
        config = self._load_generic_config()
        algorithms = config.get("algorithms", {})
        fields_lib = config.get("fields", {})
        message_defs = config.get("messages", {})

        # 1. Identify which message definition to use
        # Normalize message_type (e.g. pacs.008.001.08 -> pacs.008)
        parts = message_type.split(".")
        short_type = ".".join(parts[:2]) if len(parts) >= 2 else message_type
        
        # Try finding the exact or short version (pacs.008.cov vs pacs.008)
        msg_def = message_defs.get(message_type)
        if not msg_def:
            msg_def = message_defs.get(short_type)
            
        if not msg_def:
            # If no definition for this message type, skip this validation layer
            return

        # 2. Iterate through sections of the message (AppHdr, GrpHdr, etc.)
        for section, expected_fields in msg_def.items():
            # Find elements in data that belong to this section
            # We look for keys starting with [section] or ending with [section].[field]
            for field_name in expected_fields:
                field_cfg = fields_lib.get(field_name)
                if not field_cfg:
                    continue

                # Find all occurrences of this field in the current section
                # Use regex to find keys like Document.CdtTrfTxInf.InstrId or AppHdr.CreDtTm
                pattern = rf"(^|\.){section}(\.\w+)*\.{field_name}$"
                matching_keys = [k for k in data.keys() if re.search(pattern, k)]
                
                # Removed redundant mandatory check (XSD Layer 2 handles existence)
                # This fixes the issue where optional fields were being reported as missing
                
                for key in matching_keys:
                    # Apply regex ONLY for mandatory fields as requested by USER
                    # Generic Layer focuses on core identifiers defined as min: 1 in fields.json
                    if field_cfg.get("min", 0) <= 0:
                        continue
                        
                    value = data[key]
                    algo_name = field_cfg.get("regex")
                    algo_cfg = algorithms.get(algo_name, algo_name)
                    
                    # Extract pattern and message if it's a dictionary configuration
                    if isinstance(algo_cfg, dict):
                        regex_pattern = str(algo_cfg.get("pattern", ""))
                        error_msg = algo_cfg.get("message", f"Field '{field_name}' has invalid format: '{value}'.")
                    else:
                        regex_pattern = str(algo_cfg) if algo_cfg else ""
                        error_msg = f"Field '{field_name}' has invalid format: '{value}'."

                    # 1. Regex Validation
                    # Robust string conversion for regex matching (especially for floats)
                    if isinstance(value, (float, int)):
                        val_str = "{:.15f}".format(float(value)).rstrip('0').rstrip('.')
                    else:
                        val_str = str(value)
                        
                    if regex_pattern and not re.match(regex_pattern, val_str):
                        report.add_issue(ValidationIssue(
                            "ERROR", 3, "INVALID_FIELD_FORMAT", str(line_map.get(key, "/")),
                            error_msg,
                            f"Value must match pattern for '{algo_name}': {regex_pattern}"
                        ))

                    # 2. Attributes Validation (e.g. Ccy)
                    attrs_cfg = field_cfg.get("attributes", {})
                    for attr_name, attr_algo in attrs_cfg.items():
                        attr_key = f"{key}@{attr_name}"
                        attr_val = data.get(attr_key)
                        attr_algo_cfg = algorithms.get(attr_algo, attr_algo)
                        
                        if isinstance(attr_algo_cfg, dict):
                            attr_regex = str(attr_algo_cfg.get("pattern", ""))
                            attr_error = attr_algo_cfg.get("message", f"Attribute '{attr_name}' for '{field_name}' has invalid format: '{attr_val}'.")
                        else:
                            attr_regex = str(attr_algo_cfg) if attr_algo_cfg else ""
                            attr_error = f"Attribute '{attr_name}' for '{field_name}' has invalid format: '{attr_val}'."
                        
                        if not attr_val:
                             report.add_issue(ValidationIssue(
                                "ERROR", 3, "MISSING_ATTRIBUTE", str(line_map.get(key, "/")),
                                f"Mandatory attribute '{attr_name}' is missing for field '{field_name}'.",
                                f"Add {attr_name}=\"...\" to the <{field_name}> tag."
                            ))
                        elif attr_regex and not re.match(attr_regex, str(attr_val)):
                             report.add_issue(ValidationIssue(
                                "ERROR", 3, "INVALID_ATTRIBUTE_FORMAT", str(line_map.get(attr_key, "/")),
                                attr_error,
                                f"Value must match pattern for '{attr_algo}': {attr_regex}"
                            ))

        # No longer assigning to report.layer_status["Global Algorithms"] as requested.
        pass

    def _execute_rule_logic(self, rule: Dict[str, Any], data: Dict[str, Any], line_map: Dict[str, int], codelists: Dict[str, list], report: ValidationReport):
        """
        Advanced Dynamic Rule Dispatcher.
        """
        # Defensive check: rule must be a dictionary
        if not isinstance(rule, dict):
             return

        rule_type = rule.get("type", "expression")
        selector = rule.get("selector")
        layer = rule.get("layer", 3)
        if layer in [4, 5]:
            layer = 3
        rule_id = rule.get("rule_id", "DYNAMIC_RULE")
        severity = rule.get("severity", "ERROR")
        desc = rule.get("description", "")

        def _get_line(key):
             if not isinstance(line_map, dict) or not key:
                 return "/"
             
             # Clean up attribute suffix if present (e.g. key@Ccy -> key)
             key = key.split('@')[0]
             
             # Strip index notation to try matching clean paths
             def clean_path(p):
                 return re.sub(r'\[\d+\]', '', p)
             
             # Walk up the path parts
             parts = key.split('.')
             while parts:
                 current_path = '.'.join(parts)
                 # Try exact match first
                 l = line_map.get(current_path)
                 if l:
                     return str(l)
                 # Try clean match (without index)
                 l = line_map.get(clean_path(current_path))
                 if l:
                     return str(l)
                 # Pop the last element to climb up the parent tree
                 parts.pop()
             return "/"

        def _get_line_num(key):
             line_str = _get_line(key)
             return int(line_str) if line_str.isdigit() else None

        def _find_fallback_line(rule):
            # 1. Try to extract paths from expression, condition, or rule definition
            for field in ["expression", "condition", "selector"]:
                val = rule.get(field)
                if val and isinstance(val, str):
                    paths = re.findall(r'\b(?:Document|AppHdr)(?:\.[a-zA-Z0-9_\[\]]+)+', val)
                    for path in paths:
                        line_str = _get_line(path)
                        if line_str != "/":
                            return line_str
            
            # 2. Try to find any transaction-level nodes in data (e.g. CdtTrfTxInf, DrctDbtTxInf, etc.)
            tx_tags = ['CdtTrfTxInf', 'DrctDbtTxInf', 'TxInfAndSts', 'PmtInf', 'GrpHdr', 'AppHdr']
            for tag in tx_tags:
                matching_keys = [k for k in data.keys() if f".{tag}" in k or k == f"Document.{tag}" or k == f"AppHdr.{tag}"]
                if matching_keys:
                    line_str = _get_line(matching_keys[0])
                    if line_str != "/":
                        return line_str
            
            # 3. Fallback to Document or AppHdr root
            for root_key in ["Document", "AppHdr"]:
                if root_key in data:
                    line_str = _get_line(root_key)
                    if line_str != "/":
                        return line_str

            return "/"

        # 1. Selector Based Rules (Multiple fields)
        if selector:
            condition = rule.get("condition")
            if condition:
                if not self._evaluate_expression(condition, data, line_map, codelists=codelists, report=report):
                    return

            regex = re.compile(selector)
            matching_keys = [k for k in data.keys() if regex.match(k)]
            
            for key in matching_keys:
                value = data[key]
                if rule_type == "codelist":
                    list_name = rule.get("list_name", "").lower()
                    if list_name in codelists:
                        cl_data = codelists[list_name]
                        # Handle both list formats: {"codes": [...]} and [...]
                        if isinstance(cl_data, dict):
                            valid_codes = cl_data.get("codes", [])
                        elif isinstance(cl_data, list):
                            valid_codes = cl_data
                        else:
                            valid_codes = []
                            
                        if value not in valid_codes:
                            # Build context-aware message and fix suggestion
                            field_name = key.split('.')[-1]
                            if list_name == "country":
                                msg = f"Invalid country code '{value}' in field '{field_name}'."
                                fix = (f"'{value}' is not a valid ISO 3166-1 Alpha-2 country code. "
                                       f"Use a 2-letter code like 'US', 'GB', 'DE', 'FR', 'IN', 'SG', etc.")
                            elif list_name == "charge_bearer":
                                msg = f"Invalid Charge Bearer code '{value}'."
                                fix = (f"'{value}' is not allowed. Valid Charge Bearer codes are: "
                                       f"SLEV (Shared, as agreed), SHAR (Shared), CRED (Borne by Creditor), DEBT (Borne by Debtor).")
                            elif list_name == "purpose_code":
                                msg = f"Invalid Purpose Code '{value}'."
                                fix = (f"'{value}' is not a valid ISO 20022 Purpose Code. "
                                       f"Use a standard code such as SALA, RENT, SUPP, CORT, PENS, BONU, TRAD, LOAN, TAXS, etc.")
                            else:
                                msg = f"Field '{field_name}' contains invalid code '{value}'."
                                fix = f"Value '{value}' is not a valid code for this field. Please check the ISO 20022 standard for permitted values."
                            
                            report.add_issue(ValidationIssue(severity, layer, rule_id, key, msg, fix, line=_get_line_num(key)))
                
                elif rule_type == "bic":
                    if not re.match(r'^[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?$', str(value)):
                        report.add_issue(ValidationIssue(severity, layer, rule_id, key, f"{desc} Invalid BIC structure: '{value}'.", "BIC must be 8 or 11 characters: 4-char bank code + 2-letter country + 2-char location + optional 3-char branch (e.g., BNKGB2LXXX).", line=_get_line_num(key)))
                    elif self.supported_bics and value.upper() not in self.supported_bics:
                        report.add_issue(ValidationIssue("WARNING", layer, "BIC_NOT_FOUND", key, f"BIC '{value}' not found in official directory.", "Verify if the BIC is correct or recently decommissioned.", line=_get_line_num(key)))
                
                elif rule_type == "currency_amount":
                    ccy_path = key + "@Ccy"
                    if not ccy_path in data:
                        ccy_path = key.rsplit('.', 1)[0] + ".Ccy"
                    
                    ccy = data.get(ccy_path)
                    allowed_decimals = None
                    
                    if ccy:
                        if "currency" in codelists:
                            curr_list = codelists["currency"]
                            if isinstance(curr_list, dict):
                                currencies = curr_list.get("currencies", {})
                                if ccy in currencies:
                                    allowed_decimals = currencies.get(ccy)
                                else:
                                    report.add_issue(ValidationIssue(
                                        severity, layer, "INVALID_CURRENCY_CODE", ccy_path,
                                        f"Unrecognised Currency Code '{ccy}'.",
                                        f"The code '{ccy}' is not a valid ISO 4217 currency. Use standard codes like USD, EUR, GBP, JPY, etc.",
                                        line=_get_line_num(ccy_path)
                                    ))
                                    continue
                        
                        if allowed_decimals is not None:
                            # Robust decimal counting
                            if isinstance(value, float):
                                # Convert float to string with reasonable precision and strip
                                val_str = "{:.15f}".format(value).rstrip('0').rstrip('.')
                            else:
                                val_str = str(value)

                            actual_decimals = len(val_str.split('.')[1]) if '.' in val_str else 0
                            
                            if actual_decimals > allowed_decimals:
                                report.add_issue(ValidationIssue(
                                    severity, layer, "INVALID_DECIMAL_PRECISION", key,
                                    f"Incorrect decimal precision for {ccy} amount '{val_str}'. {ccy} allows max {allowed_decimals} decimal place(s), but {actual_decimals} were provided.",
                                    f"Adjust the fractional part: {ccy} supports {allowed_decimals} decimal place(s) (e.g., {'10.00' if allowed_decimals == 2 else '10' if allowed_decimals == 0 else '10.000'}).",
                                    line=_get_line_num(key)
                                ))

                elif rule_type == "regex":
                    pattern_cfg = rule.get("pattern", ".*")
                    if isinstance(pattern_cfg, str) and pattern_cfg in codelists.get("algorithms", {}):
                        regex_to_use = pattern_cfg
                    elif isinstance(pattern_cfg, dict):
                        regex_to_use = pattern_cfg.get("pattern", ".*")
                    else:
                        regex_to_use = pattern_cfg

                    if not re.match(str(regex_to_use), str(value)):
                        report.add_issue(ValidationIssue(severity, layer, rule_id, key, f"{desc} Value '{value}' is invalid format.", line=_get_line_num(key)))
                
                elif rule_type == "expression":
                    rule_meta = {"severity": severity, "layer": layer, "rule_id": rule_id, "desc": desc}
                    error_msg = rule.get("errorMessage", desc)
                    fix_suggestion = rule.get("fix", "")
                    
                    if not self._evaluate_expression(rule.get("expression", "True"), data, line_map, value, key, rule_meta, codelists, report):
                        report.add_issue(ValidationIssue(severity, layer, rule_id, key, error_msg, fix_suggestion, line=_get_line_num(key)))

        # 2. Logic Based Rules
        else:
            condition = rule.get("condition", "True")
            if not self._evaluate_expression(condition, data, line_map, codelists=codelists, report=report):
                return

            error_msg = rule.get("errorMessage", desc)
            fix_suggestion = rule.get("fix", "")

            for field in rule.get("mandatory_fields", []):
                if not self._evaluate_expression(f"exists({field})", data, line_map, codelists=codelists, report=report):
                    report.add_issue(ValidationIssue(severity, layer, rule_id, field, error_msg, fix_suggestion, line=_get_line_num(field)))

            expr = rule.get("expression")
            if expr:
                rule_meta = {"severity": severity, "layer": layer, "rule_id": rule_id, "desc": desc}
                if not self._evaluate_expression(expr, data, line_map, KEY="", rule_meta=rule_meta, codelists=codelists, report=report):
                     fallback_line_str = _find_fallback_line(rule)
                     fallback_line = int(fallback_line_str) if fallback_line_str.isdigit() else None
                     
                     # Try to find a path to display
                     extracted_path = "/"
                     for field in ["expression", "condition", "selector"]:
                         val = rule.get(field)
                         if val and isinstance(val, str):
                             paths = re.findall(r'\b(?:Document|AppHdr)(?:\.[a-zA-Z0-9_\[\]]+)+', val)
                             if paths:
                                 extracted_path = paths[0]
                                 break
                     
                     if extracted_path == "/":
                         # Fallback path extraction: look for tags in the rule's expression/condition/selector
                         for field in ["expression", "condition", "selector"]:
                             val = rule.get(field)
                             if val and isinstance(val, str):
                                 # Find all words that look like tags (e.g., camelCase or uppercase start)
                                 words = re.findall(r'\b[A-Z][a-zA-Z0-9_]+\b', val)
                                 found_path = False
                                 for word in words:
                                     # Avoid matching common Python functions or keywords
                                     if word in ["True", "False", "None", "DATA", "any", "all", "re", "match", "search"]:
                                         continue
                                     # Look for a key in data that contains this word
                                     for key in data.keys():
                                         if f".{word}" in key or key == word or key.startswith(word + "."):
                                             # Truncate key up to this word
                                             parts = key.split('.')
                                             for idx, part in enumerate(parts):
                                                 # Strip index for matching
                                                 part_clean = part.split('[')[0]
                                                 if part_clean == word:
                                                    extracted_path = '.'.join(parts[:idx+1])
                                                    extracted_path = re.sub(r'\[\d+\]', '', extracted_path)
                                                    found_path = True
                                                    break
                                             if found_path:
                                                 break
                                     if found_path:
                                         break
                             if extracted_path != "/":
                                 break
                     
                     report.add_issue(ValidationIssue(severity, layer, rule_id, extracted_path, error_msg, fix_suggestion, line=fallback_line))

    def _evaluate_expression(self, expr: str, data: Dict[str, Any], line_map: Dict[str, int] = None, VALUE: Any = None, KEY: str = "", rule_meta: Dict[str, Any] = None, codelists: Dict[str, Any] = None, report: ValidationReport = None) -> bool:
        """
        Evaluates dynamic expressions against the canonical data map.
        Supports indexed paths and global VALUE keyword.
        """
        # Ensure codelists is never None
        if codelists is None:
            codelists = {}
        def exists_sub(match):
            path_expr = match.group(1).strip().strip("\"'")
            # Only statically replace if it's a literal path string (no variables)
            if re.match(r'^[A-Za-z0-9._\[\]]+$', path_expr):
                parts = path_expr.split('.')
                escaped_parts = [p.replace("[", "\\[").replace("]", "\\]") for p in parts]
                regex_path = r'(\[\d+\])?\.'.join(escaped_parts)
                return "True" if any(re.match(f"^{regex_path}(\\[\\d+\\])?(\\..*)?$", k) for k in data.keys()) else "False"
            # Return original string to be handled by the 'exists' lambda in eval()
            return match.group(0)

        def count_paths(path):
            path = path.strip("\"'")
            escaped = [p.replace("[", "\\[").replace("]", "\\]") for p in path.split('.')]
            regex_path = r'(\[\d+\])?\.'.join(escaped)
            # Count only exact occurrences of the element itself, not its descendants.
            # A key matches if it IS the element (with optional index) but NOT a child path.
            pattern = re.compile(f"^{regex_path}(\\[\\d+\\])?$")
            return len([k for k in data.keys() if pattern.match(k)])

        def exists_any_paths(base_path, elements):
            base_path = base_path.strip("\"'")
            for el in elements:
                full_path = f"{base_path}.{el}"
                escaped = [p.replace("[", "\\[").replace("]", "\\]") for p in full_path.split('.')]
                regex_path = r'(\[\d+\])?\.'.join(escaped)
                if any(re.match(f"^{regex_path}(\\[\\d+\\])?(\\..*)?$", k) for k in data.keys()):
                    return True
            return False

        def all_max_length_paths(path, max_len):
            path = path.strip("\"'")
            escaped = [p.replace("[", "\\[").replace("]", "\\]") for p in path.split('.')]
            regex_path = r'(\[\d+\])?\.'.join(escaped)
            pattern = re.compile(f"^{regex_path}(\\[\\d+\\])?(\\..*)?$")
            matching_keys = [k for k in data.keys() if pattern.match(k)]
            return all(len(str(data[k])) <= max_len for k in matching_keys)

        def _gl(key):
             if not line_map: return "/"
             l = line_map.get(key)
             if not l:
                  clean = re.sub(r'\[\d+\]', '', key)
                  l = line_map.get(clean)
             return str(l) if l else "/"

        def check_address(addr_path, data, report, severity, layer, rule_id, desc):
            block_content = {k: v for k, v in data.items() if k.startswith(f"{addr_path}.")}
            if not block_content: return True # Empty block is fine

            has_adr_line = any(k.startswith(f"{addr_path}.AdrLine") for k in data.keys())
            has_town = any(k.startswith(f"{addr_path}.TwnNm") for k in data.keys())
            has_ctry = any(k.startswith(f"{addr_path}.Ctry") for k in data.keys())

            # Coexistence rule: If AdrLine is absent, TwnNm and Ctry must be present
            if not has_adr_line:
                issues_found = []
                if not has_town: issues_found.append(".TwnNm")
                if not has_ctry: issues_found.append(".Ctry")

                if issues_found:
                    for suffix in issues_found:
                        clean_field_path = (addr_path + suffix).split('.')[-4:]
                        field_path = ".".join(clean_field_path)
                        report.add_issue(ValidationIssue(
                            "ERROR", 3, "PACS009_POSTAL_ADDRESS", field_path,
                            "If Postal Address is used, and if Address Line is absent, then Town Name and Country must be present.",
                            "Add either <AdrLine> or both <TownNm> and <Ctry> tags to comply with ISO 20022 / CBPR+ coexistence rules."
                        ))
                    return False
            return True
        def check_bic_match(header_role, doc_role):
            # 1. Find Header BIC
            h_key = f"AppHdr.{header_role}.FIId.FinInstnId.BICFI"
            h_val = data.get(h_key)
            
            # 2. Find Document BIC (Search anywhere in message)
            d_val = None
            d_key = None
            suffix = f".{doc_role}.FinInstnId.BICFI"
            
            for k, v in data.items():
                if k.endswith(suffix):
                    d_val = v
                    d_key = k
                    break
            
            # If either is missing, we can't compare
            if not h_val or not d_val:
                return True 
            
            if h_val != d_val:
                 # Add specific issue with correct line number
                 line = _gl(d_key) if d_key else _gl(h_key)
                 msg = f"{rule_meta.get('desc')} (Header: '{h_val}' vs Doc: '{d_val}')"
                 
                 report.add_issue(ValidationIssue(
                     rule_meta.get("severity", "ERROR"), 
                     rule_meta.get("layer", 3), 
                     rule_meta.get("rule_id", "MX_MATCH"), 
                     line, 
                     msg,
                     f"Update {doc_role} to match the Header BIC or vice versa."
                 ))
                 return True # Suppress generic error

            return True

        def check_purpose_limit(purp_key, val, data, report, rule_meta):
            """
            Consolidated Business Validation Decision Tree (Layer 3)
            Step 1: Purpose -> Step 2: Amount Values -> Step 3: Decimal -> Step 4: Limit -> Step 5: High Value
            """
            purpose = str(val).upper()
            
            # --- Step 1: Validate Purpose Code ---
            purpose_data = codelists.get("purpose_code", {})
            supported_purposes = purpose_data.get("codes", []) if isinstance(purpose_data, dict) else []
            
            if purpose not in supported_purposes:
                report.add_issue(ValidationIssue("ERROR", 3, "INVALID_PURPOSE_CODE", _gl(purp_key), 
                    f"Invalid purpose code '{purpose}'. This code is not a recognised ISO 20022 ExternalPurpose1Code value.", 
                    f"Replace '{purpose}' with a valid ISO 20022 purpose code (e.g., SALA, CORT, PENS, BONU, TRAD, LOAN, RENT, SUPP, TAXS, etc.)."))
                return True # Suppress generic error by returning True after adding specific error
            # Discover Amount Key (climb up from Purp)
            amt_key = None
            current_path = purp_key
            while '.' in current_path:
                current_path = current_path.rsplit('.', 1)[0]
                # Try common amount tags at this level
                for tag in ["IntrBkSttlmAmt", "InstdAmt", "Amt.InstdAmt", "EqvtAmt"]:
                    candidate = f"{current_path}.{tag}"
                    if candidate in data:
                        amt_key = candidate
                        break
                if amt_key: break
            
            if not amt_key:
                return True

            amount_val = data.get(amt_key)
            if amount_val is None: return True
            
            try:
                amount = float(amount_val)
            except:
                return True
                
            ccy_key = f"{amt_key}@Ccy"
            currency = data.get(ccy_key)
            if not currency:
                return True

            # --- Step 2: Validate Amount > 0 ---
            if amount <= 0:
                report.add_issue(ValidationIssue("ERROR", 3, "INVALID_AMOUNT", _gl(amt_key), 
                    f"The payment amount {amount} must be greater than zero.", "Update to a positive value."))
                return True

            # --- Step 3: Validate Decimal Precision ---
            curr_data = codelists.get("currency", {})
            allowed_decimals = curr_data.get("currencies", {}).get(currency)
            if allowed_decimals is not None:
                raw_str = str(amount_val)
                actual_decimals = len(raw_str.split('.')[1]) if '.' in raw_str else 0
                if actual_decimals > allowed_decimals:
                    report.add_issue(ValidationIssue("ERROR", 3, "INVALID_DECIMAL_PRECISION", _gl(amt_key), 
                        f"Currency {currency} does not support {actual_decimals} decimals (Max: {allowed_decimals}). Found '{raw_str}'.", 
                        f"Adjust the value to match {currency} minor units."))
                    return True

            # --- Step 4: Validate Purpose + Currency Limit ---
            limits = self.config.get("validation_rules", {}).get("purpose_amount_limits", {})
            purpose_limits = limits.get(purpose, {})
            limit = purpose_limits.get(currency)
            
            if limit is not None and amount > limit:
                report.add_issue(ValidationIssue("ERROR", 3, "AMOUNT_EXCEEDS_PURPOSE_LIMIT", _gl(amt_key), 
                    f"The {purpose} payout of {amount:,.2f} {currency} exceeds the business cap of {limit:,.2f}.", 
                    "Ensure payment amount is within the approved purpose limits."))
                return True

            # --- Step 5: Validate High-Value Threshold ---
            hv_thresholds = self.config.get("validation_rules", {}).get("high_value_thresholds", {})
            hv_limit = hv_thresholds.get(currency)
            if hv_limit is not None and amount > hv_limit:
                 report.add_issue(ValidationIssue("WARNING", 3, "HIGH_VALUE_TRANSACTION", _gl(amt_key), 
                    f"Notice: This {currency} {amount:,.2f} payment is flagged as HIGH_VALUE_TRANSACTION.", 
                    "Risk monitoring is active for this high-value transfer."))

            return True

        def robust_exists(path):
            path = str(path).strip("\"'")
            escaped = [p.replace("[", "\\[").replace("]", "\\]") for p in path.split('.')]
            regex_path = r'(\[\d+\])?\.'.join(escaped)
            return any(re.match(f"^{regex_path}(\\[\\d+\\])?(\\..*)?$", k) for k in data.keys())

        def robust_exists_sub(match):
            path_expr = match.group(1).strip().strip("\"'")
            if re.match(r'^[A-Za-z0-9._\[\]]+$', path_expr):
                parts = path_expr.split('.')
                escaped_parts = [p.replace("[", "\\[").replace("]", "\\]") for p in parts]
                regex_path = r'(\[\d+\])?\.'.join(escaped_parts)
                return "True" if any(re.match(f"^{regex_path}(\\[\\d+\\])?(\\..*)?$", k) for k in data.keys()) else "False"
            return match.group(0)

        try:
            # Pre-evaluate robust_exists('literal.path') first so the variable
            # substitution loop cannot corrupt the path string inside the quotes.
            temp_expr = re.sub(r'robust_exists\(([^)]+)\)', robust_exists_sub, expr)
            temp_expr = re.sub(r'(?<!robust_)exists\(([^)]+)\)', exists_sub, temp_expr)
            
            mandate_date_str = self.config.get("validation_rules", {}).get("cbpr_plus_mandate_date", "2026-11-01T00:00:00")
            try:
                mandate_date = datetime.fromisoformat(mandate_date_str)
            except:
                mandate_date = datetime(2026, 11, 1)

            ctx = {
                "float": float, "int": int, "str": str, "len": len, "datetime": datetime,
                "True": True, "False": False, "None": None, "any": any, "all": all,
                "VALUE": VALUE, "KEY": KEY, "DATA": data,
                "check_address": lambda p: check_address(p, data, report, 
                                                        rule_meta.get("severity", "ERROR"), 
                                                        rule_meta.get("layer", 3), 
                                                        rule_meta.get("rule_id", "E001"), 
                                                        rule_meta.get("desc", "")) if rule_meta else True,
                "check_bic_match": check_bic_match,
                "check_purpose_limit": lambda k, v: check_purpose_limit(k, v, data, report, rule_meta),
                "check_iban_currency": lambda k, v: self._check_iban_currency(k, v, data, report, _gl, codelists),
                "is_after_2026": datetime.now() > mandate_date,
                "exists": robust_exists,
                "robust_exists": robust_exists,
                "count": count_paths,
                "exists_any": exists_any_paths,
                "all_max_length": all_max_length_paths,
                "check_msg_uetr_duplicate": lambda m, u: self._check_msg_uetr_duplicate(m, u, report),
                "re": re,
                "MESSAGE_TYPE": report.message_type if report else "Unknown"
            }
            
            reserved = set(["VALUE", "KEY", "DATA", "True", "False", "None", "exists", "count", "exists_any", "all_max_length", "check_address", "check_bic_match", "datetime", "len", "float", "int", "str", "re"])
            for key in sorted(data.keys(), key=len, reverse=True):
                pattern = r'(?<![\'\"])\b' + re.escape(key) + r'\b(?![\'\"])'
                if re.search(pattern, temp_expr) and key not in reserved:
                    val = data[key]
                    # Only substitute primitive types — dict/list values are NOT
                    # safe to inline into expressions (causes unhashable type errors)
                    if isinstance(val, str):
                        escaped_val = val.replace("'", "\\'")
                        val_str = f"'{escaped_val}'"
                    elif isinstance(val, bool):
                        val_str = "True" if val else "False"
                    elif isinstance(val, (int, float)):
                        val_str = str(val)
                    else:
                        # Skip substitution for dict/list — accessible via DATA[key]
                        continue
                    
                    # Use lambda to avoid backslash issues in re.sub
                    temp_expr = re.sub(pattern, lambda m: val_str, temp_expr)
            
            return eval(temp_expr, ctx, ctx)
        except Exception as e:
            print(f"DEBUG _evaluate_expression error for expr '{expr[:80]}': {e}")
            return False

    def _check_iban_currency(self, iban_key, iban_val, data, report, _gl, codelists):
        """
        Business Rule: Validate if the transaction currency matches the local currency 
        of the country where the Debtor's IBAN is registered.
        """
        # Rule is configurable via flag
        if not self.config.get("validation_rules", {}).get("enable_iban_currency_check", True):
            return True

        # Extract currency - searching relative or anywhere
        # User example: <InstdAmt Ccy="XXX">
        currency = None
        ccy_key = None
        
        # 1. Search for currency in common locations (InstdAmt, IntrBkSttlmAmt)
        # We prioritize tags in the same transaction block if possible
        tx_path = iban_key.rsplit('.DbtrAcct', 1)[0] if '.DbtrAcct' in iban_key else None
        
        if tx_path:
             for tag in ["InstdAmt", "IntrBkSttlmAmt", "Amt.InstdAmt"]:
                  k = f"{tx_path}.{tag}@Ccy"
                  if k in data:
                       currency = data[k]
                       ccy_key = k
                       break
        
        # Fallback: Search anywhere
        if not currency:
            for k, v in data.items():
                if k.endswith("@Ccy") and ("InstdAmt" in k or "IntrBkSttlmAmt" in k):
                    currency = v
                    ccy_key = k
                    break
        
        if not currency:
            return True # Cannot validate if currency attribute is missing from standard tags

        # IBAN basic validation (Min 15 characters as per requirement)
        if not iban_val or not isinstance(iban_val, str) or len(iban_val) < 15:
            line_str = _gl(iban_key)
            line_num = int(line_str) if line_str.isdigit() else None
            report.add_issue(ValidationIssue("ERROR", 3, "INVALID_IBAN", iban_key, "Invalid or Missing Debtor IBAN", "Ensure the IBAN is at least 15 characters long and follows the correct structure.", line=line_num))
            return True

        country_code = iban_val[:2].upper()
        if not country_code.isalpha():
             line_str = _gl(iban_key)
             line_num = int(line_str) if line_str.isdigit() else None
             report.add_issue(ValidationIssue("ERROR", 3, "INVALID_IBAN_CTRY", iban_key, "Invalid or Missing Debtor IBAN", "The first two characters of the IBAN must be a valid country code.", line=line_num))
             return True

        # Map country to currency
        iban_map = codelists.get("iban_currency_map", {})
        expected_currency = iban_map.get(country_code)
        
        if not expected_currency:
            line_str = _gl(iban_key)
            line_num = int(line_str) if line_str.isdigit() else None
            report.add_issue(ValidationIssue("ERROR", 3, "UNSUPPORTED_IBAN_CTRY", iban_key, "Unsupported IBAN Country Code", f"The country code '{country_code}' extracted from the IBAN is not supported in the currency mapping.", line=line_num))
            return True

        # Currency must follow ISO 4217 format (3 uppercase letters) and be case-sensitive
        if currency != expected_currency:
            curr_path = ccy_key or iban_key
            line_str = _gl(curr_path)
            line_num = int(line_str) if line_str.isdigit() else None
            report.add_issue(ValidationIssue(
                "ERROR", 3, "CURR_IBAN_MISMATCH", curr_path,
                f"Currency {currency} does not match expected currency {expected_currency} for IBAN country {country_code}",
                f"Update the transaction currency to {expected_currency} for the account based in {country_code}.",
                line=line_num
            ))
            return True # Suppress generic error

        return True

    def _check_msg_uetr_duplicate(self, msg_id, uetr, report):
        """
        Business Rule: Verify if the MsgId + UETR combination has been seen before.
        """
        if not hasattr(self, 'history_service') or not self.history_service:
            return True # Cannot check if service is missing

        # Query history for MsgId and UETR
        # This assumes history_service has a method to check duplicates
        # If not, we might need to implement it.
        try:
            is_dupe = self.history_service.check_duplicate_msg_uetr(msg_id, uetr)
            return not is_dupe
        except Exception as e:
            print(f"DEBUG: Error checking for duplicate MsgId/UETR: {e}")
            return True # Fail open to avoid blocking if DB has issues

    def validate_entity_mismatch(self, xml_content: str, report: ValidationReport):
        """
        Special early check for Entity Mismatch (L3-BIZ-PARTY-NAME-ENTITY-MATCH).
        Used to bring this rule forward to the very start of Step 5.
        """
        try:
            # We need normalized data to run these rules efficiently
            data, line_map = self._normalize_message(xml_content)
            all_rules = self._load_all_rules(report.message_type)
            # Filter specifically for these matching rules and scheme codes
            priority_rules = [
                r for r in all_rules 
                if r.get("rule_id") in [
                    "L3-BIZ-PARTY-NAME-ENTITY-MATCH-ORG", 
                    "L3-BIZ-PARTY-NAME-ENTITY-MATCH-PRVT",
                    "L3_ORG_SCHEME_VALIDATION",
                    "L3_PRVT_SCHEME_VALIDATION"
                ]
            ]
            
            for rule in priority_rules:
                self._execute_rule_logic(rule, data, line_map, self.codelists, report)
        except Exception as e:
            print(f"DEBUG: Early Entity mismatch check failed: {e}")

