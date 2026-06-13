import os
import re
import json
from lxml import etree
from typing import Dict, Any, List, Optional

# Standard ISO namespaces
XS = "http://www.w3.org/2001/XMLSchema"

class SchemaGenerator:
    @staticmethod
    def get_rules_dir():
        base_dir = os.path.dirname(os.path.abspath(__file__))
        return os.path.normpath(os.path.join(base_dir, "../rules"))

    @staticmethod
    def get_schema_tree(xsd_path: str) -> Optional[Dict[str, Any]]:
        if not os.path.exists(xsd_path):
            return None
            
        try:
            # Some SR2026 XSDs (e.g. pacs.002) have leading whitespace before the
            # XML declaration, which etree.parse() rejects. Read + lstrip first.
            with open(xsd_path, "rb") as _f:
                _raw = _f.read().lstrip()
            root = etree.fromstring(_raw)
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
    def _camel_to_words(name: str) -> str:
        if not name:
            return ""
        name = name.replace("BICFI", "BicFi").replace("IBAN", "Iban").replace("UETR", "Uetr")
        name = re.sub(r'\d+$', '', name)
        words = re.findall(r'[A-Z][a-z]+|[A-Z]+(?=[A-Z][a-z]|$)|[a-z]+|[A-Z]', name)
        return ' '.join(words) if words else name

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
            "label": SchemaGenerator._camel_to_words(name),
            "mandatory": min_occ != '0',
            "repeatable": max_occ == 'unbounded' or (max_occ.isdigit() and int(max_occ) > 1),
            "type": "simple",
            "children": []
        }

        # Inject common options for status fields
        if name == "Conf":
            node["options"] = ["ACCR", "PDCR", "RJCR", "CNCL"]
        elif name in ["TxSts", "GrpSts", "Sts", "TxCxlSts"]:
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
                            node["pattern"] = patterns[0].get("value")
                    else:
                        node["type"] = "simple"
        
        return node

    @staticmethod
    def _parse_complex_type(complex_node, xsd_root, visited_types, depth) -> List[Dict[str, Any]]:
        children = []
        for attr in complex_node.xpath(".//xs:attribute", namespaces={'xs': XS}):
            attr_name = attr.get('name')
            if attr_name:
                children.append({
                    "name": attr_name,
                    "label": f"@{SchemaGenerator._camel_to_words(attr_name)}",
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
