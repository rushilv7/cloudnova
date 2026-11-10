"""Modeled data: two tables and two analytical views (specs/solution.md section 4).

`invoices` holds one eligible row per invoice ID. `accounts` holds one
latest-invoice snapshot per account ID. The question interface may only read
the views `invoice_facts` and `account_facts`; their column dictionary below
is the single source for the model prompt, the SQL validator and formatting.
"""

from __future__ import annotations

from dataclasses import dataclass

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


# --- views --------------------------------------------------------------------
# Revenue eligibility, refunds, exposure and the low-CSAT flag are defined here
# and nowhere else. The model selects these columns; it does not redefine them.

def invoice_facts_ddl(view: str = "invoice_facts", table: str = "invoices") -> str:
    return f"""
CREATE VIEW {view} AS
SELECT
    invoice_id, account_id, account_name, contact_email, region, industry, plan, billing_cycle, seats,
    currency, amount_local_cents, amount_usd_cents, discount_bp, status, payment_method, signup_date,
    invoice_date, churned, csat_score, support_tickets,
    CASE WHEN status = 'paid' THEN amount_usd_cents ELSE 0 END AS paid_revenue_cents,
    CASE WHEN status IN ('paid', 'refunded') THEN amount_usd_cents ELSE 0 END AS net_revenue_cents,
    CASE WHEN status = 'refunded' THEN -amount_usd_cents ELSE 0 END AS refunds_cents,
    CASE WHEN status IN ('pending', 'failed') THEN amount_usd_cents ELSE 0 END AS exposure_cents,
    CASE WHEN instr(',' || quality_flags || ',', ',invoice_date_ambiguous,') > 0 THEN 1 ELSE 0 END
        AS invoice_date_ambiguous
FROM {table}"""


# The join below is the only invoice/account join in the system. Invoice totals
# are aggregated to one row per account first, so MRR and account counts cannot
# be multiplied by invoice history.
ACCOUNT_FACTS_DDL = f"""
CREATE VIEW account_facts AS
SELECT
    a.account_id, a.account_name, a.region, a.industry, a.signup_date, a.plan, a.billing_cycle, a.seats,
    a.discount_bp, a.churned, a.csat_score,
    CASE WHEN a.csat_score IS NULL THEN NULL WHEN a.csat_score <= {config.LOW_CSAT_MAX} THEN 1 ELSE 0 END
        AS low_csat_flag,
    a.support_tickets, a.mrr_cents, a.selected_invoice_id, a.selected_invoice_date,
    t.lifetime_paid_revenue_cents, t.lifetime_net_revenue_cents, t.lifetime_refunds_cents,
    t.lifetime_exposure_cents, t.lifetime_invoice_count,
    CASE WHEN a.disagreeing_fields <> '' THEN 1 ELSE 0 END AS has_attribute_disagreement,
    CASE WHEN instr(a.quality_flags, 'snapshot_date_ambiguous') > 0 THEN 1 ELSE 0 END AS snapshot_date_ambiguous
FROM accounts AS a
JOIN (
    SELECT
        account_id,
        SUM(paid_revenue_cents) AS lifetime_paid_revenue_cents,
        SUM(net_revenue_cents) AS lifetime_net_revenue_cents,
        SUM(refunds_cents) AS lifetime_refunds_cents,
        SUM(exposure_cents) AS lifetime_exposure_cents,
        COUNT(*) AS lifetime_invoice_count
    FROM invoice_facts
    GROUP BY account_id
) AS t ON t.account_id = a.account_id"""


# --- column dictionary --------------------------------------------------------

USD_CENTS = "usd_cents"
LOCAL_CENTS = "local_cents"
BASIS_POINTS = "basis_points"
FLAG = "flag"
COUNT = "count"
MONEY_UNITS = (USD_CENTS, LOCAL_CENTS)


@dataclass(frozen=True)
class Column:
    name: str
    type: str                      # TEXT or INTEGER
    description: str
    unit: str | None = None        # usd_cents, local_cents, basis_points, flag, count or None
    nullable: bool = False
    values: tuple[str, ...] = ()   # canonical values for enumerated text columns
    per_account: bool = False      # account-level value repeated on every invoice of the account


@dataclass(frozen=True)
class View:
    name: str
    grain: str
    id_column: str
    time_meaning: str
    columns: tuple[Column, ...]

    def column(self, name: str) -> Column | None:
        return next((c for c in self.columns if c.name == name.lower()), None)

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)


