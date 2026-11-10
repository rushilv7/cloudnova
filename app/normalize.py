"""Field-level cleaning rules (specs/solution.md section 3).

`normalize_row` turns one raw CSV record into a `Candidate`: canonical values,
quality flags for tolerated problems, and errors for problems that make the
record ineligible. Nothing here reads or writes files.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from app import config

RAW_COLUMNS = (
    "invoice_id", "account_id", "account_name", "contact_email", "region", "industry", "plan",
    "billing_cycle", "seats", "currency", "amount", "discount_pct", "status", "payment_method",
    "signup_date", "invoice_date", "churned", "csat_score", "support_tickets",
)

# Canonical source fields compared when deciding whether two records are the same.
# amount_usd_cents is derived from amount and currency, so it is not listed.
CANONICAL_FIELDS = (
    "invoice_id", "account_id", "account_name", "contact_email", "region", "industry", "plan",
    "billing_cycle", "seats", "currency", "amount_local_cents", "discount_bp", "status",
    "payment_method", "signup_date", "invoice_date", "churned", "csat_score", "support_tickets",
)


class FieldError(ValueError):
    """A value cannot be parsed under the documented rules."""


@dataclass(frozen=True)
class Candidate:
    raw: dict
    values: dict
    flags: tuple[str, ...]
    errors: tuple[str, ...]

    @property
    def valid(self) -> bool:
        return not self.errors

    def canonical_key(self) -> tuple:
        return tuple(self.values.get(name) for name in CANONICAL_FIELDS)


# --- amounts ------------------------------------------------------------------

_NUMBER = re.compile(r"(?P<int>\d{1,3}(?:,\d{3})+|\d+)(?:\.(?P<frac>\d{1,2}))?")
_ISO_SUFFIX = re.compile(r"^(?P<rest>.*\S)\s*(?P<code>[A-Za-z]{3})$")
_ISO_PREFIX = re.compile(r"^(?P<code>[A-Za-z]{3})\s*(?P<rest>\S.*)$")


def parse_amount_cents(raw: str, currency: str) -> int:
    """Parse an amount string into integer cents of the invoice currency.

    Accepts a currency symbol or ISO code, grouping commas, whitespace, a minus
    sign and up to two decimal places. `31432` means 31,432.00. A marker that
    names a different currency than the `currency` column is an error.
    """
    text = (raw or "").strip()
    if not text:
        raise FieldError("amount is blank")
    expected = (currency or "").strip().upper()
    markers: set[str] = set()

    for pattern in (_ISO_SUFFIX, _ISO_PREFIX):
        match = pattern.match(text)
        if match and match.group("code").upper() in config.FX_TO_USD:
            markers.add(match.group("code").upper())
            text = match.group("rest").strip()
            break

    negative = False
    if text.startswith("-"):
        negative, text = True, text[1:].lstrip()
    if text[:1] in config.CURRENCY_SYMBOLS:
        markers.add(config.CURRENCY_SYMBOLS[text[0]])
        text = text[1:].lstrip()
    if text.startswith("-"):
        if negative:
            raise FieldError(f"amount {raw!r} has two minus signs")
        negative, text = True, text[1:].lstrip()

    match = _NUMBER.fullmatch(text)
    if not match:
        raise FieldError(f"amount {raw!r} is not a recognized number")
    if markers - {expected}:
        raise FieldError(f"amount {raw!r} is marked {sorted(markers)} but the currency column says {expected!r}")

    cents = int(match.group("int").replace(",", "")) * 100 + int((match.group("frac") or "").ljust(2, "0"))
    return -cents if negative else cents


def to_usd_cents(local_cents: int, currency: str) -> int:
    """Convert local cents to USD cents, rounding each invoice half-up."""
    rate = config.FX_TO_USD.get(currency)
    if rate is None:
        raise FieldError(f"no FX rate for currency {currency!r}")
    return int((Decimal(local_cents) * rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


# --- dates --------------------------------------------------------------------

_MONTHS = {
    name: index
    for index, names in enumerate(
        [("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"), ("may",), ("jun", "june"),
         ("jul", "july"), ("aug", "august"), ("sep", "sept", "september"), ("oct", "october"),
         ("nov", "november"), ("dec", "december")],
        start=1,
    )
    for name in names
}
_YMD = re.compile(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})")
_MDY_SLASH = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
_DMY_HYPHEN = re.compile(r"(\d{1,2})-(\d{1,2})-(\d{4})")
_MONTH_NAME = re.compile(r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})")


def _iso(year: int, month: int, day: int, raw: str) -> str:
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        raise FieldError(f"date {raw!r} is not a real calendar date") from None


def parse_date(raw: str) -> tuple[str, bool]:
    """Return (ISO date, ambiguous). No locale inference.

    YYYY-MM-DD and YYYY/MM/DD are year-month-day; MM/DD/YYYY is US month/day;
    `Jan 5 2024` uses the month name; DD-MM-YYYY is day-month. A hyphen date is
    ambiguous when reading it as month-day also gives a real, different date.
    """
    text = (raw or "").strip()
    if (m := _YMD.fullmatch(text)) and text.count("-") in (0, 2):
        return _iso(int(m[1]), int(m[2]), int(m[3]), raw), False
    if m := _MDY_SLASH.fullmatch(text):
        return _iso(int(m[3]), int(m[1]), int(m[2]), raw), False
    if m := _DMY_HYPHEN.fullmatch(text):
        day, month, year = int(m[1]), int(m[2]), int(m[3])
        iso = _iso(year, month, day, raw)
        ambiguous = False
        if day != month:
            try:
                date(year, day, month)
                ambiguous = True
            except ValueError:
                pass
        return iso, ambiguous
    if m := _MONTH_NAME.fullmatch(text):
        month = _MONTHS.get(m[1].lower())
        if month is None:
            raise FieldError(f"date {raw!r} has an unknown month name")
        return _iso(int(m[3]), month, int(m[2]), raw), False
    raise FieldError(f"date {raw!r} is not in a recognized format")


# --- rows ---------------------------------------------------------------------

_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_DISCOUNT = re.compile(r"[0-9]+(\.[0-9]{1,2})?")
_DIGITS = re.compile(r"[0-9]+")


def _alias(raw: str, aliases: dict, label: str):
    key = (raw or "").strip().lower()
    if key not in aliases:
        raise FieldError(f"unknown {label} {raw!r}")
    return aliases[key]


def _member(raw: str, allowed: tuple[str, ...], label: str) -> str:
    value = (raw or "").strip().upper()
    if value not in allowed:
        raise FieldError(f"unknown {label} {raw!r}")
    return value


def _required_text(raw: str, label: str) -> str:
    value = (raw or "").strip()
    if not value:
        raise FieldError(f"{label} is blank")
    return value


def _seats(raw: str) -> int:
    text = (raw or "").strip()
    if not _DIGITS.fullmatch(text) or int(text) < 1:
        raise FieldError(f"seats {raw!r} is not a positive integer")
    return int(text)


def _discount_bp(raw: str) -> int:
    text = (raw or "").strip()
    if not _DISCOUNT.fullmatch(text):
        raise FieldError(f"discount {raw!r} is not a percentage with at most two decimals")
    basis_points = int(Decimal(text) * 100)
    if not 0 <= basis_points <= 10_000:
        raise FieldError(f"discount {raw!r} is outside 0 to 100")
    return basis_points


def normalize_row(raw: dict) -> Candidate:
    """Normalize one raw record. Required-field problems become errors; optional ones become flags."""
    values: dict = {}
    flags: list[str] = []
    errors: list[str] = []

    def required(raw_field: str, target: str, parse) -> None:
        try:
            values[target] = parse(raw.get(raw_field, ""))
        except FieldError as exc:
            values[target] = None
            errors.append(f"{raw_field}: {exc}")

    required("invoice_id", "invoice_id", lambda v: _required_text(v, "invoice_id"))
    required("account_id", "account_id", lambda v: _required_text(v, "account_id"))
    required("region", "region", lambda v: _member(v, config.REGIONS, "region"))
    required("plan", "plan", lambda v: _alias(v, config.PLAN_ALIASES, "plan"))
    required("billing_cycle", "billing_cycle", lambda v: _alias(v, config.BILLING_CYCLE_ALIASES, "billing cycle"))
    required("seats", "seats", _seats)
    required("currency", "currency", lambda v: _member(v, config.CURRENCIES, "currency"))
    required("discount_pct", "discount_bp", _discount_bp)
    required("status", "status", lambda v: _alias(v, config.STATUS_ALIASES, "status"))
    required("churned", "churned", lambda v: _alias(v, config.CHURN_ALIASES, "churn value"))

    # Amount: the currency column supplies the conversion currency.
    values["amount_local_cents"] = values["amount_usd_cents"] = None
    try:
        local = parse_amount_cents(raw.get("amount", ""), raw.get("currency", ""))
        values["amount_local_cents"] = local
        if values["currency"] is not None:
            values["amount_usd_cents"] = to_usd_cents(local, values["currency"])
    except FieldError as exc:
        errors.append(f"amount: {exc}")

    # Sign rule: refunds are negative, everything else is nonnegative.
    status, local = values["status"], values["amount_local_cents"]
    if status is not None and local is not None:
        if status == "refunded" and local >= 0:
            errors.append("status: a refunded invoice must have a negative amount")
        elif status != "refunded" and local < 0:
            errors.append(f"status: a {status} invoice must not have a negative amount")

    # Dates.
    values["invoice_date"] = None
    try:
        values["invoice_date"], ambiguous = parse_date(raw.get("invoice_date", ""))
        if ambiguous:
            flags.append("invoice_date_ambiguous")
    except FieldError as exc:
        errors.append(f"invoice_date: {exc}")
    values["signup_date"] = None
    try:
        values["signup_date"], ambiguous = parse_date(raw.get("signup_date", ""))
        if ambiguous:
            flags.append("signup_date_ambiguous")
    except FieldError:
        flags.append("signup_date_invalid")
    if values["invoice_date"] and values["signup_date"] and values["invoice_date"] < values["signup_date"]:
        flags.append("invoice_before_signup")

    # Optional text.
    values["account_name"] = (raw.get("account_name") or "").strip() or None
    values["industry"] = (raw.get("industry") or "").strip() or None
    email = (raw.get("contact_email") or "").strip()
    if email and not _EMAIL.fullmatch(email):
        flags.append("email_malformed")
        email = ""
    values["contact_email"] = email or None

    method = (raw.get("payment_method") or "").strip().lower()
    values["payment_method"] = config.PAYMENT_METHOD_ALIASES.get(method)
    if method and values["payment_method"] is None:
        flags.append("payment_method_unknown")

    # Optional numbers: invalid becomes null with a flag.
    csat = (raw.get("csat_score") or "").strip()
    values["csat_score"] = int(csat) if csat in ("1", "2", "3", "4", "5") else None
    if csat and values["csat_score"] is None:
        flags.append("csat_invalid")
    tickets = (raw.get("support_tickets") or "").strip()
    values["support_tickets"] = int(tickets) if _DIGITS.fullmatch(tickets) else None
    if tickets and values["support_tickets"] is None:
        flags.append("support_tickets_invalid")

    return Candidate(raw=dict(raw), values=values, flags=tuple(flags), errors=tuple(errors))
