# -*- coding: utf-8 -*-
import sys, os, re
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

XML = '<?xml version="1.0"?><Document xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08"></Document>'
print("Char at xmlns end:", repr(XML[XML.index("pacs.008.001.08"):XML.index("pacs.008.001.08")+18]))

# Test 1
m = re.search(r"((?:[a-z]+\.){2,3}\d+)(?=[\"'\s>])", XML)
print("regex 1:", m.group(1) if m else "None")

# Test 2 (just \" )
m = re.search(r'((?:[a-z]+\.){2,3}\d+)(?=")', XML)
print("regex 2:", m.group(1) if m else "None")

# Test 3 broader
m = re.search(r'((?:[a-z]+\.){2,3}\d+)', XML)
print("regex 3 (no lookahead):", m.group(1) if m else "None")

# Now reload module fresh
import importlib
import app.services.fix_suggester as fs
importlib.reload(fs)
print("Module:", fs._detect_msg_type(XML))
