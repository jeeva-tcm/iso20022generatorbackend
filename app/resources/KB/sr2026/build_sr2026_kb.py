#!/usr/bin/env python3
r"""
Build per-message CBPR+ SR2026 validation KBs from the SR2026 rules.json
extractions, in the SAME golden structure used by the SR2025 KBs
(KB/sr2025/<msg>_cbprplus_sr2025_validation_kb.json).

For each of the 17 message types that already have an SR2025 KB, this reads the
matching rules.json under
  app/sr2026/rules/messages/rules docs/output/
and emits
  app/resources/KB/sr2026/<msg>_cbprplus_sr2026_validation_kb.json

fix_suggester.py resolves SR2026 by checking KB/sr2026/ first (sr2025->sr2026
filename rename) and REPLACING the base wholesale, so these are standalone
files in the exact schema the loader + _KBContext already consume:
  domain, version, last_updated, xsd_version, xsd_namespace, purpose,
  reference_documents, iso_20022_rules (dict{name:desc}),
  cbpr_plus_formal_rules (list[str]), tags (list[obj]),
  cross_tag_dependency_rules, tag_insertion_order, schema_mapping_notes,
  merge_source.

Tag construction mirrors merge_pacs008_rules.py (mandatory-missing / pattern /
max_length / rule-violation errors + format_constraints_ref).

Idempotent: re-running overwrites each output deterministically.
"""
import json
import re
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent                 # .../KB/sr2026
KB_SR2025 = HERE.parent / "sr2025"
RULES_DIR = (
    HERE.parent.parent.parent                          # app/
    / "sr2026" / "rules" / "messages" / "rules docs" / "output"
)
TODAY = str(date.today())

# KB target -> (source rules.json discriminator, sr2025 KB filename)
TARGETS = {
    "pacs002":     ("pacs_002_001_10",                     "pacs002_cbprplus_sr2025_validation_kb.json"),
    "pacs003":     ("pacs_003_001_08",                     "pacs003_cbprplus_sr2025_validation_kb.json"),
    "pacs004":     ("pacs_004_001_09",                     "pacs004_cbprplus_sr2025_validation_kb.json"),
    "pacs008":     ("pacs_008_001_08_FIToFI",              "pacs008_cbprplus_sr2025_validation_kb.json"),
    "pacs009":     ("pacs_009_001_08_FinancialInstitution","pacs009_cbprplus_sr2025_validation_kb.json"),
    "pacs009_adv": ("pacs_009_001_08_ADV",                 "pacs009_adv_cbprplus_sr2025_validation_kb.json"),
    "pacs009_cov": ("pacs_009_001_08_COV",                 "pacs009_cov_cbprplus_sr2025_validation_kb.json"),
    "pacs010":     ("pacs_010_001_03",                     "pacs010_cbprplus_sr2025_validation_kb.json"),
    "camt052":     ("camt_052",                            "camt052_cbprplus_sr2025_validation_kb.json"),
    "camt053":     ("camt_053",                            "camt053_cbprplus_sr2025_validation_kb.json"),
    "camt054":     ("camt_054",                            "camt054_cbprplus_sr2025_validation_kb.json"),
    "camt055":     ("camt_055",                            "camt055_cbprplus_sr2025_validation_kb.json"),
    "camt056":     ("camt_056",                            "camt056_cbprplus_sr2025_validation_kb.json"),
    "camt057":     ("camt_057_001_06",                     "camt057_cbprplus_sr2025_validation_kb.json"),
    "pain001":     ("pain_001",                            "pain001_cbprplus_sr2025_validation_kb.json"),
    "pain002":     ("pain_002",                            "pain002_cbprplus_sr2025_validation_kb.json"),
    "pain008":     ("pain_008",                            "pain008_cbprplus_sr2025_validation_kb.json"),
}

# Files to exclude when matching the discriminator (variants are out of scope).
EXCLUDE = ("STP", "MultipleCharges")

