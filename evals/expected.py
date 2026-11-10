"""Expected values for the eval harness.

Every number here comes from one of two places:

1. `specs/solution.md` (reference results for the supplied CSV), or
2. hand calculation on the small ledgers in `evals/fixtures/`, with the
   arithmetic written next to the value.

Nothing in this file was produced by running the application. If the
application disagrees with a value here, report the difference; do not edit
the value to make a case pass (see AGENTS.md).

Money is in integer cents unless a name says otherwise.
"""

from decimal import Decimal

# ---------------------------------------------------------------------------
# Field parsing and FX (spec section 3 and section 8 "FX and parsing")
# ---------------------------------------------------------------------------

# (raw amount, currency column, expected local cents)
AMOUNT_PARSING = [
    ("1,200.00 EUR", "EUR", 120_000),
    ("100", "GBP", 10_000),
    ("31432", "GBP", 3_143_200),      # missing cents means .00, not 314.32
    ("$4,900.00", "USD", 490_000),
    ("$-4,900.00", "USD", -490_000),
    ("-1495.00", "USD", -149_500),
    ("€529.20", "EUR", 52_920),
    ("£28,003.50", "GBP", 2_800_350),
    ("49.00 USD", "USD", 4_900),
    ("-1,234.50 EUR", "EUR", -123_450),
    ("12250", "USD", 1_225_000),
]

# (raw amount, currency column): each must be rejected
AMOUNT_REJECTED = [
    ("$100.00", "EUR"),       # marker conflicts with the currency column
    ("100.00 GBP", "USD"),    # marker conflicts with the currency column
    ("1,23.00", "USD"),       # invalid grouping
    ("12,3456.00", "USD"),    # invalid grouping
    ("abc", "USD"),           # unrecognized token
    ("NaN", "USD"),           # nonfinite
    ("Infinity", "USD"),      # nonfinite
    ("1e3", "USD"),           # unrecognized token
    ("", "USD"),              # blank required field
    ("100.00 CAD", "USD"),    # unrecognized currency marker
]

# (local cents, currency, expected USD cents), ROUND_HALF_UP per invoice
FX_CONVERSION = [
    (120_000, "EUR", 129_600),    # 1,200.00 x 1.08 = 1,296.00
    (10_000, "GBP", 12_700),      # 100.00 x 1.27 = 127.00
    (52_920, "EUR", 57_154),      # 529.20 x 1.08 = 571.536 -> 571.54
    (490_000, "USD", 490_000),    # USD is unchanged
    (150, "GBP", 191),            # 1.50 x 1.27 = 1.905 -> 1.91 (half-even would give 1.90)
    (-150, "GBP", -191),          # decimal ROUND_HALF_UP rounds ties away from zero
    (105_840, "EUR", 114_307),    # 1,058.40 x 1.08 = 1,143.072 -> 1,143.07
]

# (raw date, expected ISO date, expected ambiguity flag)
DATE_PARSING = [
    ("2024-01-05", "2024-01-05", False),
    ("2024/01/05", "2024-01-05", False),
    ("01/05/2024", "2024-01-05", False),   # US month/day
    ("Jan 5 2024", "2024-01-05", False),
    ("05-01-2024", "2024-01-05", True),    # day-month; the month-day reading (May 1) differs
    ("13-01-2024", "2024-01-13", False),   # only one valid reading
    ("05-05-2024", "2024-05-05", False),   # both readings agree
    ("31-12-2024", "2024-12-31", False),
    ("12/31/2023", "2023-12-31", False),
    ("Feb 29 2024", "2024-02-29", False),  # leap day
]

DATE_REJECTED = ["", "31-02-2024", "2024-13-01", "13/13/2024", "Foo 5 2024", "2024-1-5x", "20240105"]

# ---------------------------------------------------------------------------
# Fixture: paid_vs_net.csv (spec section 8 "Paid vs net")
#   paid 100.00, refunded -20.00, pending 50.00, void 70.00
# ---------------------------------------------------------------------------

PAID_VS_NET = {
    "paid_revenue_cents": 10_000,   # only the paid invoice
    "net_revenue_cents": 8_000,     # 100.00 - 20.00
    "refunds_cents": 2_000,         # reported as a positive amount
    "exposure_cents": 5_000,        # pending only; void counts nowhere
}

