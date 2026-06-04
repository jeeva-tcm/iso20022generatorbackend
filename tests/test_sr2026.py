import unittest
import re
from datetime import datetime, timedelta
from fastapi.testclient import TestClient
from app.main import app
from sr2026.validators.validator import SR2026Validator
from sr2026.delta_rules.lei_validator import LEIValidator
from sr2026.delta_rules.address_validator import AddressValidator

# Mock base XML message (pacs.008) for testing
VALID_PACS008_XML = """<?xml version="1.0" encoding="UTF-8"?>
<BusMsgEnvlp xmlns="urn:swift:xsd:envelope">
	<AppHdr xmlns="urn:iso:std:iso:20022:tech:xsd:head.001.001.02">
		<Fr>
			<FIId>
				<FinInstnId>
					<BICFI>BOFAUS3NXXX</BICFI>
				</FinInstnId>
			</FIId>
		</Fr>
		<To>
			<FIId>
				<FinInstnId>
					<BICFI>BARCGB2DXXX</BICFI>
				</FinInstnId>
			</FIId>
		</To>
		<BizMsgIdr>MSG20260603001</BizMsgIdr>
		<MsgDefIdr>pacs.008.001.08</MsgDefIdr>
		<BizSvc>swift.cbprplus.02</BizSvc>
		<CreDt>2026-06-03T10:00:00+00:00</CreDt>
	</AppHdr>
	<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08">
		<FIToFICstmrCdtTrf>
			<GrpHdr>
				<MsgId>MSG20260603001</MsgId>
				<CreDtTm>2026-06-03T10:00:00+00:00</CreDtTm>
				<NbOfTxs>1</NbOfTxs>
				<SttlmInf>
					<SttlmMtd>INDA</SttlmMtd>
				</SttlmInf>
			</GrpHdr>
			<CdtTrfTxInf>
				<PmtId>
					<InstrId>INSTRDT22UI74GKA</InstrId>
					<EndToEndId>E2E5N16BE7WDJUYB5XV</EndToEndId>
					<TxId>TXLQ147BPVHDJ5UIHR</TxId>
					<UETR>570e1969-3d10-47ec-87a5-96d0bab176e4</UETR>
				</PmtId>
				<IntrBkSttlmAmt Ccy="CHF">73148.83</IntrBkSttlmAmt>
				<IntrBkSttlmDt>2026-06-04</IntrBkSttlmDt>
				<InstdAmt Ccy="CHF">73148.83</InstdAmt>
				<ChrgBr>SHAR</ChrgBr>
				<InstgAgt>
					<FinInstnId>
						<BICFI>BOFAUS3NXXX</BICFI>
					</FinInstnId>
				</InstgAgt>
				<InstdAgt>
					<FinInstnId>
						<BICFI>BARCGB2DXXX</BICFI>
					</FinInstnId>
				</InstdAgt>
				<Dbtr>
					<Nm>Meridian Financial Group</Nm>
					<PstlAdr>
						<TwnNm>Anderson City</TwnNm>
						<Ctry>CH</Ctry>
					</PstlAdr>
					<Id>
						<OrgId>
							<LEI>7ZW8QJWVPR4P1J1KQY45</LEI>
						</OrgId>
					</Id>
				</Dbtr>
				<DbtrAcct>
					<Id>
						<IBAN>CH1825923266006107191</IBAN>
					</Id>
				</DbtrAcct>
				<DbtrAgt>
					<FinInstnId>
						<BICFI>BOFAUS3NXXX</BICFI>
						<LEI>7ZW8QJWVPR4P1J1KQY45</LEI>
					</FinInstnId>
				</DbtrAgt>
				<CdtrAgt>
					<FinInstnId>
						<BICFI>BARCGB2DXXX</BICFI>
						<LEI>7ZW8QJWVPR4P1J1KQY45</LEI>
					</FinInstnId>
				</CdtrAgt>
				<Cdtr>
					<Nm>Global Trade Corp</Nm>
					<PstlAdr>
						<TwnNm>White City</TwnNm>
						<Ctry>CH</Ctry>
					</PstlAdr>
					<Id>
						<OrgId>
							<LEI>7ZW8QJWVPR4P1J1KQY45</LEI>
						</OrgId>
					</Id>
				</Cdtr>
				<CdtrAcct>
					<Id>
						<IBAN>CH6824687630417595377</IBAN>
					</Id>
				</CdtrAcct>
			</CdtTrfTxInf>
		</FIToFICstmrCdtTrf>
	</Document>
</BusMsgEnvlp>
"""