# ── SR2026 cross-cutting validator issue rules ──────────────────────────────
# Codes the SR2026 validator emits on every payment/cash message type but that
# the per-message rules.json extractions do not carry. Documented in each KB's
# iso_20022_rules so the AI fixer + KB context have grounded metadata for them.
# (NbOfTxs count mismatch is a cross-tag rule — injected separately, gated on the
# message actually declaring a GrpHdr/NbOfTxs.)
SR2026_ISSUE_ISO_RULES = [
    {
        "rule_id": "DUPLICATE_ID_VALUE",
        "name": "DuplicateIdentifierValue",
        "description": ("Identifiers required to be unique within a message "
                        "(e.g. InstrId, TxId, EndToEndId, UETR) must not repeat "
                        "across transactions or collide with GrpHdr/MsgId."),
        "error_code": "DUPLICATE_ID_VALUE",
        "severity": "Error",
        "error_text": "Identifier value is duplicated within the message.",
    },
    {
        "rule_id": "BIC_NOT_FOUND",
        "name": "BicNotInDirectory",
        "description": ("Each BICFI should resolve to a registered institution "
                        "in the official BIC directory."),
        "error_code": "BIC_NOT_FOUND",
        "severity": "Warning",
        "error_text": "BIC/SWIFT code is not recognized in the official directory.",
    },
    {
        "rule_id": "MISSING_LEI_ADVISORY",
        "name": "MissingLeiAdvisory",
        "description": ("Under SR2026 a Legal Entity Identifier (LEI) is highly "
                        "recommended for all financial institutions."),
        "error_code": "MISSING_LEI_ADVISORY",
        "severity": "Warning",
        "error_text": "Agent FinInstnId is missing a recommended LEI.",
    },
]

SR2026_NBTX_DEP_RULE = {
    "rule_id": "DEP_NB_OF_TXS",
    "rule": ("GrpHdr/NbOfTxs must equal the actual number of transaction blocks "
             "in the message."),
    "affected_tags": ["GrpHdr/NbOfTxs"],
    "fix": ("Set NbOfTxs to the count of transaction blocks, or add/remove "
            "transactions so the count matches."),
    "error_code": "NB_OF_TXS_COUNT_MISMATCH",
}


def inject_issue_rules(iso, cross, has_nb_of_txs: bool):
    """Merge the SR2026 cross-cutting validator issue rules into a KB's
    iso_20022_rules (list-of-obj OR dict{name:desc}) and cross_tag_dependency_rules.
    Idempotent: skips a rule already present by error_code / rule_id / name."""
    if isinstance(iso, list):
        present = {r.get("error_code") for r in iso if isinstance(r, dict)}
        for r in SR2026_ISSUE_ISO_RULES:
            if r["error_code"] not in present:
                iso.append(dict(r))
    elif isinstance(iso, dict):
        for r in SR2026_ISSUE_ISO_RULES:
            if r["name"] not in iso:
                iso[r["name"]] = f"[{r['error_code']}] {r['description']}"

    if has_nb_of_txs and isinstance(cross, list):
        if not any(isinstance(c, dict) and c.get("rule_id") == SR2026_NBTX_DEP_RULE["rule_id"]
                   for c in cross):
            cross.append(dict(SR2026_NBTX_DEP_RULE))
    return iso, cross

KEY_ORDER = [
    "domain", "version", "last_updated", "xsd_version", "xsd_namespace",
    "purpose", "reference_documents", "iso_20022_rules",
    "cbpr_plus_formal_rules", "tags", "cross_tag_dependency_rules",
    "tag_insertion_order", "schema_mapping_notes", "merge_source",
]


