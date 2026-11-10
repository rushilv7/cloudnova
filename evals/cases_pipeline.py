"""Duplicate policy, tables and views on the small hand-calculated ledgers."""

from evals import expected as E
from evals.harness import case, check, eq, fixture, report, scalar, table

MEASURES = (
    "SELECT SUM(paid_revenue_cents), SUM(net_revenue_cents), SUM(refunds_cents), SUM(exposure_cents) "
    "FROM invoice_facts"
)


@case("paid_vs_net", "pipeline", "Paid 100, refund -20, pending 50, void 70 give paid 100, net 80, refunds 20, exposure 50")
def paid_vs_net():
    s = fixture("paid_vs_net")
    paid, net, refunds, exposure = table(s, MEASURES)[0]
    want = E.PAID_VS_NET
    eq(paid, want["paid_revenue_cents"], "paid revenue")
    eq(net, want["net_revenue_cents"], "net revenue")
    eq(refunds, want["refunds_cents"], "refunds given back")
    eq(exposure, want["exposure_cents"], "pending/failed exposure")


@case("year_boundaries", "pipeline", "2024 includes 1 Jan and 31 Dec and excludes the neighbouring days")
def year_boundaries():
    s = fixture("dates")
    row = table(
        s,
        "SELECT SUM(paid_revenue_cents), COUNT(*) FROM invoice_facts "
        "WHERE status = 'paid' AND invoice_date >= '2024-01-01' AND invoice_date < '2025-01-01'",
    )[0]
    eq(row, (E.DATES["paid_2024_cents"], E.DATES["paid_2024_invoices"]), "2024 paid revenue and invoices")


@case("ambiguous_date_convention", "pipeline", "05-01-2024 is counted in January and flagged as ambiguous")
def ambiguous_date_convention():
    s = fixture("dates")
    january = scalar(
        s,
        "SELECT SUM(paid_revenue_cents) FROM invoice_facts "
        "WHERE invoice_date >= '2024-01-01' AND invoice_date < '2024-02-01'",
    )
    eq(january, E.DATES["paid_jan_2024_cents"], "January 2024 paid revenue")
    flagged = [r[0] for r in table(s, "SELECT invoice_id FROM invoice_facts WHERE invoice_date_ambiguous = 1 ORDER BY 1")]
    eq(flagged, E.DATES["ambiguous_invoice_ids"], "ambiguous invoice IDs")


@case("duplicate_dispositions", "pipeline", "Every source row gets one disposition and the counts reconcile")
def duplicate_dispositions():
    s = fixture("duplicates")
    want, counts = E.DUPLICATES, report(s)["counts"]
    for key in ("input_rows", "exact_duplicate_rows", "normalized_duplicate_rows", "rejected_invalid_rows",
                "held_conflict_rows"):
        eq(counts[key], want[key], key)
    eq(
        counts["exact_duplicate_rows"] + counts["normalized_duplicate_rows"] + counts["rejected_invalid_rows"]
        + counts["held_conflict_rows"] + counts["eligible_invoices"],
        counts["input_rows"],
        "row reconciliation",
    )


@case("duplicate_winners", "pipeline", "Exact and alias-only repeats collapse; negative paid/refunded selects refunded")
def duplicate_winners():
    s = fixture("duplicates")
    want = E.DUPLICATES
    rows = table(s, "SELECT invoice_id, status FROM invoices ORDER BY invoice_id")
    eq([r[0] for r in rows], want["eligible_invoice_ids"], "eligible invoice IDs")
    eq(dict(rows), want["status"], "winning status per invoice")
    lineage = report(s)["lineage"]
    eq({k: sorted(v) for k, v in lineage.items()}, want["lineage"], "source rows per eligible invoice")
    eq(scalar(s, "SELECT source_row FROM invoices WHERE invoice_id = 'D-002'"), 3, "representative row is the smallest")


@case("conflict_excluded_and_reported", "pipeline", "A positive paid/pending conflict is excluded from totals and reported")
def conflict_excluded_and_reported():
    s = fixture("duplicates")
    want, rep = E.DUPLICATES, report(s)
    eq([c["invoice_id"] for c in rep["conflicts"]], want["held_conflict_invoice_ids"], "conflict report IDs")
    statuses = sorted(c["status"] for c in rep["conflicts"][0]["candidates"])
    eq(statuses, ["paid", "pending"], "conflict candidates keep both statuses")
    paid, _net, refunds, exposure = table(s, MEASURES)[0]
    eq(paid, want["paid_revenue_cents"], "paid revenue excludes the disputed invoice")
    eq(refunds, want["refunds_cents"], "refunds")
    eq(exposure, want["exposure_cents"], "exposure excludes the disputed invoice")
    eq([r[0] for r in table(s, "SELECT account_id FROM accounts ORDER BY 1")], want["accounts"], "account snapshots")
    eq(rep["accounts_without_snapshot"], want["accounts_without_snapshot"], "accounts with only unresolved invoices")
    rejected = rep["rejected"]
    eq([(r["invoice_id"], r["source_rows"]) for r in rejected], [("D-003", [5])], "rejected candidate")


