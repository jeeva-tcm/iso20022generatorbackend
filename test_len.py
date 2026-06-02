import urllib.request, tempfile, os
from lxml import etree

xml = '<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08"><FIToFICstmrCdtTrf><GrpHdr><MsgId>12345678901234567890</MsgId><CreDtTm>2026-05-28T12:00:00Z</CreDtTm><NbOfTxs>1</NbOfTxs><SttlmInf><SttlmMtd>INDA</SttlmMtd></SttlmInf></GrpHdr></FIToFICstmrCdtTrf></Document>'
schema_txt = '''<?xml version="1.0" encoding="UTF-8"?><xs:schema xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08" xmlns:xs="http://www.w3.org/2001/XMLSchema" targetNamespace="urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08" elementFormDefault="qualified"><xs:element name="Document" type="Document"/><xs:complexType name="Document"><xs:sequence><xs:element name="FIToFICstmrCdtTrf" type="FIToFICstmrCdtTrfV08"/></xs:sequence></xs:complexType><xs:complexType name="FIToFICstmrCdtTrfV08"><xs:sequence><xs:element name="GrpHdr" type="GroupHeader93"/></xs:sequence></xs:complexType><xs:complexType name="GroupHeader93"><xs:sequence><xs:element name="MsgId" type="Max16Text"/><xs:element name="CreDtTm" type="xs:dateTime"/><xs:element name="NbOfTxs" type="xs:string"/><xs:element name="SttlmInf" type="SttlmInf"/></xs:sequence></xs:complexType><xs:complexType name="SttlmInf"><xs:sequence><xs:element name="SttlmMtd" type="xs:string"/></xs:sequence></xs:complexType><xs:simpleType name="Max16Text"><xs:restriction base="xs:string"><xs:maxLength value="16"/></xs:restriction></xs:simpleType></xs:schema>'''

with open('test_schema.xsd', 'w') as f:
    f.write(schema_txt)

schema = etree.XMLSchema(file='test_schema.xsd')
try:
    schema.assertValid(etree.fromstring(xml.encode()))
except Exception as e:
    print('ERROR:', str(e))