# --------------------------------------------------------------------------- #
# helpers (tag construction mirrors merge_pacs008_rules.py)
# --------------------------------------------------------------------------- #
def load_json(p: Path) -> dict:
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def find_source(disc: str) -> Path:
    matches = [
        p for p in RULES_DIR.glob("*.rules.json")
        if disc in p.name and not any(x in p.name for x in EXCLUDE)
    ]
    if len(matches) != 1:
        raise SystemExit(f"discriminator {disc!r} matched {len(matches)} files: "
                         f"{[m.name for m in matches]}")
    return matches[0]


def clean_path_tail(path_ref: str) -> str:
    return path_ref.strip("[]").split("/")[-1]


def generate_fixes(rule: dict) -> list:
    """Human-readable fix suggestions from a business rule (OCL-ish parse)."""
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
                fixes.append(f"Verify presence/absence of: {', '.join(tails[:3])}.")
        elif "value not included in" in fl or "value included in" in fl:
            if desc:
                fixes.append(desc)
            fixes.append("Check the element value against the allowed code list in the spec.")
        else:
            if desc:
                fixes.append(desc)
            if tails:
                fixes.append(f"Relevant paths: {', '.join(tails[:3])}.")
    elif desc:
        fixes.append(desc)
    return [f for f in fixes if f]


def is_cbpr_rule(r: dict) -> bool:
    """CBPR+ usage-guideline rule (-> cbpr_plus_formal_rules) vs base ISO rule."""
    name = (r.get("name") or "")
    return name.upper().startswith("CBPR")


def extract_cbpr(business_rules: list) -> list:
    """CBPR+ formal/textual rule descriptions from the SR2026 rules.json (str[])."""
    out, seen = [], set()
    for r in business_rules:
        if not is_cbpr_rule(r):
            continue
        desc = (r.get("description") or "").strip()
        if desc and desc not in seen:
            seen.add(desc)
            out.append(desc)
    return out


def sr2025_cbpr_strings(sr2025: dict) -> list:
    """SR2025 cbpr_plus_formal_rules as a flat str list (file may store str[] or obj[])."""
    out = []
    for r in (sr2025.get("cbpr_plus_formal_rules") or []):
        if isinstance(r, str):
            out.append(r)
        elif isinstance(r, dict):
            s = (r.get("description") or "").strip()
            if s:
                out.append(s)
    return out


def build_fc_index(format_constraints: list) -> dict:
    idx: dict = {}
    for fc in format_constraints:
        p = fc.get("path", "")
        if p:
            idx.setdefault(p, []).append(fc)
    return idx


def useful_fc(fc: dict):
    if not (fc.get("pattern") or fc.get("min_length") or fc.get("max_length")):
        return None
    return {k: v for k, v in fc.items()
            if v is not None and k in ("path", "name", "datatype", "pattern",
                                       "min_length", "max_length")}


def short_tag(path: str, root: str) -> str:
    """Strip 'Document/<Root>/' so the tag key matches SR2025 style (AppHdr/... or section/...)."""
    return re.sub(rf"^Document/{re.escape(root)}/", "", path)