# Mock invalid XML with unstructured address and invalid LEIs
INVALID_PACS008_XML = """<?xml version="1.0" encoding="UTF-8"?>
<BusMsgEnvlp xmlns="urn:swift:xsd:envelope">
	<AppHdr xmlns="urn:iso:std:iso:20022:tech:xsd:head.001.001.02">
		<Fr>
			<FIId>
				<FinInstnId>
					<BICFI>BOFAUS3NXXX</BICFI>
				</FinInstnId>
			</FIId>
		</Fr>
		<To>
			<FIId>
				<FinInstnId>
					<BICFI>BARCGB2DXXX</BICFI>
				</FinInstnId>
			</FIId>
		</To>
		<BizMsgIdr>MSG20260603001</BizMsgIdr>
		<MsgDefIdr>pacs.008.001.08</MsgDefIdr>
		<BizSvc>swift.cbprplus.02</BizSvc>
		<CreDt>2026-06-03T10:00:00+00:00</CreDt>
	</AppHdr>
	<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08">
		<FIToFICstmrCdtTrf>
			<GrpHdr>
				<MsgId>MSG20260603001</MsgId>
				<CreDtTm>2026-06-03T10:00:00+00:00</CreDtTm>
				<NbOfTxs>1</NbOfTxs>
				<SttlmInf>
					<SttlmMtd>INDA</SttlmMtd>
				</SttlmInf>
			</GrpHdr>
			<CdtTrfTxInf>
				<PmtId>
					<InstrId>INSTRDT22UI74GKA</InstrId>
					<EndToEndId>E2E5N16BE7WDJUYB5XV</EndToEndId>
					<TxId>TXLQ147BPVHDJ5UIHR</TxId>
					<UETR>INVALID_UETR</UETR>
				</PmtId>
				<IntrBkSttlmAmt Ccy="USD">73148.83</IntrBkSttlmAmt>
				<IntrBkSttlmDt>2026-06-04</IntrBkSttlmDt>
				<InstdAmt Ccy="USD">73148.83</InstdAmt>
				<ChrgBr>CRED</ChrgBr>
				<InstgAgt>
					<FinInstnId>
						<BICFI>BOFAUS3NXXX</BICFI>
					</FinInstnId>
				</InstgAgt>
				<InstdAgt>
					<FinInstnId>
						<BICFI>BARCGB2DXXX</BICFI>
					</FinInstnId>
				</InstdAgt>
				<Dbtr>
					<Nm>Meridian Financial Group</Nm>
					<PstlAdr>
						<Ctry>CH</Ctry>
						<AdrLine>Unstructured line 1</AdrLine>
					</PstlAdr>
					<Id>
						<OrgId>
							<LEI>INVALID_LEI_CODE_123</LEI>
						</OrgId>
					</Id>
				</Dbtr>
				<DbtrAcct>
					<Id>
						<IBAN>CH1825923266006107191</IBAN>
					</Id>
				</DbtrAcct>
				<DbtrAgt>
					<FinInstnId>
						<BICFI>BOFAUS3NXXX</BICFI>
						<LEI>7ZW8QJWVPR4P1J1KQY45</LEI>
					</FinInstnId>
				</DbtrAgt>
				<CdtrAgt>
					<FinInstnId>
						<BICFI>BARCGB2DXXX</BICFI>
						<LEI>7ZW8QJWVPR4P1J1KQY45</LEI>
					</FinInstnId>
				</CdtrAgt>
				<Cdtr>
					<Nm>Global Trade Corp</Nm>
					<PstlAdr>
						<Ctry>CH</Ctry>
					</PstlAdr>
					<Id>
						<OrgId>
							<LEI>7ZW8QJWVPR4P1J1KQY45</LEI>
						</OrgId>
					</Id>
				</Cdtr>
				<CdtrAcct>
					<Id>
						<IBAN>CH6824687630417595377</IBAN>
					</Id>
				</CdtrAcct>
			</CdtTrfTxInf>
		</FIToFICstmrCdtTrf>
	</Document>
</BusMsgEnvlp>
"""

