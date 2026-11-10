"""Answer contract, run offline from hand-written proposals.

Each case hands the engine a proposal in the same shape the model must return.
No model is called, so these cases test validation, execution, formatting,
warnings and evidence. Question interpretation is tested in cases_live.py.
"""

import json
from pathlib import Path

from evals import expected as E
from evals.harness import case, check, eq, fixture, full, query, run


def _evidence(answer) -> dict:
    check(answer.evidence_path, "answer has no evidence file")
    return json.loads(Path(answer.evidence_path).read_text(encoding="utf-8"))


def _result(answer):
    check(answer.kind == "result", f"expected a result, got {answer.kind}: {answer.message}")
    return answer


# --- unseen question shapes on the hand-calculated fixture --------------------

@case("unseen_monthly_breakdown", "unseen", "Monthly net revenue for EMEA in 2024 by plan (the spec's example query)")
def unseen_monthly_breakdown():
    a = _result(run(fixture("unseen"), query(
        "SELECT strftime('%Y-%m', invoice_date) AS month, plan, SUM(net_revenue_cents) AS net_revenue_cents "
        "FROM invoice_facts WHERE region = :region AND invoice_date >= :start AND invoice_date < :end "
        "GROUP BY month, plan ORDER BY month, plan",
        {"region": "EMEA", "start": "2024-01-01", "end": "2025-01-01"},
        output_units={"net_revenue_cents": "USD cents"},
    )))
    eq(a.rows, E.UNSEEN["emea_2024_monthly_net_by_plan"], "grouped totals")
    eq(a.population_count, E.UNSEEN["emea_2024_population"], "evaluated invoices")
    check("fx_discrepancy" in a.warning_codes, "EUR invoices are in scope, so the FX caveat applies")
    check("account_estimate" not in a.warning_codes, "an invoice query must not carry account-snapshot caveats")
    eq(a.units["net_revenue_cents"], "usd_cents", "unit inferred from the measure")


@case("unseen_date_range_by_region", "unseen", "Net revenue by region for an arbitrary half-open date range")
def unseen_date_range_by_region():
    a = _result(run(fixture("unseen"), query(
        "SELECT region, SUM(net_revenue_cents) AS net_revenue_cents FROM invoice_facts "
        "WHERE invoice_date >= :start AND invoice_date < :end GROUP BY region ORDER BY region",
        {"start": "2024-02-01", "end": "2024-04-01"},
    )))
    eq(a.rows, E.UNSEEN["net_by_region_feb_mar_2024"], "grouped totals")
    eq(a.population_count, E.UNSEEN["feb_mar_2024_population"], "evaluated invoices")


@case("unseen_account_mrr_by_industry", "unseen", "Average MRR by industry for active Pro accounts, at account grain")
def unseen_account_mrr_by_industry():
    a = _result(run(fixture("unseen"), query(
        "SELECT industry, SUM(mrr_cents) AS mrr_cents, COUNT(*) AS accounts FROM account_facts "
        "WHERE plan = :plan AND churned = 0 GROUP BY industry ORDER BY industry",
        {"plan": "pro"},
        view="account_facts",
        derived=[{"name": "average_mrr_usd", "kind": "usd_average", "numerator": "mrr_cents", "denominator": "accounts"}],
    )))
    want = E.UNSEEN["active_pro_mrr_by_industry"]
    eq(a.rows, [(industry, total, n) for industry, total, n, _avg in want], "MRR sum and account count per industry")
    eq(a.derived["average_mrr_usd"], [avg for *_rest, avg in want], "average MRR computed with decimal arithmetic")
    eq(a.population_count, E.UNSEEN["active_pro_population"], "evaluated accounts")
    check("account_estimate" in a.warning_codes, "account metrics are latest-invoice estimates")


@case("unseen_ticket_threshold", "unseen", "Accounts with at least 3 selected support tickets are counted as accounts")
def unseen_ticket_threshold():
    a = _result(run(fixture("unseen"), query(
        "SELECT account_id, support_tickets FROM account_facts WHERE support_tickets >= 3 ORDER BY account_id",
        view="account_facts",
    )))
    eq([r[0] for r in a.rows], E.UNSEEN["accounts_with_tickets_gte_3"], "matching accounts")
    check(len(a.rows) != E.UNSEEN["invoices_with_tickets_gte_3"], "the result must not be the invoice count")
    eq(a.returned_ids, E.UNSEEN["accounts_with_tickets_gte_3"], "returned account IDs")


@case("repeated_account_value_rejected", "unseen", "Summing support tickets across invoices is refused, not answered")
def repeated_account_value_rejected():
    a = run(fixture("unseen"), query("SELECT SUM(support_tickets) AS tickets FROM invoice_facts"))
    eq(a.kind, "error", "answer kind")
    eq(a.detail.get("reject_code"), "repeated_account_value", "rejection reason")


