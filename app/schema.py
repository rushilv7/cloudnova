"""Modeled data: two tables (specs/solution.md section 4).

`invoices` holds one eligible row per invoice ID. `accounts` holds one
latest-invoice snapshot per account ID.
"""

from __future__ import annotations

from app import config


def _in(values) -> str:
    return ", ".join(f"'{value}'" for value in values)


INVOICE_COLUMNS = (
    "invoice_id", "account_id", "account_name", "contact_email", "region", "industry", "plan",
    "billing_cycle", "seats", "currency", "amount_local_cents", "amount_usd_cents", "discount_bp",
    "status", "payment_method", "signup_date", "invoice_date", "churned", "csat_score",
    "support_tickets", "source_row", "quality_flags",
)

ACCOUNT_COLUMNS = (
    "account_id", "selected_invoice_id", "selected_invoice_date", "account_name", "region", "industry",
    "signup_date", "plan", "billing_cycle", "seats", "discount_bp", "churned", "csat_score",
    "support_tickets", "mrr_cents", "disagreeing_fields", "quality_flags",
)


def invoices_ddl(table: str = "invoices", primary_key: bool = True) -> str:
    """DDL for the invoices table. Conflict candidates reuse it without the primary key."""
    key = "TEXT PRIMARY KEY" if primary_key else "TEXT NOT NULL"
    return f"""
CREATE TABLE {table} (
    invoice_id          {key},
    account_id          TEXT NOT NULL,
    account_name        TEXT,
    contact_email       TEXT,
    region              TEXT NOT NULL CHECK (region IN ({_in(config.REGIONS)})),
    industry            TEXT,
    plan                TEXT NOT NULL CHECK (plan IN ({_in(config.PLANS)})),
    billing_cycle       TEXT NOT NULL CHECK (billing_cycle IN ({_in(config.BILLING_CYCLES)})),
    seats               INTEGER NOT NULL CHECK (seats > 0),
    currency            TEXT NOT NULL CHECK (currency IN ({_in(config.CURRENCIES)})),
    amount_local_cents  INTEGER NOT NULL,
    amount_usd_cents    INTEGER NOT NULL,
    discount_bp         INTEGER NOT NULL CHECK (discount_bp BETWEEN 0 AND 10000),
    status              TEXT NOT NULL CHECK (status IN ({_in(config.STATUSES)})),
    payment_method      TEXT CHECK (payment_method IS NULL OR payment_method IN ({_in(config.PAYMENT_METHODS)})),
    signup_date         TEXT,
    invoice_date        TEXT NOT NULL,
    churned             INTEGER NOT NULL CHECK (churned IN (0, 1)),
    csat_score          INTEGER CHECK (csat_score IS NULL OR csat_score BETWEEN 1 AND 5),
    support_tickets     INTEGER CHECK (support_tickets IS NULL OR support_tickets >= 0),
    source_row          INTEGER NOT NULL,
    quality_flags       TEXT NOT NULL,
    CHECK ((status = 'refunded' AND amount_local_cents < 0) OR (status <> 'refunded' AND amount_local_cents >= 0))
)"""


ACCOUNTS_DDL = f"""
CREATE TABLE accounts (
    account_id             TEXT PRIMARY KEY,
    selected_invoice_id    TEXT NOT NULL REFERENCES invoices (invoice_id),
    selected_invoice_date  TEXT NOT NULL,
    account_name           TEXT,
    region                 TEXT NOT NULL,
    industry               TEXT,
    signup_date            TEXT,
    plan                   TEXT NOT NULL CHECK (plan IN ({_in(config.PLANS)})),
    billing_cycle          TEXT NOT NULL,
    seats                  INTEGER NOT NULL,
    discount_bp            INTEGER NOT NULL,
    churned                INTEGER NOT NULL CHECK (churned IN (0, 1)),
    csat_score             INTEGER,
    support_tickets        INTEGER,
    mrr_cents              INTEGER NOT NULL CHECK (mrr_cents >= 0),
    disagreeing_fields     TEXT NOT NULL,
    quality_flags          TEXT NOT NULL
)"""

# Account attributes compared across an account's eligible invoices.
ACCOUNT_IDENTITY_FIELDS = ("account_name", "region", "industry", "signup_date", "churned")