class TestSR2026(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def get_xml_for_sr2025(self):
        today_str = datetime.utcnow().strftime("%Y-%m-%d")
        tomorrow_str = (datetime.utcnow() + timedelta(days=1)).strftime("%Y-%m-%d")
        xml = VALID_PACS008_XML.replace("2026-06-03", today_str).replace("2026-06-04", tomorrow_str)
        return xml

    def get_xml_for_sr2026(self):
        today_str = datetime.utcnow().strftime("%Y-%m-%d")
        tomorrow_str = (datetime.utcnow() + timedelta(days=1)).strftime("%Y-%m-%d")
        xml = VALID_PACS008_XML.replace("2026-06-03", today_str).replace("2026-06-04", tomorrow_str)
        xml = xml.replace("swift.cbprplus.02", "swift.cbprplus.04")
        return xml

    def get_invalid_xml_dynamic(self):
        today_str = datetime.utcnow().strftime("%Y-%m-%d")
        tomorrow_str = (datetime.utcnow() + timedelta(days=1)).strftime("%Y-%m-%d")
        xml = INVALID_PACS008_XML.replace("2026-06-03", today_str).replace("2026-06-04", tomorrow_str)
        xml = xml.replace("swift.cbprplus.02", "swift.cbprplus.04")
        return xml

    def test_lei_checksum(self):
        self.assertTrue(LEIValidator._is_valid_iso17442("7ZW8QJWVPR4P1J1KQY45"))
        self.assertFalse(LEIValidator._is_valid_iso17442("INVALID_LEI_CODE_123"))

    def test_validate_api_endpoint_sr2026_passed(self):
        xml = self.get_xml_for_sr2026()
        response = self.client.post("/api/validate", json={
            "version": "SR2026",
            "messageType": "pacs.008",
            "xml": xml
        })
        self.assertEqual(response.status_code, 200)
        res_json = response.json()
        self.assertEqual(res_json["status"], "PASSED")
        self.assertEqual(len(res_json["errors"]), 0)

    def test_validate_api_endpoint_sr2026_failed(self):
        xml = self.get_invalid_xml_dynamic()
        response = self.client.post("/api/validate", json={
            "version": "SR2026",
            "messageType": "pacs.008",
            "xml": xml
        })
        self.assertEqual(response.status_code, 200)
        res_json = response.json()
        self.assertEqual(res_json["status"], "FAILED")
        self.assertGreater(len(res_json["errors"]), 0)
        
        error_codes = [err["code"] for err in res_json["errors"]]
        self.assertIn("DEPRECATED_UNSTRUCTURED_ADDRESS", error_codes)
        self.assertIn("INVALID_LEI_FORMAT", error_codes)
        self.assertIn("INVALID_UETR_FORMAT", error_codes)
        self.assertIn("MISSING_TOWN_NAME", error_codes)

    def test_validate_api_endpoint_sr2025_passed(self):
        xml = self.get_xml_for_sr2025()
        response = self.client.post("/api/validate", json={
            "version": "SR2025",
            "messageType": "pacs.008.001.08",
            "xml": xml
        })
        self.assertEqual(response.status_code, 200)
        res_json = response.json()
        self.assertEqual(res_json["status"], "PASSED")

    def test_sr2026_invalid_biz_svc(self):
        xml = self.get_xml_for_sr2026().replace("swift.cbprplus.04", "swift.cbprplus.03")
        response = self.client.post("/api/validate", json={
            "version": "SR2026",
            "messageType": "pacs.008",
            "xml": xml
        })
        res_json = response.json()
        self.assertEqual(res_json["status"], "FAILED")
        error_codes = [err["code"] for err in res_json["errors"]]
        self.assertIn("INVALID_BIZ_SVC", error_codes)

    def test_sr2026_invalid_msg_def_idr(self):
        xml = self.get_xml_for_sr2026().replace("pacs.008.001.08", "pacs.008.001.07")
        response = self.client.post("/api/validate", json={
            "version": "SR2026",
            "messageType": "pacs.008",
            "xml": xml
        })
        res_json = response.json()
        self.assertEqual(res_json["status"], "FAILED")
        error_codes = [err["code"] for err in res_json["errors"]]
        self.assertIn("INVALID_MSG_DEF_IDR", error_codes)

    def test_sr2026_invalid_biz_msg_idr_format(self):
        # Insert a non-RestrictedFINX character (like '_') into BizMsgIdr
        xml = self.get_xml_for_sr2026().replace("<BizMsgIdr>MSG", "<BizMsgIdr>MSG_")
        response = self.client.post("/api/validate", json={
            "version": "SR2026",
            "messageType": "pacs.008",
            "xml": xml
        })
        res_json = response.json()
        self.assertEqual(res_json["status"], "FAILED")
        error_codes = [err["code"] for err in res_json["errors"]]
        self.assertIn("INVALID_BIZ_MSG_IDR_FORMAT", error_codes)

    def test_sr2026_message_id_mismatch(self):
        # Make BAH BizMsgIdr different from GrpHdr MsgId
        xml = self.get_xml_for_sr2026().replace("<BizMsgIdr>MSG", "<BizMsgIdr>DIFFERENT_MSG")
        response = self.client.post("/api/validate", json={
            "version": "SR2026",
            "messageType": "pacs.008",
            "xml": xml
        })
        res_json = response.json()
        self.assertEqual(res_json["status"], "FAILED")
        error_codes = [err["code"] for err in res_json["errors"]]
        self.assertIn("MESSAGE_ID_MISMATCH", error_codes)

    def test_sr2026_from_bic_mismatch(self):
        # Change the Fr BIC to some other BIC
        xml = self.get_xml_for_sr2026().replace("<BICFI>BOFAUS3NXXX</BICFI>", "<BICFI>AAAAUS3NXXX</BICFI>", 1)
        response = self.client.post("/api/validate", json={
            "version": "SR2026",
            "messageType": "pacs.008",
            "xml": xml
        })
        res_json = response.json()
        self.assertEqual(res_json["status"], "FAILED")
        error_codes = [err["code"] for err in res_json["errors"]]
        self.assertIn("FROM_BIC_MISMATCH", error_codes)

    def test_sr2026_to_bic_mismatch(self):
        # Change the To BIC to some other BIC
        xml = self.get_xml_for_sr2026().replace("<BICFI>BARCGB2DXXX</BICFI>", "<BICFI>ZZZZGB2DXXX</BICFI>", 1)
        response = self.client.post("/api/validate", json={
            "version": "SR2026",
            "messageType": "pacs.008",
            "xml": xml
        })
        res_json = response.json()
        self.assertEqual(res_json["status"], "FAILED")
        error_codes = [err["code"] for err in res_json["errors"]]
        self.assertIn("TO_BIC_MISMATCH", error_codes)

    def test_sr2026_mandatory_instd_amt_missing(self):
        # Remove InstdAmt tag
        xml = re.sub(r"<InstdAmt Ccy=\"CHF\">.*?</InstdAmt>", "", self.get_xml_for_sr2026())
        response = self.client.post("/api/validate", json={
            "version": "SR2026",
            "messageType": "pacs.008",
            "xml": xml
        })
        res_json = response.json()
        self.assertEqual(res_json["status"], "FAILED")
        error_codes = [err["code"] for err in res_json["errors"]]
        self.assertIn("MANDATORY_INSTD_AMT_MISSING", error_codes)

    def test_sr2026_invalid_end_to_end_id(self):
        # Empty EndToEndId
        xml = self.get_xml_for_sr2026().replace("<EndToEndId>E2E5N16BE7WDJUYB5XV</EndToEndId>", "<EndToEndId></EndToEndId>")
        response = self.client.post("/api/validate", json={
            "version": "SR2026",
            "messageType": "pacs.008",
            "xml": xml
        })
        res_json = response.json()
        self.assertEqual(res_json["status"], "FAILED")
        error_codes = [err["code"] for err in res_json["errors"]]
        self.assertIn("INVALID_END_TO_END_ID", error_codes)

    def test_sr2026_invalid_gpi_service_level(self):
        # Insert G002 into SvcLvl code under CdtTrfTxInf
        # Let's add PmtTpInf block to the XML
        xml = self.get_xml_for_sr2026().replace("<IntrBkSttlmAmt", "<PmtTpInf><SvcLvl><Cd>G002</Cd></SvcLvl></PmtTpInf><IntrBkSttlmAmt")
        response = self.client.post("/api/validate", json={
            "version": "SR2026",
            "messageType": "pacs.008",
            "xml": xml
        })
        res_json = response.json()
        self.assertEqual(res_json["status"], "FAILED")
        error_codes = [err["code"] for err in res_json["errors"]]
        self.assertIn("INVALID_GPI_SERVICE_LEVEL", error_codes)

    def test_sr2026_invalid_date_timezone(self):
        # Add timezone to IntrBkSttlmDt
        xml = self.get_xml_for_sr2026().replace("</IntrBkSttlmDt>", "+05:30</IntrBkSttlmDt>")
        response = self.client.post("/api/validate", json={
            "version": "SR2026",
            "messageType": "pacs.008",
            "xml": xml
        })
        res_json = response.json()
        self.assertEqual(res_json["status"], "FAILED")
        error_codes = [err["code"] for err in res_json["errors"]]
        self.assertIn("INVALID_DATE_TIMEZONE", error_codes)

    def test_sr2026_mandatory_proxy_type_missing(self):
        # Add a Prxy block without Tp under CdtrAcct
        xml = self.get_xml_for_sr2026().replace("<CdtrAcct>", "<CdtrAcct><Prxy><Id>12345</Id></Prxy>")
        response = self.client.post("/api/validate", json={
            "version": "SR2026",
            "messageType": "pacs.008",
            "xml": xml
        })
        res_json = response.json()
        self.assertEqual(res_json["status"], "FAILED")
        error_codes = [err["code"] for err in res_json["errors"]]
        self.assertIn("MANDATORY_PROXY_TYPE_MISSING", error_codes)

    def test_sr2026_strd_length_exceeded(self):
        # Add structured remittance > 9000 characters
        long_str = "A" * 9005
        xml = self.get_xml_for_sr2026().replace("</CdtrAcct>", f"</CdtrAcct><RmtInf><Strd><RfrdDocInf><Nb>{long_str}</Nb></RfrdDocInf></Strd></RmtInf>")
        response = self.client.post("/api/validate", json={
            "version": "SR2026",
            "messageType": "pacs.008",
            "xml": xml
        })
        res_json = response.json()
        self.assertEqual(res_json["status"], "FAILED")
        error_codes = [err["code"] for err in res_json["errors"]]
        self.assertIn("STRD_LENGTH_EXCEEDED", error_codes)

if __name__ == "__main__":
    unittest.main()