@case("account_snapshot_rule", "pipeline", "Snapshot is the latest invoice; date ties go to the greatest invoice ID")
def account_snapshot_rule():
    s = fixture("accounts")
    want = E.ACCOUNTS
    eq(scalar(s, "SELECT COUNT(*) FROM accounts"), want["account_count"], "account rows")
    eq(scalar(s, "SELECT COUNT(*) FROM invoices"), want["invoice_count"], "invoice rows")
    rows = table(s, "SELECT account_id, selected_invoice_id, plan, region FROM accounts ORDER BY 1")
    eq({r[0]: r[1] for r in rows}, want["selected_invoice"], "selected invoice per account")
    eq({r[0]: r[2] for r in rows}, want["plan"], "snapshot plan")
    eq({r[0]: r[3] for r in rows}, want["region"], "snapshot region")
    # Attributes are copied together from the selected invoice.
    eq(table(s, "SELECT seats, discount_bp FROM accounts WHERE account_id = 'ACC-5'")[0], (20, 2000),
       "ACC-5 seats and discount come from A-010")


@case("annual_mrr_once", "pipeline", "Two annual invoices for 10 Starter seats give MRR 490 once, not 980 or 4,900/12")
def annual_mrr_once():
    s = fixture("accounts")
    want = E.ACCOUNTS
    eq(dict(table(s, "SELECT account_id, mrr_cents FROM account_facts")), want["mrr_cents"], "MRR per account")
    row = table(s, "SELECT SUM(mrr_cents), COUNT(*) FROM account_facts WHERE churned = 0")[0]
    eq(row, (want["active_mrr_sum_cents"], want["active_accounts"]), "active MRR sum and account count")


@case("churn_account_grain", "pipeline", "Churn counts accounts, so repeated invoices do not change the denominator")
def churn_account_grain():
    s = fixture("accounts")
    rows = table(s, "SELECT plan, SUM(churned), COUNT(*) FROM account_facts GROUP BY plan")
    got = {plan: (churned, total) for plan, churned, total in rows}
    for plan, (churned, total, _pct) in E.ACCOUNTS["churn"].items():
        eq(got.get(plan), (churned, total), f"{plan} churned/accounts")


@case("csat_flag_and_unknown", "pipeline", "CSAT of 2 or less is flagged; missing CSAT stays unknown")
def csat_flag_and_unknown():
    s = fixture("accounts")
    eq(dict(table(s, "SELECT account_id, low_csat_flag FROM account_facts")), E.ACCOUNTS["low_csat_flag"],
       "low-CSAT flag per account")


@case("account_lifetime_totals", "pipeline", "Account revenue totals are aggregated per account before the snapshot join")
def account_lifetime_totals():
    s = fixture("accounts")
    want = E.ACCOUNTS
    eq(dict(table(s, "SELECT account_id, lifetime_paid_revenue_cents FROM account_facts")),
       want["lifetime_paid_cents"], "all-dates paid revenue per account")
    eq(scalar(s, "SELECT COUNT(*) FROM account_facts"), want["account_count"], "the join does not multiply accounts")
    top = [r[0] for r in table(
        s, "SELECT account_id FROM account_facts ORDER BY lifetime_net_revenue_cents DESC, account_id ASC LIMIT 5")]
    eq(top, want["top_accounts_by_net_revenue"], "accounts ranked by net revenue")


@case("account_disagreements", "pipeline", "Account fields that differ across invoices are recorded on the snapshot")
def account_disagreements():
    s = fixture("accounts")
    rows = dict(table(s, "SELECT account_id, disagreeing_fields FROM accounts"))
    region = sorted(a for a, fields in rows.items() if "region" in fields.split(","))
    churn = sorted(a for a, fields in rows.items() if "churned" in fields.split(","))
    eq(region, E.ACCOUNTS["region_disagreement"], "accounts whose region varies")
    eq(churn, E.ACCOUNTS["churn_disagreement"], "accounts whose churn flag varies")
    flagged = [r[0] for r in table(s, "SELECT account_id FROM account_facts WHERE has_attribute_disagreement = 1 ORDER BY 1")]
    check(set(E.ACCOUNTS["region_disagreement"] + E.ACCOUNTS["churn_disagreement"]) <= set(flagged),
          f"view flag should include accounts with disagreements, got {flagged}")


@case("fixture_fx_and_exposure", "pipeline", "EUR and GBP fixture invoices convert per invoice; exposure sums pending and failed")
def fixture_fx_and_exposure():
    s = fixture("unseen")
    want = E.UNSEEN
    eq(scalar(s, "SELECT COUNT(*) FROM invoice_facts"), want["invoice_count"], "eligible invoices")
    eq(scalar(s, "SELECT amount_usd_cents FROM invoice_facts WHERE invoice_id = 'U-003'"), 114_307, "U-003 EUR to USD")
    eq(scalar(s, "SELECT amount_usd_cents FROM invoice_facts WHERE invoice_id = 'U-008'"), 127_000, "U-008 GBP to USD")
    row = table(s, "SELECT SUM(exposure_cents), COUNT(*) FROM invoice_facts WHERE status IN ('pending', 'failed')")[0]
    eq(row, (want["exposure_cents"], want["exposure_invoices"]), "exposure and invoice count")
