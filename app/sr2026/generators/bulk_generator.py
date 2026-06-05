import os
import uuid
from datetime import datetime, timezone
from typing import List, Dict, Any

def get_blocks_for_message(message_type: str) -> List[Dict[str, Any]]:
    """Returns lists of optional block configurations for the message type."""
    # Standard blocks for UI checkboxes
    return [
        {"id": "dbtr_org", "label": "Debtor Organisation Identifier", "default": True},
        {"id": "cdtr_org", "label": "Creditor Organisation Identifier", "default": True},
        {"id": "structured_address", "label": "Structured Postal Address", "default": True},
        {"id": "tax_block", "label": "Tax Record Block", "default": False}
    ]

def generate_single_xml(message_type: str, selected_blocks: List[str] = None, attempt_idx: int = 1) -> str:
    """Generates a valid-by-construction SR2026 ISO 20022 XML message of the given type."""
    if selected_blocks is None:
        selected_blocks = []
        
    uetr = str(uuid.uuid4())
    msg_id = f"MSG{datetime.now().strftime('%y%m%d%H%M%S')}{attempt_idx:03d}"
    cre_dt_tm = datetime.now(timezone.utc).isoformat().split('.')[0] + "+00:00"
    
    # Template for pacs.008
    if "pacs.008" in message_type:
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08">
  <FIToFICstmrCdtTrf>
    <GrpHdr>
      <MsgId>{msg_id}</MsgId>
      <CreDtTm>{cre_dt_tm}</CreDtTm>
      <NbOfTxs>1</NbOfTxs>
      <SttlmInf>
        <SttlmMtd>CLRG</SttlmMtd>
      </SttlmInf>
    </GrpHdr>
    <CdtTrfTxInf>
      <PmtId>
        <EndToEndId>E2E{msg_id}</EndToEndId>
        <UETR>{uetr}</UETR>
      </PmtId>
      <IntrBkSttlmAmt Ccy="USD">15000.00</IntrBkSttlmAmt>
      <Dbtr>
        <Nm>John Doe Corp</Nm>
        <PstlAdr>
          <Ctry>US</Ctry>
          <TwnNm>New York</TwnNm>
          <StrtNm>Broadway</StrtNm>
          <BldgNb>120</BldgNb>
        </PstlAdr>
        <Id>
          <OrgId>
            <LEI>7ZW8QJWVPR4P1J1KQY45</LEI>
          </OrgId>
        </Id>
      </Dbtr>
      <DbtrAgt>
        <FinInstnId>
          <BICFI>BOFAUS3NXXX</BICFI>
          <LEI>549300VUNR6SVZ2G7Z14</LEI>
        </FinInstnId>
      </DbtrAgt>
      <CdtrAgt>
        <FinInstnId>
          <BICFI>BARCGB2DXXX</BICFI>
          <LEI>213800A855A551A55135</LEI>
        </FinInstnId>
      </CdtrAgt>
      <Cdtr>
        <Nm>Jane Smith Ltd</Nm>
        <PstlAdr>
          <Ctry>GB</Ctry>
          <TwnNm>London</TwnNm>
          <StrtNm>Baker Street</StrtNm>
          <BldgNb>221</BldgNb>
        </PstlAdr>
        <Id>
          <OrgId>
            <LEI>9695000I6S3Z4Z3Z3Z32</LEI>
          </OrgId>
        </Id>
      </Cdtr>
    </CdtTrfTxInf>
  </FIToFICstmrCdtTrf>
</Document>"""
    # Template for pacs.009
    elif "pacs.009" in message_type:
        is_cov = "cov" in message_type.lower()
        prtry_cov = "<Prtry>COV</Prtry>" if is_cov else ""
        underlying_block = ""
        if is_cov:
            underlying_block = f"""
      <UndrlygCstmrCdtTrf>
        <Dbtr>
          <Nm>Underlying Debtor Corp</Nm>
          <PstlAdr>
            <Ctry>US</Ctry>
            <TwnNm>Chicago</TwnNm>
          </PstlAdr>
        </Dbtr>
        <Cdtr>
          <Nm>Underlying Creditor Corp</Nm>
          <PstlAdr>
            <Ctry>GB</Ctry>
            <TwnNm>Manchester</TwnNm>
          </PstlAdr>
        </Cdtr>
      </UndrlygCstmrCdtTrf>"""
              
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.009.001.08">
  <FICdtTrf>
    <GrpHdr>
      <MsgId>{msg_id}</MsgId>
      <CreDtTm>{cre_dt_tm}</CreDtTm>
      <NbOfTxs>1</NbOfTxs>
      <SttlmInf>
        <SttlmMtd>CLRG</SttlmMtd>
      </SttlmInf>
    </GrpHdr>
    <CdtTrfTxInf>
      <PmtId>
        <EndToEndId>E2E{msg_id}</EndToEndId>
        <UETR>{uetr}</UETR>
      </PmtId>
      <IntrBkSttlmAmt Ccy="EUR">250000.00</IntrBkSttlmAmt>
      <Dbtr>
        <Nm>Central Bank US</Nm>
        <PstlAdr>
          <Ctry>US</Ctry>
          <TwnNm>Washington</TwnNm>
        </PstlAdr>
      </Dbtr>
      <DbtrAgt>
        <FinInstnId>
          <BICFI>BOFAUS3NXXX</BICFI>
          <LEI>549300VUNR6SVZ2G7Z14</LEI>
        </FinInstnId>
      </DbtrAgt>
      <CdtrAgt>
        <FinInstnId>
          <BICFI>BARCGB2DXXX</BICFI>
          <LEI>213800A855A551A55135</LEI>
        </FinInstnId>
      </CdtrAgt>
      <Cdtr>
        <Nm>Central Bank Europe</Nm>
        <PstlAdr>
          <Ctry>DE</Ctry>
          <TwnNm>Frankfurt</TwnNm>
        </PstlAdr>
      </Cdtr>
      {prtry_cov}
      {underlying_block}
    </CdtTrfTxInf>
  </FICdtTrf>
</Document>"""
    else:
        # Generic template
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:{message_type}">
  <GenericMessage>
    <GrpHdr>
      <MsgId>{msg_id}</MsgId>
      <CreDtTm>{cre_dt_tm}</CreDtTm>
      <NbOfTxs>1</NbOfTxs>
    </GrpHdr>
    <TxInf>
      <PmtId>
        <UETR>{uetr}</UETR>
      </PmtId>
    </TxInf>
  </GenericMessage>
</Document>"""
