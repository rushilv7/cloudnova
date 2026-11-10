"""Live cases: real question interpretation through the configured model.

These run only with `python -m evals --live` and credentials in `.env`; without
them every case is reported as SKIP, which is not a pass. Each case sends one
question through the model, validation, execution and formatting.

Expected values for the six baseline questions come from the spec (via
evals/expected.py). Expected values for the unseen questions come from
reference SQL written by hand below and run directly against the views, so
these cases test interpretation, not the cleaning rules a second time.

No question in this file appears in the model prompt (checked offline by
`prompt_contract`).
"""

import json
import re
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from app.__main__ import DEMO_QUESTIONS
from evals import expected as E
from evals.harness import Skip, XFail, case, check, eq, live, table

R = E.REFERENCE

# One rewording of each baseline question, in the same order as DEMO_QUESTIONS.
PARAPHRASES = (
    "How much revenue did paid invoices bring in during calendar year 2024, in US dollars?",
    "Where is average monthly recurring revenue per account the highest, by region?",
    "What is the total amount refunded to customers?",
    "Compare the share of churned accounts between the Starter and Enterprise plans.",
    "Who are our five biggest accounts by revenue, and do any of them have a CSAT score of 2 or lower?",
    "What is the count and USD value of invoices that are still pending or have failed?",
)
UNSEEN_QUESTIONS = {
    "monthly_breakdown": "Show monthly net revenue for EMEA in 2024, split by plan.",
    "account_comparison": "Compare average MRR by industry for non-churned Pro accounts.",
    "ticket_threshold": "How many accounts have at least 5 support tickets?",
    "record_lookup": "List annual invoices over USD 10,000 with discounts of at least 15%.",
}
OTHER_QUESTIONS = {
    "ambiguous": "Who are our best customers?",
    "relative_period": "What was our revenue last year?",
    "gross_margin": "What is our gross margin?",
    "churn_causes": "What caused our customers to churn?",
    "weather": "What is tomorrow's weather?",
    "explain": "Why is annual MRR not the annual invoice divided by 12?",
    "unsupported": "Show month-over-month net revenue growth for every month.",
    "monthly_churn": "What was Enterprise monthly churn in March 2024?",
}
# Held-out questions chosen by the reviewer after the build: (question, view, reference SQL returning the
# values that must appear in the answer). Empty until they are supplied; see docs/development-plan.md.
HELD_OUT: list[tuple[str, str, str]] = []

ALL_QUESTIONS = (*DEMO_QUESTIONS, *PARAPHRASES, *UNSEEN_QUESTIONS.values(), *OTHER_QUESTIONS.values(),
                 *(question for question, _view, _sql in HELD_OUT))

_MONTH = re.compile(r"\d{4}-\d{2}")


# --- helpers ------------------------------------------------------------------

def _ask(question: str):
    from app import llm

    settings, transport = live()
    return llm.ask(question, settings, transport)


def _describe(answer) -> str:
    return f"{answer.kind} {answer.error_code} {answer.message or answer.sql}".strip()


def _result(answer):
    check(answer.kind == "result", f"expected a result, got {_describe(answer)}")
    return answer


def _cells(answer, index: int) -> list:
    """Raw values of one result row followed by its application-computed ratios."""
    return list(answer.rows[index]) + [values[index] for values in answer.derived.values()]


def _matches(cell, want) -> bool:
    if isinstance(want, Decimal):
        return isinstance(cell, Decimal) and cell == want
    if isinstance(cell, Decimal) or type(cell) is not type(want):
        return False
    if isinstance(want, str) and _MONTH.fullmatch(want):
        return cell.startswith(want)          # a month may be shown as 2024-03 or 2024-03-01
    return cell == want


def _row_with(answer, *values) -> int:
    """Index of the first row that contains all the given values, in any column."""
    for index in range(len(answer.rows)):
        cells = _cells(answer, index)
        if all(any(_matches(cell, want) for cell in cells) for want in values):
            return index
    raise AssertionError(f"no result row contains {values!r}; rows were {answer.rows!r}, derived {answer.derived!r}")


