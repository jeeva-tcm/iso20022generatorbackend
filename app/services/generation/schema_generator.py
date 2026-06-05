import os
import re
import json
from lxml import etree
from typing import Dict, Any, List, Optional

# Standard ISO namespaces
XS = "http://www.w3.org/2001/XMLSchema"

# Load field rules and algorithms for enhanced schema generation
RULES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "sr2025", "resources", "rules")
with open(os.path.join(RULES_DIR, "algorithms.json"), "r") as f:
    ALGORITHMS = json.load(f).get("algorithms", {})
with open(os.path.join(RULES_DIR, "fields.json"), "r") as f:
    FIELD_RULES = json.load(f).get("fields", {})

def _camel_to_words(name: str) -> str:
    """Convert ISO 20022 CamelCase names to readable English words."""
    if not name:
        return ""
    # Handle specific common abbreviations
    name = name.replace("BICFI", "BicFi").replace("IBAN", "Iban").replace("UETR", "Uetr")
    name = re.sub(r'\d+$', '', name)
    words = re.findall(r'[A-Z][a-z]+|[A-Z]+(?=[A-Z][a-z]|$)|[a-z]+|[A-Z]', name)
    return ' '.join(words) if words else name

class SchemaGenerator:
    @staticmethod
    def get_schema_tree(xsd_path: str) -> Optional[Dict[str, Any]]:
        if not os.path.exists(xsd_path):
            return None
            
        try:
            tree = etree.parse(xsd_path)
            root = tree.getroot()
            target_ns = root.get('targetNamespace')
            root_elements = root.xpath(f"//xs:element[@name='Document' or @name='BusMsg']", namespaces={'xs': XS})
            if not root_elements:
                root_elements = root.xpath(f"/xs:schema/xs:element", namespaces={'xs': XS})
            
            if not root_elements:
                return None
                
            visited_types = set()
            result = SchemaGenerator._parse_element(root_elements[0], root, visited_types)
            if result:
                result["namespace"] = target_ns
            return result
        except Exception as e:
            print(f"Error generating schema tree for {xsd_path}: {e}")
            return None

    @staticmethod
    def _parse_element(elem, xsd_root, visited_types, depth=0) -> Dict[str, Any]:
        if depth > 20: 
            return {"name": elem.get('name'), "type": "truncated"}

        name = elem.get('name') or ""
        type_name = elem.get('type')
        min_occ = elem.get('minOccurs', '1')
        max_occ = elem.get('maxOccurs', '1')
        
        node = {
            "name": name,
            "label": _camel_to_words(name),
            "mandatory": min_occ != '0',
            "repeatable": max_occ == 'unbounded' or (max_occ.isdigit() and int(max_occ) > 1),
            "type": "simple",
            "children": []
        }

        # Inject common options for status fields (especially for camt.029 Conf)
        if name == "Conf":
            node["options"] = ["ACCR", "PDCR", "RJCR", "CNCL"]
        elif name in ["TxSts", "GrpSts", "Sts", "TxCxlSts"]:
            # Common status codes for pacs.002, pain.002, camt.029 TxCxlSts, etc.
            node["options"] = ["ACTC", "RJCT", "PDNG", "ACCP", "ACSP", "ACWC", "RJCR", "ACCR", "PDCR", "CNCL"]
        
        if not type_name:
            complex_type = elem.find(f"{{{XS}}}complexType")
            if complex_type is not None:
                node["type"] = "complex"
                node["children"] = SchemaGenerator._parse_complex_type(complex_type, xsd_root, visited_types, depth + 1)
            else:
                simple_type = elem.find(f"{{{XS}}}simpleType")
                if simple_type is not None:
                     node["type"] = "simple"
        else:
            if ":" in type_name and any(type_name.startswith(p) for p in ["xs:", "xsd:"]):
                node["type"] = type_name.split(":")[-1]
            else:
                local_type_name = type_name.split(":")[-1] if ":" in type_name else type_name
                
                type_def = xsd_root.xpath(f"//xs:complexType[@name='{local_type_name}']", namespaces={'xs': XS})
                if type_def:
                    node["type"] = "complex"
                    if local_type_name in visited_types and depth > 10:
                         node["type"] = "referred_complex"
                         node["type_name"] = local_type_name
                    else:
                        visited_types.add(local_type_name)
                        node["children"] = SchemaGenerator._parse_complex_type(type_def[0], xsd_root, visited_types, depth + 1)
                        visited_types.remove(local_type_name)
                else:
                    type_def = xsd_root.xpath(f"//xs:simpleType[@name='{local_type_name}']", namespaces={'xs': XS})
                    if type_def:
                        node["type"] = "simple"
                        restrictions = type_def[0].xpath(".//xs:enumeration", namespaces={'xs': XS})
                        if restrictions:
                             node["options"] = [r.get('value') for r in restrictions]
                        
                        patterns = type_def[0].xpath(".//xs:pattern", namespaces={'xs': XS})
                        if patterns:
                            pattern_val = patterns[0].get("value")
                            node["pattern"] = pattern_val
                        
                        node_name = node.get("name", "")
                        if node_name in FIELD_RULES:
                            rule = FIELD_RULES[node_name]
                            algo_name = rule.get("regex")
                            if algo_name in ALGORITHMS:
                                msg = ALGORITHMS[algo_name].get("message")
                                node["message"] = msg
                                node["errorMessage"] = msg
                                node["error"] = msg
                        elif local_type_name in ALGORITHMS:
                            msg = ALGORITHMS[local_type_name].get("message")
                            node["message"] = msg
                            node["errorMessage"] = msg
                            node["error"] = msg
                        elif "BIC" in local_type_name.upper():
                            if "BICFI" in ALGORITHMS:
                                msg = ALGORITHMS["BICFI"].get("message")
                                node["message"] = msg
                                node["errorMessage"] = msg
                                node["error"] = msg
                    else:
                        node["type"] = "simple"
        
        if name in FIELD_RULES:
            rule = FIELD_RULES[name]
            if min_occ == '0':
                node["mandatory"] = rule.get("mandatory", node["mandatory"])
            else:
                node["mandatory"] = True

            algo_name = rule.get("regex")
            if algo_name in ALGORITHMS:
                msg = ALGORITHMS[algo_name].get("message")
                node["pattern"] = ALGORITHMS[algo_name].get("pattern")
                node["message"] = msg
                node["errorMessage"] = msg
                node["error"] = msg
                node["error_message"] = msg
        
        # Double check for BIC specific types/names
        if not node.get("message") and ("BIC" in name.upper() or "BIC" in str(node.get("type", "")).upper()):
            msg = ALGORITHMS.get("BICFI", {}).get("message") or "Valid 8 or 11-char BIC required."
            node["message"] = msg
            node["errorMessage"] = msg
            node["error"] = msg
            node["pattern"] = ALGORITHMS.get("BICFI", {}).get("pattern")
        
        return node

    @staticmethod
    def _parse_complex_type(complex_node, xsd_root, visited_types, depth) -> List[Dict[str, Any]]:
        children = []
        for attr in complex_node.xpath(".//xs:attribute", namespaces={'xs': XS}):
            attr_name = attr.get('name')
            if attr_name:
                children.append({
                    "name": attr_name,
                    "label": f"@{_camel_to_words(attr_name)}",
                    "mandatory": attr.get('use') == 'required',
                    "repeatable": False,
                    "type": attr.get('type') or "string",
                    "isAttribute": True,
                    "children": []
                })

        complex_content = complex_node.find(f"{{{XS}}}complexContent")
        if complex_content is not None:
            extension = complex_content.find(f"{{{XS}}}extension")
            if extension is not None:
                base_type = extension.get('base')
                if base_type:
                    local_base = base_type.split(":")[-1] if ":" in base_type else base_type
                    base_def = xsd_root.xpath(f"//xs:complexType[@name='{local_base}']", namespaces={'xs': XS})
                    if base_def:
                        children.extend(SchemaGenerator._parse_complex_type(base_def[0], xsd_root, visited_types, depth))
                children.extend(SchemaGenerator._find_elements_in_container(extension, xsd_root, visited_types, depth))
            return children

        children.extend(SchemaGenerator._find_elements_in_container(complex_node, xsd_root, visited_types, depth))
        return children

    @staticmethod
    def _find_elements_in_container(container, xsd_root, visited_types, depth) -> List[Dict[str, Any]]:
        results = []
        for sub in container.xpath("./xs:element | ./xs:sequence | ./xs:choice | ./xs:all | ./xs:group", namespaces={'xs': XS}):
            tag = sub.tag.split('}')[-1]
            if tag == 'element':
                results.append(SchemaGenerator._parse_element(sub, xsd_root, visited_types, depth))
            elif tag in ['sequence', 'choice', 'all']:
                child_results = SchemaGenerator._find_elements_in_container(sub, xsd_root, visited_types, depth)
                if tag == 'choice':
                    for c in child_results:
                        c["mandatory"] = False
                results.extend(child_results)
            elif tag == 'group':
                ref = sub.get('ref')
                if ref:
                    local_ref = ref.split(":")[-1] if ":" in ref else ref
                    group_def = xsd_root.xpath(f"//xs:group[@name='{local_ref}']", namespaces={'xs': XS})
                    if group_def:
                        results.extend(SchemaGenerator._find_elements_in_container(group_def[0], xsd_root, visited_types, depth))
        return results
