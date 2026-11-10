"""Build the SQLite database and the quality report from the raw CSV.

The CSV is only read. Outputs go to the configured output directory and are
deterministic: the same input and rules give the same tables and report.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import sqlite3
from decimal import ROUND_HALF_UP, Decimal

from app import config, schema
from app.config import Settings
from app.normalize import RAW_COLUMNS
from app.resolve import (
    ELIGIBLE, EXACT_DUPLICATE, HELD_CONFLICT, NORMALIZED_DUPLICATE, REJECTED_INVALID, Group, Resolution, resolve,
)


class BuildError(Exception):
    """The input cannot be read or does not have the expected columns."""


def read_csv(path) -> tuple[list[dict], str]:
    """Return the raw records (source row 1 is the first data record) and the file's SHA-256."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise BuildError(f"cannot read input CSV {path}: {exc}") from None
    text = data.decode("utf-8-sig")
    reader = csv.DictReader(text.splitlines(keepends=True))
    missing = [column for column in RAW_COLUMNS if column not in (reader.fieldnames or [])]
    if missing:
        raise BuildError(f"input CSV {path} is missing columns: {', '.join(missing)}")
    rows = [{column: (record.get(column) or "") for column in RAW_COLUMNS} for record in reader]
    return rows, hashlib.sha256(data).hexdigest()


def _invoice_record(group: Group) -> dict:
    record = dict(group.candidate.values)
    record["source_row"] = group.representative_row
    record["quality_flags"] = ",".join(sorted(group.candidate.flags))
    return record


def mrr_cents(plan: str, seats: int, discount_bp: int, churned: int) -> int:
    """price x seats x (1 - discount), in USD cents; zero for a churned account. Billing cycle does not matter."""
    if churned:
        return 0
    amount = Decimal(config.PRICE_USD_CENTS[plan]) * seats * (10_000 - discount_bp) / Decimal(10_000)
    return int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def build_accounts(invoices: list[dict]) -> list[dict]:
    """One snapshot per account: the invoice with the greatest date, ties broken by greatest invoice ID."""
    by_account: dict[str, list[dict]] = {}
    for invoice in invoices:
        by_account.setdefault(invoice["account_id"], []).append(invoice)

    accounts = []
    for account_id in sorted(by_account):
        history = by_account[account_id]
        selected = max(history, key=lambda inv: (inv["invoice_date"], inv["invoice_id"]))
        disagreeing = [name for name in schema.ACCOUNT_IDENTITY_FIELDS if len({inv[name] for inv in history}) > 1]
        flags = []
        if any("invoice_date_ambiguous" in inv["quality_flags"].split(",") for inv in history):
            flags.append("snapshot_date_ambiguous")
        accounts.append({
            "account_id": account_id,
            "selected_invoice_id": selected["invoice_id"],
            "selected_invoice_date": selected["invoice_date"],
            # Attributes are copied together from the selected invoice.
            **{name: selected[name] for name in (
                "account_name", "region", "industry", "signup_date", "plan", "billing_cycle", "seats",
                "discount_bp", "churned", "csat_score", "support_tickets")},
            "mrr_cents": mrr_cents(selected["plan"], selected["seats"], selected["discount_bp"], selected["churned"]),
            "disagreeing_fields": ",".join(disagreeing),
            "quality_flags": ",".join(flags),
        })
    return accounts


def _list_price_usd_cents(invoice: dict) -> Decimal:
    months = 10 if invoice["billing_cycle"] == "annual" else 1
    return (Decimal(config.PRICE_USD_CENTS[invoice["plan"]]) * invoice["seats"] * months
            * (10_000 - invoice["discount_bp"]) / Decimal(10_000))