# ---------------------------------------------------------------------------
# Fixture: dates.csv (spec section 8 "Dates", both year boundaries)
#   Y-001 12/31/2023  1.00 | Y-002 2024-01-01  2.00 | Y-003 31-12-2024  4.00
#   Y-004 Jan 1 2025  8.00 | Y-005 05-01-2024 16.00 | Y-006 2024/06/15 32.00
# ---------------------------------------------------------------------------

DATES = {
    "paid_2024_cents": 5_400,          # 2 + 4 + 16 + 32; excludes 2023-12-31 and 2025-01-01
    "paid_2024_invoices": 4,
    "paid_jan_2024_cents": 1_800,      # 2 + 16; Y-005 is 5 January under day-month
    "ambiguous_invoice_ids": ["Y-005"],
}

# ---------------------------------------------------------------------------
# Fixture: duplicates.csv (spec section 3 "Which duplicate wins")
#   rows 1-2 D-001 identical              -> one exact duplicate removed
#   rows 3-4 D-002 alias-only differences -> one normalized duplicate removed
#   rows 5-6 D-003 -50.00 paid/refunded   -> refunded wins, paid row rejected
#   rows 7-8 D-004 200.00 paid/pending    -> both held, invoice excluded
#   row  9   D-005 single pending 30.00
# ---------------------------------------------------------------------------

DUPLICATES = {
    "input_rows": 9,
    "exact_duplicate_rows": 1,
    "normalized_duplicate_rows": 1,
    "rejected_invalid_rows": 1,
    "held_conflict_rows": 2,
    "held_conflict_invoice_ids": ["D-004"],
    "eligible_invoice_ids": ["D-001", "D-002", "D-003", "D-005"],   # 9 = 2 + 1 + 2 + 4
    "status": {"D-001": "paid", "D-002": "paid", "D-003": "refunded", "D-005": "pending"},
    "lineage": {"D-001": [1, 2], "D-002": [3, 4], "D-003": [6], "D-005": [9]},
    "paid_revenue_cents": 25_000,   # 100 + 150; the disputed 200 is not included
    "refunds_cents": 5_000,
    "exposure_cents": 3_000,        # D-005 only; the disputed pending 200 is not included
    "accounts": ["ACC-D1", "ACC-D2", "ACC-D3", "ACC-D5"],
    "accounts_without_snapshot": ["ACC-D4"],
}

# ---------------------------------------------------------------------------
# Fixture: accounts.csv (spec section 8 "Annual MRR and account grain" and
# "Account churn and missing CSAT"). Snapshot = latest invoice date, ties
# broken by greatest invoice ID.
#   ACC-1 -> A-003: Enterprise, 10 seats, active, CSAT 2   MRR 299 x 10        = 2,990.00
#   ACC-2 -> A-004: Enterprise,  5 seats, churned, CSAT -  MRR 0
#   ACC-3 -> A-006: Starter,     5 seats, churned, CSAT 5  MRR 0 (earlier invoice was Pro, active)
#   ACC-4 -> A-008: Starter,    10 seats, active, CSAT 5   MRR 49 x 10         =   490.00 (two annual invoices)
#   ACC-5 -> A-010: Starter,    20 seats, 20% off, CSAT 1  MRR 49 x 20 x 0.80  =   784.00 (date tie with A-009)
# ---------------------------------------------------------------------------