@case("unseen_record_lookup", "unseen", "Annual invoices over USD 10,000 with a discount of at least 15%")
def unseen_record_lookup():
    a = _result(run(fixture("unseen"), query(
        "SELECT invoice_id, account_id, amount_usd_cents, discount_bp FROM invoice_facts "
        "WHERE billing_cycle = :cycle AND amount_usd_cents > :min_cents AND discount_bp >= :min_bp ORDER BY invoice_id",
        {"cycle": "annual", "min_cents": 1_000_000, "min_bp": 1500},
    )))
    ids = E.UNSEEN["annual_over_10k_discount_15"]
    eq([r[0] for r in a.rows], ids, "matching invoice IDs")
    eq(a.truncated, False, "two rows are not truncated")
    check("truncated" not in a.warning_codes, "no truncation note when nothing was cut")
    check("fx_discrepancy" not in a.warning_codes, "only USD invoices match, so no FX caveat")
    evidence = _evidence(a)
    eq(sorted(item["invoice_id"] for item in evidence["population"]), ids, "evidence lists the matching invoices")
    eq({item["invoice_id"]: item["source_rows"] for item in evidence["population"]}, {"U-007": [7], "U-010": [10]},
       "evidence carries raw-row lineage")


@case("ambiguous_account_name", "unseen", "A name shared by two account IDs asks which ID is meant")
def ambiguous_account_name():
    a = run(fixture("unseen"), query(
        "SELECT SUM(net_revenue_cents) AS net_revenue_cents FROM invoice_facts WHERE account_name = :name",
        {"name": "Stark Labs"},
    ))
    eq(a.kind, "clarify", "answer kind")
    eq(a.detail.get("account_ids"), E.UNSEEN["stark_labs_account_ids"], "candidate account IDs")
    check(not a.rows, "no result is returned alongside a clarification")


@case("row_cap_detail_only", "unseen", "The row cap trims displayed detail rows and says so; aggregates are untouched")
def row_cap_detail_only():
    s = fixture("unseen", row_cap=3)
    detail = _result(run(s, query("SELECT invoice_id FROM invoice_facts ORDER BY invoice_id")))
    eq([r[0] for r in detail.rows], E.UNSEEN["first_three_invoice_ids"], "rows shown")
    eq(detail.truncated, True, "truncated flag")
    check("truncated" in detail.warning_codes, "truncation must be stated")
    eq(detail.population_count, E.UNSEEN["invoice_count"], "population is counted before the cap")
    total = _result(run(s, query("SELECT COUNT(*) AS invoices, SUM(exposure_cents) AS exposure_cents FROM invoice_facts")))
    eq(total.rows, [(E.UNSEEN["invoice_count"], E.UNSEEN["exposure_cents"])], "aggregate over all invoices")
    eq(total.truncated, False, "an aggregate row is not truncated")


@case("empty_population_and_zero_denominator", "unseen", "No matching rows is reported as no data; a ratio over zero is unavailable")
def empty_population_and_zero_denominator():
    a = _result(run(fixture("unseen"), query(
        "SELECT SUM(churned) AS churned_accounts, COUNT(*) AS accounts FROM account_facts "
        "WHERE region = :region AND plan = :plan",
        {"region": "LATAM", "plan": "pro"},
        view="account_facts",
        derived=[{"name": "churn_pct", "kind": "percentage", "numerator": "churned_accounts", "denominator": "accounts"}],
    )))
    eq(a.population_count, 0, "evaluated accounts")
    eq(a.no_data, True, "no-data flag")
    eq(a.derived["churn_pct"], [None], "ratio with a zero denominator is unavailable, not zero")


@case("ranking_evidence_distinguishes_groups", "unseen", "A ranking reports the full population and the records in returned groups")
def ranking_evidence_distinguishes_groups():
    a = _result(run(fixture("unseen"), query(
        "SELECT region, SUM(net_revenue_cents) AS net_revenue_cents FROM invoice_facts "
        "GROUP BY region ORDER BY SUM(net_revenue_cents) DESC LIMIT 1",
    )))
    # NA: 2,376,000 + 127,000 = 2,503,000 is the largest regional net revenue.
    eq(a.rows, [("NA", 2_503_000)], "top region")
    eq(a.population_count, E.UNSEEN["invoice_count"], "all invoices were evaluated")
    evidence = _evidence(a)
    returned = sorted(item["invoice_id"] for item in evidence["population"] if item.get("in_returned_group"))
    eq(returned, ["U-007", "U-008", "U-012"], "invoices belonging to the returned group")


# --- the six baseline questions on the supplied CSV ---------------------------

@case("base_paid_2024", "baseline", "Paid revenue in 2024 is an eligible subtotal naming three disputed invoices")
def base_paid_2024():
    a = _result(run(full(), query(
        "SELECT SUM(paid_revenue_cents) AS paid_revenue_cents, COUNT(*) AS invoices FROM invoice_facts "
        "WHERE status = :status AND invoice_date >= :start AND invoice_date < :end",
        {"status": "paid", "start": "2024-01-01", "end": "2025-01-01"},
    ), question="What was our total recognized revenue in USD for paid invoices in 2024?"))
    eq(a.rows, [(E.REFERENCE["paid_2024_cents"], E.REFERENCE["paid_2024_invoices"])], "paid revenue and invoices")
    eq(a.exclusions["invoice_ids"], E.REFERENCE["paid_2024_disputed_ids"], "disputed invoices matching the filter")
    eq(a.exclusions["possible_additional_usd_cents"], E.REFERENCE["paid_2024_disputed_additional_cents"],
       "possible additional contribution")
    eq(a.partial, True, "answer is labelled an eligible subtotal")
    check("invoice_conflicts" in a.warning_codes, "conflict caveat")
    check("32,655,352.52" in a.text(), "formatted total appears in the answer text")
    eq(len(_evidence(a)["population"]), E.REFERENCE["paid_2024_invoices"], "evidence lists every contributing invoice")


