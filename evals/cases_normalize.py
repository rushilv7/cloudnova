"""Field-level rules from spec section 3. No database and no model."""

from evals import expected as E
from evals.harness import case, check, eq

BASE_ROW = {
    "invoice_id": "T-001",
    "account_id": "ACC-T1",
    "account_name": "Test Co",
    "contact_email": "billing@test.com",
    "region": "NA",
    "industry": "Retail",
    "plan": "Starter",
    "billing_cycle": "monthly",
    "seats": "10",
    "currency": "USD",
    "amount": "490.00",
    "discount_pct": "0",
    "status": "paid",
    "payment_method": "ACH",
    "signup_date": "2023-01-01",
    "invoice_date": "2024-01-05",
    "churned": "false",
    "csat_score": "4",
    "support_tickets": "0",
}


def _normalize(**overrides):
    from app.normalize import normalize_row

    return normalize_row({**BASE_ROW, **overrides})


def _has_error(candidate, field: str) -> bool:
    return any(error.startswith(field + ":") for error in candidate.errors)


@case("amount_parsing", "normalize", "Amount strings parse to local cents without inventing cents")
def amount_parsing():
    from app.normalize import parse_amount_cents

    for raw, currency, cents in E.AMOUNT_PARSING:
        eq(parse_amount_cents(raw, currency), cents, f"amount {raw!r} ({currency})")


@case("amount_rejections", "normalize", "Conflicting markers, bad grouping and nonfinite amounts are rejected")
def amount_rejections():
    from app.normalize import FieldError, parse_amount_cents

    for raw, currency in E.AMOUNT_REJECTED:
        try:
            value = parse_amount_cents(raw, currency)
        except FieldError:
            continue
        raise AssertionError(f"amount {raw!r} ({currency}) should be rejected, got {value!r}")


@case("fx_rounding", "normalize", "FX uses EUR 1.08 / GBP 1.27 and rounds each invoice half-up to cents")
def fx_rounding():
    from app.normalize import to_usd_cents

    for local, currency, usd in E.FX_CONVERSION:
        eq(to_usd_cents(local, currency), usd, f"{local} {currency} cents to USD cents")


@case("date_parsing", "normalize", "Five date formats parse; hyphen day-month dates carry an ambiguity flag")
def date_parsing():
    from app.normalize import parse_date

    for raw, iso, ambiguous in E.DATE_PARSING:
        eq(parse_date(raw), (iso, ambiguous), f"date {raw!r}")


@case("date_rejections", "normalize", "Impossible and unrecognized dates are rejected, not guessed")
def date_rejections():
    from app.normalize import FieldError, parse_date

    for raw in E.DATE_REJECTED:
        try:
            value = parse_date(raw)
        except FieldError:
            continue
        raise AssertionError(f"date {raw!r} should be rejected, got {value!r}")


@case("category_aliases", "normalize", "Plan, status, cycle, churn and payment aliases map to canonical values")
def category_aliases():
    aliases = {
        "plan": {"starter": ["Starter", " START ", "tier 1"], "pro": ["PRO", "Professional", "Tier 2"],
                 "enterprise": ["enterprise", "ENT", "tier 3"]},
        "status": {"paid": ["Paid", "complete", "COMPLETED", "success"], "pending": ["pending", "in_review", "Awaiting"],
                   "failed": ["FAILED", "declined", "error"], "void": ["Void", "cancelled", "canceled"]},
        "billing_cycle": {"monthly": ["Monthly"], "annual": ["annual", "Annually", "YEARLY"]},
        "payment_method": {"credit_card": ["Credit Card", "credit_card"], "ACH": ["ach", "ACH"],
                           "wire": ["Wire", "wire transfer"], "invoice": ["Invoice"], None: ["", "  "]},
        "churned": {1: ["true", "YES", "1", "y"], 0: ["False", "no", "0", "N"]},
    }
    for field, mapping in aliases.items():
        for canonical, raws in mapping.items():
            for raw in raws:
                candidate = _normalize(**{field: raw})
                check(not candidate.errors, f"{field}={raw!r} produced errors {candidate.errors}")
                eq(candidate.values[field], canonical, f"{field} {raw!r}")
    # Refund aliases need a negative amount to be valid.
    for raw in ["Refunded", "REFUND", "charged_back"]:
        candidate = _normalize(status=raw, amount="-490.00")
        check(not candidate.errors, f"status={raw!r} produced errors {candidate.errors}")
        eq(candidate.values["status"], "refunded", f"status {raw!r}")


