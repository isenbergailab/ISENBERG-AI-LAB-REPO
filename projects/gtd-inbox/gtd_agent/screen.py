"""The sensitive screen (docs/PRIVACY.md): the local check every forwarded mail passes before anything is stored or any
model sees it.

It leans broad on purpose. A false alarm costs the user one look at the agent inbox's Skipped folder; a miss sends
private mail to a model. Categories are checked in order and the first hit names the skip. the user's own lists
([mail] private_senders and private_words) come last. Mail the user writes to himself is his own capture, like a
Telegram line: it skips the categories, while configured restricted domains, secrets and local lists still apply.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

from .core import SECRET_RE, Settings

RESTRICTED = "restricted work"
YOURS = "your list"


def _words(*phrases: str) -> re.Pattern[str]:
    return re.compile(r"(?<![\w-])(?:" + "|".join(re.escape(p) for p in phrases) + r")(?![\w-])", re.IGNORECASE)


# (category, sender domains, words and phrases, extra patterns)
CATEGORIES: tuple[tuple[str, tuple[str, ...], re.Pattern[str], tuple[re.Pattern[str], ...]], ...] = (
    ("money", (
        "chase.com", "bankofamerica.com", "bofa.com", "wellsfargo.com", "citi.com", "citibank.com", "capitalone.com",
        "americanexpress.com", "aexp.com", "discover.com", "usbank.com", "pnc.com", "tdbank.com", "citizensbank.com",
        "santander.com", "santanderbank.com", "ally.com", "sofi.com", "navyfederal.org", "paypal.com", "venmo.com",
        "cash.app", "squareup.com", "zellepay.com", "wise.com", "revolut.com", "fidelity.com", "vanguard.com",
        "schwab.com", "robinhood.com", "etrade.com", "coinbase.com", "experian.com", "equifax.com", "transunion.com",
        "creditkarma.com", "mohela.com", "aidvantage.com", "nelnet.com", "intuit.com", "hrblock.com", "taxact.com",
        "freetaxusa.com"),
     _words("account number", "routing number", "account ending", "card ending", "statement is ready",
            "statement is available", "account statement", "monthly statement", "e-statement", "estatement",
            "available balance", "account balance", "low balance", "your balance", "payment due", "payment received",
            "payment confirmation", "autopay", "auto pay", "direct deposit", "wire transfer", "bank transfer",
            "transaction", "transactions", "overdraft", "credit card", "debit card", "credit score", "credit report",
            "bank account", "invoice", "bill is ready", "bill is due", "venmo", "zelle", "paypal"),
     (re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)"), re.compile(r"(?i)\bending (?:in|with) \d{4}\b"))),
    ("health", (
        "aetna.com", "cigna.com", "uhc.com", "unitedhealthcare.com", "optum.com", "anthem.com", "humana.com",
        "bcbs.com", "bluecrossma.org", "bluecross.com", "harvardpilgrim.org", "tuftshealthplan.com", "point32health.org",
        "kaiserpermanente.org", "deltadental.com", "deltadentalma.com", "vsp.com", "cvs.com", "cvshealth.com",
        "caremark.com", "walgreens.com", "riteaid.com", "express-scripts.com", "goodrx.com", "questdiagnostics.com",
        "labcorp.com", "zocdoc.com", "onemedical.com", "mychart.com", "mychart.org"),
     _words("lab results", "lab result", "test results", "test result", "prescription", "prescriptions", "refill",
            "pharmacy", "patient portal", "patient id", "dear patient", "new patient", "mychart", "diagnosis",
            "diagnosed", "explanation of benefits", "after visit summary", "visit summary", "copay", "co-pay",
            "deductible", "insurance card", "member id", "medical record", "medical records", "health insurance",
            "dentist", "physician", "therapist", "therapy", "vaccine", "vaccination", "immunization"),
     ()),
    ("accounts", (),
     _words("verification code", "security code", "one-time code", "one-time passcode", "one-time password",
            "passcode", "login code", "sign-in code", "authentication code", "password", "reset your password",
            "two-factor", "2-step", "2fa", "mfa", "new sign-in", "new login", "unusual sign-in", "security alert",
            "suspicious activity", "verify your email", "confirm your email", "verify your account", "recovery code",
            "backup codes", "date of birth", "social security number"),
     (re.compile(r"(?i)\b(?:code|passcode|pin|otp)\b\D{0,30}\d{4,8}\b"),)),
    ("government", (".gov", ".mil"),
     _words("tax return", "tax refund", "tax document", "tax form", "1099", "1098", "1098-t", "w-2", "w2", "irs",
            "social security", "ssn", "jury duty", "jury summons", "passport", "rmv", "dmv", "voter registration",
            "uscis", "immigration", "green card", "citizenship", "visa"),
     (re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)"),)),
    ("legal or housing", ("lemonade.com", "geico.com", "progressive.com", "statefarm.com", "allstate.com",
                          "libertymutual.com"),
     _words("lease", "leasing office", "landlord", "tenant", "rent payment", "rent is due", "rent due", "rent receipt",
            "security deposit", "eviction", "notice to quit", "legal notice", "court date", "court hearing", "summons",
            "subpoena", "attorney", "lawyer", "lawsuit", "settlement", "insurance claim", "claim number",
            "renters insurance"),
     ()),
    ("job or pay", ("adp.com", "workday.com", "myworkday.com", "gusto.com", "paychex.com", "paylocity.com",
                    "justworks.com", "hireright.com", "sterlingcheck.com", "checkr.com"),
     _words("offer letter", "offer of employment", "job offer", "salary", "compensation", "payroll", "pay stub",
            "paystub", "paycheck", "background check", "i-9", "w-4", "signing bonus"),
     ()),
)
ADDRESS_RE = re.compile(r"[\w.!#$%&'*+/=?^`{|}~-]+@[\w-]+(?:\.[\w-]+)+", re.IGNORECASE)
ORDER = (*(category for category, *_ in CATEGORIES), RESTRICTED, "secrets", YOURS)  # how TODAY lists skips


def _domain_hit(address: str, entries: Iterable[str]) -> bool:
    """An entry is an address (exact), a domain (it and its subdomains) or a suffix such as '.gov'."""
    address = address.lower()
    domain = address.rpartition("@")[2]
    for entry in entries:
        entry = entry.strip().lower()
        if "@" in entry.lstrip("@"):
            if address == entry:
                return True
            continue
        entry = entry.lstrip("@")
        if entry and (domain.endswith(entry) if entry.startswith(".") else
                      domain == entry or domain.endswith("." + entry)):
            return True
    return False


def screen(texts: Iterable[str], addresses: Iterable[str], settings: Settings, categories: bool = True) -> str | None:
    """The category that keeps this mail away from every model, or None when it may pass. categories False (mail
    the user wrote to himself) checks ExampleCorp, secrets and his own lists only."""
    texts = [text for text in texts if text]
    addresses = [address.lower() for address in addresses if address]
    embedded_addresses = (match.group(0) for text in texts for match in ADDRESS_RE.finditer(text))
    if any(_domain_hit(a, settings.mail_restricted_domains) for a in (*addresses, *embedded_addresses)):
        return RESTRICTED
    if any(SECRET_RE.search(text) for text in texts):
        return "secrets"
    for category, domains, words, patterns in CATEGORIES if categories else ():
        if any(_domain_hit(a, domains) for a in addresses):
            return category
        if any(words.search(text) or any(p.search(text) for p in patterns) for text in texts):
            return category
    if settings.mail_private_senders and any(_domain_hit(a, settings.mail_private_senders) for a in addresses):
        return YOURS
    if settings.mail_private_words and any(_words(*settings.mail_private_words).search(t) for t in texts):
        return YOURS
    return None