ACCOUNTS = {
    "account_count": 5,
    "invoice_count": 10,
    "selected_invoice": {"ACC-1": "A-003", "ACC-2": "A-004", "ACC-3": "A-006", "ACC-4": "A-008", "ACC-5": "A-010"},
    "mrr_cents": {"ACC-1": 299_000, "ACC-2": 0, "ACC-3": 0, "ACC-4": 49_000, "ACC-5": 78_400},
    "plan": {"ACC-1": "enterprise", "ACC-2": "enterprise", "ACC-3": "starter", "ACC-4": "starter", "ACC-5": "starter"},
    "region": {"ACC-1": "NA", "ACC-2": "NA", "ACC-3": "APAC", "ACC-4": "EMEA", "ACC-5": "APAC"},
    # churned accounts / all account snapshots in the plan
    "churn": {"enterprise": (1, 2, Decimal("50.00")), "starter": (1, 3, Decimal("33.33"))},
    "low_csat_flag": {"ACC-1": 1, "ACC-2": None, "ACC-3": 0, "ACC-4": 0, "ACC-5": 1},
    # (2,990.00 + 490.00 + 784.00) / 3 active accounts = 1,421.333... -> 1,421.33
    "active_mrr_sum_cents": 426_400,
    "active_accounts": 3,
    "average_active_mrr_usd": Decimal("1421.33"),
    # all-dates paid revenue per account
    "lifetime_paid_cents": {
        "ACC-1": 897_000,     # 3 x 2,990.00
        "ACC-2": 1_495_000,
        "ACC-3": 74_000,      # 495.00 + 245.00
        "ACC-4": 980_000,     # 2 x 4,900.00
        "ACC-5": 127_400,     # 490.00 + 784.00
    },
    "top_accounts_by_net_revenue": ["ACC-2", "ACC-4", "ACC-1", "ACC-5", "ACC-3"],
    "region_disagreement": ["ACC-4"],
    "churn_disagreement": ["ACC-3"],
}

# ---------------------------------------------------------------------------
# Fixture: unseen.csv (spec section 8 "Unseen ..." cases). USD cents per invoice:
#   U-001 EMEA pro      paid      9,900.00 USD            ->   990,000   2024-01-15
#   U-002 EMEA pro      refunded   -990.00 USD            ->   -99,000   2024-01-20
#   U-003 EMEA starter  paid      1,058.40 EUR x 1.08     ->   114,307   2024-01-25
#   U-004 EMEA starter  pending   1,058.40 EUR x 1.08     ->   114,307   2024-02-25
#   U-005 EMEA pro      paid        445.50 USD            ->    44,550   2024-02-10
#   U-006 EMEA pro      paid        445.50 USD            ->    44,550   2024-03-10
#   U-007 NA   pro      paid     23,760.00 USD (20% off)  -> 2,376,000   2024-02-05
#   U-008 NA   pro      paid      1,000.00 GBP x 1.27     ->   127,000   2024-03-03
#   U-009 APAC ent      paid     11,960.00 USD            -> 1,196,000   2023-12-30
#   U-010 APAC ent      paid     10,166.00 USD (15% off)  -> 1,016,600   2024-12-30
#   U-011 EMEA pro      failed    1,188.00 USD            ->   118,800   2024-03-15
#   U-012 NA   pro      void      7,920.00 USD            ->   792,000   2024-04-01
#   U-013 LATAM starter paid      4,165.00 USD (15% off)  ->   416,500   2024-06-01
#   U-014 LATAM ent     pending  11,960.00 USD (20% off)  -> 1,196,000   2024-07-01
# ---------------------------------------------------------------------------

