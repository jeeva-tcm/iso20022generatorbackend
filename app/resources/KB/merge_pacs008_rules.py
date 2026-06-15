#!/usr/bin/env python3
"""
Merge CBPRPlus SR2025 rules.json into the existing pacs.008 validation KB.

Actions performed:
  1. Enrich cbpr_plus_formal_rules with formal_definition, referenced_paths, kind
  2. Add new CBPR+ rules not yet in the KB (with auto-generated fix suggestions)
  3. Add format_constraints_ref to every matching existing tag
  4. Add new tag entries for elements missing from the KB
  5. Update metadata (last_updated, merge_source, reference_documents)
"""

import json
import re
import shutil
from pathlib import Path
from datetime import date

KB_PATH = Path(__file__).parent / "pacs008_cbprplus_sr2025_validation_kb.json"
RULES_PATH = Path(__file__).parent / (
    "CBPRPlus_SR2025_(Combined)_CBPRPlus-pacs_008_001_08"
    "_FIToFICustomerCreditTransfer_20260525_0455.rules.json"
)
BACKUP_PATH = KB_PATH.parent / "pacs008_cbprplus_sr2025_validation_kb.backup.json"

# Depth limits from section root when adding new tags
MAX_DEPTH_CDTTRTXINF = 2   # e.g. CdtTrfTxInf/UltmtDbtr (1) and CdtTrfTxInf/PmtId/InstrId (2)
MAX_DEPTH_GRPHDR = 2        # e.g. GrpHdr/TtlIntrBkSttlmAmt (1) and GrpHdr/SttlmInf/ClrSys (2)

CDTTRTXINF_PREFIX = "Document/FIToFICstmrCdtTrf/CdtTrfTxInf"
GRPHDR_PREFIX = "Document/FIToFICstmrCdtTrf/GrpHdr"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def norm_name(name: str) -> str:
    """Strip FormalRule/TextualRule suffix, lowercase, remove underscores for fuzzy matching."""
    s = re.sub(r"_(FormalRule|TextualRule|Rule)$", "", name, flags=re.I)
    return s.lower().replace("_", "")


def clean_path_tail(path_ref: str) -> str:
    """Extract the last path component from a referenced_path string like '[A/B/C]'."""
    return path_ref.strip("[]").split("/")[-1]


def generate_fixes(rule: dict) -> list:
    """
    Generate human-readable fix suggestions from a business rule.
    Parses formal_definition for common OCL-like patterns; falls back to description.
    """
    fixes = []
    desc = (rule.get("description") or "").strip()
    formal = (rule.get("formal_definition") or "").strip()
    paths = rule.get("referenced_paths") or []
    tails = [clean_path_tail(p) for p in paths[:4]]

    if formal:
        fl = formal.lower()

        if "same value" in fl:
            if len(tails) >= 2:
                fixes.append(f"Ensure {tails[0]} and {tails[1]} carry identical values.")
            if desc:
                fixes.append(desc)

        elif "must be absent" in fl or "not allowed" in fl or "is not present" in fl:
            for t in tails[:2]:
                fixes.append(f"Remove {t} when the stated condition applies.")
            if desc:
                fixes.append(desc)

        elif "absent" in fl and ("must" in fl or "present" in fl):
            if desc:
                fixes.append(desc)
            if tails:
                fixes.append(
                    f"Verify presence/absence of: {', '.join(tails[:3])}."
                )

        elif "value not included in" in fl or "value included in" in fl:
            if desc:
                fixes.append(desc)
            fixes.append("Check the element value against the allowed code list in the spec.")

        else:
            if desc:
                fixes.append(desc)
            if tails:
                fixes.append(f"Relevant paths: {', '.join(tails[:3])}.")
    else:
        if desc:
            fixes.append(desc)

    return [f for f in fixes if f]


# ---------------------------------------------------------------------------
# Step 1 â€“ Enrich cbpr_plus_formal_rules
# ---------------------------------------------------------------------------