INVOICE_FACTS = View(
    name="invoice_facts",
    grain="one row per eligible invoice",
    id_column="invoice_id",
    time_meaning="Attributes are as recorded on that invoice at its invoice date.",
    columns=(
        Column("invoice_id", "TEXT", "Invoice key; unique in this view."),
        Column("account_id", "TEXT", "Customer account key. Group accounts by this, never by name."),
        Column("account_name", "TEXT", "Name on this invoice. One account ID often carries several names and one "
               "name can belong to several account IDs.", nullable=True),
        Column("contact_email", "TEXT", "Billing contact on this invoice; null when blank or malformed.",
               nullable=True),
        Column("region", "TEXT", "Region on this invoice.", values=config.REGIONS),
        Column("industry", "TEXT", "Industry on this invoice; null when not recorded.", nullable=True),
        Column("plan", "TEXT", "Plan on this invoice.", values=config.PLANS),
        Column("billing_cycle", "TEXT", "Billing cycle of this invoice.", values=config.BILLING_CYCLES),
        Column("seats", "INTEGER", "Licensed seats on this invoice."),
        Column("currency", "TEXT", "Currency the invoice was issued in.", values=config.CURRENCIES),
        Column("amount_local_cents", "INTEGER", "Recorded amount in cents of `currency`; negative for refunds. "
               "Only meaningful within one currency.", unit=LOCAL_CENTS),
        Column("amount_usd_cents", "INTEGER", "Recorded amount converted to USD cents (EUR x 1.08, GBP x 1.27) "
               "for any status; negative for refunds.", unit=USD_CENTS),
        Column("discount_bp", "INTEGER", "Discount in basis points: 1500 means 15%.", unit=BASIS_POINTS),
        Column("status", "TEXT", "Payment status.", values=config.STATUSES),
        Column("payment_method", "TEXT", "Payment method; null when blank.", nullable=True,
               values=config.PAYMENT_METHODS),
        Column("signup_date", "TEXT", "Account signup date as recorded on this invoice, YYYY-MM-DD.", nullable=True),
        Column("invoice_date", "TEXT", "Invoice issue date, YYYY-MM-DD. The only date available for period "
               "filters; there are no payment or due dates."),
        Column("churned", "INTEGER", "Churn flag as recorded on this invoice. For churn analysis use "
               "account_facts.", unit=FLAG, per_account=True),
        Column("csat_score", "INTEGER", "CSAT 1-5 as recorded on this invoice; null when missing. An account-level "
               "value repeated per invoice.", nullable=True, per_account=True),
        Column("support_tickets", "INTEGER", "Support tickets as recorded on this invoice. An account-level value "
               "repeated per invoice.", nullable=True, per_account=True),
        Column("paid_revenue_cents", "INTEGER", "amount_usd_cents when status is 'paid', otherwise 0.",
               unit=USD_CENTS),
        Column("net_revenue_cents", "INTEGER", "amount_usd_cents when status is 'paid' or 'refunded' (refunds are "
               "negative), otherwise 0. This is the default meaning of revenue.", unit=USD_CENTS),
        Column("refunds_cents", "INTEGER", "Refunded amount as a positive number of USD cents when status is "
               "'refunded', otherwise 0.", unit=USD_CENTS),
        Column("exposure_cents", "INTEGER", "amount_usd_cents when status is 'pending' or 'failed', otherwise 0.",
               unit=USD_CENTS),
        Column("invoice_date_ambiguous", "INTEGER", "1 when the raw date was DD-MM-YYYY and could also be read as "
               "MM-DD-YYYY.", unit=FLAG),
    ),
)

ACCOUNT_FACTS = View(
    name="account_facts",
    grain="one row per account, taken from the account's latest eligible invoice",
    id_column="account_id",
    time_meaning="Attributes are a latest-invoice estimate, not a verified current state. Totals named "
                 "lifetime_* cover all available invoice dates.",
    columns=(
        Column("account_id", "TEXT", "Customer account key; unique in this view."),
        Column("account_name", "TEXT", "Name on the selected invoice. One name can belong to several account IDs.",
               nullable=True),
        Column("region", "TEXT", "Region on the selected invoice.", values=config.REGIONS),
        Column("industry", "TEXT", "Industry on the selected invoice; null when not recorded.", nullable=True),
        Column("signup_date", "TEXT", "Signup date on the selected invoice, YYYY-MM-DD.", nullable=True),
        Column("plan", "TEXT", "Plan on the selected invoice.", values=config.PLANS),
        Column("billing_cycle", "TEXT", "Billing cycle of the selected invoice.", values=config.BILLING_CYCLES),
        Column("seats", "INTEGER", "Seats on the selected invoice."),
        Column("discount_bp", "INTEGER", "Discount on the selected invoice in basis points.", unit=BASIS_POINTS),
        Column("churned", "INTEGER", "1 when the selected invoice marks the account as churned. No churn date "
               "exists.", unit=FLAG),
        Column("csat_score", "INTEGER", "CSAT 1-5 on the selected invoice; null means unknown.", nullable=True),
        Column("low_csat_flag", "INTEGER", f"1 when csat_score <= {config.LOW_CSAT_MAX}, 0 when higher, null when "
               "CSAT is unknown.", unit=FLAG, nullable=True),
        Column("support_tickets", "INTEGER", "Support tickets on the selected invoice.", nullable=True),
        Column("mrr_cents", "INTEGER", "Estimated MRR in USD cents: list price x seats x (1 - discount); 0 when "
               "churned. Same for monthly and annual billing.", unit=USD_CENTS),
        Column("selected_invoice_id", "TEXT", "The invoice the snapshot was taken from."),
        Column("selected_invoice_date", "TEXT", "Invoice date of the selected invoice, YYYY-MM-DD."),
        Column("lifetime_paid_revenue_cents", "INTEGER", "Sum of paid_revenue_cents over all the account's eligible "
               "invoices, all dates.", unit=USD_CENTS),
        Column("lifetime_net_revenue_cents", "INTEGER", "Sum of net_revenue_cents (paid plus negative refunds) over "
               "all the account's eligible invoices, all dates.", unit=USD_CENTS),
        Column("lifetime_refunds_cents", "INTEGER", "Sum of refunds_cents over all the account's eligible invoices, "
               "all dates.", unit=USD_CENTS),
        Column("lifetime_exposure_cents", "INTEGER", "Sum of exposure_cents over all the account's eligible "
               "invoices, all dates.", unit=USD_CENTS),
        Column("lifetime_invoice_count", "INTEGER", "Number of the account's eligible invoices, all dates.",
               unit=COUNT),
        Column("has_attribute_disagreement", "INTEGER", "1 when name, region, industry, signup date or churn flag "
               "differ across the account's invoices.", unit=FLAG),
        Column("snapshot_date_ambiguous", "INTEGER", "1 when any of the account's invoices has an ambiguous date, "
               "so the selected invoice could differ.", unit=FLAG),
    ),
)

VIEWS = {view.name: view for view in (INVOICE_FACTS, ACCOUNT_FACTS)}
