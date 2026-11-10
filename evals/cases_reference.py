"""Regression against the reference results in specs/solution.md for the supplied CSV.

These cases read the built views with hand-written SQL. They test the cleaning
rules and view definitions, not question interpretation.
"""

import tempfile
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from evals import expected as E
from evals.harness import case, eq, file_sha256, full, report, scalar, table


def _ratio(numerator: int, denominator: int, scale: int = 1) -> Decimal:
    return (Decimal(numerator) * scale / Decimal(denominator)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@case("ref_input_hash", "reference", "The supplied CSV is the file the spec profiled")
def ref_input_hash():
    s = full()
    eq(file_sha256(s.csv_path), E.REFERENCE_SHA256, "CSV SHA-256")
    eq(report(s)["input"]["sha256"], E.REFERENCE_SHA256, "hash recorded in the quality report")


@case("ref_reconciliation", "reference", "5,125 rows = 109 duplicates + 2 rejected + 28 held + 4,986 eligible")
def ref_reconciliation():
    s = full()
    counts = report(s)["counts"]
    for key, want in E.REFERENCE_COUNTS.items():
        eq(counts[key], want, key)
    eq(scalar(s, "SELECT COUNT(*) FROM invoices"), E.REFERENCE_COUNTS["eligible_invoices"], "rows in invoices")
    eq(scalar(s, "SELECT COUNT(*) FROM accounts"), E.REFERENCE_COUNTS["accounts"], "rows in accounts")
    eq(scalar(s, "SELECT COUNT(*) FROM account_facts"), E.REFERENCE_COUNTS["accounts"], "rows in account_facts")


@case("ref_profile", "reference", "Date ambiguity, blanks, malformed emails and date coverage match the spec's profile")
def ref_profile():
    profile = report(full())["profile"]
    for key, want in E.REFERENCE_PROFILE.items():
        eq(profile[key], want, key)


@case("ref_rebuild_deterministic", "reference", "Rebuilding the same input gives the same tables and quality report")
def ref_rebuild_deterministic():
    from app.build import build
    from app.config import load_settings

    first = full()
    with tempfile.TemporaryDirectory(prefix="cloudnova-rebuild-") as tmp:
        second = load_settings(env={}, csv_path=first.csv_path, out_dir=Path(tmp))
        build(second)
        for name, key in (("invoices", "invoice_id"), ("accounts", "account_id")):
            sql = f"SELECT * FROM {name} ORDER BY {key}"
            eq(table(second, sql), table(first, sql), f"{name} table contents")
        eq(Path(second.report_path).read_text(encoding="utf-8"),
           Path(first.report_path).read_text(encoding="utf-8"), "quality report")


@case("ref_paid_2024", "reference", "Paid revenue in 2024 is USD 32,655,352.52 from 888 invoices")
def ref_paid_2024():
    row = table(
        full(),
        "SELECT SUM(paid_revenue_cents), COUNT(*) FROM invoice_facts "
        "WHERE status = 'paid' AND invoice_date >= '2024-01-01' AND invoice_date < '2025-01-01'",
    )[0]
    eq(row, (E.REFERENCE["paid_2024_cents"], E.REFERENCE["paid_2024_invoices"]), "paid revenue and invoice count")


@case("ref_paid_2024_disputed", "reference", "Three disputed 2024 invoices could add USD 31,018.68 of paid revenue")
def ref_paid_2024_disputed():
    conflicts = report(full())["conflicts"]
    ids, extra = [], 0
    for conflict in conflicts:
        paid_2024 = [c for c in conflict["candidates"]
                     if c["status"] == "paid" and "2024-01-01" <= c["invoice_date"] < "2025-01-01"]
        if paid_2024:
            ids.append(conflict["invoice_id"])
            extra += paid_2024[0]["amount_usd_cents"]
    eq(sorted(ids), E.REFERENCE["paid_2024_disputed_ids"], "disputed 2024 invoice IDs with a paid candidate")
    eq(extra, E.REFERENCE["paid_2024_disputed_additional_cents"], "possible additional paid contribution")


@case("ref_region_mrr", "reference", "APAC has the highest average MRR: USD 6,164.03 across 752 active accounts")
def ref_region_mrr():
    rows = table(full(), "SELECT region, SUM(mrr_cents), COUNT(*) FROM account_facts WHERE churned = 0 GROUP BY region")
    best = max(rows, key=lambda r: Decimal(r[1]) / r[2])
    eq(best[0], E.REFERENCE["top_region"], "region with the highest average MRR")
    eq(best[2], E.REFERENCE["top_region_active_accounts"], "active accounts in that region")
    eq(_ratio(best[1], best[2] * 100), E.REFERENCE["top_region_average_mrr_usd"], "average MRR in USD")


@case("ref_refunds", "reference", "Refunds given back are USD 8,925,163.84 from 292 invoices")
def ref_refunds():
    row = table(full(), "SELECT SUM(refunds_cents), COUNT(*) FROM invoice_facts WHERE status = 'refunded'")[0]
    eq(row, (E.REFERENCE["refunds_cents"], E.REFERENCE["refund_invoices"]), "refund total and invoice count")


@case("ref_churn", "reference", "Churn is 170/778 (21.85%) for Enterprise and 353/1,879 (18.79%) for Starter")
def ref_churn():
    rows = table(full(), "SELECT plan, SUM(churned), COUNT(*) FROM account_facts GROUP BY plan")
    got = {plan: (churned, total) for plan, churned, total in rows}
    for plan, (churned, total, pct) in E.REFERENCE["churn"].items():
        eq(got.get(plan), (churned, total), f"{plan} churned/accounts")
        eq(_ratio(churned, total, 100), pct, f"{plan} churn percentage")


@case("ref_top_five", "reference", "Top five accounts by net revenue match the spec table, including unknown CSAT")
def ref_top_five():
    rows = table(
        full(),
        "SELECT account_id, lifetime_net_revenue_cents, csat_score, low_csat_flag FROM account_facts "
        "ORDER BY lifetime_net_revenue_cents DESC, account_id ASC LIMIT 5",
    )
    eq(rows, E.REFERENCE["top_five"], "top five accounts")


@case("ref_exposure", "reference", "Pending/failed exposure is 894 invoices and USD 28,983,255.97")
def ref_exposure():
    row = table(full(), "SELECT COUNT(*), SUM(exposure_cents) FROM invoice_facts WHERE status IN ('pending', 'failed')")[0]
    eq(row, (E.REFERENCE["exposure_invoices"], E.REFERENCE["exposure_cents"]), "invoice count and exposure")