@case("unknown_categories", "normalize", "Unknown required categories are errors; unknown payment method is null with a flag")
def unknown_categories():
    for field, raw in [("plan", "Platinum"), ("status", "shipped"), ("billing_cycle", "weekly"),
                       ("churned", "maybe"), ("currency", "CAD"), ("region", "MOON")]:
        check(_has_error(_normalize(**{field: raw}), field), f"{field}={raw!r} should be an error")
    candidate = _normalize(payment_method="bitcoin")
    check(not candidate.errors, f"unknown payment method must not be an error: {candidate.errors}")
    eq(candidate.values["payment_method"], None, "unknown payment method value")
    check("payment_method_unknown" in candidate.flags, "unknown payment method should be flagged")


@case("numeric_fields", "normalize", "Seats, discount, CSAT and support tickets follow their type rules")
def numeric_fields():
    eq(_normalize(discount_pct="15").values["discount_bp"], 1500, "discount 15")
    eq(_normalize(discount_pct="12.5").values["discount_bp"], 1250, "discount 12.5")
    eq(_normalize(discount_pct="100").values["discount_bp"], 10000, "discount 100")
    for raw in ["-1", "100.01", "12.345", "abc", ""]:
        check(_has_error(_normalize(discount_pct=raw), "discount_pct"), f"discount {raw!r} should be an error")
    for raw in ["0", "-3", "2.5", "ten", ""]:
        check(_has_error(_normalize(seats=raw), "seats"), f"seats {raw!r} should be an error")
    # Optional values: invalid becomes null with a flag, never an error.
    blank = _normalize(csat_score="")
    eq((blank.values["csat_score"], "csat_invalid" in blank.flags), (None, False), "blank CSAT")
    for raw in ["0", "6", "4.5", "high"]:
        candidate = _normalize(csat_score=raw)
        check(not candidate.errors, f"CSAT {raw!r} must not be an error")
        eq((candidate.values["csat_score"], "csat_invalid" in candidate.flags), (None, True), f"CSAT {raw!r}")
    for raw in ["-1", "1.5", "many"]:
        candidate = _normalize(support_tickets=raw)
        check(not candidate.errors, f"tickets {raw!r} must not be an error")
        eq((candidate.values["support_tickets"], "support_tickets_invalid" in candidate.flags), (None, True),
           f"tickets {raw!r}")


@case("emails_and_nulls", "normalize", "Blank optional fields stay null; malformed emails become null with a flag")
def emails_and_nulls():
    blank = _normalize(contact_email="", industry=" ")
    eq((blank.values["contact_email"], blank.values["industry"]), (None, None), "blank email and industry")
    check("email_malformed" not in blank.flags, "a blank email is missing, not malformed")
    bad = _normalize(contact_email="billing_at_test.com")
    eq(bad.values["contact_email"], None, "malformed email value")
    check("email_malformed" in bad.flags, "malformed email should be flagged")
    eq(bad.raw["contact_email"], "billing_at_test.com", "raw email is preserved")
    good = _normalize(contact_email=" billing@test.com ")
    eq(good.values["contact_email"], "billing@test.com", "valid email")


@case("sign_rule", "normalize", "Refunds must be negative; every other status must be nonnegative")
def sign_rule():
    check(_has_error(_normalize(status="paid", amount="-10.00"), "status"), "negative paid should be an error")
    check(_has_error(_normalize(status="refunded", amount="10.00"), "status"), "positive refund should be an error")
    check(_has_error(_normalize(status="refunded", amount="0.00"), "status"), "zero refund should be an error")
    check(not _normalize(status="refunded", amount="-10.00").errors, "negative refund is valid")
    check(not _normalize(status="void", amount="0.00").errors, "zero void is valid")


@case("date_field_policy", "normalize", "Bad invoice date is an error; bad signup date is null with a flag")
def date_field_policy():
    check(_has_error(_normalize(invoice_date="31-02-2024"), "invoice_date"), "invalid invoice date should be an error")
    signup = _normalize(signup_date="not a date")
    check(not signup.errors, f"invalid signup date must not be an error: {signup.errors}")
    eq(signup.values["signup_date"], None, "invalid signup date value")
    check("signup_date_invalid" in signup.flags, "invalid signup date should be flagged")
    early = _normalize(signup_date="2024-06-01", invoice_date="2024-01-05")
    check(not early.errors, "invoice before signup is flagged, not rejected")
    check("invoice_before_signup" in early.flags, "invoice before signup should be flagged")
    eq(early.values["invoice_date"], "2024-01-05", "invoice date is not silently changed")
    ambiguous = _normalize(invoice_date="05-01-2024", signup_date="03-02-2023")
    check({"invoice_date_ambiguous", "signup_date_ambiguous"} <= set(ambiguous.flags), "ambiguity flags")
