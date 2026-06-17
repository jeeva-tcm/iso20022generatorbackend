import sys; sys.path.insert(0,".")
from app.services.generation.bulk_generator import generate_single_xml, get_blocks_for_message
from lxml import etree
xml = generate_single_xml("pacs.010", [b["id"] for b in get_blocks_for_message("pacs.010")], idx=1, version="SR2026")
root = etree.fromstring(xml.encode())
nb = root.xpath("//*[local-name()=\"NbOfTxs\"]")
cdtinstr = root.xpath("//*[local-name()=\"CdtInstr\"]")
print("NbOfTxs:", [e.text for e in nb])
print("CdtInstr count:", len(cdtinstr))
# Also check what tags are in GrpHdr
grphdr = root.xpath("//*[local-name()=\"GrpHdr\"]")
if grphdr:
    print("GrpHdr children:", [etree.QName(c.tag).localname for c in grphdr[0] if isinstance(c.tag,str)])
