"""Query boundary (spec section 6 "Supported query operations and safeguards")."""

from evals.harness import XFail, case, check, eq, file_sha256, fixture, full, query, run, scalar

V = "invoice_facts"

# (label, sql, parameters): every one must be rejected before execution.
REJECTED = [
    ("second statement", "SELECT invoice_id FROM invoice_facts; DROP TABLE invoices", {}),
    ("hidden delete", "SELECT 1 FROM invoice_facts; DELETE FROM invoices WHERE 1 = 1", {}),
    ("insert", "INSERT INTO invoices SELECT * FROM invoices", {}),
    ("update", "UPDATE invoices SET status = 'paid'", {}),
    ("delete", "DELETE FROM invoices", {}),
    ("drop", "DROP VIEW invoice_facts", {}),
    ("create", "CREATE TABLE t AS SELECT * FROM invoice_facts", {}),
    ("attach", "ATTACH DATABASE 'other.db' AS other", {}),
    ("pragma", "PRAGMA table_info(invoices)", {}),
    ("base table", "SELECT invoice_id FROM invoices", {}),
    ("catalog", "SELECT name FROM sqlite_master", {}),
    ("schema-qualified view", "SELECT invoice_id FROM main.invoice_facts", {}),
    ("other view than declared", "SELECT account_id FROM account_facts", {}),
    ("join", "SELECT i.invoice_id FROM invoice_facts i JOIN account_facts a ON a.account_id = i.account_id", {}),
    ("comma join", "SELECT invoice_id FROM invoice_facts, account_facts", {}),
    ("subquery in filter", "SELECT invoice_id FROM invoice_facts WHERE account_id IN (SELECT account_id FROM account_facts)", {}),
    ("derived table", "SELECT n FROM (SELECT COUNT(*) AS n FROM invoice_facts)", {}),
    ("cte", "WITH x AS (SELECT invoice_id FROM invoice_facts) SELECT invoice_id FROM x", {}),
    ("union", "SELECT invoice_id FROM invoice_facts UNION SELECT name FROM sqlite_master", {}),
    ("window", "SELECT invoice_id, ROW_NUMBER() OVER (ORDER BY invoice_date) AS n FROM invoice_facts", {}),
    ("extension loading", "SELECT load_extension('evil') FROM invoice_facts", {}),
    ("unapproved function", "SELECT randomblob(16) FROM invoice_facts", {}),
    ("unknown column", "SELECT gross_margin FROM invoice_facts", {}),
    ("positional parameter", "SELECT invoice_id FROM invoice_facts WHERE region = ?", {}),
    ("unbound parameter", "SELECT invoice_id FROM invoice_facts WHERE region = :region", {}),
    ("unused parameter", "SELECT invoice_id FROM invoice_facts", {"region": "EMEA"}),
    ("inline filter value", "SELECT invoice_id FROM invoice_facts WHERE account_name = 'Stark Labs'", {}),
    ("non-canonical category", "SELECT invoice_id FROM invoice_facts WHERE region = :r", {"r": "Europe"}),
    ("comment", "SELECT invoice_id FROM invoice_facts -- WHERE status = 'paid'", {}),
    ("select star", "SELECT * FROM invoice_facts", {}),
    ("average of money in SQL", "SELECT AVG(amount_usd_cents) AS a FROM invoice_facts", {}),
    ("division of aggregates in output", "SELECT SUM(net_revenue_cents) / COUNT(*) AS a FROM invoice_facts", {}),
    ("mixed-currency sum", "SELECT SUM(amount_local_cents) AS total FROM invoice_facts", {}),
    ("empty", "", {}),
    ("not sql", "show me everything", {}),
]

# (label, sql, parameters, view): every one must be accepted.
ACCEPTED = [
    ("spec example",
     "SELECT strftime('%Y-%m', invoice_date) AS month, plan, SUM(net_revenue_cents) AS net_revenue_cents "
     "FROM invoice_facts WHERE region = :region AND invoice_date >= :start AND invoice_date < :end "
     "GROUP BY month, plan ORDER BY month, plan",
     {"region": "EMEA", "start": "2024-01-01", "end": "2025-01-01"}, V),
    ("conditional aggregation",
     "SELECT SUM(CASE WHEN invoice_date < :mid THEN net_revenue_cents ELSE 0 END) AS first_half_cents, "
     "SUM(CASE WHEN invoice_date >= :mid THEN net_revenue_cents ELSE 0 END) AS second_half_cents "
     "FROM invoice_facts WHERE invoice_date >= :start AND invoice_date < :end",
     {"start": "2024-01-01", "mid": "2024-07-01", "end": "2025-01-01"}, V),
    ("having and distinct",
     "SELECT industry, COUNT(DISTINCT account_id) AS accounts FROM invoice_facts WHERE industry IS NOT NULL "
     "GROUP BY industry HAVING COUNT(*) >= 2 ORDER BY accounts DESC LIMIT 3", {}, V),
    ("in, or and null tests",
     "SELECT invoice_id FROM invoice_facts WHERE (status IN (:a, :b) OR payment_method IS NULL) AND seats >= 10",
     {"a": "pending", "b": "failed"}, V),
    ("local sum per currency",
     "SELECT currency, SUM(amount_local_cents) AS local_cents FROM invoice_facts GROUP BY currency", {}, V),
    ("account view ordering by ratio",
     "SELECT region, SUM(mrr_cents) AS mrr_cents, COUNT(*) AS accounts FROM account_facts WHERE churned = 0 "
     "GROUP BY region ORDER BY SUM(mrr_cents) * 1.0 / COUNT(*) DESC", {}, "account_facts"),
    ("min, max and average of a non-money column",
     "SELECT plan, MIN(seats) AS min_seats, MAX(seats) AS max_seats, AVG(seats) AS avg_seats FROM account_facts "
     "GROUP BY plan", {}, "account_facts"),
]


