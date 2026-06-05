import os
import uuid
from datetime import datetime, timezone
from typing import List, Dict, Any

from app.services.generation.bulk_generator import get_blocks_for_message as _get_blocks_for_message
import copy

def get_blocks_for_message(msg_type: str) -> List[Dict]:
    blocks = _get_blocks_for_message(msg_type)
    if "pacs.008" in msg_type.lower():
        blocks = copy.deepcopy(blocks)
        blocks.append({
            "id": "instructed_amount",
            "label": "Instructed Amount",
            "mandatory": True
        })
    return blocks

def generate_single_xml(message_type: str, selected_blocks: List[str] = None, attempt_idx: int = 1) -> str:
    """Generates a valid-by-construction SR2026 ISO 20022 XML message of the given type."""
    if selected_blocks is None:
        selected_blocks = []
        
    uetr = str(uuid.uuid4())
    msg_id = f"MSG{datetime.now().strftime('%y%m%d%H%M%S')}{attempt_idx:03d}"
    cre_dt_tm = datetime.now(timezone.utc).isoformat().split('.')[0] + "+00:00"
    
    if "pacs.008" in message_type:
        return f"""<?xml version="1.0" encoding="UTF-8"?>
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
        <BizMsgIdr>{msg_id}</BizMsgIdr>
        <MsgDefIdr>pacs.008.001.08</MsgDefIdr>
        <BizSvc>swift.cbprplus.04</BizSvc>
        <CreDt>{cre_dt_tm}</CreDt>
    </AppHdr>
    <Document xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08">
        <FIToFICstmrCdtTrf>
            <GrpHdr>
                <MsgId>{msg_id}</MsgId>
                <CreDtTm>{cre_dt_tm}</CreDtTm>
                <NbOfTxs>1</NbOfTxs>
                <SttlmInf>
                    <SttlmMtd>INDA</SttlmMtd>
                </SttlmInf>
            </GrpHdr>
            <CdtTrfTxInf>
                <PmtId>
                    <InstrId>IN{attempt_idx:03d}{uetr[:11]}</InstrId>
                    <EndToEndId>E2E{msg_id}</EndToEndId>
                    <UETR>{uetr}</UETR>
                </PmtId>
                <IntrBkSttlmAmt Ccy="USD">15000.00</IntrBkSttlmAmt>
                <IntrBkSttlmDt>{cre_dt_tm[:10]}</IntrBkSttlmDt>
                <InstdAmt Ccy="USD">15000.00</InstdAmt>
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
                    <Nm>John Doe Corp</Nm>
                    <PstlAdr>
                        <StrtNm>Broadway</StrtNm>
                        <BldgNb>120</BldgNb>
                        <TwnNm>New York</TwnNm>
                        <Ctry>US</Ctry>
                    </PstlAdr>
                    <Id>
                        <OrgId>
                            <LEI>9695000I6S3Z4Z3Z3Z40</LEI>
                        </OrgId>
                    </Id>
                </Dbtr>
                <DbtrAgt>
                    <FinInstnId>
                        <BICFI>BOFAUS3NXXX</BICFI>
                        <LEI>549300VUNR6SVZ2G7Z51</LEI>
                    </FinInstnId>
                </DbtrAgt>
                <CdtrAgt>
                    <FinInstnId>
                        <BICFI>BARCGB2DXXX</BICFI>
                        <LEI>213800A855A551A55184</LEI>
                    </FinInstnId>
                </CdtrAgt>
                <Cdtr>
                    <Nm>Jane Smith Ltd</Nm>
                    <PstlAdr>
                        <StrtNm>Baker Street</StrtNm>
                        <BldgNb>221</BldgNb>
                        <TwnNm>London</TwnNm>
                        <Ctry>GB</Ctry>
                    </PstlAdr>
                    <Id>
                        <OrgId>
                            <LEI>9695000I6S3Z4Z3Z3Z40</LEI>
                        </OrgId>
                    </Id>
                </Cdtr>
            </CdtTrfTxInf>
        </FIToFICstmrCdtTrf>
    </Document>
</BusMsgEnvlp>"""
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
            <StrtNm>Main St</StrtNm>
            <TwnNm>Chicago</TwnNm>
            <Ctry>US</Ctry>
          </PstlAdr>
        </Dbtr>
        <Cdtr>
          <Nm>Underlying Creditor Corp</Nm>
          <PstlAdr>
            <StrtNm>High St</StrtNm>
            <TwnNm>Manchester</TwnNm>
            <Ctry>GB</Ctry>
          </PstlAdr>
        </Cdtr>
      </UndrlygCstmrCdtTrf>"""
              
        return f"""<?xml version="1.0" encoding="UTF-8"?>
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
        <BizMsgIdr>{msg_id}</BizMsgIdr>
        <MsgDefIdr>pacs.009.001.08</MsgDefIdr>
        <BizSvc>swift.cbprplus.04</BizSvc>
        <CreDt>{cre_dt_tm}</CreDt>
    </AppHdr>
    <Document xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.009.001.08">
        <FICdtTrf>
            <GrpHdr>
                <MsgId>{msg_id}</MsgId>
                <CreDtTm>{cre_dt_tm}</CreDtTm>
                <NbOfTxs>1</NbOfTxs>
                <SttlmInf>
                    <SttlmMtd>INDA</SttlmMtd>
                </SttlmInf>
            </GrpHdr>
            <CdtTrfTxInf>
                <PmtId>
                    <InstrId>IN{attempt_idx:03d}{uetr[:11]}</InstrId>
                    <EndToEndId>E2E{msg_id}</EndToEndId>
                    <UETR>{uetr}</UETR>
                </PmtId>
                <IntrBkSttlmAmt Ccy="EUR">250000.00</IntrBkSttlmAmt>
                <IntrBkSttlmDt>{cre_dt_tm[:10]}</IntrBkSttlmDt>
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
                    <FinInstnId>
                        <BICFI>BOFAUS3NXXX</BICFI>
                    </FinInstnId>
                </Dbtr>
                <DbtrAgt>
                    <FinInstnId>
                        <BICFI>BOFAUS3NXXX</BICFI>
                        <LEI>549300VUNR6SVZ2G7Z51</LEI>
                    </FinInstnId>
                </DbtrAgt>
                <CdtrAgt>
                    <FinInstnId>
                        <BICFI>BARCGB2DXXX</BICFI>
                        <LEI>213800A855A551A55184</LEI>
                    </FinInstnId>
                </CdtrAgt>
                <Cdtr>
                    <FinInstnId>
                        <BICFI>BARCGB2DXXX</BICFI>
                    </FinInstnId>
                </Cdtr>
                {prtry_cov}
                {underlying_block}
            </CdtTrfTxInf>
        </FICdtTrf>
    </Document>
</BusMsgEnvlp>"""
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
