import sys; sys.path.insert(0,".")
from app.services.generation.bulk_generator import generate_single_xml, get_blocks_for_message
from lxml import etree
xml = generate_single_xml("pacs.009.cov", [b["id"] for b in get_blocks_for_message("pacs.009.cov")], idx=1, version="SR2026")
root = etree.fromstring(xml.encode())
for el in root.iter():
    if isinstance(el.tag,str) and etree.QName(el.tag).localname == "UndrlygCstmrCdtTrf":
        print(etree.tostring(el, pretty_print=True).decode()[:800])
        break