def enrich_cbpr_rules(kb: dict, new_rules_list: list) -> tuple:
    existing_rules: list = kb.setdefault("cbpr_plus_formal_rules", [])

    # Index existing rules by normalised name
    by_norm: dict = {}
    for r in existing_rules:
        by_norm[norm_name(r["name"])] = r

    added, enriched = 0, 0
    seen_ids: set = set()

    for nr in new_rules_list:
        nn = norm_name(nr["name"])

        if nn in by_norm:
            # Enrich in-place
            existing = by_norm[nn]
            existing["spec_rule_id"] = nr["id"]
            existing["kind"] = nr.get("kind", "")
            if "formal_definition" in nr:
                existing["formal_definition"] = nr["formal_definition"]
            if "referenced_paths" in nr:
                existing["referenced_paths"] = nr["referenced_paths"]
            enriched += 1
        else:
            if nr["id"] not in seen_ids:
                new_entry = {
                    "rule_id": nr["id"],
                    "name": nr["name"],
                    "kind": nr.get("kind", ""),
                    "description": nr.get("description", ""),
                    "fix_suggestions": generate_fixes(nr),
                }
                if "formal_definition" in nr:
                    new_entry["formal_definition"] = nr["formal_definition"]
                if "referenced_paths" in nr:
                    new_entry["referenced_paths"] = nr["referenced_paths"]
                existing_rules.append(new_entry)
                seen_ids.add(nr["id"])
                added += 1

    return enriched, added


# ---------------------------------------------------------------------------
# Step 2 â€“ Attach format_constraints_ref to existing tags
# ---------------------------------------------------------------------------

def build_fc_index(format_constraints: list) -> dict:
    """Build path â†’ list[constraint] lookup."""
    idx: dict = {}
    for fc in format_constraints:
        p = fc.get("path", "")
        if p:
            idx.setdefault(p, []).append(fc)
    return idx


def _useful_fc(fc: dict) -> dict | None:
    """Return a cleaned constraint dict if it carries useful validation info."""
    if not (fc.get("pattern") or fc.get("min_length") or fc.get("max_length")):
        return None
    return {k: v for k, v in fc.items()
            if v is not None and k in ("path", "name", "datatype", "pattern",
                                       "min_length", "max_length")}


def attach_format_constraints(kb: dict, fc_index: dict) -> int:
    enriched = 0
    for tag in kb.get("tags", []):
        xpath_norm = tag.get("xpath", "").lstrip("/")
        if not xpath_norm:
            continue
        matched = []
        prefix = xpath_norm + "/"
        for fc_path, fcs in fc_index.items():
            if fc_path == xpath_norm or fc_path.startswith(prefix):
                for fc in fcs:
                    cleaned = _useful_fc(fc)
                    if cleaned:
                        matched.append(cleaned)
        if matched:
            tag["format_constraints_ref"] = matched
            enriched += 1
    return enriched


# ---------------------------------------------------------------------------
# Step 3 â€“ Add new tags from element_structure
# ---------------------------------------------------------------------------

def _path_depth_from(path: str, prefix: str) -> int:
    suffix = path[len(prefix):].lstrip("/")
    return len(suffix.split("/")) if suffix else 0