def _fx_discrepancy(invoices: list[dict]) -> dict:
    """Count foreign-currency invoices whose recorded amount equals the USD list price times the FX rate.

    The briefing says amounts are in local currency and are multiplied by the rate
    to reach USD. If the recorded local amount already equals list price x rate,
    converting again overstates USD. This only measures the pattern; conversion
    still follows the briefing.
    """
    foreign = [inv for inv in invoices if inv["currency"] != "USD" and inv["status"] != "refunded"]
    times_rate = equals_list = 0
    for inv in foreign:
        list_price = _list_price_usd_cents(inv)
        scaled = (list_price * config.FX_TO_USD[inv["currency"]]).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        if inv["amount_local_cents"] == int(scaled):
            times_rate += 1
        elif Decimal(inv["amount_local_cents"]) == list_price:
            equals_list += 1
    return {
        "foreign_currency_non_refund_invoices": len(foreign),
        "amount_equals_usd_list_price_times_rate": times_rate,
        "amount_equals_usd_list_price": equals_list,
        "other": len(foreign) - times_rate - equals_list,
    }


def _candidate_entry(group: Group) -> dict:
    return {**group.candidate.values, "flags": sorted(group.candidate.flags), "source_rows": group.source_rows}


def build_report(settings: Settings, sha256: str, rows: list[dict], resolution: Resolution,
                 invoices: list[dict], accounts: list[dict]) -> dict:
    def blank(column: str) -> int:
        return sum(1 for raw in rows if not raw[column].strip())

    def flagged_rows(flag: str) -> list[int]:
        return [n for n, candidate in resolution.row_candidates.items() if flag in candidate.flags]

    ambiguous_invoice_rows = flagged_rows("invoice_date_ambiguous")
    seen_accounts = {c.values["account_id"] for c in resolution.row_candidates.values() if c.values["account_id"]}
    with_snapshot = {account["account_id"] for account in accounts}

    invoices_per_account: dict[str, int] = {}
    for inv in invoices:
        invoices_per_account[inv["account_id"]] = invoices_per_account.get(inv["account_id"], 0) + 1
    multi = [a for a in accounts if invoices_per_account[a["account_id"]] > 1]

    flag_counts: dict[str, int] = {}
    for inv in invoices:
        for flag in filter(None, inv["quality_flags"].split(",")):
            flag_counts[flag] = flag_counts.get(flag, 0) + 1

    dates = [inv["invoice_date"] for inv in invoices]
    counts = {
        "input_rows": len(rows),
        "distinct_invoice_ids": len({raw["invoice_id"].strip() for raw in rows}),
        "distinct_account_ids": len(seen_accounts),
        "exact_duplicate_rows": resolution.count(EXACT_DUPLICATE),
        "normalized_duplicate_rows": resolution.count(NORMALIZED_DUPLICATE),
        "duplicate_rows_removed": resolution.count(EXACT_DUPLICATE) + resolution.count(NORMALIZED_DUPLICATE),
        "rejected_invalid_rows": resolution.count(REJECTED_INVALID),
        "held_conflict_rows": resolution.count(HELD_CONFLICT),
        "held_conflict_invoice_ids": len(resolution.conflicts),
        "eligible_invoices": resolution.count(ELIGIBLE),
        "accounts": len(accounts),
        "accounts_without_snapshot": len(seen_accounts - with_snapshot),
    }
    return {
        "input": {"file": settings.csv_path.name, "sha256": sha256, "rows": len(rows), "columns": list(RAW_COLUMNS)},
        "rules_version": config.RULES_VERSION,
        "rules": {
            "fx_to_usd": {currency: str(rate) for currency, rate in config.FX_TO_USD.items()},
            "price_usd_cents_per_seat_month": config.PRICE_USD_CENTS,
            "rounding": "ROUND_HALF_UP to USD cents per invoice and per account MRR",
            "hyphen_dates": "DD-MM-YYYY is read as day-month; flagged when month-day is also a real, different date",
            "slash_dates": "MM/DD/YYYY is read as US month/day; YYYY/MM/DD as year-month-day",
            "duplicates": "exact and normalized duplicates collapse; one valid candidate wins; "
                          "several distinct valid candidates exclude the invoice",
            "account_snapshot": "invoice with the greatest invoice date, ties broken by greatest invoice ID",
        },
        "counts": counts,
        "profile": {
            "ambiguous_invoice_date_rows": len(ambiguous_invoice_rows),
            "ambiguous_invoice_date_ids": len({rows[n - 1]["invoice_id"].strip() for n in ambiguous_invoice_rows}),
            "ambiguous_signup_date_rows": len(flagged_rows("signup_date_ambiguous")),
            "blank_email_rows": blank("contact_email"),
            "blank_industry_rows": blank("industry"),
            "blank_csat_rows": blank("csat_score"),
            "blank_payment_method_rows": blank("payment_method"),
            "malformed_email_rows": len(flagged_rows("email_malformed")),
            "invoice_date_min": min(dates) if dates else None,
            "invoice_date_max": max(dates) if dates else None,
        },
        "eligible_invoice_flag_counts": dict(sorted(flag_counts.items())),
        "fx_discrepancy": _fx_discrepancy(invoices),
        "account_disagreements": {
            "accounts_with_multiple_eligible_invoices": len(multi),
            "by_field": {name: sum(1 for a in accounts if name in a["disagreeing_fields"].split(","))
                         for name in schema.ACCOUNT_IDENTITY_FIELDS},
        },
        "accounts_without_snapshot": sorted(seen_accounts - with_snapshot),
        "lineage": {g.candidate.values["invoice_id"]: g.source_rows for g in resolution.eligible},
        "conflicts": [
            {
                "invoice_id": invoice_id,
                "account_ids": sorted({g.candidate.values["account_id"] for g in groups}),
                "differing_fields": sorted(
                    name for name in groups[0].candidate.values
                    if len({json.dumps(g.candidate.values[name]) for g in groups}) > 1),
                "candidates": [_candidate_entry(g) for g in groups],
            }
            for invoice_id, groups in sorted(resolution.conflicts.items())
        ],
        "rejected": [
            {
                "invoice_id": g.candidate.raw.get("invoice_id", "").strip(),
                "source_rows": g.source_rows,
                "reasons": list(g.candidate.errors),
            }
            for g in resolution.rejected
        ],
    }


