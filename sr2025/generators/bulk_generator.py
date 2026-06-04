"""
Bulk ISO 20022 Message Generator
Generates N valid randomized ISO 20022 messages based on selected message type and blocks.
"""
import uuid
import random
import string
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional
import contextvars

# Context variable to hold selected blocks for deep XML generators
selected_blocks_ctx = contextvars.ContextVar('selected_blocks', default=set())

# ── Random Data Pools ──────────────────────────────────────────────────────────
FIRST_NAMES = ["James", "Emma", "Oliver", "Sophia", "Liam", "Ava", "Noah", "Isabella",
               "Ethan", "Mia", "William", "Charlotte", "Benjamin", "Amelia", "Lucas",
               "Harper", "Mason", "Evelyn", "Logan", "Abigail", "Alexander", "Emily"]
LAST_NAMES = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
              "Davis", "Wilson", "Martinez", "Anderson", "Taylor", "Thomas", "Jackson",
              "White", "Harris", "Martin", "Lewis", "Walker", "Hall", "Allen", "Young"]
COMPANY_NAMES = [
    "Global Trade Corp", "Alpha Finance Ltd", "Pacific Investments SA",
    "Atlantic Capital Group", "Euro Commerce AG", "Northern Bank Holdings",
    "Southern Assets Ltd", "Eastern Capital Management", "Western Finance Corp",
    "Premier Financial Services", "International Trade Solutions", "Apex Capital SA",
    "Summit Bank Holdings", "Meridian Financial Group", "Horizon Investments Ltd",
    "Pinnacle Capital Partners", "Stellar Finance Corp", "Quantum Trading AG",
]
CURRENCIES = ["USD", "EUR", "GBP", "CHF", "CAD", "AUD", "SEK", "NOK", "DKK", "JPY"]
# Countries used for PstlAdr/Ctry (any ISO country)
COUNTRIES = ["GB", "DE", "FR", "CH", "NL", "SE", "NO", "DK", "BE", "AT", "IT", "ES", "PT"]
BIC_BANK_CODES = [
    "AAAA", "BBBB", "CCCC", "DDDD", "EEEE", "FFFF", "GGGG", "HHHH", "IIII", "JJJJ",
    "KKKK", "MMMM", "NNNN", "PPPP", "QQQQ", "RRRR", "SSSS", "TTTT", "UUUU", "VVVV",
    "CITI", "HSBC", "DEUT", "BNPA", "UBSW", "CRED", "BARC", "RBOS", "LLOY", "INGB",
    "ABNA", "RABO", "TRIO", "WEST", "EAST", "NORT", "SOCY", "AGRI", "MUFG", "SMBC",
]
BIC_COUNTRIES = {
    "GB": "2L", "DE": "FF", "FR": "PP", "CH": "SS",
    "NL": "NL", "SE": "SS",
    "NO": "NO", "DK": "KK", "BE": "BB", "AT": "WW", "IT": "MM",
}
CHARGE_BEARERS = ["SHAR", "CRED", "DEBT"]
SETTLEMENT_METHODS = ["INDA", "INGA"]
RETURN_REASONS = ["AC01", "AC04", "AC06", "AG01", "AM04", "CUST", "FF01", "MD01", "MS03", "RR01"]
SERVICE_LEVELS = ["G001", "SDVA", "URGP", "NURG"]
PURPOSE_CODES = ["SALA", "SUPP", "TRAD", "CASH", "COLL", "INTC", "LOAN", "DIVD", "PENS", "OTHR"]
# Only countries that participate in IBAN scheme (ISO 13616) — US, CA, AU, JP do NOT use IBAN
IBAN_COUNTRIES_GB = ["GB", "DE", "FR", "NL", "BE", "AT", "IT", "ES", "PT", "SE", "NO", "DK"]


# ── Random Data Generators ─────────────────────────────────────────────────────

def rng_bic(country_code: Optional[str] = None) -> str:
    """Generate a random valid BIC (11 chars: BANKCCLLXXX)."""
    bank = random.choice(BIC_BANK_CODES)
    # Only use countries that exist in BIC_COUNTRIES map
    if country_code and country_code in BIC_COUNTRIES:
        cc = country_code
        loc = BIC_COUNTRIES[country_code]
    else:
        cc = random.choice(list(BIC_COUNTRIES.keys()))
        loc = BIC_COUNTRIES[cc]
    return f"{bank}{cc}{loc}XXX"


def _iban_check_digits(country: str, bban: str) -> str:
    """Compute valid IBAN check digits using ISO 13616 MOD-97-10 algorithm."""
    # Rearrange: BBAN + country + "00"
    raw = bban + country + "00"
    # Convert letters to numbers (A=10, B=11, ..., Z=35)
    numeric = ''.join(str(ord(c) - 55) if c.isalpha() else c for c in raw)
    # MOD 97 calculation
    remainder = int(numeric) % 97
    check = 98 - remainder
    return f"{check:02d}"


def rng_iban(country: Optional[str] = None) -> str:
    """Generate a random IBAN with valid MOD-97 check digits."""
    c = country or random.choice(IBAN_COUNTRIES_GB)
    # Generate country-specific BBAN, then compute valid check digits
    if c == "GB":
        bank = ''.join(random.choices(string.ascii_uppercase, k=4))
        sort = ''.join(random.choices(string.digits, k=6))
        acct = ''.join(random.choices(string.digits, k=8))
        bban = f"{bank}{sort}{acct}"
    elif c == "DE":
        bban = ''.join(random.choices(string.digits, k=18))
    elif c == "FR":
        bban = ''.join(random.choices(string.digits, k=23))
    elif c == "NL":
        bank = ''.join(random.choices(string.ascii_uppercase, k=4))
        acct = ''.join(random.choices(string.digits, k=10))
        bban = f"{bank}{acct}"
    elif c == "BE":
        bban = ''.join(random.choices(string.digits, k=12))
    elif c == "AT":
        bban = ''.join(random.choices(string.digits, k=16))
    elif c == "IT":
        check_char = random.choice(string.ascii_uppercase)
        bank = ''.join(random.choices(string.digits, k=5))
        branch = ''.join(random.choices(string.digits, k=5))
        acct = ''.join(random.choices(string.digits, k=12))
        bban = f"{check_char}{bank}{branch}{acct}"
    elif c == "ES":
        bban = ''.join(random.choices(string.digits, k=20))
    elif c == "PT":
        bban = ''.join(random.choices(string.digits, k=21))
    elif c == "SE":
        bban = ''.join(random.choices(string.digits, k=20))
    elif c == "NO":
        bban = ''.join(random.choices(string.digits, k=11))
    elif c == "DK":
        bban = ''.join(random.choices(string.digits, k=14))
    else:
        # Fallback: use GB format
        c = "GB"
        bank = ''.join(random.choices(string.ascii_uppercase, k=4))
        sort = ''.join(random.choices(string.digits, k=6))
        acct = ''.join(random.choices(string.digits, k=8))
        bban = f"{bank}{sort}{acct}"
    check = _iban_check_digits(c, bban)
    return f"{c}{check}{bban}"


def rng_amount(currency: str = "USD") -> str:
    """Generate a random valid amount string."""
    if currency == "JPY":
        return str(random.randint(10000, 9999999))
    base = random.uniform(100.0, 500000.0)
    return f"{base:.2f}"


def rng_date(offset_days: int = 0) -> str:
    """Generate a date string. Default is today; use offset_days >= 1 for future dates."""
    # For settlement dates use offset_days=1 to avoid past-date errors
    d = datetime.now(timezone.utc) + timedelta(days=max(offset_days, 0))
    return d.strftime("%Y-%m-%d")


def rng_datetime() -> str:
    """Generate a CBPR+ compliant datetime string.
    CBPR+ rules:
      - No 'Z' UTC indicator (FORBIDDEN)
      - No milliseconds (FORBIDDEN)
      - Must have explicit timezone offset like +00:00
    """
    d = datetime.now(timezone.utc)
    # Use a safe business time (e.g., 10 AM) to avoid daily cutoff errors (CUT001) during generation
    return d.strftime("%Y-%m-%dT10:00:00+00:00")


def rng_currency_and_country() -> tuple[str, str]:
    """Returns a valid (currency, country_code) pair that will pass business rules."""
    curr_map = {
        "GB": "GBP", "DE": "EUR", "FR": "EUR", "NL": "EUR", "BE": "EUR",
        "AT": "EUR", "IT": "EUR", "ES": "EUR", "PT": "EUR", "SE": "SEK",
        "NO": "NOK", "DK": "DKK", "CH": "CHF", "US": "USD", "CA": "CAD"
    }
    country = random.choice(list(curr_map.keys()))
    return curr_map[country], country


def rng_id(prefix: str = "", length: int = 16, max_total: int = 35) -> str:
    """Generate a random ID with prefix. Total length capped at max_total (default 35 for ISO 20022)."""
    chars = string.ascii_uppercase + string.digits
    # Ensure total length does not exceed max_total
    available = max(1, max_total - len(prefix))
    actual_len = min(length, available)
    return f"{prefix}{''.join(random.choices(chars, k=actual_len))}"


def rng_uetr() -> str:
    return str(uuid.uuid4())


def rng_name() -> str:
    if random.random() > 0.4:
        return f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"
    return random.choice(COMPANY_NAMES)


def rng_currency() -> str:
    return random.choice(CURRENCIES)


def rng_country() -> str:
    return random.choice(COUNTRIES)


# ── XML Helpers ────────────────────────────────────────────────────────────────