def _build_tag_from_element(
    elem: dict,
    fc_index: dict,
    rules_by_id: dict,
) -> dict:
    path = elem["path"]
    xml_tag = elem.get("xml_tag", "")
    name = elem.get("name", xml_tag)
    multiplicity = elem.get("multiplicity", "[0..1]")
    datatype = elem.get("datatype")
    rule_ids = elem.get("rules", [])
    mt_synonym = elem.get("mt_synonym")

    xpath = "/" + path
    # Shorten tag key: strip Document/FIToFICstmrCdtTrf/ prefix
    tag_key = re.sub(r"^Document/FIToFICstmrCdtTrf/", "", path)

    is_mandatory = "1..1" in multiplicity or multiplicity.startswith("[1..")

    # Format constraints relevant to this path
    useful_fcs = [
        c for c in
        (_useful_fc(fc) for fcs in [fc_index.get(path, [])] for fc in fcs)
        if c
    ]

    # Description
    desc_parts = [name]
    if mt_synonym:
        desc_parts.append(f"MT synonym: {mt_synonym}.")
    rule_names = [rules_by_id[r]["name"] for r in rule_ids if r in rules_by_id]
    if rule_names:
        desc_parts.append(f"Rules: {', '.join(rule_names[:2])}.")
    description = " ".join(desc_parts)

    errors = []

    # Missing error for mandatory non-Choice elements
    if is_mandatory and datatype != "Choice":
        errors.append({
            "error_id": f"{xml_tag.upper()}_MISSING",
            "error_code": "MISSING_MANDATORY_FIELD",
            "severity": "Fatal",
            "description": f"{name} is absent; mandatory per CBPR+ SR2025.",
            "affected_tags": [tag_key],
            "possible_fixes": [
                f"Insert <{xml_tag}></{xml_tag}> inside the appropriate parent element.",
            ],
        })

    # Pattern/length errors from constraints
    for fc in useful_fcs:
        if fc.get("pattern"):
            errors.append({
                "error_id": f"{xml_tag.upper()}_PATTERN_ERROR",
                "error_code": "INVALID_FORMAT",
                "severity": "Fatal",
                "description": f"{name} does not match the required format pattern.",
                "affected_tags": [tag_key],
                "possible_fixes": [
                    f"Value must match pattern: {fc['pattern']}.",
                ],
            })
        if fc.get("max_length"):
            errors.append({
                "error_id": f"{xml_tag.upper()}_TOO_LONG",
                "error_code": "ID_LENGTH_ERROR",
                "severity": "Fatal",
                "description": f"{name} exceeds maximum length of {fc['max_length']} characters.",
                "affected_tags": [tag_key],
                "possible_fixes": [
                    f"Truncate to at most {fc['max_length']} characters.",
                ],
            })

    # Rule-specific errors
    for rule_id in rule_ids:
        if rule_id in rules_by_id:
            r = rules_by_id[rule_id]
            fixes = generate_fixes(r)
            if fixes:
                errors.append({
                    "error_id": f"{xml_tag.upper()}_{rule_id}_VIOLATION",
                    "error_code": rule_id,
                    "severity": "Fatal" if r.get("kind") == "FormalRule" else "Warning",
                    "description": r.get("description", ""),
                    "rule_id": rule_id,
                    "rule_name": r["name"],
                    "affected_tags": [tag_key],
                    "possible_fixes": fixes,
                })

    tag: dict = {
        "tag": tag_key,
        "xml_element": xml_tag,
        "xpath": xpath,
        "occurrence": multiplicity,
        "mandatory": is_mandatory,
        "description": description,
    }
    if datatype:
        tag["datatype"] = datatype
    if mt_synonym:
        tag["mt_synonym"] = mt_synonym
    if rule_ids:
        tag["spec_rule_ids"] = rule_ids
    if useful_fcs:
        tag["format_constraints_ref"] = useful_fcs
    if errors:
        tag["errors"] = errors

    return tag