def _evidence_ids(answer, key: str) -> list[str]:
    check(answer.evidence_path, "answer has no evidence file")
    document = json.loads(Path(answer.evidence_path).read_text(encoding="utf-8"))
    return sorted(item[key] for item in document["population"])


def _usd(cents: int, count: int) -> Decimal:
    return (Decimal(cents) / Decimal(count) / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# --- the six baseline questions and one paraphrase each ----------------------

def _paid_2024(a):
    _result(a)
    eq(a.source_view, "invoice_facts", "view")
    _row_with(a, R["paid_2024_cents"])
    eq(a.population_count, R["paid_2024_invoices"], "evaluated invoices (status and 2024 filters)")
    eq(a.exclusions["invoice_ids"], R["paid_2024_disputed_ids"], "disputed invoices matching the filter")
    check(a.partial and "invoice_conflicts" in a.warning_codes, "must be labelled an eligible subtotal")
    eq(len(_evidence_ids(a, "invoice_id")), R["paid_2024_invoices"], "evidence lists every contributing invoice")


def _region_mrr(a):
    _result(a)
    eq(a.source_view, "account_facts", "view")
    top = _cells(a, 0)
    check(R["top_region"] in top, f"top row should be {R['top_region']}, got {top!r}")
    check(any(_matches(cell, R["top_region_average_mrr_usd"]) for cell in top),
          f"top row should carry average MRR {R['top_region_average_mrr_usd']}, got {top!r}")
    check(R["top_region_active_accounts"] in top, f"top row should count active accounts only, got {top!r}")
    check({"account_estimate", "account_identity"} <= a.warning_codes, "snapshot and identity caveats")


def _refunds(a):
    _result(a)
    _row_with(a, R["refunds_cents"])
    eq(a.population_count, R["refund_invoices"], "evaluated invoices (refunded only)")
    check("all_dates" in a.warning_codes, "no period was asked for, so coverage must be stated")


def _churn(a):
    _result(a)
    eq(a.source_view, "account_facts", "view")
    for plan in ("enterprise", "starter"):
        churned, accounts, percent = R["churn"][plan]
        _row_with(a, churned, accounts)
        _row_with(a, percent)
    check({"churn_proportion", "account_estimate"} <= a.warning_codes, "must not be presented as a period rate")


def _top_five(a):
    _result(a)
    eq(a.source_view, "account_facts", "view")
    eq(len(a.rows), 5, "rows returned")
    check("low_csat_flag" in a.sql or "csat_score" in a.sql, "the answer must show CSAT for the flagging request")
    for position, (account_id, net_cents, csat, low) in enumerate(R["top_five"]):
        cells = _cells(a, position)
        check(account_id in cells and net_cents in cells, f"row {position + 1} should be {account_id} with "
              f"{net_cents} cents, got {cells!r}")
        if csat is None:
            check("Unknown" in a.display_rows[position], f"unknown CSAT for {account_id} must be shown as Unknown")
            check("Yes" not in a.display_rows[position], f"{account_id} must not be flagged low CSAT")
        elif low is not None:
            check(("Yes" in a.display_rows[position]) == bool(low), f"low-CSAT flag for {account_id}")
    check("csat_unknown" in a.warning_codes, "missing CSAT caveat")


def _exposure(a):
    _result(a)
    eq(a.source_view, "invoice_facts", "view")
    _row_with(a, R["exposure_cents"])
    eq(a.population_count, R["exposure_invoices"], "evaluated invoices (pending and failed only)")
    check("status_exposure" in a.warning_codes, "exposure is by status, not by days overdue")


_BASELINE_CHECKS = (
    ("paid_2024", _paid_2024), ("region_mrr", _region_mrr), ("refunds", _refunds),
    ("churn", _churn), ("top_five", _top_five), ("exposure", _exposure),
)


def _register_baselines() -> None:
    for (name, checker), question, paraphrase in zip(_BASELINE_CHECKS, DEMO_QUESTIONS, PARAPHRASES):
        for case_id, text, label in ((f"live_{name}", question, "stakeholder question"),
                                     (f"live_{name}_paraphrase", paraphrase, "paraphrase")):
            def run(text=text, checker=checker):
                checker(_ask(text))

            case(case_id, "live-baseline", f"{label}: {text}", live=True)(run)


_register_baselines()


# --- unseen answerable questions ----------------------------------------------

@case("live_unseen_monthly_breakdown", "live-unseen", UNSEEN_QUESTIONS["monthly_breakdown"], live=True)
def live_unseen_monthly_breakdown():
    settings, _transport = live()
    reference = table(
        settings,
        "SELECT strftime('%Y-%m', invoice_date), plan, SUM(net_revenue_cents) FROM invoice_facts "
        "WHERE region = 'EMEA' AND invoice_date >= '2024-01-01' AND invoice_date < '2025-01-01' GROUP BY 1, 2")
    a = _result(_ask(UNSEEN_QUESTIONS["monthly_breakdown"]))
    eq(a.source_view, "invoice_facts", "view")
    wanted = [row for row in reference if row[2] != 0]
    for month, plan, cents in wanted:
        _row_with(a, month, plan, cents)
    check(len(wanted) <= len(a.rows) <= len(reference),
          f"expected between {len(wanted)} and {len(reference)} month/plan rows, got {len(a.rows)}")
    check("account_estimate" not in a.warning_codes, "an invoice query must not carry account-snapshot caveats")


@case("live_unseen_account_comparison", "live-unseen", UNSEEN_QUESTIONS["account_comparison"], live=True)
def live_unseen_account_comparison():
    settings, _transport = live()
    reference = table(
        settings,
        "SELECT industry, SUM(mrr_cents), COUNT(*) FROM account_facts WHERE plan = 'pro' AND churned = 0 "
        "GROUP BY industry")
    a = _result(_ask(UNSEEN_QUESTIONS["account_comparison"]))
    eq(a.source_view, "account_facts", "view (accounts must not be counted as invoices)")
    for industry, cents, accounts in reference:
        if industry is not None:
            _row_with(a, industry, _usd(cents, accounts))
    check("account_estimate" in a.warning_codes, "account metrics are latest-invoice estimates")


@case("live_unseen_ticket_threshold", "live-unseen", UNSEEN_QUESTIONS["ticket_threshold"], live=True)
def live_unseen_ticket_threshold():
    settings, _transport = live()
    accounts = table(settings, "SELECT COUNT(*) FROM account_facts WHERE support_tickets >= 5")[0][0]
    invoices = table(settings, "SELECT COUNT(*) FROM invoice_facts WHERE support_tickets >= 5")[0][0]
    a = _result(_ask(UNSEEN_QUESTIONS["ticket_threshold"]))
    eq(a.source_view, "account_facts", "view (accounts must not be counted as invoices)")
    check(accounts != invoices, "reference check: the account and invoice counts differ on this file")
    _row_with(a, accounts)


@case("live_unseen_record_lookup", "live-unseen", UNSEEN_QUESTIONS["record_lookup"], live=True)
def live_unseen_record_lookup():
    settings, _transport = live()
    ids = [row[0] for row in table(
        settings,
        "SELECT invoice_id FROM invoice_facts WHERE billing_cycle = 'annual' AND amount_usd_cents > 1000000 "
        "AND discount_bp >= 1500 ORDER BY invoice_id")]
    a = _result(_ask(UNSEEN_QUESTIONS["record_lookup"]))
    eq(_evidence_ids(a, "invoice_id"), ids, "evidence lists exactly the matching invoices")
    eq(a.truncated, len(ids) > settings.row_cap, "truncation is stated exactly when rows were cut")
    eq("truncated" in a.warning_codes, a.truncated, "truncation note")
    check(a.returned_ids is not None and set(a.returned_ids) <= set(ids), "returned rows are matching invoice IDs")


@case("live_held_out", "live-unseen", "Held-out questions chosen by the reviewer after the build", live=True)
def live_held_out():
    if not HELD_OUT:
        raise Skip("no held-out questions have been supplied yet (HELD_OUT in evals/cases_live.py is empty)")
    settings, _transport = live()
    for question, view, sql in HELD_OUT:
        a = _result(_ask(question))
        eq(a.source_view, view, f"view for {question!r}")
        for row in table(settings, sql):
            _row_with(a, *row)


# --- clarification, limits and refusals ---------------------------------------

def _no_figures(answer) -> None:
    check(not answer.rows and not answer.sql and not answer.evidence_path, "no query may run for this request")


@case("live_ambiguous_best_customers", "live-limits", OTHER_QUESTIONS["ambiguous"], live=True)
def live_ambiguous_best_customers():
    a = _ask(OTHER_QUESTIONS["ambiguous"])
    check(a.kind == "clarify", f"expected a clarifying question about the ranking measure, got {_describe(a)}")
    _no_figures(a)


@case("live_relative_period", "live-limits", OTHER_QUESTIONS["relative_period"], live=True)
def live_relative_period():
    a = _ask(OTHER_QUESTIONS["relative_period"])
    check(a.kind == "clarify", f"the ledger has no as-of date, so the period must be asked for; got {_describe(a)}")
    _no_figures(a)


@case("live_missing_gross_margin", "live-limits", OTHER_QUESTIONS["gross_margin"], live=True)
def live_missing_gross_margin():
    a = _ask(OTHER_QUESTIONS["gross_margin"])
    check(a.kind == "unavailable" and a.detail.get("kind") == "missing_data",
          f"expected missing data (no costs), got {_describe(a)}")
    check("cost" in a.message.lower(), f"the reason should name the missing cost data: {a.message!r}")
    _no_figures(a)


@case("live_missing_churn_causes", "live-limits", OTHER_QUESTIONS["churn_causes"], live=True)
def live_missing_churn_causes():
    a = _ask(OTHER_QUESTIONS["churn_causes"])
    check(a.kind == "unavailable" and a.detail.get("kind") == "missing_data",
          f"expected missing data (no cancellation reasons), got {_describe(a)}")
    _no_figures(a)


@case("live_out_of_domain_weather", "live-limits", OTHER_QUESTIONS["weather"], live=True)
def live_out_of_domain_weather():
    a = _ask(OTHER_QUESTIONS["weather"])
    check(a.kind == "out_of_domain", f"expected out of domain, got {_describe(a)}")
    _no_figures(a)


@case("live_explain_annual_mrr", "live-limits", OTHER_QUESTIONS["explain"], live=True)
def live_explain_annual_mrr():
    a = _ask(OTHER_QUESTIONS["explain"])
    check(a.kind == "explain", f"expected an explanation of the rule, got {_describe(a)}")
    check(a.detail.get("source"), "the explanation must name its source")
    _no_figures(a)


@case("live_unsupported_window", "live-limits", OTHER_QUESTIONS["unsupported"], live=True)
def live_unsupported_window():
    a = _ask(OTHER_QUESTIONS["unsupported"])
    check(a.kind == "unavailable" and a.detail.get("kind") == "unsupported_operation",
          f"expected an unsupported-operation limit, distinct from missing data; got {_describe(a)}")
    _no_figures(a)


@case("live_xfail_monthly_churn", "live-limits", OTHER_QUESTIONS["monthly_churn"], live=True)
def live_xfail_monthly_churn():
    a = _ask(OTHER_QUESTIONS["monthly_churn"])
    if a.kind == "result":
        raise AssertionError(f"a churn figure was produced for a period the data cannot support: {a.sql}")
    if a.kind == "unavailable":
        raise XFail(
            "the product cannot answer this: the ledger has no churn-event dates and no historical subscription "
            f"populations. It declined ({a.detail.get('kind')}) instead of fabricating a rate.")
    raise AssertionError(f"expected the question to be declined as unavailable, got {_describe(a)}")