def xe(val: str) -> str:
    """XML-escape a string."""
    return (val or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def tabs(n: int) -> str:
    return '\t' * n


def el(tag: str, val: str, indent: int = 3) -> str:
    if not val or not str(val).strip():
        return ''
    return f"{tabs(indent)}<{tag}>{xe(str(val))}</{tag}>\n"


def tag_wrap(tag: str, content: str, indent: int = 3) -> str:
    if not content or not content.strip():
        return ''
    return f"{tabs(indent)}<{tag}>\n{content}{tabs(indent)}</{tag}>\n"


def _rng_pstl_adr(indent: int, country: str = None) -> str:
    t2 = tabs(indent)
    t3 = tabs(indent + 1)
    c = country or rng_country()
    
    xml = f"{t2}<PstlAdr>\n"
    # Always use structured address — AdrLine alongside structured fields triggers a
    # broken rule (CBPR_GracePeriod_Unstructured_FormalRule) that unconditionally caps
    # AdrLine at 35 chars even when TwnNm+Ctry are present. Structured-only is safe.
    xml += f"{t3}<StrtNm>{xe(random.choice(LAST_NAMES))} Street</StrtNm>\n"
    xml += f"{t3}<BldgNb>{random.randint(1, 999)}</BldgNb>\n"
    if random.random() > 0.5:
        xml += f"{t3}<PstCd>{random.randint(10000, 99999)}</PstCd>\n"
    xml += f"{t3}<TwnNm>{xe(random.choice(LAST_NAMES))} City</TwnNm>\n"
    xml += f"{t3}<Ctry>{xe(c)}</Ctry>\n"
    xml += f"{t2}</PstlAdr>\n"
    return xml


def _rng_pstl_adr_cov(indent: int, country: str = None) -> str:
    t2 = tabs(indent)
    t3 = tabs(indent + 1)
    c = country or rng_country()
    
    xml = f"{t2}<PstlAdr>\n"
    # Always use structured address — same reason as _rng_pstl_adr (AdrLine triggers
    # a broken 35-char rule unconditionally regardless of coexisting structured fields).
    xml += f"{t3}<StrtNm>{xe(random.choice(LAST_NAMES))} Street</StrtNm>\n"
    xml += f"{t3}<BldgNb>{random.randint(1, 999)}</BldgNb>\n"
    if random.random() > 0.5:
        xml += f"{t3}<PstCd>{random.randint(10000, 99999)}</PstCd>\n"
    xml += f"{t3}<TwnNm>{xe(random.choice(LAST_NAMES))} City</TwnNm>\n"
    xml += f"{t3}<Ctry>{xe(c)}</Ctry>\n"
    xml += f"{t2}</PstlAdr>\n"
    return xml


def _validate_postal_address(xml_content: str):
    from lxml import etree
    try:
        parser = etree.XMLParser(recover=True, no_network=True, resolve_entities=False)
        root = etree.fromstring(xml_content.encode('utf-8'), parser)
    except Exception as e:
        raise ValueError(f"XML parsing error during pre-validation: {str(e)}")

    pstl_adrs = root.xpath("//*[local-name()='PstlAdr']")

    for pstl_adr in pstl_adrs:
        all_children = list(pstl_adr)
        adr_lines = [c for c in all_children if c.tag.split('}')[-1] == 'AdrLine']
        adr_line_count = len(adr_lines)

        if adr_line_count > 2:
            raise ValueError(f"Postal Address Validation Error: A maximum of two occurrences of Address Line are allowed. Found {adr_line_count}.")

        # CBPR+ E001: If AdrLine is absent, TwnNm and Ctry are mandatory.
        # If AdrLine is present, TwnNm and Ctry are optional.
        if adr_line_count == 0:
            twn_nm = pstl_adr.xpath("*[local-name()='TwnNm']")
            ctry = pstl_adr.xpath("*[local-name()='Ctry']")
            if not twn_nm:
                raise ValueError(
                    "Postal Address Validation Error: If Address Line is absent, Town Name is mandatory (CBPR+ E001)."
                )
            if not ctry:
                raise ValueError(
                    "Postal Address Validation Error: If Address Line is absent, Country is mandatory (CBPR+ E001)."
                )


def _normalize_postal_addresses(xml_content: str) -> str:
    """
    Post-process any generated XML and ensure every <PstlAdr> block complies with CBPR+ rules:
      1. Maximum 2 <AdrLine> elements (extras removed).
      2. Hybrid mode (TwnNm + Ctry + AdrLine) is explicitly supported and valid.
         If AdrLine coexists with DETAIL structured fields (StrtNm, BldgNb, BldgNm,
         Flr, PstBx, Room, PstCd, Dept, SubDept, TwnLctnNm, DstrctNm, CtrySubDvsn)
         those detail fields are removed. TwnNm and Ctry are kept alongside AdrLine.
      3. If AdrLine is present, TwnNm and Ctry are mandatory (hybrid rule).
      4. If AdrLine is absent, TwnNm and Ctry are mandatory (structured rule).
      5. Re-order all children to match the canonical CBPR+ schema sequence.
    Only re-serializes if changes were actually made (preserves namespace declarations).
    """
    from lxml import etree
    try:
        parser = etree.XMLParser(recover=True, no_network=True, resolve_entities=False)
        root = etree.fromstring(xml_content.encode('utf-8'), parser)
    except Exception:
        return xml_content

    changed = False

    SCHEMA_SEQUENCE = [
        "Dept", "SubDept", "StrtNm", "BldgNb", "BldgNm", "Flr", "PstCd",
        "TwnLctnNm", "DstrctNm", "CtrySubDvsn", "TwnNm", "Ctry", "AdrLine"
    ]
    seq_map = {name: idx for idx, name in enumerate(SCHEMA_SEQUENCE)}



    for pstl_adr in root.iter():
        if not isinstance(pstl_adr.tag, str):
            continue
        local = pstl_adr.tag.split('}')[-1] if '}' in pstl_adr.tag else pstl_adr.tag
        if local != 'PstlAdr':
            continue

        # Derive namespace prefix for creating new elements
        ns_prefix = pstl_adr.tag.split('}')[0] + '}' if '}' in pstl_adr.tag else ''

        # Take a snapshot of current children
        children = list(pstl_adr)

        def _ltag(el):
            return el.tag.split('}')[-1] if '}' in el.tag else el.tag

        # --- Step 1: Restrict to max 2 AdrLine entries ---
        adr_lines = [c for c in children if _ltag(c) == 'AdrLine']
        if len(adr_lines) > 2:
            for extra in adr_lines[2:]:
                pstl_adr.remove(extra)
            changed = True
            children = list(pstl_adr)
            adr_lines = adr_lines[:2]

        # --- Step 1b: Hybrid rule — AdrLine may coexist with TwnNm + Ctry (hybrid mode).
        #   However DETAIL structured fields (StrtNm, BldgNb, BldgNm, Flr, PstBx, Room,
        #   PstCd, Dept, SubDept, TwnLctnNm, DstrctNm, CtrySubDvsn) must NOT appear
        #   alongside AdrLine. Strip those detail fields; keep TwnNm and Ctry. ---
        DETAIL_STRUCTURED_TAGS = {'StrtNm', 'BldgNb', 'BldgNm', 'Flr', 'PstBx', 'Room',
                                  'PstCd', 'Dept', 'SubDept', 'TwnLctnNm', 'DstrctNm', 'CtrySubDvsn'}
        if adr_lines:
            detail_children = [c for c in children if _ltag(c) in DETAIL_STRUCTURED_TAGS]
            if detail_children:
                for dc in detail_children:
                    pstl_adr.remove(dc)
                changed = True
                children = list(pstl_adr)

        # --- Step 2: TwnNm + Ctry mandatory in both hybrid (AdrLine present) and
        #   pure structured (AdrLine absent) modes ---
        if True:
            def _get(tag_name):
                return next((c for c in children if _ltag(c) == tag_name), None)

            twn_el = _get('TwnNm')
            if twn_el is None:
                twn_el = etree.SubElement(pstl_adr, f'{ns_prefix}TwnNm')
                twn_el.text = 'New York'
                changed = True
            elif not twn_el.text or not twn_el.text.strip():
                twn_el.text = 'New York'
                changed = True

            ctry_el = _get('Ctry')
            if ctry_el is None:
                ctry_el = etree.SubElement(pstl_adr, f'{ns_prefix}Ctry')
                ctry_el.text = 'US'
                changed = True
            elif not ctry_el.text or not ctry_el.text.strip():
                ctry_el.text = 'US'
                changed = True

            children = list(pstl_adr)

        # --- Step 3: Re-order children per CBPR+ schema sequence ---
        children_sorted = sorted(
            children,
            key=lambda c: seq_map.get(_ltag(c), 99)
        )
        if [id(c) for c in children] != [id(c) for c in children_sorted]:
            for c in children:
                pstl_adr.remove(c)
            for c in children_sorted:
                pstl_adr.append(c)
            changed = True

    if not changed:
        return xml_content
    result = etree.tostring(root, encoding='unicode')
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + result


def _normalize_cbpr_r9(xml_content: str) -> str:
    """
    CBPR_COM_R9: If BICFI is present in FinInstnId, then Nm and PstlAdr must NOT appear.
    Remove any Nm and PstlAdr children from FinInstnId elements that also have a BICFI.
    """
    from lxml import etree
    try:
        parser = etree.XMLParser(recover=True, no_network=True, resolve_entities=False)
        root = etree.fromstring(xml_content.encode('utf-8'), parser)
    except Exception:
        return xml_content

    changed = False
    for fi_id in root.iter():
        if not isinstance(fi_id.tag, str):
            continue
        local = fi_id.tag.split('}')[-1] if '}' in fi_id.tag else fi_id.tag
        if local != 'FinInstnId':
            continue
        bicfi_els = [c for c in fi_id
                     if (c.tag.split('}')[-1] if '}' in c.tag else c.tag) == 'BICFI'
                     and c.text and c.text.strip()]
        if not bicfi_els:
            continue
        for c in list(fi_id):
            ltag = c.tag.split('}')[-1] if '}' in c.tag else c.tag
            if ltag in ('Nm', 'PstlAdr'):
                fi_id.remove(c)
                changed = True

    if not changed:
        return xml_content
    result = etree.tostring(root, encoding='unicode')
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + result


def _validate_charges_information(xml_content: str):
    from lxml import etree
    try:
        parser = etree.XMLParser(recover=True, no_network=True, resolve_entities=False)
        root = etree.fromstring(xml_content.encode('utf-8'), parser)
    except Exception as e:
        raise ValueError(f"XML parsing error during pre-validation: {str(e)}")

    chrg_br_els = root.xpath("//*[local-name()='ChrgBr']")
    for chrg_br_el in chrg_br_els:
        if chrg_br_el.text == "CRED":
            parent = chrg_br_el.getparent()
            chrgs_inf = parent.xpath("*[local-name()='ChrgsInf']")
            if not chrgs_inf:
                raise ValueError("Charges Information Validation Error: Charge information is mandatory if CRED is present.")
            
            for chg in chrgs_inf:
                amt_els = chg.xpath("*[local-name()='Amt']")
                if not amt_els:
                    raise ValueError("Charges Information Validation Error: ChrgsInf must contain Amt.")
                amt_el = amt_els[0]
                ccy = amt_el.get("Ccy")
                if not ccy:
                    raise ValueError("Charges Information Validation Error: Charges Amt must specify Ccy (Currency).")
                
                agt_els = chg.xpath("*[local-name()='Agt']")
                if not agt_els:
                    raise ValueError("Charges Information Validation Error: ChrgsInf must contain Agt (Agent Details).")
                
                fin_instn = agt_els[0].xpath("*[local-name()='FinInstnId']")
                if not fin_instn:
                    raise ValueError("Charges Information Validation Error: Agt must contain FinInstnId.")
                
                bic = fin_instn[0].xpath("*[local-name()='BICFI']")
                if not bic or not bic[0].text:
                    raise ValueError("Charges Information Validation Error: FinInstnId must contain a valid BICFI.")


def _normalize_charges_information(xml_content: str) -> str:
    from lxml import etree
    try:
        parser = etree.XMLParser(recover=True, no_network=True, resolve_entities=False)
        root = etree.fromstring(xml_content.encode('utf-8'), parser)
    except Exception as e:
        return xml_content

    chrg_br_els = root.xpath("//*[local-name()='ChrgBr']")
    for chrg_br_el in chrg_br_els:
        if chrg_br_el.text == "CRED":
            parent = chrg_br_el.getparent()
            ns = ""
            if parent.tag.startswith('{'):
                ns = parent.tag.split('}')[0] + '}'

            chrgs_inf_els = parent.xpath("*[local-name()='ChrgsInf']")
            if not chrgs_inf_els:
                idx = parent.index(chrg_br_el)
                ccy = "EUR"
                amt_els = parent.xpath("./*[local-name()='IntrBkSttlmAmt' or local-name()='InstdAmt' or local-name()='RtrdIntrBkSttlmAmt']")
                if amt_els and amt_els[0].get("Ccy"):
                    ccy = amt_els[0].get("Ccy")

                new_chg = etree.Element(f"{ns}ChrgsInf")
                new_amt = etree.Element(f"{ns}Amt", Ccy=ccy)
                new_amt.text = "0.00"
                new_chg.append(new_amt)
                
                new_agt = etree.Element(f"{ns}Agt")
                new_fin = etree.Element(f"{ns}FinInstnId")
                new_bic = etree.Element(f"{ns}BICFI")
                new_bic.text = "BANKDEFFXXX"
                new_fin.append(new_bic)
                new_agt.append(new_fin)
                new_chg.append(new_agt)
                
                parent.insert(idx + 1, new_chg)
            else:
                for chg in chrgs_inf_els:
                    amt_els = chg.xpath("*[local-name()='Amt']")
                    if not amt_els:
                        ccy = "EUR"
                        amt_els_tx = parent.xpath("//*[local-name()='RtrdIntrBkSttlmAmt']")
                        if amt_els_tx and amt_els_tx[0].get("Ccy"):
                            ccy = amt_els_tx[0].get("Ccy")
                        new_amt = etree.Element(f"{ns}Amt", Ccy=ccy)
                        new_amt.text = "0.00"
                        chg.insert(0, new_amt)
                    else:
                        amt_el = amt_els[0]
                        if not amt_el.text or not amt_el.text.strip():
                            amt_el.text = "0.00"
                        if not amt_el.get("Ccy"):
                            ccy = "EUR"
                            amt_els_tx = parent.xpath("//*[local-name()='RtrdIntrBkSttlmAmt']")
                            if amt_els_tx and amt_els_tx[0].get("Ccy"):
                                ccy = amt_els_tx[0].get("Ccy")
                            amt_el.set("Ccy", ccy)
                    
                    agt_els = chg.xpath("*[local-name()='Agt']")
                    if not agt_els:
                        new_agt = etree.Element(f"{ns}Agt")
                        new_fin = etree.Element(f"{ns}FinInstnId")
                        new_bic = etree.Element(f"{ns}BICFI")
                        new_bic.text = "BANKDEFFXXX"
                        new_fin.append(new_bic)
                        new_agt.append(new_fin)
                        chg.append(new_agt)
                    else:
                        agt = agt_els[0]
                        fin_instn = agt.xpath("*[local-name()='FinInstnId']")
                        if not fin_instn:
                            new_fin = etree.Element(f"{ns}FinInstnId")
                            new_bic = etree.Element(f"{ns}BICFI")
                            new_bic.text = "BANKDEFFXXX"
                            new_fin.append(new_bic)
                            agt.append(new_fin)
                        else:
                            fin = fin_instn[0]
                            bic = fin.xpath("*[local-name()='BICFI']")
                            if not bic:
                                new_bic = etree.Element(f"{ns}BICFI")
                                new_bic.text = "BANKDEFFXXX"
                                fin.append(new_bic)
                            elif not bic[0].text or not bic[0].text.strip():
                                bic[0].text = "BANKDEFFXXX"

    result = etree.tostring(root, encoding='unicode')
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + result


def agent_xml(tag: str, bic: str, indent: int = 4) -> str:
    t = tabs(indent)
    t1 = tabs(indent + 1)
    t2 = tabs(indent + 2)
    
    # CBPR_1A: if BICFI is present, Nm and PstlAdr are NOT allowed.
    xml = f"{t}<{tag}>\n{t1}<FinInstnId>\n{t2}<BICFI>{xe(bic)}</BICFI>\n"
    xml += f"{t1}</FinInstnId>\n{t}</{tag}>\n"
    return xml


def _fi_party(tag: str, bic: str, country: str = None, indent: int = 4) -> str:  # noqa: ARG001
    """
    Build an FI-type party (e.g. Dbtr/Cdtr in pacs.009) with BICFI only.
    CBPR_COM_R9: when BICFI is present, Nm and PstlAdr must NOT be present.
    """
    t = tabs(indent); t1 = tabs(indent + 1); t2 = tabs(indent + 2)
    return (f"{t}<{tag}>\n{t1}<FinInstnId>\n"
            f"{t2}<BICFI>{xe(bic)}</BICFI>\n"
            f"{t1}</FinInstnId>\n{t}</{tag}>\n")


def account_xml(tag: str, iban: str, indent: int = 4) -> str:
    t = tabs(indent)
    t1 = tabs(indent + 1)
    t2 = tabs(indent + 2)
    return f"{t}<{tag}>\n{t1}<Id>\n{t2}<IBAN>{xe(iban)}</IBAN>\n{t1}</Id>\n{t}</{tag}>\n"


def account_othr_xml(tag: str, acct_id: str, indent: int = 4) -> str:
    t = tabs(indent)
    t1 = tabs(indent + 1)
    t2 = tabs(indent + 2)
    t3 = tabs(indent + 3)
    return f"{t}<{tag}>\n{t1}<Id>\n{t2}<Othr>\n{t3}<Id>{xe(acct_id)}</Id>\n{t2}</Othr>\n{t1}</Id>\n{t}</{tag}>\n"


def party_xml(tag: str, name: str, country: str, indent: int = 4) -> str:
    t = tabs(indent)
    t1 = tabs(indent + 1)
    
    content = f"{t1}<Nm>{xe(name)}</Nm>\n"
    
    # Always include full address (StrtNm, BldgNb, PstCd, TwnNm, Ctry, AdrLine)
    # CBPR+ rule: If PstlAdr is used and AdrLine is absent, TwnNm + Ctry are mandatory
    content += _rng_pstl_adr(indent + 1, country)
        
    return f"{t}<{tag}>\n{content}{t}</{tag}>\n"


def apphdr_fi(bic: str) -> str:
    return f"\t\t\t<FIId>\n\t\t\t\t<FinInstnId>\n\t\t\t\t\t<BICFI>{xe(bic)}</BICFI>\n\t\t\t\t</FinInstnId>\n\t\t\t</FIId>\n"


# ── Message Block Definitions ──────────────────────────────────────────────────

MESSAGE_BLOCKS: Dict[str, List[Dict]] = {
    "pacs.008": [
        {"id": "instructing_agent",          "label": "Instructing Agent",           "mandatory": True},
        {"id": "instructed_agent",           "label": "Instructed Agent",            "mandatory": True},
        {"id": "debtor_agent",               "label": "Debtor Agent",                "mandatory": True},
        {"id": "debtor",                     "label": "Debtor",                      "mandatory": True},
        {"id": "debtor_account",             "label": "Debtor Account",              "mandatory": True,  "requires": ["debtor"]},
        {"id": "creditor_agent",             "label": "Creditor Agent",              "mandatory": True},
        {"id": "creditor",                   "label": "Creditor",                    "mandatory": True},
        {"id": "creditor_account",           "label": "Creditor Account",            "mandatory": True,  "requires": ["creditor"]},
        {"id": "previous_instructing_agent_1","label": "Previous Instructing Agent 1","mandatory": False},
        {"id": "previous_instructing_agent_2","label": "Previous Instructing Agent 2","mandatory": False, "requires": ["previous_instructing_agent_1"]},
        {"id": "previous_instructing_agent_3","label": "Previous Instructing Agent 3","mandatory": False, "requires": ["previous_instructing_agent_2"]},
        {"id": "intermediary_agent_1",       "label": "Intermediary Agent 1",        "mandatory": False},
        {"id": "intermediary_agent_1_account","label": "Intermediary Agent 1 Account","mandatory": False, "requires": ["intermediary_agent_1"]},
        {"id": "intermediary_agent_2",       "label": "Intermediary Agent 2",        "mandatory": False, "requires": ["intermediary_agent_1"]},
        {"id": "intermediary_agent_2_account","label": "Intermediary Agent 2 Account","mandatory": False, "requires": ["intermediary_agent_2"]},
        {"id": "intermediary_agent_3",       "label": "Intermediary Agent 3",        "mandatory": False, "requires": ["intermediary_agent_2"]},
        {"id": "intermediary_agent_3_account","label": "Intermediary Agent 3 Account","mandatory": False, "requires": ["intermediary_agent_3"]},
        {"id": "debtor_agent_account",       "label": "Debtor Agent Account",        "mandatory": False, "requires": ["debtor_agent"]},
        {"id": "creditor_agent_account",     "label": "Creditor Agent Account",      "mandatory": False, "requires": ["creditor_agent"]},
        {"id": "ultimate_debtor",            "label": "Ultimate Debtor",             "mandatory": False},
        {"id": "ultimate_creditor",          "label": "Ultimate Creditor",           "mandatory": False},
        {"id": "payment_type_information",   "label": "Payment Type Information",    "mandatory": False},
        {"id": "remittance_information",     "label": "Remittance Information",      "mandatory": False},
        {"id": "charges_information",        "label": "Charges Information",         "mandatory": False},
        {"id": "settlement_time_request",    "label": "Settlement Time Request",     "mandatory": False},
    ],
    "pacs.009": [
        {"id": "instructing_agent",          "label": "Instructing Agent",           "mandatory": True},
        {"id": "instructed_agent",           "label": "Instructed Agent",            "mandatory": True},
        {"id": "debtor",                     "label": "Debtor (FI)",                 "mandatory": True},
        {"id": "debtor_account",             "label": "Debtor Account",              "mandatory": False},
        {"id": "debtor_agent",               "label": "Debtor Agent",                "mandatory": False},
        {"id": "debtor_agent_account",       "label": "Debtor Agent Account",        "mandatory": False, "requires": ["debtor_agent"]},
        {"id": "creditor_agent",             "label": "Creditor Agent",              "mandatory": False},
        {"id": "creditor_agent_account",     "label": "Creditor Agent Account",      "mandatory": False, "requires": ["creditor_agent"]},
        {"id": "creditor",                   "label": "Creditor (FI)",               "mandatory": True},
        {"id": "creditor_account",           "label": "Creditor Account",            "mandatory": False},
        {"id": "previous_instructing_agent_1","label": "Previous Instructing Agent 1","mandatory": False},
        {"id": "previous_instructing_agent_2","label": "Previous Instructing Agent 2","mandatory": False, "requires": ["previous_instructing_agent_1"]},
        {"id": "previous_instructing_agent_3","label": "Previous Instructing Agent 3","mandatory": False, "requires": ["previous_instructing_agent_2"]},
        {"id": "intermediary_agent_1",       "label": "Intermediary Agent 1",        "mandatory": False, "requires": ["creditor_agent"]},
        {"id": "intermediary_agent_1_account","label": "Intermediary Agent 1 Account","mandatory": False, "requires": ["intermediary_agent_1"]},
        {"id": "intermediary_agent_2",       "label": "Intermediary Agent 2",        "mandatory": False, "requires": ["intermediary_agent_1"]},
        {"id": "intermediary_agent_2_account","label": "Intermediary Agent 2 Account","mandatory": False, "requires": ["intermediary_agent_2"]},
        {"id": "intermediary_agent_3",       "label": "Intermediary Agent 3",        "mandatory": False, "requires": ["intermediary_agent_2"]},
        {"id": "intermediary_agent_3_account","label": "Intermediary Agent 3 Account","mandatory": False, "requires": ["intermediary_agent_3"]},
        {"id": "payment_type_information",   "label": "Payment Type Information",    "mandatory": False},
        {"id": "remittance_information",     "label": "Remittance Information",      "mandatory": False},
        {"id": "settlement_time_request",    "label": "Settlement Time Request",     "mandatory": False},
    ],
    "pacs.009.adv": [
        {"id": "instructing_agent",          "label": "Instructing Agent",           "mandatory": True},
        {"id": "instructed_agent",           "label": "Instructed Agent",            "mandatory": True},
        {"id": "debtor",                     "label": "Debtor (FI)",                 "mandatory": True},
        {"id": "debtor_account",             "label": "Debtor Account",              "mandatory": False},
        {"id": "debtor_agent",               "label": "Debtor Agent",                "mandatory": False},
        {"id": "debtor_agent_account",       "label": "Debtor Agent Account",        "mandatory": False, "requires": ["debtor_agent"]},
        {"id": "creditor_agent",             "label": "Creditor Agent",              "mandatory": False},
        {"id": "creditor_agent_account",     "label": "Creditor Agent Account",      "mandatory": False, "requires": ["creditor_agent"]},
        {"id": "creditor",                   "label": "Creditor (FI)",               "mandatory": True},
        {"id": "creditor_account",           "label": "Creditor Account",            "mandatory": False},
        {"id": "previous_instructing_agent_1","label": "Previous Instructing Agent 1","mandatory": False},
        {"id": "previous_instructing_agent_2","label": "Previous Instructing Agent 2","mandatory": False, "requires": ["previous_instructing_agent_1"]},
        {"id": "previous_instructing_agent_3","label": "Previous Instructing Agent 3","mandatory": False, "requires": ["previous_instructing_agent_2"]},
        {"id": "intermediary_agent_1",       "label": "Intermediary Agent 1",        "mandatory": False, "requires": ["creditor_agent"]},
        {"id": "intermediary_agent_1_account","label": "Intermediary Agent 1 Account","mandatory": False, "requires": ["intermediary_agent_1"]},
        {"id": "intermediary_agent_2",       "label": "Intermediary Agent 2",        "mandatory": False, "requires": ["intermediary_agent_1"]},
        {"id": "intermediary_agent_2_account","label": "Intermediary Agent 2 Account","mandatory": False, "requires": ["intermediary_agent_2"]},
        {"id": "intermediary_agent_3",       "label": "Intermediary Agent 3",        "mandatory": False, "requires": ["intermediary_agent_2"]},
        {"id": "intermediary_agent_3_account","label": "Intermediary Agent 3 Account","mandatory": False, "requires": ["intermediary_agent_3"]},
        {"id": "payment_type_information",   "label": "Payment Type Information",    "mandatory": False},
        {"id": "remittance_information",     "label": "Remittance Information",      "mandatory": False},
        {"id": "settlement_time_request",    "label": "Settlement Time Request",     "mandatory": False},
    ],
    "pacs.009.cov": [
        {"id": "instructing_agent",          "label": "Instructing Agent",           "mandatory": True},
        {"id": "instructed_agent",           "label": "Instructed Agent",            "mandatory": True},
        {"id": "debtor",                     "label": "Debtor (FI)",                 "mandatory": True},
        {"id": "debtor_account",             "label": "Debtor Account",              "mandatory": False},
        {"id": "debtor_agent",               "label": "Debtor Agent",                "mandatory": False},
        {"id": "debtor_agent_account",       "label": "Debtor Agent Account",        "mandatory": False, "requires": ["debtor_agent"]},
        {"id": "creditor_agent",             "label": "Creditor Agent",              "mandatory": False},
        {"id": "creditor_agent_account",     "label": "Creditor Agent Account",      "mandatory": False, "requires": ["creditor_agent"]},
        {"id": "creditor",                   "label": "Creditor (FI)",               "mandatory": True},
        {"id": "creditor_account",           "label": "Creditor Account",            "mandatory": False},
        {"id": "previous_instructing_agent_1","label": "Previous Instructing Agent 1","mandatory": False},
        {"id": "previous_instructing_agent_2","label": "Previous Instructing Agent 2","mandatory": False, "requires": ["previous_instructing_agent_1"]},
        {"id": "previous_instructing_agent_3","label": "Previous Instructing Agent 3","mandatory": False, "requires": ["previous_instructing_agent_2"]},
        {"id": "intermediary_agent_1",       "label": "Intermediary Agent 1",        "mandatory": False, "requires": ["creditor_agent"]},
        {"id": "intermediary_agent_1_account","label": "Intermediary Agent 1 Account","mandatory": False, "requires": ["intermediary_agent_1"]},
        {"id": "intermediary_agent_2",       "label": "Intermediary Agent 2",        "mandatory": False, "requires": ["intermediary_agent_1"]},
        {"id": "intermediary_agent_2_account","label": "Intermediary Agent 2 Account","mandatory": False, "requires": ["intermediary_agent_2"]},
        {"id": "intermediary_agent_3",       "label": "Intermediary Agent 3",        "mandatory": False, "requires": ["intermediary_agent_2"]},
        {"id": "intermediary_agent_3_account","label": "Intermediary Agent 3 Account","mandatory": False, "requires": ["intermediary_agent_3"]},
        {"id": "underlying_customer_credit_transfer", "label": "Underlying Customer Credit Transfer (COV)", "mandatory": True},
        {"id": "settlement_time_request",    "label": "Settlement Time Request",     "mandatory": False},
        {"id": "payment_type_information",   "label": "Payment Type Information",    "mandatory": False},
        {"id": "remittance_information",     "label": "Remittance Information",      "mandatory": False},
    ],
    "pacs.004": [
        {"id": "instructing_agent",          "label": "Instructing Agent",           "mandatory": True},
        {"id": "instructed_agent",           "label": "Instructed Agent",            "mandatory": True},
        {"id": "debtor_agent",               "label": "Return Debtor Agent",         "mandatory": True},
        {"id": "debtor",                     "label": "Return Debtor",               "mandatory": True},
        {"id": "debtor_account",             "label": "Return Debtor Account",       "mandatory": False, "requires": ["debtor"]},
        {"id": "creditor_agent",             "label": "Return Creditor Agent",       "mandatory": True},
        {"id": "creditor",                   "label": "Return Creditor",             "mandatory": True},
        {"id": "creditor_account",           "label": "Return Creditor Account",     "mandatory": False, "requires": ["creditor"]},
        {"id": "intermediary_agent_1",       "label": "Intermediary Agent 1",        "mandatory": False},
        {"id": "ultimate_debtor",            "label": "Ultimate Debtor",             "mandatory": False},
        {"id": "ultimate_creditor",          "label": "Ultimate Creditor",           "mandatory": False},
        {"id": "charges_information",        "label": "Charges Information",         "mandatory": False},
        {"id": "remittance_information",     "label": "Remittance Information",      "mandatory": False},
    ],
    "pacs.003": [
        {"id": "instructing_agent",          "label": "Instructing Agent",           "mandatory": True},
        {"id": "instructed_agent",           "label": "Instructed Agent",            "mandatory": True},
        {"id": "debtor_agent",               "label": "Debtor Agent",                "mandatory": True},
        {"id": "debtor",                     "label": "Debtor",                      "mandatory": True},
        {"id": "debtor_account",             "label": "Debtor Account",              "mandatory": True,  "requires": ["debtor"]},
        {"id": "creditor_agent",             "label": "Creditor Agent",              "mandatory": True},
        {"id": "creditor",                   "label": "Creditor",                    "mandatory": True},
        {"id": "creditor_account",           "label": "Creditor Account",            "mandatory": True,  "requires": ["creditor"]},
        {"id": "creditor_agent_account",     "label": "Creditor Agent Account",      "mandatory": False, "requires": ["creditor_agent"]},
        {"id": "direct_debit_transaction",   "label": "Direct Debit Transaction",    "mandatory": False},
        {"id": "intermediary_agent_1",       "label": "Intermediary Agent 1",        "mandatory": False},
        {"id": "intermediary_agent_2",       "label": "Intermediary Agent 2",        "mandatory": False, "requires": ["intermediary_agent_1"]},
        {"id": "intermediary_agent_3",       "label": "Intermediary Agent 3",        "mandatory": False, "requires": ["intermediary_agent_2"]},
        {"id": "ultimate_debtor",            "label": "Ultimate Debtor",             "mandatory": False},
        {"id": "ultimate_creditor",          "label": "Ultimate Creditor",           "mandatory": False},
        {"id": "payment_type_information",   "label": "Payment Type Information",    "mandatory": False},
        {"id": "remittance_information",     "label": "Remittance Information",      "mandatory": False},
        {"id": "charges_information",        "label": "Charges Information",         "mandatory": False},
    ],
    "pacs.002": [
        {"id": "instructing_agent",          "label": "Instructing Agent",           "mandatory": True},
        {"id": "instructed_agent",           "label": "Instructed Agent",            "mandatory": True},
        {"id": "debtor",                     "label": "Debtor",                      "mandatory": False},
        {"id": "creditor",                   "label": "Creditor",                    "mandatory": False},
    ],
    "pacs.010": [
        {"id": "instructing_agent",          "label": "Instructing Agent",           "mandatory": True},
        {"id": "instructed_agent",           "label": "Instructed Agent",            "mandatory": True},
        {"id": "debtor_agent",               "label": "Debtor Agent",                "mandatory": True},
        {"id": "debtor",                     "label": "Debtor",                      "mandatory": True},
        {"id": "debtor_account",             "label": "Debtor Account",              "mandatory": True,  "requires": ["debtor"]},
        {"id": "creditor_agent",             "label": "Creditor Agent",              "mandatory": False},
        {"id": "creditor",                   "label": "Creditor",                    "mandatory": False},
        {"id": "creditor_account",           "label": "Creditor Account",            "mandatory": False, "requires": ["creditor"]},
        {"id": "payment_type_information",   "label": "Payment Type Information",    "mandatory": False},
        {"id": "intermediary_agent_1",       "label": "Intermediary Agent 1",        "mandatory": False},
        {"id": "remittance_information",     "label": "Remittance Information",      "mandatory": False},
    ],
    "pacs.010.v3": [
        {"id": "instructing_agent",          "label": "Instructing Agent",           "mandatory": True},
        {"id": "instructed_agent",           "label": "Instructed Agent",            "mandatory": True},
        {"id": "debtor_agent",               "label": "Debtor Agent",                "mandatory": True},
        {"id": "debtor",                     "label": "Debtor",                      "mandatory": True},
        {"id": "debtor_account",             "label": "Debtor Account",              "mandatory": True,  "requires": ["debtor"]},
        {"id": "creditor_agent",             "label": "Creditor Agent",              "mandatory": False},
        {"id": "creditor",                   "label": "Creditor",                    "mandatory": False},
        {"id": "creditor_account",           "label": "Creditor Account",            "mandatory": False, "requires": ["creditor"]},
        {"id": "payment_type_information",   "label": "Payment Type Information",    "mandatory": False},
        {"id": "intermediary_agent_1",       "label": "Intermediary Agent 1",        "mandatory": False},
        {"id": "remittance_information",     "label": "Remittance Information",      "mandatory": False},
    ],

    # ── CAMT Messages ──────────────────────────────────────────────────────────
    "camt.057": [
        {"id": "group_header",              "label": "Group Header",                "mandatory": True},
        {"id": "debtor",                    "label": "Debtor",                      "mandatory": True},
        {"id": "debtor_account",            "label": "Debtor Account",              "mandatory": True,  "requires": ["debtor"]},
        {"id": "debtor_agent",              "label": "Debtor Agent",                "mandatory": True},
        {"id": "intermediary_agent_1",      "label": "Intermediary Agent 1",        "mandatory": False},
    ],
    "camt.052": [
        {"id": "group_header",              "label": "Group Header",                "mandatory": True},
        {"id": "account_identification",    "label": "Account Identification",      "mandatory": True},
        {"id": "account_owner",             "label": "Account Owner",               "mandatory": False},
        {"id": "account_servicer",          "label": "Account Servicer",            "mandatory": False},
        {"id": "balance",                   "label": "Balance",                     "mandatory": True},
        {"id": "transaction_summary",       "label": "Transaction Summary",         "mandatory": False},
        {"id": "entry",                     "label": "Entry Details",               "mandatory": False},
    ],
    "camt.053": [
        {"id": "group_header",              "label": "Group Header",                "mandatory": True},
        {"id": "account_identification",    "label": "Account Identification",      "mandatory": True},
        {"id": "account_owner",             "label": "Account Owner",               "mandatory": False},
        {"id": "account_servicer",          "label": "Account Servicer",            "mandatory": False},
        {"id": "balance",                   "label": "Balance",                     "mandatory": True},
        {"id": "transaction_summary",       "label": "Transaction Summary",         "mandatory": False},
        {"id": "entry",                     "label": "Entry Details",               "mandatory": False},
    ],
    "camt.054": [
        {"id": "group_header",              "label": "Group Header",                "mandatory": True},
        {"id": "account_identification",    "label": "Account Identification",      "mandatory": True},
        {"id": "account_owner",             "label": "Account Owner",               "mandatory": False},
        {"id": "account_servicer",          "label": "Account Servicer",            "mandatory": False},
        {"id": "entry",                     "label": "Entry Details",               "mandatory": True},
    ],
    "camt.055": [
        {"id": "group_header",              "label": "Group Header (Assignment)",   "mandatory": True},
        {"id": "original_group_information","label": "Original Group Information",  "mandatory": True},
        {"id": "original_payment_information","label": "Original Payment Info",     "mandatory": True},
        {"id": "cancellation_reason",       "label": "Cancellation Reason",         "mandatory": False},
    ],
    # Note: camt.056 is mostly hardcoded (fixed structure). Only cancellation_reason and original_transaction are optional.
    "camt.056": [
        {"id": "group_header",              "label": "Group Header (Assignment)",   "mandatory": True},
        {"id": "original_group_information","label": "Original Group Information",  "mandatory": True},
        {"id": "transaction_information",   "label": "Transaction Information",     "mandatory": True},
        {"id": "cancellation_reason",       "label": "Cancellation Reason",         "mandatory": False},
    ],

    # ── PAIN Messages ──────────────────────────────────────────────────────────
    "pain.001": [
        {"id": "group_header",              "label": "Group Header",                "mandatory": True},
        {"id": "initiating_party",          "label": "Initiating Party",            "mandatory": True},
        {"id": "forwarding_agent",          "label": "Forwarding Agent",            "mandatory": True},
        {"id": "debtor",                    "label": "Debtor",                      "mandatory": True},
        {"id": "debtor_account",            "label": "Debtor Account",              "mandatory": True,  "requires": ["debtor"]},
        {"id": "debtor_agent",              "label": "Debtor Agent",                "mandatory": True},
        {"id": "creditor",                  "label": "Creditor",                    "mandatory": True},
        {"id": "creditor_account",          "label": "Creditor Account",            "mandatory": True,  "requires": ["creditor"]},
        {"id": "creditor_agent",            "label": "Creditor Agent",              "mandatory": False},
        {"id": "ultimate_debtor",           "label": "Ultimate Debtor",             "mandatory": False},
        {"id": "ultimate_creditor",         "label": "Ultimate Creditor",           "mandatory": False},
        {"id": "payment_type_information",  "label": "Payment Type Information",    "mandatory": False},
        {"id": "remittance_information",    "label": "Remittance Information",      "mandatory": False},
    ],
    "pain.002": [
        {"id": "group_header",              "label": "Group Header",                "mandatory": True},
        {"id": "initiating_party",          "label": "Initiating Party",            "mandatory": True},
        {"id": "original_group_information","label": "Original Group Information",  "mandatory": True},
        {"id": "original_payment_information","label": "Original Payment Info",     "mandatory": True},
        {"id": "status_reason",             "label": "Status Reason Information",   "mandatory": False},
        {"id": "original_transaction",      "label": "Original Transaction Info",   "mandatory": False},
    ],
    "pain.008": [
        {"id": "group_header",              "label": "Group Header",                "mandatory": True},
        {"id": "initiating_party",          "label": "Initiating Party",            "mandatory": True},
        {"id": "forwarding_agent",          "label": "Forwarding Agent",            "mandatory": True},
        {"id": "creditor",                  "label": "Creditor",                    "mandatory": True},
        {"id": "creditor_account",          "label": "Creditor Account",            "mandatory": True,  "requires": ["creditor"]},
        {"id": "creditor_agent",            "label": "Creditor Agent",              "mandatory": True},
        {"id": "debtor",                    "label": "Debtor",                      "mandatory": True},
        {"id": "debtor_account",            "label": "Debtor Account",              "mandatory": True,  "requires": ["debtor"]},
        {"id": "debtor_agent",              "label": "Debtor Agent",                "mandatory": True},
        {"id": "ultimate_creditor",         "label": "Ultimate Creditor",           "mandatory": False},
        {"id": "ultimate_debtor",           "label": "Ultimate Debtor",             "mandatory": False},
        {"id": "payment_type_information",  "label": "Payment Type Information",    "mandatory": False},
        {"id": "remittance_information",    "label": "Remittance Information",      "mandatory": False},
    ],
}


def get_blocks_for_message(msg_type: str) -> List[Dict]:
    key = msg_type.lower().replace(" ", ".").replace("pacs.009.001.08 cov", "pacs.009.cov").replace("pacs.009cov", "pacs.009.cov")
    # Normalise
    if "pacs.008" in key:
        return MESSAGE_BLOCKS["pacs.008"]
    if "pacs.009" in key and "cov" in key:
        return MESSAGE_BLOCKS["pacs.009.cov"]
    if "pacs.009" in key and "adv" in key:
        return MESSAGE_BLOCKS["pacs.009.adv"]
    if "pacs.009" in key:
        return MESSAGE_BLOCKS["pacs.009"]
    if "pacs.004" in key:
        return MESSAGE_BLOCKS["pacs.004"]
    if "pacs.003" in key:
        return MESSAGE_BLOCKS["pacs.003"]
    if "pacs.002" in key:
        return MESSAGE_BLOCKS["pacs.002"]
    if "pacs.010" in key and ("001.03" in key or "v3" in key):
        return MESSAGE_BLOCKS["pacs.010.v3"]
    if "pacs.010" in key:
        return MESSAGE_BLOCKS["pacs.010"]
    # CAMT messages
    if "camt.057" in key:
        return MESSAGE_BLOCKS["camt.057"]
    if "camt.052" in key:
        return MESSAGE_BLOCKS["camt.052"]
    if "camt.053" in key:
        return MESSAGE_BLOCKS["camt.053"]
    if "camt.054" in key:
        return MESSAGE_BLOCKS["camt.054"]
    if "camt.055" in key:
        return MESSAGE_BLOCKS["camt.055"]
    if "camt.056" in key:
        return MESSAGE_BLOCKS["camt.056"]
    # PAIN messages
    if "pain.001" in key:
        return MESSAGE_BLOCKS["pain.001"]
    if "pain.002" in key:
        return MESSAGE_BLOCKS["pain.002"]
    if "pain.008" in key:
        return MESSAGE_BLOCKS["pain.008"]
    return MESSAGE_BLOCKS.get("pacs.008", [])


# ── Pacs.008 Generator ─────────────────────────────────────────────────────────

def _gen_pacs008(selected: set, idx: int) -> str:
    """
    pacs.008 — Customer Credit Transfer — constructive variant.

    Every party/agent BIC/IBAN/currency is derived from a single MessageScenario
    so the cross-field rules (BIC country = IBAN country = address country =
    settlement currency zone) hold by construction. No more retry-on-fail loop
    for these fields.
    """
    from .scenario import MessageScenario, make_iban, make_bic

    scenario = MessageScenario.random()
    ccy = scenario.currency

    msg_id = rng_id("MSG", 16)
    biz_msg_id = msg_id  # CBPR_R5/CBPR_COM_R2: BizMsgIdr must equal GrpHdr/MsgId
    instr_id = rng_id("INSTR", 11)
    e2e_id = rng_id("E2E", 16)
    tx_id = rng_id("TX", 16)
    uetr = scenario.uetr
    cre_dt = rng_datetime()
    sttlm_dt = rng_date(1)
    sttlm_mtd = random.choice(SETTLEMENT_METHODS)
    amount = rng_amount(ccy)

    debtor = scenario.debtor
    creditor = scenario.creditor

    # Transaction XML — elements MUST follow strict XSD CreditTransferTransaction39 sequence
    tx = ""

    # 1. PmtTpInf (optional)
    if "payment_type_information" in selected:
        svc = random.choice(SERVICE_LEVELS)
        tx += f"\t\t\t\t<PmtTpInf>\n\t\t\t\t\t<SvcLvl>\n\t\t\t\t\t\t<Cd>{svc}</Cd>\n\t\t\t\t\t</SvcLvl>\n\t\t\t\t</PmtTpInf>\n"

    # 2. IntrBkSttlmAmt (mandatory)
    tx += f"\t\t\t\t<IntrBkSttlmAmt Ccy=\"{xe(ccy)}\">{amount}</IntrBkSttlmAmt>\n"

    # 3. IntrBkSttlmDt
    tx += el("IntrBkSttlmDt", sttlm_dt, 4)

    # 4. SttlmTmReq (optional)
    if "settlement_time_request" in selected:
        tx += f"\t\t\t\t<SttlmTmReq>\n\t\t\t\t\t<CLSTm>14:00:00+00:00</CLSTm>\n\t\t\t\t</SttlmTmReq>\n"

    # 4b. InstdAmt (XSD position: after SttlmTmReq, before ChrgBr)
    tx += f"\t\t\t\t<InstdAmt Ccy=\"{xe(ccy)}\">{rng_amount(ccy)}</InstdAmt>\n"

    # 5. ChrgBr (MANDATORY in pacs.008)
    charge_br = random.choice(["CRED", "DEBT", "SHAR"])
    tx += el("ChrgBr", charge_br, 4)

    # 6. ChrgsInf (ISO 20022 / CBPR+ rules):
    #   CRED → mandatory; use 0.00 if no charges deducted by InstructingAgent
    #   DEBT → optional; may communicate charges added for InstructedAgent
    #   SHAR / SLEV → optional
    needs_chrgs_inf = charge_br == "CRED" or "charges_information" in selected
    if needs_chrgs_inf:
        chg_amt = rng_amount(ccy) if charge_br != "CRED" else rng_amount(ccy)
        chg_bic = scenario.make_intermediary_agent().bic
        tx += (f"\t\t\t\t<ChrgsInf>\n"
               f"\t\t\t\t\t<Amt Ccy=\"{xe(ccy)}\">{chg_amt}</Amt>\n"
               f"\t\t\t\t\t<Agt>\n\t\t\t\t\t\t<FinInstnId>\n\t\t\t\t\t\t\t<BICFI>{xe(chg_bic)}</BICFI>\n"
               f"\t\t\t\t\t\t</FinInstnId>\n\t\t\t\t\t</Agt>\n\t\t\t\t</ChrgsInf>\n")

    # 7. PrvsInstgAgt1/2/3 (optional) — all in same currency zone as the settlement
    if "previous_instructing_agent_1" in selected:
        tx += agent_xml("PrvsInstgAgt1", scenario.make_intermediary_agent().bic, 4)
    if "previous_instructing_agent_2" in selected:
        tx += agent_xml("PrvsInstgAgt2", scenario.make_intermediary_agent().bic, 4)
    if "previous_instructing_agent_3" in selected:
        tx += agent_xml("PrvsInstgAgt3", scenario.make_intermediary_agent().bic, 4)

    # 8. InstgAgt / InstdAgt (in CdtTrfTxInf per XSD v13 CreditTransferTransaction70)
    if "instructing_agent" in selected:
        tx += agent_xml("InstgAgt", scenario.sender_bic, 4)
    if "instructed_agent" in selected:
        tx += agent_xml("InstdAgt", scenario.receiver_bic, 4)

    # 9. IntrmyAgt1/2/3 (optional)
    if "intermediary_agent_1" in selected:
        tx += agent_xml("IntrmyAgt1", scenario.make_intermediary_agent().bic, 4)
        if "intermediary_agent_1_account" in selected:
            tx += account_othr_xml("IntrmyAgt1Acct", rng_id("ACCT", 10), 4)
    if "intermediary_agent_2" in selected:
        tx += agent_xml("IntrmyAgt2", scenario.make_intermediary_agent().bic, 4)
        if "intermediary_agent_2_account" in selected:
            tx += account_othr_xml("IntrmyAgt2Acct", rng_id("ACCT", 10), 4)
    if "intermediary_agent_3" in selected:
        tx += agent_xml("IntrmyAgt3", scenario.make_intermediary_agent().bic, 4)
        if "intermediary_agent_3_account" in selected:
            tx += account_othr_xml("IntrmyAgt3Acct", rng_id("ACCT", 10), 4)

    # 10. UltmtDbtr (optional) — same country as the debtor
    if "ultimate_debtor" in selected:
        tx += party_xml("UltmtDbtr", scenario.debtor.name + " Group", debtor.country, 4)

    # 11. Dbtr (mandatory)
    tx += party_xml("Dbtr", debtor.name, debtor.country, 4)

    # 12. DbtrAcct (mandatory)
    tx += account_xml("DbtrAcct", debtor.iban, 4)

    # 13. DbtrAgt (mandatory)
    tx += agent_xml("DbtrAgt", scenario.debtor_agent.bic, 4)
    if "debtor_agent_account" in selected:
        tx += account_othr_xml("DbtrAgtAcct", rng_id("ACCT", 10), 4)

    # 14. CdtrAgt (mandatory)
    tx += agent_xml("CdtrAgt", scenario.creditor_agent.bic, 4)
    if "creditor_agent_account" in selected:
        tx += account_othr_xml("CdtrAgtAcct", rng_id("ACCT", 10), 4)

    # 15. Cdtr (mandatory)
    tx += party_xml("Cdtr", creditor.name, creditor.country, 4)

    # 16. CdtrAcct (mandatory)
    tx += account_xml("CdtrAcct", creditor.iban, 4)

    # 17. UltmtCdtr (optional) — same country as creditor
    if "ultimate_creditor" in selected:
        tx += party_xml("UltmtCdtr", creditor.name + " Group", creditor.country, 4)

    # 18. RmtInf (optional)
    if "remittance_information" in selected:
        rmt_ref = rng_id("REF", 16)
        tx += f"\t\t\t\t<RmtInf>\n\t\t\t\t\t<Ustrd>{xe(rmt_ref)}</Ustrd>\n\t\t\t\t</RmtInf>\n"

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<BusMsgEnvlp xmlns="urn:swift:xsd:envelope">
\t<AppHdr xmlns="urn:iso:std:iso:20022:tech:xsd:head.001.001.02">
\t\t<Fr>
{apphdr_fi(scenario.sender_bic)}\t\t</Fr>
\t\t<To>
{apphdr_fi(scenario.receiver_bic)}\t\t</To>
\t\t<BizMsgIdr>{xe(biz_msg_id)}</BizMsgIdr>
\t\t<MsgDefIdr>pacs.008.001.08</MsgDefIdr>
\t\t<BizSvc>swift.cbprplus.02</BizSvc>
\t\t<CreDt>{cre_dt}</CreDt>
\t</AppHdr>
\t<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08">
\t\t<FIToFICstmrCdtTrf>
\t\t\t<GrpHdr>
\t\t\t\t<MsgId>{xe(msg_id)}</MsgId>
\t\t\t\t<CreDtTm>{cre_dt}</CreDtTm>
\t\t\t\t<NbOfTxs>1</NbOfTxs>
\t\t\t\t<SttlmInf>
\t\t\t\t\t<SttlmMtd>{xe(sttlm_mtd)}</SttlmMtd>
\t\t\t\t</SttlmInf>
\t\t\t</GrpHdr>
\t\t\t<CdtTrfTxInf>
\t\t\t\t<PmtId>
\t\t\t\t\t<InstrId>{xe(instr_id)}</InstrId>
\t\t\t\t\t<EndToEndId>{xe(e2e_id)}</EndToEndId>
\t\t\t\t\t<TxId>{xe(tx_id)}</TxId>
\t\t\t\t\t<UETR>{xe(uetr)}</UETR>
\t\t\t\t</PmtId>
{tx}\t\t\t</CdtTrfTxInf>
\t\t</FIToFICstmrCdtTrf>
\t</Document>
</BusMsgEnvlp>"""
    return xml


# ── Pacs.009 Generator ─────────────────────────────────────────────────────────

def _gen_pacs009(selected: set, idx: int, is_cov: bool = False, is_adv: bool = False) -> str:
    """
    pacs.009 — FI Credit Transfer (base / ADV / COV) — constructive variant.

    Anchored to one MessageScenario. In pacs.009 the Dbtr/Cdtr are themselves
    FIs (BICs, type BranchAndFinancialInstitutionIdentification8). We map them
    onto the scenario's debtor_agent and creditor_agent. All intermediary /
    settlement reimbursement agents are pulled from the same currency zone.

    For COV: the inner UndrlygCstmrCdtTrf block uses scenario.debtor and
    scenario.creditor (the actual customer parties behind the cover payment).

    v12 schema differences vs v08:
      - Root element: FICdtTrf  (was FinInstnCdtTrf)
      - Namespace:    pacs.009.001.08
      - Dbtr / Cdtr:  MANDATORY, type BranchAndFinancialInstitutionIdentification8
      - UltmtDbtr / UltmtCdtr: also BranchAndFinancialInstitutionIdentification8
      - ChrgsInf:     does NOT exist in CreditTransferTransaction67
    """
    from .scenario import MessageScenario

    scenario = MessageScenario.random()
    ccy = scenario.currency
    from_bic = scenario.sender_bic
    to_bic = scenario.receiver_bic
    # In pacs.009 the FI Dbtr/Cdtr are the actual FIs sending/receiving
    debtor_bic = scenario.debtor_agent.bic
    creditor_bic = scenario.creditor_agent.bic

    msg_id = rng_id("MSG", 16)
    biz_msg_id = msg_id  # CBPR_P9_R5: BizMsgIdr must equal GrpHdr/MsgId
    instr_id = rng_id("INSTR", 11)
    e2e_id = rng_id("E2E", 16)
    tx_id = rng_id("TX", 16)
    uetr = scenario.uetr
    cre_dt = rng_datetime()
    sttlm_dt = rng_date(1)
    if is_adv or is_cov:
        sttlm_mtd = "COVE"
    else:
        sttlm_mtd = random.choice(SETTLEMENT_METHODS)
    amount = rng_amount(ccy)

    sttlm_inf = f"\t\t\t\t\t<SttlmMtd>{xe(sttlm_mtd)}</SttlmMtd>"
    if sttlm_mtd == "COVE":
        rmbrs_choice = random.choice(["instg", "instd", "both"])
        if rmbrs_choice in ["instg", "both"]:
            sttlm_inf += f"\n\t\t\t\t\t<InstgRmbrsmntAgt>\n\t\t\t\t\t\t<FinInstnId>\n\t\t\t\t\t\t\t<BICFI>{xe(scenario.make_intermediary_agent().bic)}</BICFI>\n\t\t\t\t\t\t</FinInstnId>\n\t\t\t\t\t</InstgRmbrsmntAgt>"
        if rmbrs_choice in ["instd", "both"]:
            sttlm_inf += f"\n\t\t\t\t\t<InstdRmbrsmntAgt>\n\t\t\t\t\t\t<FinInstnId>\n\t\t\t\t\t\t\t<BICFI>{xe(scenario.make_intermediary_agent().bic)}</BICFI>\n\t\t\t\t\t\t</FinInstnId>\n\t\t\t\t\t</InstdRmbrsmntAgt>"
    elif sttlm_mtd in ["INDA", "INGA"] and random.random() < 0.5:
        sttlm_inf += f"\n\t\t\t\t\t<SttlmAcct>\n\t\t\t\t\t\t<Id>\n\t\t\t\t\t\t\t<IBAN>{xe(scenario.debtor.iban)}</IBAN>\n\t\t\t\t\t\t</Id>\n\t\t\t\t\t</SttlmAcct>"

    tx = ""

    # ── Element order must match XSD CreditTransferTransaction67 sequence ──

    # 1. PmtTpInf (optional)
    if "payment_type_information" in selected or is_adv:
        svc = random.choice(SERVICE_LEVELS)
        tx += "\t\t\t\t<PmtTpInf>\n"
        if "payment_type_information" in selected:
            tx += f"\t\t\t\t\t<SvcLvl>\n\t\t\t\t\t\t<Cd>{svc}</Cd>\n\t\t\t\t\t</SvcLvl>\n"
        if is_adv:
            tx += "\t\t\t\t\t<LclInstrm>\n\t\t\t\t\t\t<Prtry>ADV</Prtry>\n\t\t\t\t\t</LclInstrm>\n"
        tx += "\t\t\t\t</PmtTpInf>\n"

    # 2. IntrBkSttlmAmt (mandatory)
    tx += f"\t\t\t\t<IntrBkSttlmAmt Ccy=\"{xe(ccy)}\">{amount}</IntrBkSttlmAmt>\n"
    # 3. IntrBkSttlmDt (optional)
    tx += el("IntrBkSttlmDt", sttlm_dt, 4)

    # 4. SttlmTmReq (optional)
    if "settlement_time_request" in selected:
        tx += f"\t\t\t\t<SttlmTmReq>\n\t\t\t\t\t<CLSTm>14:00:00+00:00</CLSTm>\n\t\t\t\t</SttlmTmReq>\n"

    # 5. PrvsInstgAgt1/2/3 (optional) — in-zone intermediaries
    if "previous_instructing_agent_1" in selected:
        tx += agent_xml("PrvsInstgAgt1", scenario.make_intermediary_agent().bic, 4)
    if "previous_instructing_agent_2" in selected:
        tx += agent_xml("PrvsInstgAgt2", scenario.make_intermediary_agent().bic, 4)
    if "previous_instructing_agent_3" in selected:
        tx += agent_xml("PrvsInstgAgt3", scenario.make_intermediary_agent().bic, 4)

    # 6. InstgAgt / InstdAgt (optional)
    if "instructing_agent" in selected or is_adv or is_cov:
        tx += agent_xml("InstgAgt", from_bic, 4)
    if "instructed_agent" in selected or is_adv or is_cov:
        tx += agent_xml("InstdAgt", to_bic, 4)

    # 7. IntrmyAgt1/2/3 (optional)
    if "intermediary_agent_1" in selected:
        tx += agent_xml("IntrmyAgt1", scenario.make_intermediary_agent().bic, 4)
        if "intermediary_agent_1_account" in selected:
            tx += account_othr_xml("IntrmyAgt1Acct", rng_id("ACCT", 10), 4)
    if "intermediary_agent_2" in selected:
        tx += agent_xml("IntrmyAgt2", scenario.make_intermediary_agent().bic, 4)
        if "intermediary_agent_2_account" in selected:
            tx += account_othr_xml("IntrmyAgt2Acct", rng_id("ACCT", 10), 4)
    if "intermediary_agent_3" in selected:
        tx += agent_xml("IntrmyAgt3", scenario.make_intermediary_agent().bic, 4)
        if "intermediary_agent_3_account" in selected:
            tx += account_othr_xml("IntrmyAgt3Acct", rng_id("ACCT", 10), 4)

    # 9. Dbtr — MANDATORY in v12, FI type — always include Nm + PstlAdr (CBPR+ rule)
    tx += _fi_party("Dbtr", debtor_bic, scenario.debtor.country, 4)

    # 10. DbtrAcct (optional)
    if "debtor_account" in selected:
        tx += account_xml("DbtrAcct", scenario.debtor.iban, 4)
    # 11. DbtrAgt (optional) — a correspondent for the Dbtr FI, same currency zone
    if "debtor_agent" in selected or is_adv or is_cov:
        tx += agent_xml("DbtrAgt", scenario.make_intermediary_agent().bic, 4)
    if "debtor_agent_account" in selected:
        tx += account_othr_xml("DbtrAgtAcct", rng_id("ACCT", 10), 4)

    # 12. CdtrAgt (optional) — a correspondent for the Cdtr FI, same currency zone
    if "creditor_agent" in selected or is_adv or is_cov:
        tx += agent_xml("CdtrAgt", scenario.make_intermediary_agent().bic, 4)
    if "creditor_agent_account" in selected:
        tx += account_othr_xml("CdtrAgtAcct", rng_id("ACCT", 10), 4)

    # 13. Cdtr — MANDATORY in v12, FI type — always include Nm + PstlAdr (CBPR+ rule)
    tx += _fi_party("Cdtr", creditor_bic, scenario.creditor.country, 4)

    # 14. CdtrAcct (optional)
    if "creditor_account" in selected:
        tx += account_xml("CdtrAcct", scenario.creditor.iban, 4)

    # 16. RmtInf (optional)
    if "remittance_information" in selected:
        tx += f"\t\t\t\t<RmtInf>\n\t\t\t\t\t<Ustrd>{xe(rng_id('REF', 16))}</Ustrd>\n\t\t\t\t</RmtInf>\n"

    # 17. UndrlygCstmrCdtTrf — COV only — uses the consistent customer-side scenario data
    if is_cov and "underlying_customer_credit_transfer" in selected:
        cov_dbtr_addr = _rng_pstl_adr_cov(5, scenario.debtor.country)
        cov_cdtr_addr = _rng_pstl_adr_cov(5, scenario.creditor.country)
        tx += f"""\t\t\t\t<UndrlygCstmrCdtTrf>
\t\t\t\t\t<Dbtr>
\t\t\t\t\t\t<Nm>{xe(scenario.debtor.name)}</Nm>
{cov_dbtr_addr.rstrip()}
\t\t\t\t\t</Dbtr>
\t\t\t\t\t<DbtrAcct><Id><IBAN>{xe(scenario.debtor.iban)}</IBAN></Id></DbtrAcct>
\t\t\t\t\t<DbtrAgt><FinInstnId><BICFI>{xe(scenario.debtor.bic)}</BICFI></FinInstnId></DbtrAgt>
\t\t\t\t\t<CdtrAgt><FinInstnId><BICFI>{xe(scenario.creditor.bic)}</BICFI></FinInstnId></CdtrAgt>
\t\t\t\t\t<Cdtr>
\t\t\t\t\t\t<Nm>{xe(scenario.creditor.name)}</Nm>
{cov_cdtr_addr.rstrip()}
\t\t\t\t\t</Cdtr>
\t\t\t\t\t<CdtrAcct><Id><IBAN>{xe(scenario.creditor.iban)}</IBAN></Id></CdtrAcct>
\t\t\t\t\t<RmtInf><Ustrd>{xe(rng_id('COVREF', 10))}</Ustrd></RmtInf>
\t\t\t\t</UndrlygCstmrCdtTrf>
"""

    # ── v12 namespace and root element ──
    ns = "urn:iso:std:iso:20022:tech:xsd:pacs.009.001.08"
    msg_def = "pacs.009.001.08"
    if is_cov:
        biz_svc = "swift.cbprplus.cov.03"
    elif is_adv:
        biz_svc = "swift.cbprplus.adv.03"
    else:
        biz_svc = "swift.cbprplus.03"

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<BusMsgEnvlp xmlns="urn:swift:xsd:envelope">
\t<AppHdr xmlns="urn:iso:std:iso:20022:tech:xsd:head.001.001.02">
\t\t<Fr>
{apphdr_fi(from_bic)}\t\t</Fr>
\t\t<To>
{apphdr_fi(to_bic)}\t\t</To>
\t\t<BizMsgIdr>{xe(biz_msg_id)}</BizMsgIdr>
\t\t<MsgDefIdr>{msg_def}</MsgDefIdr>
\t\t<BizSvc>{biz_svc}</BizSvc>
\t\t<CreDt>{cre_dt}</CreDt>
\t</AppHdr>
\t<Document xmlns="{ns}">
\t\t<FICdtTrf>
\t\t\t<GrpHdr>
\t\t\t\t<MsgId>{xe(msg_id)}</MsgId>
\t\t\t\t<CreDtTm>{cre_dt}</CreDtTm>
\t\t\t\t<NbOfTxs>1</NbOfTxs>
\t\t\t\t<SttlmInf>
{sttlm_inf}
\t\t\t\t</SttlmInf>
\t\t\t</GrpHdr>
\t\t\t<CdtTrfTxInf>
\t\t\t\t<PmtId>
\t\t\t\t\t<InstrId>{xe(instr_id)}</InstrId>
\t\t\t\t\t<EndToEndId>{xe(e2e_id)}</EndToEndId>
\t\t\t\t\t<TxId>{xe(tx_id)}</TxId>
\t\t\t\t\t<UETR>{xe(uetr)}</UETR>
\t\t\t\t</PmtId>
{tx}\t\t\t</CdtTrfTxInf>
\t\t</FICdtTrf>
\t</Document>
</BusMsgEnvlp>"""
    return xml


# ── Pacs.004 Generator ─────────────────────────────────────────────────────────

def _gen_pacs004(selected: set, idx: int) -> str:
    """
    pacs.004 — Payment Return — constructive variant.

    Anchored to one MessageScenario. The return chain reflects a payment that
    *was* going Dbtr → Cdtr; this return reverses it. Both parties and their
    agents come from the same currency zone via the scenario.
    """
    from .scenario import MessageScenario

    scenario = MessageScenario.random()
    ccy = scenario.currency
    debtor_party = scenario.debtor
    creditor_party = scenario.creditor

    msg_id = rng_id("MSG", 16)
    biz_msg_id = msg_id  # CBPR_COM_R2: BizMsgIdr must equal GrpHdr/MsgId
    rtr_id = rng_id("RTR", 16)
    orig_instr_id = rng_id("ORGINSTR", 11)
    orig_e2e = rng_id("ORIE2E", 10)
    orig_tx = rng_id("ORITX", 10)
    orig_uetr = rng_uetr()
    cre_dt = rng_datetime()
    sttlm_dt = rng_date(1)
    amount = rng_amount(ccy)
    rtr_reason = random.choice(RETURN_REASONS)
    charge_br = random.choice(["SHAR", "CRED"])

    tx = ""
    rtrd_instd_amt_xml = ""

    # -- ChrgsInf (optional) — kept in the same currency zone --
    if "charges_information" in selected or charge_br == "CRED":
        chg_amt = "0.00" if charge_br == "CRED" and "charges_information" not in selected else rng_amount(ccy)
        chg_bic = scenario.make_intermediary_agent().bic
        tx += (f"\t\t\t\t<ChrgsInf>\n"
               f"\t\t\t\t\t<Amt Ccy=\"{xe(ccy)}\">{chg_amt}</Amt>\n"
               f"\t\t\t\t\t<Agt>\n\t\t\t\t\t\t<FinInstnId>\n\t\t\t\t\t\t\t<BICFI>{xe(chg_bic)}</BICFI>\n"
               f"\t\t\t\t\t\t</FinInstnId>\n\t\t\t\t\t</Agt>\n\t\t\t\t</ChrgsInf>\n")
        # If ChargesInformation is present, ReturnedInstructedAmount must be present
        rtrd_instd_amt_xml = f'\t\t\t\t<RtrdInstdAmt Ccy="{xe(ccy)}">{amount}</RtrdInstdAmt>\n'

    # -- InstgAgt / InstdAgt --
    if "instructing_agent" in selected:
        tx += agent_xml("InstgAgt", scenario.sender_bic, 4)
    if "instructed_agent" in selected:
        tx += agent_xml("InstdAgt", scenario.receiver_bic, 4)

    # -- RtrChain (TransactionParties11) --
    chain = ""
    if "ultimate_debtor" in selected:
        chain += f"\t\t\t\t\t<UltmtDbtr>\n\t\t\t\t\t\t<Pty>\n{party_xml('_unused', debtor_party.name + ' Group', debtor_party.country, 7).replace('<_unused>','').replace('</_unused>','')}\t\t\t\t\t\t</Pty>\n\t\t\t\t\t</UltmtDbtr>\n"

    # Dbtr (Mandatory)
    chain += f"\t\t\t\t\t<Dbtr>\n\t\t\t\t\t\t<Pty>\n{party_xml('_unused', debtor_party.name, debtor_party.country, 7).replace('<_unused>','').replace('</_unused>','')}\t\t\t\t\t\t</Pty>\n\t\t\t\t\t</Dbtr>\n"

    if "debtor_agent" in selected:
        chain += agent_xml("DbtrAgt", scenario.debtor_agent.bic, 5)
    if "intermediary_agent_1" in selected:
        chain += agent_xml("IntrmyAgt1", scenario.make_intermediary_agent().bic, 5)
    if "creditor_agent" in selected:
        chain += agent_xml("CdtrAgt", scenario.creditor_agent.bic, 5)

    # Cdtr (Mandatory)
    chain += f"\t\t\t\t\t<Cdtr>\n\t\t\t\t\t\t<Pty>\n{party_xml('_unused', creditor_party.name, creditor_party.country, 7).replace('<_unused>','').replace('</_unused>','')}\t\t\t\t\t\t</Pty>\n\t\t\t\t\t</Cdtr>\n"

    if "ultimate_creditor" in selected:
        chain += f"\t\t\t\t\t<UltmtCdtr>\n\t\t\t\t\t\t<Pty>\n{party_xml('_unused', creditor_party.name + ' Group', creditor_party.country, 7).replace('<_unused>','').replace('</_unused>','')}\t\t\t\t\t\t</Pty>\n\t\t\t\t\t</UltmtCdtr>\n"
    if chain:
        tx += f"\t\t\t\t<RtrChain>\n{chain}\t\t\t\t</RtrChain>\n"

    # -- RtrRsnInf --
    tx += f"\t\t\t\t<RtrRsnInf>\n\t\t\t\t\t<Rsn>\n\t\t\t\t\t\t<Cd>{xe(rtr_reason)}</Cd>\n\t\t\t\t\t</Rsn>\n\t\t\t\t</RtrRsnInf>\n"

    # -- OrgnlTxRef --
    orgnl_tx_ref = ""
    if "debtor_account" in selected or "creditor_account" in selected:
        orgnl_tx_ref = "\t\t\t\t<OrgnlTxRef>\n"
        if "debtor_account" in selected:
            orgnl_tx_ref += f"\t\t\t\t\t<Dbtr>\n{party_xml('Pty', debtor_party.name, debtor_party.country, 6)}\t\t\t\t\t</Dbtr>\n"
            orgnl_tx_ref += account_xml("DbtrAcct", debtor_party.iban, 5)
        if "creditor_account" in selected:
            orgnl_tx_ref += f"\t\t\t\t\t<Cdtr>\n{party_xml('Pty', creditor_party.name, creditor_party.country, 6)}\t\t\t\t\t</Cdtr>\n"
            orgnl_tx_ref += account_xml("CdtrAcct", creditor_party.iban, 5)
        orgnl_tx_ref += "\t\t\t\t</OrgnlTxRef>\n"

    if "remittance_information" in selected:
        pass  # RmtInf is inside OrgnlTxRef, not directly in TxInf for pacs.004

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<BusMsgEnvlp xmlns="urn:swift:xsd:envelope">
\t<AppHdr xmlns="urn:iso:std:iso:20022:tech:xsd:head.001.001.02">
\t\t<Fr>
{apphdr_fi(scenario.sender_bic)}\t\t</Fr>
\t\t<To>
{apphdr_fi(scenario.receiver_bic)}\t\t</To>
\t\t<BizMsgIdr>{xe(biz_msg_id)}</BizMsgIdr>
\t\t<MsgDefIdr>pacs.004.001.09</MsgDefIdr>
\t\t<BizSvc>swift.cbprplus.02</BizSvc>
\t\t<CreDt>{cre_dt}</CreDt>
\t</AppHdr>
\t<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.004.001.09">
\t\t<PmtRtr>
\t\t\t<GrpHdr>
\t\t\t\t<MsgId>{xe(msg_id)}</MsgId>
\t\t\t\t<CreDtTm>{cre_dt}</CreDtTm>
\t\t\t\t<NbOfTxs>1</NbOfTxs>
\t\t\t\t<SttlmInf>
\t\t\t\t\t<SttlmMtd>INDA</SttlmMtd>
\t\t\t\t</SttlmInf>
\t\t\t</GrpHdr>
\t\t\t<TxInf>
\t\t\t\t<RtrId>{xe(rtr_id)}</RtrId>
\t\t\t\t<OrgnlGrpInf>
\t\t\t\t\t<OrgnlMsgId>{xe(rng_id("ORIGMSG", 10))}</OrgnlMsgId>
\t\t\t\t\t<OrgnlMsgNmId>pacs.008.001.08</OrgnlMsgNmId>
\t\t\t\t</OrgnlGrpInf>
\t\t\t\t<OrgnlInstrId>{xe(orig_instr_id)}</OrgnlInstrId>
\t\t\t\t<OrgnlEndToEndId>{xe(orig_e2e)}</OrgnlEndToEndId>
\t\t\t\t<OrgnlTxId>{xe(orig_tx)}</OrgnlTxId>
\t\t\t\t<OrgnlUETR>{xe(orig_uetr)}</OrgnlUETR>
\t\t\t\t<RtrdIntrBkSttlmAmt Ccy="{xe(ccy)}">{amount}</RtrdIntrBkSttlmAmt>
\t\t\t\t<IntrBkSttlmDt>{xe(sttlm_dt)}</IntrBkSttlmDt>
{rtrd_instd_amt_xml}				<ChrgBr>{xe(charge_br)}</ChrgBr>
{tx}{orgnl_tx_ref}			</TxInf>
\t\t</PmtRtr>
\t</Document>
</BusMsgEnvlp>"""
    return xml


# ── Pacs.003 Generator ─────────────────────────────────────────────────────────

def _gen_pacs003(selected: set, idx: int) -> str:
    """
    pacs.003 — Customer Direct Debit — constructive variant.

    Anchored to one MessageScenario:
      - creditor (the merchant collecting funds) = scenario.creditor / scenario.creditor_agent
      - debtor (the customer being debited)      = scenario.debtor / scenario.debtor_agent
      - all amounts in scenario.currency
    """
    from .scenario import MessageScenario

    scenario = MessageScenario.random()
    ccy = scenario.currency
    debtor_party = scenario.debtor
    creditor_party = scenario.creditor

    msg_id = rng_id("MSG", 16)
    biz_msg_id = msg_id  # CBPR_COM_R2: BizMsgIdr must equal GrpHdr/MsgId
    instr_id = rng_id("INSTR", 11)
    e2e_id = rng_id("E2E", 16)
    tx_id = rng_id("TX", 16)
    uetr = scenario.uetr
    mndt_id = rng_id("MNDT", 10)
    cre_dt = rng_datetime()
    sttlm_dt = rng_date(1)
    amount = rng_amount(ccy)

    tx = ""

    if "payment_type_information" in selected:
        svc = random.choice(SERVICE_LEVELS)
        tx += f"\t\t\t\t<PmtTpInf>\n\t\t\t\t\t<SvcLvl>\n\t\t\t\t\t\t<Cd>{svc}</Cd>\n\t\t\t\t\t</SvcLvl>\n\t\t\t\t</PmtTpInf>\n"

    tx += f"\t\t\t\t<IntrBkSttlmAmt Ccy=\"{xe(ccy)}\">{amount}</IntrBkSttlmAmt>\n"
    tx += el("IntrBkSttlmDt", sttlm_dt, 4)

    # -- ChrgBr (mandatory per XSD) --
    charge_br = random.choice(["SHAR", "CRED", "DEBT"])
    needs_chrgs_inf = "charges_information" in selected or charge_br == "CRED"

    # -- InstdAmt (optional) — MANDATORY when ChrgsInf is present (CBPR+ rule) --
    if needs_chrgs_inf:
        instd_amt = amount if charge_br == "CRED" and "charges_information" not in selected else rng_amount(ccy)
        tx += f"\t\t\t\t<InstdAmt Ccy=\"{xe(ccy)}\">{instd_amt}</InstdAmt>\n"

    tx += el("ChrgBr", charge_br, 4)

    # -- ChrgsInf (optional) — agent BIC kept in the same currency zone --
    if needs_chrgs_inf:
        chg_amt = "0.00" if charge_br == "CRED" and "charges_information" not in selected else rng_amount(ccy)
        chg_bic = scenario.make_intermediary_agent().bic
        tx += (f"\t\t\t\t<ChrgsInf>\n"
               f"\t\t\t\t\t<Amt Ccy=\"{xe(ccy)}\">{chg_amt}</Amt>\n"
               f"\t\t\t\t\t<Agt>\n\t\t\t\t\t\t<FinInstnId>\n\t\t\t\t\t\t\t<BICFI>{xe(chg_bic)}</BICFI>\n"
               f"\t\t\t\t\t\t</FinInstnId>\n\t\t\t\t\t</Agt>\n\t\t\t\t</ChrgsInf>\n")

    # -- ReqdColltnDt (pos 11) --
    tx += el("ReqdColltnDt", rng_date(2), 4)

    # -- DrctDbtTx (optional, after ReqdColltnDt per XSD) --
    if "direct_debit_transaction" in selected:
        tx += (f"\t\t\t\t<DrctDbtTx>\n"
               f"\t\t\t\t\t<MndtRltdInf>\n"
               f"\t\t\t\t\t\t<MndtId>{xe(mndt_id)}</MndtId>\n"
               f"\t\t\t\t\t\t<DtOfSgntr>{rng_date(-30)}</DtOfSgntr>\n"
               f"\t\t\t\t\t</MndtRltdInf>\n"
               f"\t\t\t\t</DrctDbtTx>\n")

    # -- Cdtr (Mandatory) — uses scenario.creditor for country/IBAN/BIC coherence --
    tx += party_xml("Cdtr", creditor_party.name, creditor_party.country, 4)
    # CdtrAcct (Mandatory)
    tx += account_xml("CdtrAcct", creditor_party.iban, 4)

    # -- CdtrAgt (Mandatory) --
    tx += agent_xml("CdtrAgt", scenario.creditor_agent.bic, 4)
    if "creditor_agent_account" in selected:
        tx += account_othr_xml("CdtrAgtAcct", rng_id("ACCT", 10), 4)

    # -- UltmtCdtr — same country as the creditor --
    if "ultimate_creditor" in selected:
        tx += party_xml("UltmtCdtr", creditor_party.name + " Group", creditor_party.country, 4)

    # -- InstgAgt / InstdAgt --
    if "instructing_agent" in selected:
        tx += agent_xml("InstgAgt", scenario.sender_bic, 4)
    if "instructed_agent" in selected:
        tx += agent_xml("InstdAgt", scenario.receiver_bic, 4)

    # -- IntrmyAgt1/2/3 — also in-zone --
    if "intermediary_agent_1" in selected:
        tx += agent_xml("IntrmyAgt1", scenario.make_intermediary_agent().bic, 4)
    if "intermediary_agent_2" in selected:
        tx += agent_xml("IntrmyAgt2", scenario.make_intermediary_agent().bic, 4)
    if "intermediary_agent_3" in selected:
        tx += agent_xml("IntrmyAgt3", scenario.make_intermediary_agent().bic, 4)

    # -- Dbtr (Mandatory) — the customer being debited --
    tx += party_xml("Dbtr", debtor_party.name, debtor_party.country, 4)
    # DbtrAcct (Mandatory)
    tx += account_xml("DbtrAcct", debtor_party.iban, 4)
    # DbtrAgt (Mandatory)
    tx += agent_xml("DbtrAgt", scenario.debtor_agent.bic, 4)

    # -- UltmtDbtr — same country as debtor --
    if "ultimate_debtor" in selected:
        tx += party_xml("UltmtDbtr", debtor_party.name + " Group", debtor_party.country, 4)

    # -- RmtInf --
    if "remittance_information" in selected:
        tx += f"\t\t\t\t<RmtInf>\n\t\t\t\t\t<Ustrd>{xe(rng_id('REF', 16))}</Ustrd>\n\t\t\t\t</RmtInf>\n"

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<BusMsgEnvlp xmlns="urn:swift:xsd:envelope">
\t<AppHdr xmlns="urn:iso:std:iso:20022:tech:xsd:head.001.001.02">
\t\t<Fr>
{apphdr_fi(scenario.sender_bic)}\t\t</Fr>
\t\t<To>
{apphdr_fi(scenario.receiver_bic)}\t\t</To>
\t\t<BizMsgIdr>{xe(biz_msg_id)}</BizMsgIdr>
\t\t<MsgDefIdr>pacs.003.001.08</MsgDefIdr>
\t\t<BizSvc>swift.cbprplus.02</BizSvc>
\t\t<CreDt>{cre_dt}</CreDt>
\t</AppHdr>
\t<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.003.001.08">
\t\t<FIToFICstmrDrctDbt>
\t\t\t<GrpHdr>
\t\t\t\t<MsgId>{xe(msg_id)}</MsgId>
\t\t\t\t<CreDtTm>{cre_dt}</CreDtTm>
\t\t\t\t<NbOfTxs>1</NbOfTxs>
\t\t\t\t<SttlmInf>
\t\t\t\t\t<SttlmMtd>INDA</SttlmMtd>
\t\t\t\t</SttlmInf>
\t\t\t</GrpHdr>
\t\t\t<DrctDbtTxInf>
\t\t\t\t<PmtId>
\t\t\t\t\t<InstrId>{xe(instr_id)}</InstrId>
\t\t\t\t\t<EndToEndId>{xe(e2e_id)}</EndToEndId>
\t\t\t\t\t<TxId>{xe(tx_id)}</TxId>
\t\t\t\t\t<UETR>{xe(uetr)}</UETR>
\t\t\t\t</PmtId>
{tx}\t\t\t</DrctDbtTxInf>
\t\t</FIToFICstmrDrctDbt>
\t</Document>
</BusMsgEnvlp>"""
    return xml


# ── Pacs.002 Generator ─────────────────────────────────────────────────────────

def _gen_pacs002(selected: set, idx: int) -> str:
    """
    Generate a valid pacs.002.001.10 (FI-to-FI Payment Status Report) message.

    CBPR+ rules enforced:
      - AppHdr/Fr BICFI == TxInfAndSts/InstgAgt BICFI  (when CpyDplct absent)
      - AppHdr/To BICFI == TxInfAndSts/InstdAgt BICFI  (when CpyDplct absent)
      - TxInfAndSts element order:
            OrgnlGrpInf → OrgnlEndToEndId → OrgnlTxId → OrgnlUETR →
            TxSts → StsRsnInf (if RJCT) → InstgAgt → InstdAgt → OrgnlTxRef
      - InstgAgt / InstdAgt are NEVER placed inside GrpHdr for pacs.002.
    """
    from .scenario import MessageScenario

    scenario = MessageScenario.random()
    debtor_party = scenario.debtor
    creditor_party = scenario.creditor
    # Shared BICs: AppHdr Fr == InstgAgt, AppHdr To == InstdAgt (CBPR+ BAH rule)
    from_bic = scenario.sender_bic
    to_bic   = scenario.receiver_bic
    msg_id      = rng_id("MSG", 16)
    biz_msg_id  = msg_id  # CBPR_COM_R2: BizMsgIdr must equal GrpHdr/MsgId
    cre_dt      = rng_datetime()
    orig_msg_id = rng_id("ORIGMSG", 10)
    orig_e2e    = rng_id("ORIE2E", 10)
    orig_tx     = rng_id("ORITX", 10)
    orig_uetr   = scenario.uetr
    status_codes = ["ACSC", "ACCP", "ACSP", "RJCT", "PDNG"]
    status = random.choice(status_codes)

    # ── StsRsnInf — only for RJCT ──────────────────────────────────────────────
    reject_reason = ""
    if status == "RJCT":
        reasons = ["AM04", "AC01", "FF01", "AG01"]
        reject_reason = (
            "\t\t\t\t<StsRsnInf>\n"
            "\t\t\t\t\t<Rsn>\n"
            f"\t\t\t\t\t\t<Cd>{random.choice(reasons)}</Cd>\n"
            "\t\t\t\t\t</Rsn>\n"
            "\t\t\t\t</StsRsnInf>\n"
        )

    # ── InstgAgt / InstdAgt inside TxInfAndSts (with BAH-matched BICs) ─────────
    # These MUST use the same BICs as AppHdr/Fr and AppHdr/To respectively.
    tx_instg_agt = ""
    tx_instd_agt = ""
    if "instructing_agent" in selected:
        tx_instg_agt = (
            "\t\t\t\t<InstgAgt>\n"
            "\t\t\t\t\t<FinInstnId>\n"
            f"\t\t\t\t\t\t<BICFI>{xe(from_bic)}</BICFI>\n"
            "\t\t\t\t\t</FinInstnId>\n"
            "\t\t\t\t</InstgAgt>\n"
        )
    if "instructed_agent" in selected:
        tx_instd_agt = (
            "\t\t\t\t<InstdAgt>\n"
            "\t\t\t\t\t<FinInstnId>\n"
            f"\t\t\t\t\t\t<BICFI>{xe(to_bic)}</BICFI>\n"
            "\t\t\t\t\t</FinInstnId>\n"
            "\t\t\t\t</InstdAgt>\n"
        )

    # ── OrgnlTxRef — disabled for CBPR+ ────────────────────────────────────────
    orgnl_tx_ref = ""

    # ── Assemble ────────────────────────────────────────────────────────────────
    # GrpHdr contains ONLY: MsgId, CreDtTm — NO InstgAgt/InstdAgt here.
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<BusMsgEnvlp xmlns="urn:swift:xsd:envelope">
\t<AppHdr xmlns="urn:iso:std:iso:20022:tech:xsd:head.001.001.02">
\t\t<Fr>
{apphdr_fi(from_bic)}\t\t</Fr>
\t\t<To>
{apphdr_fi(to_bic)}\t\t</To>
\t\t<BizMsgIdr>{xe(biz_msg_id)}</BizMsgIdr>
\t\t<MsgDefIdr>pacs.002.001.10</MsgDefIdr>
\t\t<BizSvc>swift.cbprplus.02</BizSvc>
\t\t<CreDt>{cre_dt}</CreDt>
\t</AppHdr>
\t<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.002.001.10">
\t\t<FIToFIPmtStsRpt>
\t\t\t<GrpHdr>
\t\t\t\t<MsgId>{xe(msg_id)}</MsgId>
\t\t\t\t<CreDtTm>{cre_dt}</CreDtTm>
\t\t\t</GrpHdr>
\t\t\t<TxInfAndSts>
\t\t\t\t<OrgnlGrpInf>
\t\t\t\t\t<OrgnlMsgId>{xe(orig_msg_id)}</OrgnlMsgId>
\t\t\t\t\t<OrgnlMsgNmId>pacs.008.001.08</OrgnlMsgNmId>
\t\t\t\t</OrgnlGrpInf>
\t\t\t\t<OrgnlEndToEndId>{xe(orig_e2e)}</OrgnlEndToEndId>
\t\t\t\t<OrgnlTxId>{xe(orig_tx)}</OrgnlTxId>
\t\t\t\t<OrgnlUETR>{xe(orig_uetr)}</OrgnlUETR>
\t\t\t\t<TxSts>{xe(status)}</TxSts>
{reject_reason}{tx_instg_agt}{tx_instd_agt}{orgnl_tx_ref}\t\t\t</TxInfAndSts>
\t\t</FIToFIPmtStsRpt>
\t</Document>
</BusMsgEnvlp>"""
    return xml



# ── Pacs.010 Generator ─────────────────────────────────────────────────────────

def _gen_pacs010(selected: set, idx: int) -> str:
    """
    pacs.010 — FI Direct Debit (covers both base + v3/Margin Collection) —
    constructive variant.

    Anchored to one MessageScenario: Dbtr FI = scenario.debtor_agent,
    Cdtr FI = scenario.creditor_agent, all intermediaries from the same
    currency zone.
    """
    from .scenario import MessageScenario

    scenario = MessageScenario.random()
    ccy = scenario.currency
    from_bic = scenario.sender_bic
    to_bic = scenario.receiver_bic
    debtor_bic = scenario.debtor_agent.bic
    creditor_bic = scenario.creditor_agent.bic

    msg_id = rng_id("MSG", 16)
    biz_msg_id = msg_id  # CBPR_COM_R2: BizMsgIdr must equal GrpHdr/MsgId
    cdt_id = rng_id("CDT", 16)
    instr_id = rng_id("INSTR", 11)
    e2e_id = rng_id("E2E", 16)
    tx_id = rng_id("TX", 16)
    uetr = scenario.uetr
    cre_dt = rng_datetime()
    sttlm_dt = rng_date(1)
    amount = rng_amount(ccy)

    # Both pacs.010 variants (main and v3 / Margin Collection) use the same
    # CBPR+ namespace; the v3 flag only changes the XML body structure.
    ns = "urn:iso:std:iso:20022:tech:xsd:pacs.010.001.03"
    msg_def = "pacs.010.001.03"

    # DrctDbtTxInf body: PmtId → PmtTpInf → IntrBkSttlmAmt → IntrBkSttlmDt → UltmtDbtr → Dbtr → DbtrAcct → DbtrAgt → RmtInf
    dd_tx = ""
    if "payment_type_information" in selected:
        svc = random.choice(SERVICE_LEVELS)
        dd_tx += f"\t\t\t\t\t<PmtTpInf>\n\t\t\t\t\t\t<SvcLvl>\n\t\t\t\t\t\t\t<Cd>{svc}</Cd>\n\t\t\t\t\t\t</SvcLvl>\n\t\t\t\t\t</PmtTpInf>\n"

    dd_tx += f"\t\t\t\t\t<IntrBkSttlmAmt Ccy=\"{xe(ccy)}\">{amount}</IntrBkSttlmAmt>\n"
    dd_tx += el("IntrBkSttlmDt", sttlm_dt, 5)

    # Dbtr mandatory (BranchAndFinancialInstitutionIdentification8) — always Nm+addr
    dd_tx += _fi_party("Dbtr", debtor_bic, scenario.debtor.country, 5)
    if "debtor_account" in selected:
        dd_tx += account_xml("DbtrAcct", scenario.debtor.iban, 5)
    if "debtor_agent" in selected:
        dd_tx += agent_xml("DbtrAgt", scenario.make_intermediary_agent().bic, 5)

    if "remittance_information" in selected:
        dd_tx += f"\t\t\t\t\t<RmtInf>\n\t\t\t\t\t\t<Ustrd>{xe(rng_id('REF', 16))}</Ustrd>\n\t\t\t\t\t</RmtInf>\n"

    # CdtInstr body: CdtId → IntrBkSttlmDt → InstgAgt → InstdAgt → CdtrAgt → Cdtr → CdtrAcct → DrctDbtTxInf
    cdt_body = ""
    if "instructing_agent" in selected:
        cdt_body += agent_xml("InstgAgt", from_bic, 4)
    if "instructed_agent" in selected:
        cdt_body += agent_xml("InstdAgt", to_bic, 4)
    if "creditor_agent" in selected:
        cdt_body += agent_xml("CdtrAgt", scenario.make_intermediary_agent().bic, 4)
    # NOTE: pacs.010 CdtInstr XSD sequence does NOT permit IntrmyAgt between
    # CdtrAgt and Cdtr — skip it even when the block is selected.
    # Cdtr mandatory (BranchAndFinancialInstitutionIdentification8) — always Nm+addr
    cdt_body += _fi_party("Cdtr", creditor_bic, scenario.creditor.country, 4)
    if "creditor_account" in selected:
        cdt_body += account_xml("CdtrAcct", scenario.creditor.iban, 4)

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<BusMsgEnvlp xmlns="urn:swift:xsd:envelope">
\t<AppHdr xmlns="urn:iso:std:iso:20022:tech:xsd:head.001.001.02">
\t\t<Fr>
{apphdr_fi(from_bic)}\t\t</Fr>
\t\t<To>
{apphdr_fi(to_bic)}\t\t</To>
\t\t<BizMsgIdr>{xe(biz_msg_id)}</BizMsgIdr>
\t\t<MsgDefIdr>{msg_def}</MsgDefIdr>
\t\t<BizSvc>swift.cbprplus.02</BizSvc>
\t\t<CreDt>{cre_dt}</CreDt>
\t</AppHdr>
\t<Document xmlns="{ns}">
\t\t<FIDrctDbt>
\t\t\t<GrpHdr>
\t\t\t\t<MsgId>{xe(msg_id)}</MsgId>
\t\t\t\t<CreDtTm>{cre_dt}</CreDtTm>
\t\t\t\t<NbOfTxs>1</NbOfTxs>
\t\t\t</GrpHdr>
\t\t\t<CdtInstr>
\t\t\t\t<CdtId>{xe(cdt_id)}</CdtId>
{cdt_body}\t\t\t\t<DrctDbtTxInf>
\t\t\t\t\t<PmtId>
\t\t\t\t\t\t<InstrId>{xe(instr_id)}</InstrId>
\t\t\t\t\t\t<EndToEndId>{xe(e2e_id)}</EndToEndId>
\t\t\t\t\t\t<TxId>{xe(tx_id)}</TxId>
\t\t\t\t\t\t<UETR>{xe(uetr)}</UETR>
\t\t\t\t\t</PmtId>
{dd_tx}\t\t\t\t</DrctDbtTxInf>
\t\t\t</CdtInstr>
\t\t</FIDrctDbt>
\t</Document>
</BusMsgEnvlp>"""
    return xml


# ── CAMT.057 Generator ─────────────────────────────────────────────────────────

def _gen_camt057(selected: set, idx: int) -> str:
    """
    camt.057 — Notification to Receive — constructive variant.

    Anchored to one MessageScenario so the account IBAN, settlement currency,
    AppHdr BICs, and any debtor / agent BICs all share a coherent country/
    currency zone.
    """
    from .scenario import MessageScenario

    scenario = MessageScenario.random()
    ccy = scenario.currency
    # The account being credited belongs to the "creditor" side of this scenario.
    account_holder = scenario.creditor
    # The notifying party is the debtor (the one sending the incoming payment).
    debtor_party = scenario.debtor
    debtor_agent_bic = scenario.debtor_agent.bic

    msg_id = rng_id("MSG", 16)
    biz_msg_id = msg_id  # CBPR BizMsgIdr must equal GrpHdr/MsgId
    notif_id = rng_id("NTFN", 10)
    cre_dt = rng_datetime()
    amount = rng_amount(ccy)
    e2e_id = rng_id("E2E", 16)

    items = ""
    if "debtor" in selected:
        items += f"\t\t\t\t\t<Dbtr>\n\t\t\t\t\t\t<Pty>\n{party_xml('_unused', debtor_party.name, debtor_party.country, 6).replace('<_unused>','').replace('</_unused>','')}\t\t\t\t\t\t</Pty>\n\t\t\t\t\t</Dbtr>\n"
    if "debtor_agent" in selected:
        items += agent_xml("DbtrAgt", debtor_agent_bic, 5)
    if "intermediary_agent_1" in selected:
        items += agent_xml("IntrmyAgt", scenario.make_intermediary_agent().bic, 5)

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<BusMsgEnvlp xmlns="urn:swift:xsd:envelope">
\t<AppHdr xmlns="urn:iso:std:iso:20022:tech:xsd:head.001.001.02">
\t\t<Fr>
{apphdr_fi(scenario.sender_bic)}\t\t</Fr>
\t\t<To>
{apphdr_fi(scenario.receiver_bic)}\t\t</To>
\t\t<BizMsgIdr>{xe(biz_msg_id)}</BizMsgIdr>
\t\t<MsgDefIdr>camt.057.001.06</MsgDefIdr>
\t\t<BizSvc>swift.cbprplus.03</BizSvc>
\t\t<CreDt>{cre_dt}</CreDt>
\t</AppHdr>
\t<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.057.001.06">
\t\t<NtfctnToRcv>
\t\t\t<GrpHdr>
\t\t\t\t<MsgId>{xe(msg_id)}</MsgId>
\t\t\t\t<CreDtTm>{cre_dt}</CreDtTm>
\t\t\t</GrpHdr>
\t\t\t<Ntfctn>
\t\t\t\t<Id>{xe(notif_id)}</Id>
\t\t\t\t<Acct><Id><IBAN>{xe(account_holder.iban)}</IBAN></Id></Acct>
\t\t\t\t<XpctdValDt>{rng_date(2)}</XpctdValDt>
\t\t\t\t<Itm>
\t\t\t\t\t<Id>{xe(rng_id("ITM", 10))}</Id>
\t\t\t\t\t<EndToEndId>{xe(e2e_id)}</EndToEndId>
\t\t\t\t\t<UETR>{xe(scenario.uetr)}</UETR>
\t\t\t\t\t<Amt Ccy="{xe(ccy)}">{amount}</Amt>
{items}\t\t\t\t</Itm>
\t\t\t</Ntfctn>
\t\t</NtfctnToRcv>
\t</Document>
</BusMsgEnvlp>"""
    return xml


# ── CAMT.052 Generator ─────────────────────────────────────────────────────────

def _gen_camt052(selected: set, idx: int) -> str:
    """
    camt.052 — Bank to Customer Account Report — constructive variant.

    Anchored to one MessageScenario:
      - account holder = scenario.debtor (customer being reported on)
      - account servicer = scenario.debtor_agent (their bank)
      - all amounts in scenario.currency
    """
    from .scenario import MessageScenario

    scenario = MessageScenario.random()
    ccy = scenario.currency
    account_holder = scenario.debtor
    account_servicer_bic = scenario.debtor_agent.bic

    msg_id = rng_id("MSG", 16)
    biz_msg_id = msg_id  # CBPR_COM_R2: BizMsgIdr must equal GrpHdr/MsgId
    cre_dt = rng_datetime()

    rpt = ""
    # Account block with nested Ownr/Svcr and mandatory Ccy
    rpt += f"\t\t\t\t<Acct>\n\t\t\t\t\t<Id><IBAN>{xe(account_holder.iban)}</IBAN></Id>\n"
    rpt += f"\t\t\t\t\t<Ccy>{xe(ccy)}</Ccy>\n"
    if "account_owner" in selected:
        rpt += f"\t\t\t\t\t<Ownr>\n{party_xml('_unused', account_holder.name, account_holder.country, 7).replace('<_unused>','').replace('</_unused>','')}\t\t\t\t\t</Ownr>\n"
    if "account_servicer" in selected:
        rpt += f"\t\t\t\t\t<Svcr>\n{agent_xml('_unused', account_servicer_bic, 7).replace('<_unused>','').replace('</_unused>','')}\t\t\t\t\t</Svcr>\n"
    rpt += "\t\t\t\t</Acct>\n"
    if "balance" in selected:
        bal_amt = rng_amount(ccy)
        rpt += f"""\t\t\t\t<Bal>
\t\t\t\t\t<Tp><CdOrPrtry><Cd>OPBD</Cd></CdOrPrtry></Tp>
\t\t\t\t\t<Amt Ccy="{xe(ccy)}">{bal_amt}</Amt>
\t\t\t\t\t<CdtDbtInd>CRDT</CdtDbtInd>
\t\t\t\t\t<Dt><Dt>{rng_date(0)}</Dt></Dt>
\t\t\t\t</Bal>
"""
    if "transaction_summary" in selected:
        rpt += f"""\t\t\t\t<TxsSummry>
\t\t\t\t\t<TtlNtries><NbOfNtries>1</NbOfNtries></TtlNtries>
\t\t\t\t</TxsSummry>
"""
    if "entry" in selected:
        entry_amt = rng_amount(ccy)
        rpt += f"""\t\t\t\t<Ntry>
\t\t\t\t\t<Amt Ccy="{xe(ccy)}">{entry_amt}</Amt>
\t\t\t\t\t<CdtDbtInd>CRDT</CdtDbtInd>
\t\t\t\t\t<Sts><Cd>BOOK</Cd></Sts>
\t\t\t\t\t<BookgDt><Dt>{rng_date(0)}</Dt></BookgDt>
\t\t\t\t\t<ValDt><Dt>{rng_date(0)}</Dt></ValDt>
\t\t\t\t\t<BkTxCd><Prtry><Cd>VALL</Cd><Issr>CBPR</Issr></Prtry></BkTxCd>
\t\t\t\t</Ntry>
"""

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<BusMsgEnvlp xmlns="urn:swift:xsd:envelope">
\t<AppHdr xmlns="urn:iso:std:iso:20022:tech:xsd:head.001.001.02">
\t\t<Fr>
{apphdr_fi(scenario.sender_bic)}\t\t</Fr>
\t\t<To>
{apphdr_fi(scenario.receiver_bic)}\t\t</To>
\t\t<BizMsgIdr>{xe(biz_msg_id)}</BizMsgIdr>
\t\t<MsgDefIdr>camt.052.001.08</MsgDefIdr>
\t\t<BizSvc>swift.cbprplus.02</BizSvc>
\t\t<CreDt>{cre_dt}</CreDt>
\t</AppHdr>
\t<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.052.001.08">
\t\t<BkToCstmrAcctRpt>
\t\t\t<GrpHdr>
\t\t\t\t<MsgId>{xe(msg_id)}</MsgId>
\t\t\t\t<CreDtTm>{cre_dt}</CreDtTm>
\t\t\t</GrpHdr>
\t\t\t<Rpt>
\t\t\t\t<Id>{xe(rng_id("RPT", 10))}</Id>
\t\t\t\t<RptPgntn>
\t\t\t\t\t<PgNb>1</PgNb>
\t\t\t\t\t<LastPgInd>true</LastPgInd>
\t\t\t\t</RptPgntn>
\t\t\t\t<CreDtTm>{cre_dt}</CreDtTm>
{rpt}\t\t\t</Rpt>
\t\t</BkToCstmrAcctRpt>
\t</Document>
</BusMsgEnvlp>"""
    return xml


# ── CAMT.053 Generator ─────────────────────────────────────────────────────────

def _gen_camt053(selected: set, idx: int) -> str:
    """
    camt.053 — Bank to Customer Statement — constructive variant.

    Same anchor pattern as camt.052: customer account = scenario.debtor,
    their bank = scenario.debtor_agent, balances/entries all in
    scenario.currency. Two mandatory balances (OPBD + CLBD).
    """
    from .scenario import MessageScenario

    scenario = MessageScenario.random()
    ccy = scenario.currency
    account_holder = scenario.debtor
    account_servicer_bic = scenario.debtor_agent.bic

    msg_id = rng_id("MSG", 16)
    biz_msg_id = msg_id  # CBPR_COM_R2: BizMsgIdr must equal GrpHdr/MsgId
    cre_dt = rng_datetime()

    stmt = ""
    # Account block in CBPR+/schema order: Id, Tp, Ccy, Ownr, Svcr.
    stmt += f"""\t\t\t\t<Acct>
\t\t\t\t\t<Id><IBAN>{xe(account_holder.iban)}</IBAN></Id>
\t\t\t\t\t<Tp><Cd>CACC</Cd></Tp>
\t\t\t\t\t<Ccy>{xe(ccy)}</Ccy>
"""
    if "account_owner" in selected:
        stmt += f"\t\t\t\t\t<Ownr>\n{party_xml('_unused', account_holder.name, account_holder.country, 7).replace('<_unused>','').replace('</_unused>','')}\t\t\t\t\t</Ownr>\n"
    if "account_servicer" in selected:
        stmt += f"\t\t\t\t\t<Svcr>\n{agent_xml('_unused', account_servicer_bic, 7).replace('<_unused>','').replace('</_unused>','')}\t\t\t\t\t</Svcr>\n"
    stmt += "\t\t\t\t</Acct>\n"

    # Balance is Mandatory in camt.053 AccountStatement
    bal_amt = rng_amount(ccy)
    stmt += f"""\t\t\t\t<Bal>
\t\t\t\t\t<Tp><CdOrPrtry><Cd>OPBD</Cd></CdOrPrtry></Tp>
\t\t\t\t\t<Amt Ccy="{xe(ccy)}">{bal_amt}</Amt>
\t\t\t\t\t<CdtDbtInd>CRDT</CdtDbtInd>
\t\t\t\t\t<Dt><Dt>{rng_date(0)}</Dt></Dt>
\t\t\t\t</Bal>
\t\t\t\t<Bal>
\t\t\t\t\t<Tp><CdOrPrtry><Cd>CLBD</Cd></CdOrPrtry></Tp>
\t\t\t\t\t<Amt Ccy="{xe(ccy)}">{bal_amt}</Amt>
\t\t\t\t\t<CdtDbtInd>CRDT</CdtDbtInd>
\t\t\t\t\t<Dt><Dt>{rng_date(0)}</Dt></Dt>
\t\t\t\t</Bal>
"""
    if "transaction_summary" in selected:
        stmt += f"""\t\t\t\t<TxsSummry>
\t\t\t\t\t<TtlNtries><NbOfNtries>1</NbOfNtries></TtlNtries>
\t\t\t\t</TxsSummry>
"""
    if "entry" in selected:
        entry_amt = rng_amount(ccy)
        stmt += f"""\t\t\t\t<Ntry>
\t\t\t\t\t<Amt Ccy="{xe(ccy)}">{entry_amt}</Amt>
\t\t\t\t\t<CdtDbtInd>DBIT</CdtDbtInd>
\t\t\t\t\t<Sts><Cd>BOOK</Cd></Sts>
\t\t\t\t\t<BookgDt><Dt>{rng_date(0)}</Dt></BookgDt>
\t\t\t\t\t<ValDt><Dt>{rng_date(0)}</Dt></ValDt>
\t\t\t\t\t<BkTxCd><Prtry><Cd>VALL</Cd><Issr>CBPR</Issr></Prtry></BkTxCd>
\t\t\t\t</Ntry>
"""

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<BusMsgEnvlp xmlns="urn:swift:xsd:envelope">
\t<AppHdr xmlns="urn:iso:std:iso:20022:tech:xsd:head.001.001.02">
\t\t<Fr>
{apphdr_fi(scenario.sender_bic)}\t\t</Fr>
\t\t<To>
{apphdr_fi(scenario.receiver_bic)}\t\t</To>
\t\t<BizMsgIdr>{xe(biz_msg_id)}</BizMsgIdr>
\t\t<MsgDefIdr>camt.053.001.08</MsgDefIdr>
\t\t<BizSvc>swift.cbprplus.02</BizSvc>
\t\t<CreDt>{cre_dt}</CreDt>
\t</AppHdr>
\t<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.08">
\t\t<BkToCstmrStmt>
\t\t\t<GrpHdr>
\t\t\t\t<MsgId>{xe(msg_id)}</MsgId>
\t\t\t\t<CreDtTm>{cre_dt}</CreDtTm>
\t\t\t</GrpHdr>
\t\t\t<Stmt>
\t\t\t\t<Id>{xe(rng_id("STMT", 10))}</Id>
\t\t\t\t<StmtPgntn>
\t\t\t\t\t<PgNb>1</PgNb>
\t\t\t\t\t<LastPgInd>true</LastPgInd>
\t\t\t\t</StmtPgntn>
\t\t\t\t<ElctrncSeqNb>1</ElctrncSeqNb>
\t\t\t\t<LglSeqNb>{idx + 1}</LglSeqNb>
\t\t\t\t<CreDtTm>{cre_dt}</CreDtTm>
\t\t\t\t<FrToDt>
\t\t\t\t\t<FrDtTm>{rng_date(-1)}T00:00:00+00:00</FrDtTm>
\t\t\t\t\t<ToDtTm>{rng_date(0)}T23:59:59+00:00</ToDtTm>
\t\t\t\t</FrToDt>
{stmt}\t\t\t</Stmt>
\t\t</BkToCstmrStmt>
\t</Document>
</BusMsgEnvlp>"""
    return xml


# ── CAMT.054 Generator ─────────────────────────────────────────────────────────

def _gen_camt054(selected: set, idx: int) -> str:
    """
    camt.054 — Bank to Customer Debit/Credit Notification — constructive variant.

    Same anchor pattern as camt.052/053. The notification reports activity on
    one customer account (scenario.debtor) held at scenario.debtor_agent.
    """
    from .scenario import MessageScenario

    scenario = MessageScenario.random()
    ccy = scenario.currency
    account_holder = scenario.debtor
    account_servicer_bic = scenario.debtor_agent.bic

    msg_id = rng_id("MSG", 16)
    biz_msg_id = msg_id  # CBPR_COM_R2: BizMsgIdr must equal GrpHdr/MsgId
    cre_dt = rng_datetime()

    ntfctn = ""
    # Account block in CBPR+/schema order: Id, Tp, Ccy, Ownr, Svcr.
    ntfctn += f"""\t\t\t\t<Acct>
\t\t\t\t\t<Id><IBAN>{xe(account_holder.iban)}</IBAN></Id>
\t\t\t\t\t<Tp><Cd>CACC</Cd></Tp>
\t\t\t\t\t<Ccy>{xe(ccy)}</Ccy>
"""
    if "account_owner" in selected:
        ntfctn += f"\t\t\t\t\t<Ownr>\n{party_xml('_unused', account_holder.name, account_holder.country, 7).replace('<_unused>','').replace('</_unused>','')}\t\t\t\t\t</Ownr>\n"
    if "account_servicer" in selected:
        ntfctn += f"\t\t\t\t\t<Svcr>\n{agent_xml('_unused', account_servicer_bic, 7).replace('<_unused>','').replace('</_unused>','')}\t\t\t\t\t</Svcr>\n"
    ntfctn += "\t\t\t\t</Acct>\n"
    if "entry" in selected:
        entry_amt = rng_amount(ccy)
        cdt_dbt = random.choice(["CRDT", "DBIT"])
        ntfctn += f"""\t\t\t\t<Ntry>
\t\t\t\t\t<NtryRef>{xe(rng_id("NTRY", 12))}</NtryRef>
\t\t\t\t\t<Amt Ccy="{xe(ccy)}">{entry_amt}</Amt>
\t\t\t\t\t<CdtDbtInd>{cdt_dbt}</CdtDbtInd>
\t\t\t\t\t<Sts><Cd>BOOK</Cd></Sts>
\t\t\t\t\t<BookgDt><DtTm>{rng_date(0)}T10:00:00+00:00</DtTm></BookgDt>
\t\t\t\t\t<ValDt><DtTm>{rng_date(0)}T10:00:00+00:00</DtTm></ValDt>
\t\t\t\t\t<BkTxCd><Prtry><Cd>VALL</Cd><Issr>CBPR</Issr></Prtry></BkTxCd>
\t\t\t\t</Ntry>
"""

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<BusMsgEnvlp xmlns="urn:swift:xsd:envelope">
\t<AppHdr xmlns="urn:iso:std:iso:20022:tech:xsd:head.001.001.02">
\t\t<Fr>
{apphdr_fi(scenario.sender_bic)}\t\t</Fr>
\t\t<To>
{apphdr_fi(scenario.receiver_bic)}\t\t</To>
\t\t<BizMsgIdr>{xe(biz_msg_id)}</BizMsgIdr>
\t\t<MsgDefIdr>camt.054.001.08</MsgDefIdr>
\t\t<BizSvc>swift.cbprplus.02</BizSvc>
\t\t<CreDt>{cre_dt}</CreDt>
\t</AppHdr>
\t<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.054.001.08">
\t\t<BkToCstmrDbtCdtNtfctn>
\t\t\t<GrpHdr>
\t\t\t\t<MsgId>{xe(msg_id)}</MsgId>
\t\t\t\t<CreDtTm>{cre_dt}</CreDtTm>
\t\t\t</GrpHdr>
\t\t\t<Ntfctn>
\t\t\t\t<Id>{xe(rng_id("NTFN", 10))}</Id>
\t\t\t\t<CreDtTm>{cre_dt}</CreDtTm>
{ntfctn}\t\t\t</Ntfctn>
\t\t</BkToCstmrDbtCdtNtfctn>
\t</Document>
</BusMsgEnvlp>"""
    return xml


# ── CAMT.055 Generator ─────────────────────────────────────────────────────────

def _gen_camt055(selected: set, idx: int) -> str:
    """
    camt.055 — Customer Payment Cancellation Request — constructive variant.

    Anchored to one MessageScenario:
      - assignor / case creator agent BIC = scenario.sender_bic
      - assignee BIC                       = scenario.receiver_bic
      - original instructed amount currency = scenario.currency
    All BICs sit in the same currency zone so any downstream country/currency
    checks pass.
    """
    from .scenario import MessageScenario

    scenario = MessageScenario.random()
    ccy = scenario.currency
    from_bic = scenario.sender_bic
    to_bic = scenario.receiver_bic

    msg_id = rng_id("MSG", 16)
    biz_msg_id = msg_id  # CBPR_COM_R2: BizMsgIdr must equal GrpHdr/MsgId
    cre_dt = rng_datetime()
    requested_execution_date = rng_date(1)

    # CBPR+/MyStandards expects payment-level cancellation information here.
    # Keep TxInf under OrgnlPmtInfAndCxl and populate exactly one requested date.
    # NOTE: OrgnlTxRef intentionally omitted — strict CBPR+ profile validators
    # reject it at this position. The cancellation reason is sufficient by itself.
    body = f"""\t\t\t\t<OrgnlPmtInfAndCxl>
\t\t\t\t\t<OrgnlPmtInfId>{xe(rng_id("ORIGPMT", 10))}</OrgnlPmtInfId>
\t\t\t\t\t<OrgnlGrpInf>
\t\t\t\t\t\t<OrgnlMsgId>{xe(rng_id("ORIGMSG", 10))}</OrgnlMsgId>
\t\t\t\t\t\t<OrgnlMsgNmId>pain.001.001.09</OrgnlMsgNmId>
\t\t\t\t\t</OrgnlGrpInf>
\t\t\t\t\t<TxInf>
\t\t\t\t\t\t<CxlId>{xe(rng_id("CXLID", 10))}</CxlId>
\t\t\t\t\t\t<Case>
\t\t\t\t\t\t\t<Id>{xe(rng_id("CASE", 10))}</Id>
\t\t\t\t\t\t\t<Cretr>
\t\t\t\t\t\t\t\t<Agt>
\t\t\t\t\t\t\t\t\t<FinInstnId>
\t\t\t\t\t\t\t\t\t\t<BICFI>{xe(from_bic)}</BICFI>
\t\t\t\t\t\t\t\t\t</FinInstnId>
\t\t\t\t\t\t\t\t</Agt>
\t\t\t\t\t\t\t</Cretr>
\t\t\t\t\t\t</Case>
\t\t\t\t\t\t<OrgnlInstrId>{xe(rng_id("ORIINSTR", 10))}</OrgnlInstrId>
\t\t\t\t\t\t<OrgnlEndToEndId>{xe(rng_id("ORIE2E", 10))}</OrgnlEndToEndId>
\t\t\t\t\t\t<OrgnlUETR>{scenario.uetr}</OrgnlUETR>
\t\t\t\t\t\t<OrgnlInstdAmt Ccy="{xe(ccy)}">{rng_amount(ccy)}</OrgnlInstdAmt>
\t\t\t\t\t\t<OrgnlReqdExctnDt>
\t\t\t\t\t\t\t<Dt>{requested_execution_date}</Dt>
\t\t\t\t\t\t</OrgnlReqdExctnDt>
\t\t\t\t\t\t<CxlRsnInf>
\t\t\t\t\t\t\t<Rsn><Cd>{"DUPL" if "cancellation_reason" in selected else "CUST"}</Cd></Rsn>
\t\t\t\t\t\t</CxlRsnInf>
\t\t\t\t\t</TxInf>
\t\t\t\t</OrgnlPmtInfAndCxl>
"""

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<BusMsgEnvlp xmlns="urn:swift:xsd:envelope">
\t<AppHdr xmlns="urn:iso:std:iso:20022:tech:xsd:head.001.001.02">
\t\t<Fr>
{apphdr_fi(from_bic)}\t\t</Fr>
\t\t<To>
{apphdr_fi(to_bic)}\t\t</To>
\t\t<BizMsgIdr>{xe(biz_msg_id)}</BizMsgIdr>
\t\t<MsgDefIdr>camt.055.001.08</MsgDefIdr>
\t\t<BizSvc>swift.cbprplus.02</BizSvc>
\t\t<CreDt>{cre_dt}</CreDt>
\t</AppHdr>
\t<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.055.001.08">
\t\t<CstmrPmtCxlReq>
\t\t\t<Assgnmt>
\t\t\t\t<Id>{xe(rng_id("ASSGNMT", 10))}</Id>
\t\t\t\t<Assgnr><Agt><FinInstnId><BICFI>{xe(from_bic)}</BICFI></FinInstnId></Agt></Assgnr>
\t\t\t\t<Assgne><Agt><FinInstnId><BICFI>{xe(to_bic)}</BICFI></FinInstnId></Agt></Assgne>
\t\t\t\t<CreDtTm>{cre_dt}</CreDtTm>
\t\t\t</Assgnmt>
\t\t\t<Undrlyg>
{body}\t\t\t</Undrlyg>
\t\t</CstmrPmtCxlReq>
\t</Document>
</BusMsgEnvlp>"""
    return xml


# ── CAMT.056 Generator ─────────────────────────────────────────────────────────

def _gen_camt056(selected: set, idx: int) -> str:
    """
    camt.056 — FI to FI Payment Cancellation Request — constructive variant.

    Anchored to one MessageScenario:
      - assignor / case creator agent BIC = scenario.sender_bic
      - assignee BIC                      = scenario.receiver_bic
      - original interbank settlement currency = scenario.currency
      - first TxInf uses scenario.uetr; an optional extra TxInf uses a fresh
        UETR but stays in the same currency zone.
    """
    from .scenario import MessageScenario

    scenario = MessageScenario.random()
    ccy = scenario.currency
    from_bic = scenario.sender_bic
    to_bic = scenario.receiver_bic

    biz_msg_id = rng_id("MSG", 16)  # camt056: standalone BizMsgIdr (no GrpHdr)
    cre_dt = rng_datetime()

    cxl_id = rng_id("CXLID", 10)
    case_id = rng_id("CASE", 10)
    org_msg_id = rng_id("ORIGMSG", 10)
    e2e_id = rng_id("ORIE2E", 10)
    uetr = scenario.uetr
    amt = rng_amount(ccy)
    settle_dt = rng_date(-1)

    reasons = ["DUPL", "CUST", "AGNT", "CURR", "UPAY", "FRAD"]
    reason_cd = random.choice(reasons)

    body = f"""\t\t\t\t<TxInf>
\t\t\t\t\t<CxlId>{xe(cxl_id)}</CxlId>
\t\t\t\t\t<Case>
\t\t\t\t\t\t<Id>{xe(case_id)}</Id>
\t\t\t\t\t\t<Cretr>
\t\t\t\t\t\t\t<Agt>
\t\t\t\t\t\t\t\t<FinInstnId>
\t\t\t\t\t\t\t\t\t<BICFI>{xe(from_bic)}</BICFI>
\t\t\t\t\t\t\t\t</FinInstnId>
\t\t\t\t\t\t\t</Agt>
\t\t\t\t\t\t</Cretr>
\t\t\t\t\t</Case>
\t\t\t\t\t<OrgnlGrpInf>
\t\t\t\t\t\t<OrgnlMsgId>{xe(org_msg_id)}</OrgnlMsgId>
\t\t\t\t\t\t<OrgnlMsgNmId>pacs.008.001.08</OrgnlMsgNmId>
\t\t\t\t\t</OrgnlGrpInf>
\t\t\t\t\t<OrgnlEndToEndId>{xe(e2e_id)}</OrgnlEndToEndId>
\t\t\t\t\t<OrgnlUETR>{xe(uetr)}</OrgnlUETR>
\t\t\t\t\t<OrgnlIntrBkSttlmAmt Ccy="{xe(ccy)}">{amt}</OrgnlIntrBkSttlmAmt>
\t\t\t\t\t<OrgnlIntrBkSttlmDt>{settle_dt}</OrgnlIntrBkSttlmDt>
\t\t\t\t\t<CxlRsnInf>
\t\t\t\t\t\t<Rsn>
\t\t\t\t\t\t\t<Cd>{reason_cd}</Cd>
\t\t\t\t\t\t</Rsn>
\t\t\t\t\t\t<AddtlInf>FI cancellation requested</AddtlInf>
\t\t\t\t\t</CxlRsnInf>
\t\t\t\t</TxInf>
"""
    # Optional: additional OrgnlTxRef information — still in the same currency zone.
    if "original_transaction" in selected:
        body += f"""\t\t\t\t<TxInf>
\t\t\t\t\t<CxlId>{xe(rng_id("ORIGCXL", 10))}</CxlId>
\t\t\t\t\t<OrgnlGrpInf>
\t\t\t\t\t\t<OrgnlMsgId>{xe(rng_id("ORIGMSG2", 10))}</OrgnlMsgId>
\t\t\t\t\t\t<OrgnlMsgNmId>pacs.008.001.08</OrgnlMsgNmId>
\t\t\t\t\t</OrgnlGrpInf>
\t\t\t\t\t<OrgnlEndToEndId>{xe(rng_id("ORIE2E2", 10))}</OrgnlEndToEndId>
\t\t\t\t\t<OrgnlUETR>{rng_uetr()}</OrgnlUETR>
\t\t\t\t\t<OrgnlIntrBkSttlmAmt Ccy="{xe(ccy)}">{rng_amount(ccy)}</OrgnlIntrBkSttlmAmt>
\t\t\t\t\t<OrgnlIntrBkSttlmDt>{rng_date(-2)}</OrgnlIntrBkSttlmDt>
\t\t\t\t\t<CxlRsnInf>
\t\t\t\t\t\t<Rsn><Cd>{reason_cd}</Cd></Rsn>
\t\t\t\t\t\t<AddtlInf>Additional transaction cancellation</AddtlInf>
\t\t\t\t\t</CxlRsnInf>
\t\t\t\t</TxInf>
"""

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<BusMsgEnvlp xmlns="urn:swift:xsd:envelope">
\t<AppHdr xmlns="urn:iso:std:iso:20022:tech:xsd:head.001.001.02">
\t\t<Fr>
{apphdr_fi(from_bic)}\t\t</Fr>
\t\t<To>
{apphdr_fi(to_bic)}\t\t</To>
\t\t<BizMsgIdr>{xe(biz_msg_id)}</BizMsgIdr>
\t\t<MsgDefIdr>camt.056.001.08</MsgDefIdr>
\t\t<BizSvc>swift.cbprplus.02</BizSvc>
\t\t<CreDt>{cre_dt}</CreDt>
\t</AppHdr>
\t<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.056.001.08">
\t\t<FIToFIPmtCxlReq>
\t\t\t<Assgnmt>
\t\t\t\t<Id>{xe(rng_id("ASSGNMT", 10))}</Id>
\t\t\t\t<Assgnr><Agt><FinInstnId><BICFI>{xe(from_bic)}</BICFI></FinInstnId></Agt></Assgnr>
\t\t\t\t<Assgne><Agt><FinInstnId><BICFI>{xe(to_bic)}</BICFI></FinInstnId></Agt></Assgne>
\t\t\t\t<CreDtTm>{cre_dt}</CreDtTm>
\t\t\t</Assgnmt>
\t\t\t<Undrlyg>
{body}\t\t\t</Undrlyg>
\t\t</FIToFIPmtCxlReq>
\t</Document>
</BusMsgEnvlp>"""
    return xml


# ── PAIN.001 Generator ─────────────────────────────────────────────────────────

def _gen_pain001(selected: set, idx: int) -> str:
    """
    pain.001 — Customer Credit Transfer Initiation — constructive variant.

    Anchored to one MessageScenario. InitgPty == Dbtr (the customer initiating
    the credit transfer). All BICs/IBANs/countries derived consistently.
    """
    from .scenario import MessageScenario

    scenario = MessageScenario.random()
    ccy = scenario.currency
    debtor_party = scenario.debtor
    creditor_party = scenario.creditor

    msg_id = rng_id("MSG", 16)
    biz_msg_id = msg_id  # CBPR_COM_R2: BizMsgIdr must equal GrpHdr/MsgId
    pmt_inf_id = rng_id("PMTINF", 10)
    e2e_id = rng_id("E2E", 16)
    instr_id = rng_id("INSTR", 11)
    cre_dt = rng_datetime()
    amount = rng_amount(ccy)

    # InitgPty mandatory — the debtor itself initiates
    initg = party_xml("InitgPty", debtor_party.name, debtor_party.country, 4)

    pmt_tp = ""
    if "payment_type_information" in selected:
        svc = random.choice(SERVICE_LEVELS)
        pmt_tp = f"\t\t\t\t<PmtTpInf><SvcLvl><Cd>{svc}</Cd></SvcLvl></PmtTpInf>\n"

    # Debtor, DebtorAccount, DebtorAgent are MANDATORY in pain.001
    dbtr_info = party_xml("Dbtr", debtor_party.name, debtor_party.country, 4)
    dbtr_info += account_xml("DbtrAcct", debtor_party.iban, 4)
    dbtr_info += agent_xml("DbtrAgt", scenario.debtor_agent.bic, 4)
    if "ultimate_debtor" in selected:
        dbtr_info += party_xml("UltmtDbtr", debtor_party.name + " Group", debtor_party.country, 4)

    cdt_tf = ""
    # CdtrAgt, Cdtr, CdtrAcct are MANDATORY in pain.001
    cdt_tf += agent_xml("CdtrAgt", scenario.creditor_agent.bic, 5)
    cdt_tf += party_xml("Cdtr", creditor_party.name, creditor_party.country, 5)
    cdt_tf += account_xml("CdtrAcct", creditor_party.iban, 5)

    if "ultimate_creditor" in selected:
        cdt_tf += party_xml("UltmtCdtr", creditor_party.name + " Group", creditor_party.country, 5)
    if "remittance_information" in selected:
        cdt_tf += f"\t\t\t\t\t<RmtInf><Ustrd>{xe(rng_id('REF', 16))}</Ustrd></RmtInf>\n"

    # FwdgAgt: optional in pain.001 standard XSD but Mandatory in CBPR+ usage.
    fwdg_agt = agent_xml("FwdgAgt", scenario.sender_bic, 4)

    # NOTE: NbOfTxs is intentionally omitted in <PmtInf>. It is optional in
    # the base XSD and SWIFT CBPR+ validation against the pain.001 usage
    # profile rejects it inside <PmtInf>. The authoritative count lives in
    # <GrpHdr>/<NbOfTxs>.

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<BusMsgEnvlp xmlns="urn:swift:xsd:envelope">
\t<AppHdr xmlns="urn:iso:std:iso:20022:tech:xsd:head.001.001.02">
\t\t<Fr>
{apphdr_fi(scenario.sender_bic)}\t\t</Fr>
\t\t<To>
{apphdr_fi(scenario.receiver_bic)}\t\t</To>
\t\t<BizMsgIdr>{xe(biz_msg_id)}</BizMsgIdr>
\t\t<MsgDefIdr>pain.001.001.09</MsgDefIdr>
\t\t<BizSvc>swift.cbprplus.02</BizSvc>
\t\t<CreDt>{cre_dt}</CreDt>
\t</AppHdr>
\t<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pain.001.001.09">
\t\t<CstmrCdtTrfInitn>
\t\t\t<GrpHdr>
\t\t\t\t<MsgId>{xe(msg_id)}</MsgId>
\t\t\t\t<CreDtTm>{cre_dt}</CreDtTm>
\t\t\t\t<NbOfTxs>1</NbOfTxs>
{initg}{fwdg_agt}\t\t\t</GrpHdr>
\t\t\t<PmtInf>
\t\t\t\t<PmtInfId>{xe(pmt_inf_id)}</PmtInfId>
\t\t\t\t<PmtMtd>TRF</PmtMtd>
{pmt_tp}\t\t\t\t<ReqdExctnDt><Dt>{rng_date(1)}</Dt></ReqdExctnDt>
{dbtr_info}\t\t\t\t<CdtTrfTxInf>
\t\t\t\t\t<PmtId>
\t\t\t\t\t\t<InstrId>{xe(instr_id)}</InstrId>
\t\t\t\t\t\t<EndToEndId>{xe(e2e_id)}</EndToEndId>
\t\t\t\t\t\t<UETR>{rng_uetr()}</UETR>
\t\t\t\t\t</PmtId>
\t\t\t\t\t<Amt><InstdAmt Ccy="{xe(ccy)}">{amount}</InstdAmt></Amt>
{cdt_tf}\t\t\t\t</CdtTrfTxInf>
\t\t\t</PmtInf>
\t\t</CstmrCdtTrfInitn>
\t</Document>
</BusMsgEnvlp>"""
    return xml


# ── PAIN.002 Generator ─────────────────────────────────────────────────────────

def _gen_pain002(selected: set, idx: int) -> str:
    """
    pain.002 — Customer Payment Status Report — constructive variant.

    Anchored to one MessageScenario so AppHdr BICs stay in the same currency
    zone (no body amounts to mismatch, but the BAH consistency matters for
    L3 country/BIC rules).
    """
    from .scenario import MessageScenario

    scenario = MessageScenario.random()
    from_bic = scenario.sender_bic
    to_bic = scenario.receiver_bic
    initg_party = scenario.debtor  # initiating party of the original payment
    msg_id = rng_id("MSG", 16)
    biz_msg_id = msg_id  # CBPR_COM_R2: BizMsgIdr must equal GrpHdr/MsgId
    cre_dt = rng_datetime()
    # OrgnlCreDtTm must be at or before report CreDtTm. Use a fixed earlier time
    # on the same UTC day so timezone offsets stay consistent (CBPR+ TZ rule).
    orgnl_cre_dt = datetime.now(timezone.utc).strftime("%Y-%m-%dT09:00:00+00:00")
    status_codes = ["ACSC", "ACCP", "ACSP", "RJCT", "PDNG"]
    status = random.choice(status_codes)

    # GrpHdr: InitgPty is mandatory in SWIFT CBPR+ usage of pain.002.
    # CBPR+ requires Id instead of Nm.
    initg = (
        f"\t\t\t\t<InitgPty>\n"
        f"\t\t\t\t\t<Id>\n"
        f"\t\t\t\t\t\t<OrgId>\n"
        f"\t\t\t\t\t\t\t<AnyBIC>{xe(from_bic)}</AnyBIC>\n"
        f"\t\t\t\t\t\t</OrgId>\n"
        f"\t\t\t\t\t</Id>\n"
        f"\t\t\t\t</InitgPty>\n"
    )

    # OrgnlGrpInfAndSts XSD sequence:
    #   OrgnlMsgId, OrgnlMsgNmId, OrgnlCreDtTm(0..1), OrgnlNbOfTxs(0..1),
    #   OrgnlCtrlSum(0..1), GrpSts(0..1), StsRsnInf(0..*), NbOfTxsPerSts(0..*)
    # OrgnlCreDtTm must precede GrpSts.
    body = f"""\t\t\t\t<OrgnlGrpInfAndSts>
\t\t\t\t\t<OrgnlMsgId>{xe(rng_id("ORIGMSG", 10))}</OrgnlMsgId>
\t\t\t\t\t<OrgnlMsgNmId>pain.001.001.09</OrgnlMsgNmId>
\t\t\t\t\t<OrgnlCreDtTm>{orgnl_cre_dt}</OrgnlCreDtTm>
\t\t\t\t</OrgnlGrpInfAndSts>
"""
    # Always emit OrgnlPmtInfAndSts. The XSD declares it 0..unbounded but the
    # SWIFT CBPR+ usage of pain.002 requires at least one occurrence so the
    # report is operationally meaningful (it always references the original
    # payment instruction that's being reported on).
    body += f"""\t\t\t\t<OrgnlPmtInfAndSts>
\t\t\t\t\t<OrgnlPmtInfId>{xe(rng_id("ORIGPMT", 10))}</OrgnlPmtInfId>
"""
    if "original_transaction" in selected or status == "RJCT":
        # If status is RJCT we must also emit a StsRsnInf block (PAIN002_RJCT_REQUIRES_RSN).
        tx_rsn = ""
        if status == "RJCT":
            reasons = ["AM04", "AC01", "FF01", "AG01"]
            tx_rsn = (f"\t\t\t\t\t\t<StsRsnInf>\n"
                      f"\t\t\t\t\t\t\t<Rsn><Cd>{random.choice(reasons)}</Cd></Rsn>\n"
                      f"\t\t\t\t\t\t</StsRsnInf>\n")
        body += f"""\t\t\t\t\t<TxInfAndSts>
\t\t\t\t\t\t<OrgnlEndToEndId>{xe(rng_id("ORIE2E", 10))}</OrgnlEndToEndId>
\t\t\t\t\t\t<OrgnlUETR>{rng_uetr()}</OrgnlUETR>
\t\t\t\t\t\t<TxSts>{status}</TxSts>
{tx_rsn}\t\t\t\t\t</TxInfAndSts>
"""
    body += "\t\t\t\t</OrgnlPmtInfAndSts>\n"

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<BusMsgEnvlp xmlns="urn:swift:xsd:envelope">
\t<AppHdr xmlns="urn:iso:std:iso:20022:tech:xsd:head.001.001.02">
\t\t<Fr>
{apphdr_fi(from_bic)}\t\t</Fr>
\t\t<To>
{apphdr_fi(to_bic)}\t\t</To>
\t\t<BizMsgIdr>{xe(biz_msg_id)}</BizMsgIdr>
\t\t<MsgDefIdr>pain.002.001.10</MsgDefIdr>
\t\t<BizSvc>swift.cbprplus.02</BizSvc>
\t\t<CreDt>{cre_dt}</CreDt>
\t</AppHdr>
\t<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pain.002.001.10">
\t\t<CstmrPmtStsRpt>
\t\t\t<GrpHdr>
\t\t\t\t<MsgId>{xe(msg_id)}</MsgId>
\t\t\t\t<CreDtTm>{cre_dt}</CreDtTm>
{initg}\t\t\t</GrpHdr>
{body}\t\t</CstmrPmtStsRpt>
\t</Document>
</BusMsgEnvlp>"""
    return xml


# ── PAIN.008 Generator ─────────────────────────────────────────────────────────

def _gen_pain008(selected: set, idx: int) -> str:
    """
    pain.008 — Customer Direct Debit Initiation — constructive variant.

    Anchored to one MessageScenario. The creditor (merchant) initiates the DD
    against the debtor (customer). All parties/agents/currency consistent.
    """
    from .scenario import MessageScenario

    scenario = MessageScenario.random()
    ccy = scenario.currency
    debtor_party = scenario.debtor
    creditor_party = scenario.creditor

    msg_id = rng_id("MSG", 16)
    biz_msg_id = msg_id  # CBPR_COM_R2: BizMsgIdr must equal GrpHdr/MsgId
    pmt_inf_id = rng_id("PMTINF", 10)
    e2e_id = rng_id("E2E", 16)
    instr_id = rng_id("INSTR", 11)
    mndt_id = rng_id("MNDT", 10)
    cre_dt = rng_datetime()
    amount = rng_amount(ccy)

    # InitgPty is the creditor (merchant) initiating the DD collection
    initg = party_xml("InitgPty", creditor_party.name, creditor_party.country, 4)

    # FwdgAgt is mandatory in SWIFT CBPR+ pain.008 GrpHdr (PAIN008_FWDGAGT_MANDATORY).
    # Reuse the sender BIC from the BAH so the routing chain stays consistent.
    fwdg_agt = agent_xml("FwdgAgt", scenario.sender_bic, 4)

    pmt_tp = ""
    if "payment_type_information" in selected:
        svc = random.choice(SERVICE_LEVELS)
        pmt_tp += f"\t\t\t\t<PmtTpInf><SvcLvl><Cd>{svc}</Cd></SvcLvl></PmtTpInf>\n"

    pmt_body = ""
    # Cdtr, CdtrAcct, CdtrAgt are MANDATORY in pain.008
    pmt_body += party_xml("Cdtr", creditor_party.name, creditor_party.country, 4)
    pmt_body += account_xml("CdtrAcct", creditor_party.iban, 4)
    pmt_body += agent_xml("CdtrAgt", scenario.creditor_agent.bic, 4)
    if "ultimate_creditor" in selected:
        pass # Not allowed in CBPR+ pain.008 profile

    dd_tx = ""
    # DbtrAgt, Dbtr, DbtrAcct are MANDATORY in pain.008
    # CBPR+ often rejects PstlAdr in DbtrAgt here, so we exclude it.
    dd_tx += agent_xml("DbtrAgt", scenario.debtor_agent.bic, 5)
    dd_tx += party_xml("Dbtr", debtor_party.name, debtor_party.country, 5)
    dd_tx += account_xml("DbtrAcct", debtor_party.iban, 5)

    if "ultimate_debtor" in selected:
        dd_tx += party_xml("UltmtDbtr", debtor_party.name + " Group", debtor_party.country, 5)
    if "remittance_information" in selected:
        dd_tx += f"\t\t\t\t\t<RmtInf><Ustrd>{xe(rng_id('REF', 16))}</Ustrd></RmtInf>\n"

    # NOTE: NbOfTxs is intentionally NOT emitted in <PmtInf>. It is optional there
    # (minOccurs="0") and SWIFT's CBPR+ pain.008 usage guide validates against an
    # XSD profile that rejects it in this position. NbOfTxs in <GrpHdr> still
    # provides the authoritative transaction count for the message.

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<BusMsgEnvlp xmlns="urn:swift:xsd:envelope">
\t<AppHdr xmlns="urn:iso:std:iso:20022:tech:xsd:head.001.001.02">
\t\t<Fr>
{apphdr_fi(scenario.sender_bic)}\t\t</Fr>
\t\t<To>
{apphdr_fi(scenario.receiver_bic)}\t\t</To>
\t\t<BizMsgIdr>{xe(biz_msg_id)}</BizMsgIdr>
\t\t<MsgDefIdr>pain.008.001.08</MsgDefIdr>
\t\t<BizSvc>swift.cbprplus.02</BizSvc>
\t\t<CreDt>{cre_dt}</CreDt>
\t</AppHdr>
\t<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pain.008.001.08">
\t\t<CstmrDrctDbtInitn>
\t\t\t<GrpHdr>
\t\t\t\t<MsgId>{xe(msg_id)}</MsgId>
\t\t\t\t<CreDtTm>{cre_dt}</CreDtTm>
\t\t\t\t<NbOfTxs>1</NbOfTxs>
{initg}{fwdg_agt}\t\t\t</GrpHdr>
\t\t\t<PmtInf>
\t\t\t\t<PmtInfId>{xe(pmt_inf_id)}</PmtInfId>
\t\t\t\t<PmtMtd>DD</PmtMtd>
\t\t\t\t<ReqdColltnDt>{rng_date(2)}</ReqdColltnDt>
{pmt_body}\t\t\t\t<DrctDbtTxInf>
\t\t\t\t\t<PmtId>
\t\t\t\t\t\t<InstrId>{xe(instr_id)}</InstrId>
\t\t\t\t\t\t<EndToEndId>{xe(e2e_id)}</EndToEndId>
\t\t\t\t\t\t<UETR>{rng_uetr()}</UETR>
\t\t\t\t\t</PmtId>
\t\t\t\t\t<InstdAmt Ccy="{xe(ccy)}">{amount}</InstdAmt>
\t\t\t\t\t<DrctDbtTx>
\t\t\t\t\t\t<MndtRltdInf>
\t\t\t\t\t\t\t<MndtId>{xe(mndt_id)}</MndtId>
\t\t\t\t\t\t\t<DtOfSgntr>{rng_date(-30)}</DtOfSgntr>
\t\t\t\t\t\t</MndtRltdInf>
\t\t\t\t\t</DrctDbtTx>
{dd_tx}\t\t\t\t</DrctDbtTxInf>
\t\t\t</PmtInf>
\t\t</CstmrDrctDbtInitn>
\t</Document>
</BusMsgEnvlp>"""
    return xml


# ── Single Message Generator ───────────────────────────────────────────────────

def generate_single_xml(
    message_type: str,
    selected_blocks: List[str],
    idx: int
) -> str:
    """Generate one ISO 20022 XML message for the given type and return raw XML."""
    selected = set(b.lower() for b in selected_blocks)

    # Always include mandatory blocks; randomly include optional ones so each
    # generated message has a different structure (50–80% inclusion per block).
    blocks_for_type = get_blocks_for_message(message_type)
    block_map = {blk["id"].lower(): blk for blk in blocks_for_type}
    mandatory_ids = {blk["id"].lower() for blk in blocks_for_type if blk.get("mandatory")}
    optional_ids  = {blk["id"].lower() for blk in blocks_for_type if not blk.get("mandatory")}

    # Start with mandatory blocks + any user-pre-selected optional blocks,
    # then randomly add more optional blocks (65% chance each).
    candidate_optional = selected & optional_ids  # user pre-selected optional
    for bid in optional_ids:
        if bid not in candidate_optional and random.random() < 0.65:
            candidate_optional.add(bid)

    # Enforce `requires` dependency: if a block requires another, that parent
    # must also be included (walk the chain until all deps are satisfied).
    def _ensure_deps(bid: str, acc: set):
        blk = block_map.get(bid)
        if not blk:
            return
        for req in blk.get("requires", []):
            req_lower = req.lower()
            if req_lower not in acc:
                acc.add(req_lower)
                _ensure_deps(req_lower, acc)

    for bid in list(candidate_optional):
        _ensure_deps(bid, candidate_optional)

    selected = mandatory_ids | candidate_optional

    selected_blocks_ctx.set(selected)
    msg_lower = message_type.lower()
    if "pacs.008" in msg_lower:
        xml = _gen_pacs008(selected, idx)
        xml = _normalize_charges_information(xml)
        _validate_charges_information(xml)
        xml = _normalize_postal_addresses(xml)
        xml = _normalize_cbpr_r9(xml)
        return xml
    elif "pacs.009" in msg_lower and "cov" in msg_lower:
        xml = _gen_pacs009(selected, idx, is_cov=True)
        xml = _normalize_postal_addresses(xml)
        xml = _normalize_cbpr_r9(xml)
        _validate_postal_address(xml)
        return xml
    elif "pacs.009" in msg_lower and "adv" in msg_lower:
        xml = _gen_pacs009(selected, idx, is_adv=True)
        xml = _normalize_postal_addresses(xml)
        xml = _normalize_cbpr_r9(xml)
        return xml
    elif "pacs.009" in msg_lower:
        xml = _gen_pacs009(selected, idx)
        xml = _normalize_postal_addresses(xml)
        xml = _normalize_cbpr_r9(xml)
        return xml
    elif "pacs.004" in msg_lower:
        xml = _gen_pacs004(selected, idx)
        xml = _normalize_charges_information(xml)
        _validate_charges_information(xml)
        xml = _normalize_postal_addresses(xml)
        xml = _normalize_cbpr_r9(xml)
        _validate_postal_address(xml)
        return xml
    elif "pacs.003" in msg_lower:
        xml = _gen_pacs003(selected, idx)
        xml = _normalize_charges_information(xml)
        _validate_charges_information(xml)
        xml = _normalize_postal_addresses(xml)
        xml = _normalize_cbpr_r9(xml)
        return xml
    elif "pacs.002" in msg_lower:
        xml = _gen_pacs002(selected, idx)
        xml = _normalize_cbpr_r9(xml)
        return xml
    elif "pacs.010" in msg_lower:
        xml = _gen_pacs010(selected, idx)
        xml = _normalize_postal_addresses(xml)
        xml = _normalize_cbpr_r9(xml)
        return xml
    # CAMT generators
    elif "camt.057" in msg_lower:
        xml = _gen_camt057(selected, idx)
        xml = _normalize_postal_addresses(xml)
        xml = _normalize_cbpr_r9(xml)
        _validate_postal_address(xml)
        return xml
    elif "camt.052" in msg_lower:
        xml = _gen_camt052(selected, idx)
        xml = _normalize_postal_addresses(xml)
        xml = _normalize_cbpr_r9(xml)
        _validate_postal_address(xml)
        return xml
    elif "camt.053" in msg_lower:
        xml = _gen_camt053(selected, idx)
        xml = _normalize_cbpr_r9(xml)
        return xml
    elif "camt.054" in msg_lower:
        xml = _gen_camt054(selected, idx)
        xml = _normalize_cbpr_r9(xml)
        return xml
    elif "camt.055" in msg_lower:
        xml = _gen_camt055(selected, idx)
        xml = _normalize_postal_addresses(xml)
        xml = _normalize_cbpr_r9(xml)
        return xml
    elif "camt.056" in msg_lower:
        xml = _gen_camt056(selected, idx)
        xml = _normalize_cbpr_r9(xml)
        return xml
    # PAIN generators — always normalize postal addresses so AdrLine/TwnNm/Ctry
    # coexistence and the 2-AdrLine maximum hold even when agent_xml randomly
    # injects an inner <PstlAdr>.
    elif "pain.001" in msg_lower:
        xml = _gen_pain001(selected, idx)
        xml = _normalize_postal_addresses(xml)
        xml = _normalize_cbpr_r9(xml)
        _validate_postal_address(xml)
        return xml
    elif "pain.002" in msg_lower:
        xml = _gen_pain002(selected, idx)
        xml = _normalize_postal_addresses(xml)
        xml = _normalize_cbpr_r9(xml)
        _validate_postal_address(xml)
        return xml
    elif "pain.008" in msg_lower:
        xml = _gen_pain008(selected, idx)
        xml = _normalize_postal_addresses(xml)
        xml = _normalize_cbpr_r9(xml)
        _validate_postal_address(xml)
        return xml
    else:
        xml = _gen_pacs008(selected, idx)
        xml = _normalize_postal_addresses(xml)
        xml = _normalize_cbpr_r9(xml)
        return xml


# ── Main Generator ─────────────────────────────────────────────────────────────

def generate_bulk_messages(
    message_type: str,
    count: int,
    selected_blocks: List[str]
) -> List[Dict[str, Any]]:
    
    results = []
    for i in range(1, count + 1):
        try:
            xml = generate_single_xml(message_type, selected_blocks, i)
            results.append({
                "index": i,
                "xml": xml,
                "message_type": message_type,
                "status": "generated"
            })
        except Exception as e:
            results.append({
                "index": i,
                "xml": f"<!-- Generation error: {str(e)} -->",
                "message_type": message_type,
                "status": "error",
                "error": str(e)
            })

    return results
