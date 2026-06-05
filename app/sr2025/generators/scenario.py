"""
Constructive party/scenario generation for ISO 20022 bulk messages.

The original bulk_generator.py picks random BIC/IBAN/country/currency values
independently. That produces lots of invalid combinations (country GB + currency
JPY, BIC for DE + IBAN for FR, etc.) which then fail Layer 2/3 validation and
force the retry loop to spin.

This module flips the model: every party is *anchored* to one country, and all
derived fields (BIC, IBAN, currency, postal address) are chosen consistently
with that anchor. A message-level scenario picks one or more party anchors and
threads them through every generated XML block, so the result is valid by
construction.

Usage:
    from .scenario import MessageScenario
    s = MessageScenario.random()
    s.debtor.bic      # consistent with s.debtor.country
    s.debtor.iban     # checksum-valid IBAN for s.debtor.country
    s.currency        # currency that matches both parties' countries
"""
from __future__ import annotations

import random
import string
import uuid
from dataclasses import dataclass, field
from typing import Optional


# ── Country → currency map (only well-behaved pairs) ───────────────────────────
# Restricted to countries we can also generate IBANs for, so the same anchor
# country produces a valid IBAN, BIC, and currency in one shot.
COUNTRY_CURRENCY: dict[str, str] = {
    "GB": "GBP",
    "DE": "EUR",
    "FR": "EUR",
    "NL": "EUR",
    "BE": "EUR",
    "AT": "EUR",
    "IT": "EUR",
    "ES": "EUR",
    "PT": "EUR",
    "SE": "SEK",
    "NO": "NOK",
    "DK": "DKK",
    "CH": "CHF",
}

# BIC location-code component (positions 5-6 of the BIC). Hand-picked so they
# don't end in "0" or "1" (BIC rule), and they look plausible for the country.
BIC_LOCATION: dict[str, str] = {
    "GB": "2L",
    "DE": "FF",
    "FR": "PP",
    "NL": "AA",
    "BE": "BB",
    "AT": "WW",
    "IT": "MM",
    "ES": "MM",
    "PT": "LL",
    "SE": "SS",
    "NO": "KK",
    "DK": "KH",
    "CH": "ZZ",
}

# A curated list of plausible 4-letter institution codes used as BIC prefixes.
BANK_PREFIXES = [
    "CITI", "HSBC", "DEUT", "BNPA", "UBSW", "CRED", "BARC", "RBOS", "LLOY", "INGB",
    "ABNA", "RABO", "BARC", "SOGE", "CACR", "BNPP", "DBSS", "NWBK",
]

FIRST_NAMES = [
    "James", "Emma", "Oliver", "Sophia", "Liam", "Ava", "Noah", "Isabella",
    "Ethan", "Mia", "William", "Charlotte", "Benjamin", "Amelia", "Lucas",
]
LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Wilson", "Martinez", "Anderson", "Taylor", "Thomas", "Jackson",
]
COMPANY_NAMES = [
    "Global Trade Corp", "Alpha Finance Ltd", "Pacific Investments SA",
    "Atlantic Capital Group", "Euro Commerce AG", "Northern Bank Holdings",
    "Premier Financial Services", "International Trade Solutions",
    "Summit Bank Holdings", "Meridian Financial Group", "Horizon Investments Ltd",
]


# ── IBAN: country-specific BBAN + valid MOD-97 check digits ───────────────────

def _iban_check_digits(country: str, bban: str) -> str:
    raw = bban + country + "00"
    numeric = "".join(str(ord(c) - 55) if c.isalpha() else c for c in raw)
    remainder = int(numeric) % 97
    return f"{98 - remainder:02d}"


def _bban_for(country: str) -> str:
    """Build a country-shaped BBAN. Lengths come from ISO 13616 Annex C."""
    digits = string.digits
    upper = string.ascii_uppercase

    if country == "GB":
        return "".join(random.choices(upper, k=4)) + "".join(random.choices(digits, k=14))
    if country == "DE":
        return "".join(random.choices(digits, k=18))
    if country == "FR":
        return "".join(random.choices(digits, k=23))
    if country == "NL":
        return "".join(random.choices(upper, k=4)) + "".join(random.choices(digits, k=10))
    if country == "BE":
        return "".join(random.choices(digits, k=12))
    if country == "AT":
        return "".join(random.choices(digits, k=16))
    if country == "IT":
        return random.choice(upper) + "".join(random.choices(digits, k=22))
    if country == "ES":
        return "".join(random.choices(digits, k=20))
    if country == "PT":
        return "".join(random.choices(digits, k=21))
    if country == "SE":
        return "".join(random.choices(digits, k=20))
    if country == "NO":
        return "".join(random.choices(digits, k=11))
    if country == "DK":
        return "".join(random.choices(digits, k=14))
    if country == "CH":
        return "".join(random.choices(digits, k=17))
    # Fallback: 18 digits (DE shape)
    return "".join(random.choices(digits, k=18))