@case("guard_rejects", "guard", f"{len(REJECTED)} unsafe or unsupported queries are rejected before execution")
def guard_rejects():
    from app.sqlguard import QueryRejected, validate_query

    passed = []
    for label, sql, parameters in REJECTED:
        try:
            validate_query(sql, parameters, V)
        except QueryRejected:
            continue
        passed.append(label)
    check(not passed, f"these should have been rejected: {passed}")


@case("guard_accepts", "guard", f"{len(ACCEPTED)} queries inside the supported grammar are accepted")
def guard_accepts():
    from app.sqlguard import QueryRejected, validate_query

    refused = []
    for label, sql, parameters, view in ACCEPTED:
        try:
            validate_query(sql, parameters, view)
        except QueryRejected as exc:
            refused.append(f"{label}: {exc}")
    check(not refused, f"these should have been accepted: {refused}")


@case("guard_no_mutation", "guard", "Rejected proposals and a bound injection attempt leave the database file unchanged")
def guard_no_mutation():
    s = fixture("unseen")
    before = file_sha256(s.db_path)
    for label, sql, parameters in REJECTED:
        answer = run(s, query(sql, parameters))
        eq(answer.kind, "error", f"answer kind for {label!r}")
        check(not answer.rows, f"{label!r} returned rows")
    injected = run(s, query(
        "SELECT invoice_id FROM invoice_facts WHERE account_name = :name",
        {"name": "x' OR '1'='1'; DROP TABLE invoices; --"},
    ))
    eq(injected.kind, "result", "a bound value is data, so the query runs")
    eq(injected.rows, [], "the injected text matches no account name")
    eq(scalar(s, "SELECT COUNT(*) FROM invoices"), 14, "invoices table still has its rows")
    eq(file_sha256(s.db_path), before, "database file hash")


@case("authorizer_denies", "guard", "The connection itself refuses writes, catalog reads and unapproved functions")
def authorizer_denies():
    from app.execute import QueryDenied, run_query

    s = fixture("unseen")
    before = file_sha256(s.db_path)
    # These bypass the SQL validator on purpose to test the second layer.
    for sql in ["DELETE FROM invoices", "SELECT name FROM sqlite_master", "SELECT randomblob(8) FROM invoices",
                "PRAGMA table_info(invoices)", "ATTACH DATABASE ':memory:' AS other",
                "CREATE TABLE t (x)", "UPDATE invoices SET status = 'paid'"]:
        try:
            run_query(s, sql, {})
        except QueryDenied:
            continue
        raise AssertionError(f"the connection executed: {sql}")
    eq(file_sha256(s.db_path), before, "database file hash")


@case("query_deadline", "guard", "A query that exceeds the execution deadline is stopped and reported")
def query_deadline():
    from app.execute import QueryTimeout, run_query

    s = full(query_timeout_s=1e-9)
    try:
        run_query(s, "SELECT COUNT(*) FROM account_facts", {})
    except QueryTimeout:
        pass
    else:
        raise AssertionError("the query finished despite an expired deadline")
    answer = run(s, query("SELECT COUNT(*) AS accounts FROM account_facts", view="account_facts"))
    eq(answer.kind, "error", "answer kind")
    eq(answer.error_code, "query_timeout", "error code")


@case("xfail_window_trend", "guard", "Month-over-month growth for every month needs a window function (unsupported in v1)")
def xfail_window_trend():
    answer = run(full(), query(
        "SELECT strftime('%Y-%m', invoice_date) AS month, SUM(net_revenue_cents) AS net_revenue_cents, "
        "SUM(net_revenue_cents) - LAG(SUM(net_revenue_cents)) OVER (ORDER BY strftime('%Y-%m', invoice_date)) "
        "AS change_cents FROM invoice_facts GROUP BY month ORDER BY month",
    ), question="Show month-over-month net revenue growth for every month.")
    if answer.kind == "error" and answer.detail.get("reject_code") == "window":
        raise XFail(
            "the data can answer this, but window functions are outside the v1 single-view grammar; "
            "the query is refused instead of being run unvalidated"
        )
    raise AssertionError(f"expected a window-function rejection, got {answer.kind}: {answer.message}")