def add_new_tags(
    kb: dict,
    element_structure: list,
    fc_index: dict,
    new_rules_list: list,
) -> int:
    existing_norm = {t.get("xpath", "").lstrip("/") for t in kb.get("tags", [])}
    rules_by_id = {r["id"]: r for r in new_rules_list}
    new_tags = []

    for elem in element_structure:
        path = elem.get("path", "")
        if not path:
            continue

        norm_xpath = path  # element_structure paths have no leading slash
        if norm_xpath in existing_norm:
            continue

        is_cdttrtxinf = path.startswith(CDTTRTXINF_PREFIX + "/")
        is_grphdr = path.startswith(GRPHDR_PREFIX + "/")

        if is_cdttrtxinf:
            depth = _path_depth_from(path, CDTTRTXINF_PREFIX)
            if depth > MAX_DEPTH_CDTTRTXINF:
                continue
        elif is_grphdr:
            depth = _path_depth_from(path, GRPHDR_PREFIX)
            if depth > MAX_DEPTH_GRPHDR:
                continue
        else:
            continue

        datatype = elem.get("datatype")
        rule_ids = elem.get("rules", [])
        multiplicity = elem.get("multiplicity", "[0..1]")
        is_mandatory = "1..1" in multiplicity or multiplicity.startswith("[1..")
        has_constraints = path in fc_index

        # Skip Choice containers and bare non-mandatory containers with no rules/constraints
        if datatype == "Choice":
            continue
        if datatype is None and not rule_ids and not has_constraints and not is_mandatory:
            continue

        tag = _build_tag_from_element(elem, fc_index, rules_by_id)
        new_tags.append(tag)
        existing_norm.add(norm_xpath)

    kb["tags"].extend(new_tags)
    return len(new_tags)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("=== pacs.008 KB merge: rules.json -> validation KB ===\n")

    kb = load_json(KB_PATH)
    new_file = load_json(RULES_PATH)

    shutil.copy(KB_PATH, BACKUP_PATH)
    print(f"Backup saved -> {BACKUP_PATH.name}")

    new_rules_list: list = new_file.get("business_rules", [])
    format_constraints: list = new_file.get("format_constraints", [])
    element_structure: list = new_file.get("element_structure", [])

    print(f"Source: {len(new_rules_list)} business rules | "
          f"{len(format_constraints)} format constraints | "
          f"{len(element_structure)} elements\n")

    before_cbpr = len(kb.get("cbpr_plus_formal_rules", []))
    before_tags = len(kb.get("tags", []))

    # â”€â”€ Step 1 â”€â”€
    print("Step 1 â€“ Enrich cbpr_plus_formal_rules â€¦")
    enriched_rules, added_rules = enrich_cbpr_rules(kb, new_rules_list)
    after_cbpr = len(kb.get("cbpr_plus_formal_rules", []))
    print(f"         {before_cbpr} â†’ {after_cbpr}  "
          f"({enriched_rules} enriched, {added_rules} new)\n")

    # â”€â”€ Step 2 â”€â”€
    print("Step 2 â€“ Attach format_constraints_ref to existing tags â€¦")
    fc_index = build_fc_index(format_constraints)
    enriched_tags = attach_format_constraints(kb, fc_index)
    print(f"         {enriched_tags} tags enriched with format constraints\n")

    # â”€â”€ Step 3 â”€â”€
    print("Step 3 â€“ Add new tags from element_structure â€¦")
    added_tags = add_new_tags(kb, element_structure, fc_index, new_rules_list)
    after_tags = len(kb.get("tags", []))
    print(f"         {before_tags} â†’ {after_tags}  ({added_tags} new tags)\n")

    # â”€â”€ Step 4 â€“ Update metadata â”€â”€
    kb["last_updated"] = str(date.today())
    existing_refs = set(kb.get("reference_documents", []))
    existing_refs.add(new_file.get("source_file", ""))
    kb["reference_documents"] = sorted(existing_refs)
    kb["merge_source"] = {
        "file": new_file.get("source_file", ""),
        "extracted_with": new_file.get("extracted_with", ""),
        "spec_counts": new_file.get("counts", {}),
        "merged_on": str(date.today()),
    }

    save_json(KB_PATH, kb)
    print(f"Saved â†’ {KB_PATH.name}\n")
    print("â”€â”€ Summary â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€")
    print(f"  iso_20022_rules           : {len(kb.get('iso_20022_rules', []))}")
    print(f"  cbpr_plus_formal_rules    : {after_cbpr}  (was {before_cbpr})")
    print(f"  tags                      : {after_tags}  (was {before_tags})")
    print(f"  cross_tag_dependency_rules: {len(kb.get('cross_tag_dependency_rules', []))}")
    print("â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€")


if __name__ == "__main__":
    main()