def build_tag(elem: dict, fc_index: dict, rules_by_id: dict, root: str) -> dict:
    path = elem["path"]
    xml_tag = elem.get("xml_tag", "")
    name = elem.get("name", xml_tag)
    multiplicity = elem.get("multiplicity", "[0..1]")
    datatype = elem.get("datatype")
    rule_ids = elem.get("rules", [])

    xpath = "/" + path
    tag_key = short_tag(path, root)
    is_mandatory = "1..1" in multiplicity or multiplicity.startswith("[1..")

    useful = [c for c in (useful_fc(fc) for fc in fc_index.get(path, [])) if c]

    desc_parts = [name]
    rule_names = [rules_by_id[r]["name"] for r in rule_ids if r in rules_by_id]
    if rule_names:
        desc_parts.append(f"Rules: {', '.join(rule_names[:2])}.")
    description = " ".join(desc_parts)

    errors = []
    if is_mandatory and datatype != "Choice":
        errors.append({
            "error_id": f"{xml_tag.upper()}_MISSING",
            "error_code": "MISSING_MANDATORY_FIELD",
            "severity": "Fatal",
            "description": f"{name} is absent; mandatory per CBPR+ SR2026.",
            "affected_tags": [tag_key],
            "possible_fixes": [
                f"Insert <{xml_tag}></{xml_tag}> inside the appropriate parent element.",
            ],
        })
    for fc in useful:
        if fc.get("pattern"):
            errors.append({
                "error_id": f"{xml_tag.upper()}_PATTERN_ERROR",
                "error_code": "INVALID_FORMAT",
                "severity": "Fatal",
                "description": f"{name} does not match the required format pattern.",
                "affected_tags": [tag_key],
                "possible_fixes": [f"Value must match pattern: {fc['pattern']}."],
            })
        if fc.get("max_length"):
            errors.append({
                "error_id": f"{xml_tag.upper()}_TOO_LONG",
                "error_code": "ID_LENGTH_ERROR",
                "severity": "Fatal",
                "description": f"{name} exceeds maximum length of {fc['max_length']} characters.",
                "affected_tags": [tag_key],
                "possible_fixes": [f"Truncate to at most {fc['max_length']} characters."],
            })
    for rid in rule_ids:
        r = rules_by_id.get(rid)
        if not r:
            continue
        fixes = generate_fixes(r)
        if fixes:
            errors.append({
                "error_id": f"{xml_tag.upper()}_{rid}_VIOLATION",
                "error_code": rid,
                "severity": "Fatal" if r.get("kind") == "FormalRule" else "Warning",
                "description": r.get("description", ""),
                "rule_id": rid,
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
    mx = next((fc.get("max_length") for fc in useful if fc.get("max_length")), None)
    if mx:
        tag["max_length"] = mx
    if rule_ids:
        tag["spec_rule_ids"] = rule_ids
    if useful:
        tag["format_constraints_ref"] = useful
    if errors:
        tag["errors"] = errors
    return tag


def keep_element(elem: dict, fc_index: dict) -> bool:
    """Emit a tag for AppHdr leaves and any Document element that is mandatory,
    rule-bearing, constrained, or a typed leaf. Skip Choice containers and bare
    untyped optional containers (mirrors merge_pacs008 selectivity, minus the
    pacs008-specific depth cap)."""
    path = elem.get("path", "")
    if not path:
        return False
    if elem.get("is_choice") or elem.get("datatype") == "Choice":
        return False
    datatype = elem.get("datatype")
    rule_ids = elem.get("rules", [])
    multiplicity = elem.get("multiplicity", "[0..1]")
    is_mandatory = "1..1" in multiplicity or multiplicity.startswith("[1..")
    has_fc = path in fc_index
    if datatype is None and not rule_ids and not has_fc and not is_mandatory:
        return False
    return True


def build_one(disc: str, sr2025_name: str) -> dict:
    src = find_source(disc)
    rules = load_json(src)
    sr2025 = load_json(KB_SR2025 / sr2025_name) if (KB_SR2025 / sr2025_name).exists() else {}

    ms = rules.get("message_summary", {})
    mt = ms.get("message_type", "")
    es = rules.get("element_structure", [])
    fcs = rules.get("format_constraints", [])
    brs = rules.get("business_rules", [])

    # Document root element (e.g. FIToFICstmrCdtTrf)
    root = ""
    for e in es:
        p = e.get("path", "")
        if p.startswith("Document/"):
            root = p.split("/")[1]
            break

    fc_index = build_fc_index(fcs)
    rules_by_id = {r["id"]: r for r in brs if "id" in r}

    # iso_20022_rules are hand-authored base-ISO schema rules; they carry forward
    # from SR2025 unchanged (the SR2026 rules.json carries only CBPR_* rules).
    iso = sr2025.get("iso_20022_rules", {})

    # cbpr_plus_formal_rules: SR2025 curated strings first, then any genuinely
    # new CBPR+ rule descriptions extracted from the SR2026 spec (deduped).
    cbpr, seen = [], set()
    for s in sr2025_cbpr_strings(sr2025) + extract_cbpr(brs):
        if s not in seen:
            seen.add(s)
            cbpr.append(s)

    tags = [build_tag(e, fc_index, rules_by_id, root)
            for e in es if keep_element(e, fc_index)]

    # Merge SR2026 cross-cutting validator issue rules (DUPLICATE_ID_VALUE,
    # BIC_NOT_FOUND, MISSING_LEI_ADVISORY, and the NbOfTxs count rule when the
    # message declares a GrpHdr/NbOfTxs).
    has_nb_of_txs = any(
        e.get("xml_tag") == "NbOfTxs" or e.get("path", "").endswith("/NbOfTxs")
        for e in es
    )
    cross_rules = list(sr2025.get("cross_tag_dependency_rules", []))
    iso, cross_rules = inject_issue_rules(
        json.loads(json.dumps(iso)), cross_rules, has_nb_of_txs)

    # AppHdr (BAH) mandatory note for all CBPR+ messages, kept first in cbpr list.
    bah_note = ("AppHdr (BusinessApplicationHeaderV02) is mandatory for all CBPR+ "
                "messages and must precede the Document element.")
    if bah_note not in cbpr:
        cbpr.insert(0, bah_note)

    out = {
        "domain": f"SWIFT MX CBPR+ {mt}",
        "version": "SR2026",
        "last_updated": TODAY,
        "purpose": (f"Context KB for {mt} {ms.get('message_name', '')} validation "
                    "(CBPR+ SR2026). For each XML tag: possible errors, affected "
                    "tags, and fix suggestions, plus ISO 20022 / CBPR+ formal rules."),
        "reference_documents": [rules.get("source_file", src.name)],
        "iso_20022_rules": iso,
        "cbpr_plus_formal_rules": cbpr,
        "tags": tags,
        "cross_tag_dependency_rules": cross_rules,
        "tag_insertion_order": sr2025.get("tag_insertion_order", {}),
        "schema_mapping_notes": sr2025.get("schema_mapping_notes", {}),
        "merge_source": {
            "file": rules.get("source_file", src.name),
            "extracted_with": rules.get("extracted_with", ""),
            "spec_counts": rules.get("counts", {}),
            "built_on": TODAY,
        },
    }
    # Carry real XSD mapping from the SR2025 KB when it declared one (CBPR+ maps
    # the .08 guideline onto a later .NN schema; unchanged in SR2026).
    if sr2025.get("xsd_version"):
        out["xsd_version"] = sr2025["xsd_version"]
    if sr2025.get("xsd_namespace"):
        out["xsd_namespace"] = sr2025["xsd_namespace"]

    # reorder
    ordered = {k: out[k] for k in KEY_ORDER if k in out}
    for k in out:
        ordered.setdefault(k, out[k])
    return ordered


def main() -> None:
    print("=== Build CBPR+ SR2026 per-message validation KBs ===\n")
    summary = []
    for kb_name, (disc, sr2025_name) in TARGETS.items():
        kb = build_one(disc, sr2025_name)
        out_path = HERE / f"{kb_name}_cbprplus_sr2026_validation_kb.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(kb, f, ensure_ascii=False, indent=2)
            f.write("\n")
        summary.append(
            f"{kb_name:12} -> {out_path.name:46} "
            f"iso={len(kb['iso_20022_rules']):3} "
            f"cbpr={len(kb['cbpr_plus_formal_rules']):3} "
            f"tags={len(kb['tags']):4} "
            f"cross={len(kb['cross_tag_dependency_rules']):2}"
        )
    print("\n".join(summary))
    print(f"\n{len(TARGETS)} SR2026 KBs written to {HERE}")


if __name__ == "__main__":
    main()