def write_database(settings: Settings, invoices: list[dict], accounts: list[dict]) -> None:
    """Write a fresh database file and move it into place only when complete."""
    settings.out_dir.mkdir(parents=True, exist_ok=True)
    tmp = settings.db_path.with_suffix(".db.tmp")
    tmp.unlink(missing_ok=True)
    conn = sqlite3.connect(tmp)
    try:
        conn.execute(schema.invoices_ddl())
        conn.execute(schema.ACCOUNTS_DDL)
        conn.execute(schema.invoice_facts_ddl())
        conn.execute(schema.ACCOUNT_FACTS_DDL)
        conn.executemany(
            f"INSERT INTO invoices ({', '.join(schema.INVOICE_COLUMNS)}) "
            f"VALUES ({', '.join(':' + c for c in schema.INVOICE_COLUMNS)})",
            invoices,
        )
        conn.executemany(
            f"INSERT INTO accounts ({', '.join(schema.ACCOUNT_COLUMNS)}) "
            f"VALUES ({', '.join(':' + c for c in schema.ACCOUNT_COLUMNS)})",
            accounts,
        )
        conn.commit()
    finally:
        conn.close()
    os.replace(tmp, settings.db_path)


def build(settings: Settings) -> dict:
    """Clean the CSV, write the database and the quality report, and return the report."""
    rows, sha256 = read_csv(settings.csv_path)
    resolution = resolve(rows)
    invoices = sorted((_invoice_record(g) for g in resolution.eligible), key=lambda inv: inv["invoice_id"])
    accounts = build_accounts(invoices)
    report = build_report(settings, sha256, rows, resolution, invoices, accounts)
    write_database(settings, invoices, accounts)
    settings.report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report