UNSEEN = {
    "invoice_count": 14,
    # Monthly net revenue for EMEA in 2024 by plan (the example query in spec section 6).
    # The population is every EMEA invoice dated in 2024: U-001..U-006 and U-011.
    "emea_2024_monthly_net_by_plan": [
        ("2024-01", "pro", 891_000),       # 990,000 - 99,000
        ("2024-01", "starter", 114_307),
        ("2024-02", "pro", 44_550),
        ("2024-02", "starter", 0),         # U-004 is pending
        ("2024-03", "pro", 44_550),        # U-011 is failed
    ],
    "emea_2024_population": 7,
    # Net revenue by region for 2024-02-01 <= invoice date < 2024-04-01.
    # In range: U-004, U-005, U-006, U-011 (EMEA) and U-007, U-008 (NA). U-012 is dated 04-01.
    "net_by_region_feb_mar_2024": [
        ("EMEA", 89_100),        # 44,550 + 44,550
        ("NA", 2_503_000),       # 2,376,000 + 127,000
    ],
    "feb_mar_2024_population": 6,
    # Account snapshots (latest invoice):
    #   ACC-U1 U-002 pro 10 seats            MRR  990.00   Fintech  tickets 5
    #   ACC-U2 U-004 starter 20 seats        MRR  980.00   Retail   tickets 1
    #   ACC-U3 U-006 pro 5 seats 10% off     MRR  445.50   Retail   tickets 3
    #   ACC-U4 U-007 pro 30 seats 20% off    MRR 2,376.00  Fintech  tickets 7
    #   ACC-U5 U-012 pro 8 seats             MRR  792.00   Media    tickets 2
    #   ACC-U6 U-010 ent 4 seats 15% off     MRR 1,016.60  Media    tickets 0
    #   ACC-U7 U-011 pro 12 seats, churned   MRR    0      Fintech  tickets 9
    #   ACC-U8 U-014 ent 50 seats 20% off    MRR 11,960.00 Energy   tickets 1
    # Active Pro accounts by industry: (industry, MRR sum cents, accounts, average USD)
    "active_pro_mrr_by_industry": [
        ("Fintech", 336_600, 2, Decimal("1683.00")),   # (990.00 + 2,376.00) / 2
        ("Media", 79_200, 1, Decimal("792.00")),
        ("Retail", 44_550, 1, Decimal("445.50")),
    ],
    "active_pro_population": 4,
    # Accounts whose selected support tickets are >= 3.
    "accounts_with_tickets_gte_3": ["ACC-U1", "ACC-U3", "ACC-U4", "ACC-U7"],
    # The wrong grain would count invoices instead: U-002, U-006, U-007, U-008, U-011.
    "invoices_with_tickets_gte_3": 5,
    # Annual invoices over USD 10,000 with a discount of at least 15%.
    "annual_over_10k_discount_15": ["U-007", "U-010"],
    # "Stark Labs" is the name on two different account IDs.
    "stark_labs_account_ids": ["ACC-U3", "ACC-U5"],
    # Pending/failed exposure: U-004 + U-011 + U-014
    "exposure_cents": 1_429_107,
    "exposure_invoices": 3,
    "first_three_invoice_ids": ["U-001", "U-002", "U-003"],
}

# ---------------------------------------------------------------------------
# Supplied CSV: reference results copied from specs/solution.md
# ---------------------------------------------------------------------------

REFERENCE_SHA256 = "f542bc1f8193ab367e0a009a7afe56620335ce23b5817f5edc0ed4f896c89d84"

# Section 2 (observations on the raw file) and section 3 (dispositions)
REFERENCE_COUNTS = {
    "input_rows": 5_125,
    "exact_duplicate_rows": 82,
    "normalized_duplicate_rows": 27,
    "duplicate_rows_removed": 109,
    "rejected_invalid_rows": 2,
    "held_conflict_rows": 28,
    "held_conflict_invoice_ids": 14,
    "eligible_invoices": 4_986,
    "accounts": 3_803,
    "accounts_without_snapshot": 12,
}

REFERENCE_PROFILE = {
    "ambiguous_invoice_date_rows": 82,
    "ambiguous_invoice_date_ids": 81,
    "ambiguous_signup_date_rows": 125,
    "blank_email_rows": 292,
    "blank_industry_rows": 422,
    "blank_csat_rows": 623,
    "blank_payment_method_rows": 637,
    "malformed_email_rows": 142,
    "invoice_date_min": "2021-01-19",
    "invoice_date_max": "2027-07-23",
}

# Section 5
REFERENCE = {
    "paid_2024_cents": 3_265_535_252,
    "paid_2024_invoices": 888,
    "paid_2024_disputed_ids": ["INV-101678", "INV-103437", "INV-103655"],
    "paid_2024_disputed_additional_cents": 3_101_868,
    "top_region": "APAC",
    "top_region_active_accounts": 752,
    "top_region_average_mrr_usd": Decimal("6164.03"),
    "refunds_cents": 892_516_384,
    "refund_invoices": 292,
    "churn": {
        "enterprise": (170, 778, Decimal("21.85")),
        "starter": (353, 1_879, Decimal("18.79")),
    },
    # (account ID, eligible net revenue cents, selected CSAT, low-CSAT flag)
    "top_five": [
        ("ACC-7509", 121_059_275, 4, 0),
        ("ACC-1420", 120_809_275, None, None),
        ("ACC-9946", 120_564_275, 4, 0),
        ("ACC-1196", 110_735_284, 3, 0),
        ("ACC-5211", 108_507_848, 5, 0),
    ],
    "exposure_invoices": 894,
    "exposure_cents": 2_898_325_597,
}