def make_iban(country: str) -> str:
    """Generate a syntactically- and checksum-valid IBAN for the given country."""
    bban = _bban_for(country)
    check = _iban_check_digits(country, bban)
    return f"{country}{check}{bban}"


def make_bic(country: str, *, branch: bool = True) -> str:
    """Generate a valid 11-char BIC for the given country."""
    bank = random.choice(BANK_PREFIXES)
    loc = BIC_LOCATION.get(country, "AA")
    suffix = "XXX" if branch else ""
    return f"{bank}{country}{loc}{suffix}"


def make_address_country(country: str) -> str:
    """Return the country code to use in PstlAdr.Ctry."""
    return country


def make_person_name() -> str:
    return f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"


def make_company_name() -> str:
    return random.choice(COMPANY_NAMES)


def make_party_name() -> str:
    """Coin toss: company or person."""
    return make_company_name() if random.random() < 0.6 else make_person_name()


# ── Party / scenario dataclasses ──────────────────────────────────────────────

@dataclass
class Party:
    """A single consistent party (debtor, creditor, ultimate-* …)."""
    country: str
    name: str
    iban: str
    bic: str

    @property
    def currency(self) -> str:
        return COUNTRY_CURRENCY[self.country]

    @classmethod
    def anchor(cls, country: Optional[str] = None) -> "Party":
        c = country or random.choice(list(COUNTRY_CURRENCY.keys()))
        return cls(
            country=c,
            name=make_party_name(),
            iban=make_iban(c),
            bic=make_bic(c),
        )


@dataclass
class Agent:
    """A consistent agent (DbtrAgt, CdtrAgt, IntrmyAgt*…)."""
    country: str
    bic: str

    @classmethod
    def anchor(cls, country: Optional[str] = None) -> "Agent":
        c = country or random.choice(list(COUNTRY_CURRENCY.keys()))
        return cls(country=c, bic=make_bic(c))


@dataclass
class MessageScenario:
    """All consistent data needed to build one ISO 20022 payment message.

    The scenario picks ONE settlement currency, then derives party countries
    that legitimately use that currency. Agents are typically same-country as
    their party. This means every cross-field rule (currency/country/IBAN/BIC)
    is satisfied by construction.
    """
    debtor: Party
    creditor: Party
    debtor_agent: Agent
    creditor_agent: Agent
    currency: str
    sender_bic: str       # AppHdr <Fr> BIC
    receiver_bic: str     # AppHdr <To> BIC
    uetr: str = field(default_factory=lambda: str(uuid.uuid4()))

    @classmethod
    def random(cls, *, currency: Optional[str] = None) -> "MessageScenario":
        # Currency anchor first — pick a settlement currency, then pick parties
        # that natively use it. Falls back gracefully if currency unknown.
        if currency and currency in COUNTRY_CURRENCY.values():
            eligible = [c for c, ccy in COUNTRY_CURRENCY.items() if ccy == currency]
        else:
            currency = random.choice(list(set(COUNTRY_CURRENCY.values())))
            eligible = [c for c, ccy in COUNTRY_CURRENCY.items() if ccy == currency]

        dbtr_country = random.choice(eligible)
        cdtr_country = random.choice(eligible)

        debtor = Party.anchor(dbtr_country)
        creditor = Party.anchor(cdtr_country)
        # Agents typically live in the same country as their party
        debtor_agent = Agent.anchor(dbtr_country)
        creditor_agent = Agent.anchor(cdtr_country)

        # Sender / receiver of the AppHdr — independent BICs, but kept in the
        # same currency zone so any downstream country/currency checks pass.
        sender_bic = make_bic(random.choice(eligible))
        receiver_bic = make_bic(random.choice(eligible))

        return cls(
            debtor=debtor,
            creditor=creditor,
            debtor_agent=debtor_agent,
            creditor_agent=creditor_agent,
            currency=currency,
            sender_bic=sender_bic,
            receiver_bic=receiver_bic,
        )

    def make_intermediary_agent(self) -> Agent:
        """Optional intermediary — same currency zone, random country in it."""
        eligible = [c for c, ccy in COUNTRY_CURRENCY.items() if ccy == self.currency]
        return Agent.anchor(random.choice(eligible))