@case("base_region_mrr", "baseline", "Average MRR per active account by region, labelled a latest-invoice estimate")
def base_region_mrr():
    a = _result(run(full(), query(
        "SELECT region, SUM(mrr_cents) AS mrr_cents, COUNT(*) AS active_accounts FROM account_facts "
        "WHERE churned = 0 GROUP BY region ORDER BY SUM(mrr_cents) * 1.0 / COUNT(*) DESC",
        view="account_facts",
        derived=[{"name": "average_mrr_usd", "kind": "usd_average", "numerator": "mrr_cents",
                  "denominator": "active_accounts"}],
    ), question="Which region has the highest average MRR per account?"))
    eq(a.rows[0][0], E.REFERENCE["top_region"], "top region")
    eq(a.rows[0][2], E.REFERENCE["top_region_active_accounts"], "active accounts")
    eq(a.derived["average_mrr_usd"][0], E.REFERENCE["top_region_average_mrr_usd"], "average MRR")
    eq(len(a.rows), 4, "all four regions are shown")
    check({"account_estimate", "account_identity"} <= a.warning_codes, "snapshot and identity caveats")


@case("base_refunds", "baseline", "Refunds given back, shown as a positive amount over all available dates")
def base_refunds():
    a = _result(run(full(), query(
        "SELECT SUM(refunds_cents) AS refunds_cents, COUNT(*) AS invoices FROM invoice_facts WHERE status = :status",
        {"status": "refunded"},
    ), question="How much have we given back in refunds?"))
    eq(a.rows, [(E.REFERENCE["refunds_cents"], E.REFERENCE["refund_invoices"])], "refund total and invoices")
    check("all_dates" in a.warning_codes, "no date filter, so the answer states its date coverage")


@case("base_churn", "baseline", "Enterprise vs Starter churn as observed proportions of account snapshots")
def base_churn():
    a = _result(run(full(), query(
        "SELECT plan, SUM(churned) AS churned_accounts, COUNT(*) AS accounts FROM account_facts "
        "WHERE plan IN (:p1, :p2) GROUP BY plan ORDER BY plan",
        {"p1": "enterprise", "p2": "starter"},
        view="account_facts",
        derived=[{"name": "churn_pct", "kind": "percentage", "numerator": "churned_accounts", "denominator": "accounts"}],
    ), question="What's the churn rate among Enterprise accounts vs Starter?"))
    want = E.REFERENCE["churn"]
    eq(a.rows, [("enterprise", *want["enterprise"][:2]), ("starter", *want["starter"][:2])], "churned/accounts")
    eq(a.derived["churn_pct"], [want["enterprise"][2], want["starter"][2]], "churn percentages")
    check({"churn_proportion", "account_estimate"} <= a.warning_codes, "not a period churn rate")


@case("base_top_five", "baseline", "Top five accounts by net revenue with the low-CSAT flag and unknown CSAT kept unknown")
def base_top_five():
    a = _result(run(full(), query(
        "SELECT account_id, lifetime_net_revenue_cents, csat_score, low_csat_flag FROM account_facts "
        "ORDER BY lifetime_net_revenue_cents DESC, account_id ASC LIMIT 5",
        view="account_facts",
    ), question="List the top 5 accounts by revenue, and flag any with CSAT <= 2."))
    eq(a.rows, E.REFERENCE["top_five"], "top five rows")
    eq(a.returned_ids, [row[0] for row in E.REFERENCE["top_five"]], "returned account IDs")
    eq(a.population_count, E.REFERENCE_COUNTS["accounts"], "all account snapshots were ranked")
    eq(a.display_rows[1][2:], ["Unknown", "Unknown"], "missing CSAT is shown as unknown")
    check("csat_unknown" in a.warning_codes, "missing CSAT caveat")


@case("base_exposure", "baseline", "Pending/failed invoice count and exposed amount, partial where statuses are disputed")
def base_exposure():
    a = _result(run(full(), query(
        "SELECT COUNT(*) AS invoices, SUM(exposure_cents) AS exposure_cents FROM invoice_facts "
        "WHERE status IN (:s1, :s2)",
        {"s1": "pending", "s2": "failed"},
    ), question="How many invoices are stuck in pending/failed and what's the exposed $?"))
    eq(a.rows, [(E.REFERENCE["exposure_invoices"], E.REFERENCE["exposure_cents"])], "invoice count and exposure")
    eq(a.partial, bool(a.exclusions["invoice_ids"]), "partial exactly when disputed invoices match the filter")
    check("status_exposure" in a.warning_codes, "exposure is by status; days overdue cannot be established")
