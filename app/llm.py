"""Question interpretation: prompt, provider adapter and the single repair attempt.

The model receives the view schemas, business definitions, date coverage, a
data-quality summary and the output contract (specs/solution.md section 6).
It never receives ledger rows, account names, IDs or email addresses. Its
reply is a proposal: `app.engine` validates and executes it like any other.
"""

from __future__ import annotations

import json
from typing import Protocol
from urllib.parse import urlsplit

from app import config, engine, schema
from app.answer import Answer
from app.config import ConfigError, Settings
from app.execute import QueryFailed
from app.proposal import ProposalError
from app.sqlguard import QueryRejected

# Failures the model may correct once: the proposal's shape, its SQL, or a statement SQLite cannot prepare.
REPAIRABLE = (ProposalError, QueryRejected, QueryFailed)

# (how the prompt lists it, an expression over invoice_facts that evals run through the guard and the connection)
SQL_FUNCTIONS = (
    ("COALESCE(a, b)", "COALESCE(payment_method, :text)"),
    ("IFNULL(a, b)", "IFNULL(industry, :text)"),
    ("NULLIF(a, b)", "NULLIF(seats, 0)"),
    ("ROUND(x, digits)", "ROUND(seats * 1.5, 1)"),
    ("ABS(x)", "ABS(amount_usd_cents)"),
    ("CAST(x AS INTEGER), CAST(x AS REAL), CAST(x AS TEXT)", "CAST(strftime('%m', invoice_date) AS INTEGER)"),
    ("SUBSTR(text, start, length)", "SUBSTR(invoice_date, 1, 7)"),
    ("UPPER(text)", "UPPER(industry)"),
    ("LOWER(text)", "LOWER(industry)"),
    ("LENGTH(text)", "LENGTH(account_name)"),
    ("MIN(a, b) and MAX(a, b) with two arguments", "MAX(MIN(seats, 100), 10)"),
    ("strftime(format, date)", "strftime('%Y-%m', invoice_date)"),
    ("date(date, modifier)", "date(invoice_date, 'start of month')"),
    ("julianday(date)", "julianday(invoice_date) - julianday(signup_date)"),
)
QUARTER_EXPRESSION = ("strftime('%Y', invoice_date) || '-Q' || "
                      "((CAST(strftime('%m', invoice_date) AS INTEGER) + 2) / 3)")

# Worked examples for the prompt. None of them is a stakeholder question or an eval question, and evals
# check that every example passes the same validation as a model reply.
EXAMPLES = (
    ("How did NA net revenue in the first quarter of 2023 compare with the second quarter?", {
        "action": "query",
        "source_view": "invoice_facts",
        "interpretation": "Net revenue (paid invoices plus negative refunds) on NA invoices dated January to "
                          "March 2023 and April to June 2023",
        "sql": "SELECT SUM(CASE WHEN invoice_date < :q2_start THEN net_revenue_cents ELSE 0 END) AS q1_net_revenue_cents, "
               "SUM(CASE WHEN invoice_date >= :q2_start THEN net_revenue_cents ELSE 0 END) AS q2_net_revenue_cents "
               "FROM invoice_facts WHERE region = :region AND status IN (:paid, :refunded) "
               "AND invoice_date >= :start AND invoice_date < :end",
        "parameters": {"region": "NA", "paid": "paid", "refunded": "refunded", "start": "2023-01-01",
                       "q2_start": "2023-04-01", "end": "2023-07-01"},
        "output_units": {"q1_net_revenue_cents": "USD cents", "q2_net_revenue_cents": "USD cents"},
    }),
    ("What percentage of LATAM accounts on annual billing have a low CSAT score?", {
        "action": "query",
        "source_view": "account_facts",
        "interpretation": "Share of LATAM accounts on annual billing with a known CSAT score whose score is 2 or "
                          "lower; accounts with unknown CSAT are left out of the denominator",
        "sql": "SELECT SUM(low_csat_flag) AS low_csat_accounts, COUNT(low_csat_flag) AS accounts_with_csat "
               "FROM account_facts WHERE region = :region AND billing_cycle = :cycle",
        "parameters": {"region": "LATAM", "cycle": "annual"},
        "derived": [{"name": "low_csat_pct", "kind": "percentage", "numerator": "low_csat_accounts",
                     "denominator": "accounts_with_csat"}],
    }),
    ("Show me our large accounts.", {
        "action": "clarify",
        "question": "Large by which measure (seats, MRR or lifetime revenue), and above what threshold?",
    }),
    ("How long do customers take to pay their invoices?", {
        "action": "unavailable",
        "kind": "missing_data",
        "reason": "The ledger records when an invoice was issued and its payment status, but not when it was paid, "
                  "so time to payment cannot be calculated.",
        "alternative": "I can count invoices and total their amounts by payment status.",
    }),
    ("For each region, list the three accounts with the highest lifetime revenue.", {
        "action": "unavailable",
        "kind": "unsupported_operation",
        "reason": "The data holds lifetime revenue and region for every account, but a top three within each "
                  "region needs a window function, which this tool does not accept.",
        "alternative": "Ask for the top accounts by lifetime revenue in one named region.",
    }),
    ("What happens to an invoice when the source systems disagree about it?", {
        "action": "explain",
        "explanation": "Rows that are identical, or identical once spellings and formats are normalized, collapse "
                       "into one invoice. If the remaining rows still disagree, a row wins only when it is the "
                       "single valid one: a refund must be negative and every other status must not be. When "
                       "several valid rows remain, the invoice is excluded from totals and listed as disputed. "
                       "No status is preferred, because nothing says which source system is right.",
        "source": "Cleaning rules in the solution spec (section 3, which duplicate wins)",
    }),
    ("Write a haiku about spring.", {
        "action": "out_of_domain",
        "reason": "This tool answers questions about CloudNova's invoices and accounts.",
    }),
)


class ModelError(Exception):
    """The provider could not be reached or did not return usable text."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class Transport(Protocol):
    """Anything that turns chat messages into the model's reply text."""

    def complete(self, messages: list[dict]) -> str: ...


# --- prompt -------------------------------------------------------------------

_UNIT_LABELS = {schema.USD_CENTS: "USD cents", schema.LOCAL_CENTS: "cents of the invoice currency",
                schema.BASIS_POINTS: "basis points", schema.FLAG: "0/1 flag", schema.COUNT: "count"}


def _column_line(column: schema.Column) -> str:
    facts = [column.type]
    if column.unit:
        facts.append(_UNIT_LABELS[column.unit])
    if column.values:
        facts.append("one of " + ", ".join(f"'{value}'" for value in column.values))
    if column.nullable:
        facts.append("nullable")
    return f"- {column.name} ({'; '.join(facts)}): {column.description}"


def _views_section() -> str:
    parts = []
    for view in schema.VIEWS.values():
        parts.append(f"### {view.name}: {view.grain}\n{view.time_meaning}\n"
                     + "\n".join(_column_line(column) for column in view.columns))
    return "\n\n".join(parts)


def _money(cents: int) -> str:
    return f"USD {cents // 100}"


def system_prompt(report: dict) -> str:
    """The full instruction text. A function of the schema, the business constants and the quality report."""
    counts, profile = report["counts"], report["profile"]
    disagreements = report["account_disagreements"]["by_field"]
    fx = report["fx_discrepancy"]
    prices = config.PRICE_USD_CENTS
    eur, gbp = config.FX_TO_USD["EUR"], config.FX_TO_USD["GBP"]
    industries = profile.get("industry_values")
    industry_line = (
        "Recorded industry values: " + ", ".join(f"'{value}'" for value in industries) + "."
        if industries else "Industry values are free text; match them with LOWER(industry) = :value.")
    functions = "\n".join(f"  - {shown}" for shown, _example in SQL_FUNCTIONS)
    examples = "\n\n".join(f"Question: {question}\n{json.dumps(proposal, indent=2)}" for question, proposal in EXAMPLES)

    return f"""You are the question interpreter inside a command-line tool that CloudNova's finance team uses to query its invoice ledger. CloudNova is a B2B SaaS company that sells a per-seat subscription. Finance asks a question in plain English and you turn it into one JSON proposal.

You do not answer the question yourself. A separate application checks your proposal, runs approved SQL against a read-only SQLite database, formats every number, and attaches data-quality caveats and evidence. You never see ledger rows, so any figure about the data that you wrote yourself would be invented. Keep figures about the data out of every text field.

The questions are open-ended. Finance will ask about combinations of fields, filters, periods, groupings and rankings that nobody listed in advance. Answer anything the two views below can establish, and say plainly when they cannot.

## What you return

Return exactly one JSON object and nothing else. Its "action" is one of five. Use only the fields shown for the action you choose; extra fields are rejected.

"query": the views can answer the question.
{{"action": "query", "source_view": "...", "interpretation": "...", "sql": "...", "parameters": {{}}, "output_units": {{}}, "derived": []}}
- source_view: the one view the SQL reads.
- interpretation: one sentence saying what the query measures and over which scope, including any default you applied (for example "all available dates" or "active accounts only"). The user reads it to check that you understood the question.
- sql: one SELECT statement, following the rules below.
- parameters: a value for every :name used in the SQL.
- output_units: optional. For each output column measured in USD cents, write "USD cents". The application checks this against its own inference.
- derived: optional. Ratios for the application to compute; see "Ratios, averages and percentages".

"clarify": the question has two or more reasonable readings that would give materially different answers, and no default in this prompt settles it. Ask one targeted question.
{{"action": "clarify", "question": "..."}}

"explain": the user asks how a business or cleaning rule works, not for a figure. Explain only from the rules written in this prompt and name where the rule comes from.
{{"action": "explain", "explanation": "...", "source": "..."}}

"unavailable": the question is about CloudNova's invoices or accounts but cannot be answered.
{{"action": "unavailable", "kind": "missing_data", "reason": "...", "alternative": "..."}}
- kind "missing_data": the ledger does not contain what is needed. Name what is missing.
- kind "unsupported_operation": the data could answer it, but not within the SQL this tool accepts. Name the operation it would need.
- alternative: optional. A related question this tool can answer. Describe it; do not answer it.

"out_of_domain": the request is not about CloudNova's invoices or accounts.
{{"action": "out_of_domain", "reason": "..."}}

## The two views

These are the only two relations you may read, and each query reads exactly one of them.

{_views_section()}

Choosing a view:
- Money in a period, money by an invoice attribute, or anything about individual invoices: invoice_facts. invoice_date is the only date that can define a period.
- Anything counted or averaged per account (MRR, churn, CSAT, support tickets, seats, number of accounts): account_facts. Each account appears once there, so nothing is counted twice.
- Revenue per account over all dates: the lifetime_* columns of account_facts. Revenue per account within a period: invoice_facts grouped by account_id.
- The two views cannot be combined in one query. A question that needs account-level values (MRR, churn, CSAT) restricted or split by an invoice period or invoice status is an unsupported_operation.

## Business definitions

These are fixed and already built into the view columns. Select those columns; do not rebuild the logic in SQL.

Revenue
- "Revenue", "recognized revenue" and "net revenue" mean paid invoices plus refunds, where refunds are negative: net_revenue_cents. Filter status to paid and refunded.
- When the user restricts the question to paid invoices, use paid_revenue_cents, filter status to paid, and do not subtract refunds.
- Refunds "given back" are refunds_cents, a positive amount. Filter status to refunded.
- Pending, failed and void invoices are never revenue.
- "Stuck", "outstanding", "exposed" or "at risk" invoices are those with status pending or failed: exposure_cents. There are no due dates, so "overdue" and "days late" cannot be established.
- Revenue is dated by invoice_date. There are no payment dates.
- USD amounts use fixed rates: EUR x {eur} and GBP x {gbp}.

MRR and activity
- MRR exists only as account_facts.mrr_cents: list price per seat per month (Starter {_money(prices['starter'])}, Pro {_money(prices['pro'])}, Enterprise {_money(prices['enterprise'])}) x seats x (1 - discount), and 0 for churned accounts. Annual billing does not change it: an annual invoice is not divided by 12 and is not counted as monthly revenue.
- An account is active when churned = 0. Payment status does not decide activity.
- Average MRR per account is taken over active accounts (churned = 0) unless the user asks for a different population. Say so in the interpretation.

Churn
- The only churn fact is a flag on each account's latest invoice. Report churn as the share of account snapshots flagged churned: SUM(churned) and COUNT(*) from account_facts with a derived percentage. The denominator includes churned and non-churned accounts.
- There are no churn dates and no history of who was subscribed when. A churn rate for a period (monthly churn, churn in a given quarter, a churn trend) is missing_data.

Accounts and customers
- A customer is an account, identified by account_id. Count customers with COUNT(*) in account_facts, or with COUNT(DISTINCT account_id) in invoice_facts when a period or invoice filter applies.
- Account names are not identifiers. If the user names a customer, filter account_name = :name; the application checks whether that name belongs to several accounts. If the user gives an account ID, filter on account_id.
- CSAT of {config.LOW_CSAT_MAX} or lower is low: use low_csat_flag. A missing CSAT is unknown, never satisfied or dissatisfied.
- The ledger describes what happened, not why. Questions about causes ("why did accounts churn", "what drives refunds") are missing_data; you may offer a descriptive breakdown as the alternative.

Units
- Money columns are integer cents. Convert a threshold the user gives in dollars: USD 10,000 is 1000000.
- discount_bp is basis points: 15% is 1500.
- amount_local_cents mixes currencies. Use it only when you filter or group by currency; otherwise use the USD columns.

Not in the ledger: costs and margins, payment dates, due dates, churn or cancellation dates and reasons, subscription history, renewals, product usage, sales ownership, tax, and anything about individual users.

## SQL the application accepts

One SELECT statement over one view. The application parses the statement and rejects anything outside this list, so a query that is valid SQLite can still be refused.

Allowed:
- Columns by name, DISTINCT, WHERE with AND, OR and NOT, comparisons (= <> < <= > >=), IN, BETWEEN, LIKE, IS NULL, IS NOT NULL, CASE WHEN, GROUP BY, HAVING, ORDER BY, and LIMIT with a positive integer.
- COUNT(*), COUNT(column), COUNT(DISTINCT column), SUM, MIN, MAX, and AVG of values that are not money.
- Arithmetic (+ - * / %) and || for joining text.
- These functions and no others:
{functions}
- Buckets: month is strftime('%Y-%m', invoice_date), year is strftime('%Y', invoice_date), quarter is {QUARTER_EXPRESSION}.
- Side-by-side comparison of periods or categories: conditional aggregation, SUM(CASE WHEN ... THEN measure ELSE 0 END).

Not allowed: joins, subqueries, CTEs (WITH), UNION, INTERSECT, EXCEPT, window functions (OVER), SELECT *, comments, more than one statement, and any function not listed. This rules out, for example, running totals, the change from each month to the next, the top N within every group, and comparison against an overall average. If the question needs one of these, return unavailable with kind unsupported_operation. Do not approximate it with a different query.

Requirements:
- Read the view named in source_view, unqualified and without an alias.
- Every text value compared with a column must be a named parameter such as :region, never a quoted literal. Numbers may be written inline. Format strings inside strftime and date are fine. Every parameter must be used, and ? is not accepted.
- Enumerated columns accept only the exact values listed for them (plan is 'starter', not 'Starter').
- Dates are YYYY-MM-DD text. Filter a period as a half-open range: invoice_date >= :start AND invoice_date < :end.
- Put every restriction the user asked for in WHERE. The application derives the evaluated population, the evidence file and the check for excluded disputed invoices from your WHERE clause. A measure that is 0 for non-matching rows is not a substitute for the filter: for paid revenue also filter status = :paid, and for exposure also filter status IN (:pending, :failed).
- Give every computed output a name with AS. Output names must be unique. GROUP BY column names or output aliases, not positions. Every output that is not aggregated must be in GROUP BY.
- For "which X has the highest ..." return every group, sorted, unless the user asks for a top N. For a ranking, add a tie-break on the ID or group column so the order is deterministic.
- For a list of records, include the ID column (invoice_id or account_id).
- In invoice_facts never SUM or AVG churned, csat_score or support_tickets. They are account values repeated on every invoice; use account_facts.
- Money outputs must keep a clear unit: select a money column, aggregate it, add or subtract like units, or multiply by a plain number.

## Ratios, averages and percentages

Do not average money in SQL and do not divide in an output column. Return the integer numerator and denominator as two output columns and declare the ratio in "derived". The application divides with decimal arithmetic and rounds for display.

{{"name": "average_mrr_usd", "kind": "usd_average", "numerator": "mrr_cents", "denominator": "accounts"}}

- usd_average: a sum in USD cents divided by a count, shown as USD per unit.
- percentage: numerator / denominator x 100. Both must be in the same unit (two counts, or two USD-cent sums).
- ratio: numerator / denominator. Both must be in the same unit.

The name must be a new identifier. The numerator and denominator must be output column names. To sort by a ratio, ORDER BY SUM(a) * 1.0 / COUNT(*) is accepted because it is not an output column.

## Dates and coverage

- Eligible invoices are dated from {profile['invoice_date_min']} to {profile['invoice_date_max']}. The range can extend past today's date and is not an export date.
- When the user gives no period, do not add a date filter. The answer then covers all available dates and the application says so.
- The ledger has no "as of" date, so a relative period ("last year", "this quarter", "recently", "so far") has no reliable anchor. Ask which calendar period the user means.
- A year means the calendar year, from January 1 up to but not including the next January 1.

## Data quality summary

This is background. The application attaches the relevant caveats to each answer, so do not repeat them in the interpretation.

- The export has {counts['input_rows']:,} rows. After {counts['duplicate_rows_removed']:,} duplicate rows and {counts['rejected_invalid_rows']:,} invalid rows are removed, {counts['eligible_invoices']:,} invoices are eligible.
- {counts['held_conflict_invoice_ids']:,} invoice IDs are disputed: the source systems disagree about them. They are in neither view. The application reports when they could change an answer.
- {counts['accounts']:,} accounts have a snapshot in account_facts. {counts['accounts_without_snapshot']:,} accounts have only disputed invoices and no snapshot.
- Account attributes are not stable: {disagreements.get('account_name', 0):,} accounts carry more than one name, {disagreements.get('region', 0):,} more than one region and {disagreements.get('churned', 0):,} more than one churn flag across their invoices. account_facts takes the latest invoice.
- Some invoice dates are written DD-MM-YYYY and could be read month-first. They are read day-first and flagged.
- {fx['amount_equals_usd_list_price_times_rate']:,} of {fx['foreign_currency_non_refund_invoices']:,} foreign-currency non-refund invoices carry an amount equal to the USD list price times the FX rate, so converting them again may overstate USD. Conversion follows the customer briefing; this is unresolved with finance.
- industry, csat_score, payment_method and contact_email are sometimes null. {industry_line}

## Rules you can explain

From the customer briefing:
- List price per seat per month is {_money(prices['starter'])} for Starter, {_money(prices['pro'])} for Pro and {_money(prices['enterprise'])} for Enterprise.
- Billing is monthly or annual. An annual invoice bills 10 months, not 12: two months are free.
- The discount percentage applies to the gross invoice amount.
- Invoice amounts are stored in the account's currency (USD, EUR or GBP) and normalized to USD at the fixed rates above.
- MRR for an active subscription is list price x seats x (1 - discount). An annual invoice maps to the same MRR; the annual lump sum is not counted again.
- Only invoices whose status normalizes to paid are recognized revenue. Pending, failed and void are not revenue. Refunded invoices carry a negative amount and net against revenue.

From the cleaning rules in the solution spec:
- Status, plan, billing cycle and churn spellings are mapped to canonical values. An unknown required category makes the row invalid; it is never mapped to a default.
- Amounts are parsed as decimals, converted per invoice and rounded half-up to USD cents.
- Dates written YYYY-MM-DD, YYYY/MM/DD, MM/DD/YYYY (US order) and "Jan 5 2024" are accepted. DD-MM-YYYY is read day-first and flagged when month-first would also be a real date.
- Duplicates: identical rows and rows identical after normalization collapse to one invoice. If rows still disagree, a row wins only when it is the single valid one (a refund must be negative, any other status must not be). Otherwise the invoice is excluded and reported as disputed; no status is preferred.
- The account snapshot is the invoice with the latest invoice date, ties broken by the greatest invoice ID. Its attributes are copied together.
- Missing CSAT, industry and email stay unknown. A malformed email becomes null.

## Examples

These show the format. They are not a list of supported questions.

{examples}
"""


def user_message(question: str, clarification: tuple[str | None, str] | None = None) -> str:
    """The user turn. With a clarification, the original question is restated together with the reply."""
    if not clarification:
        return f"Question: {question}"
    asked, reply = clarification
    lines = [f"Question: {question}", ""]
    if asked:
        lines.append(f"The user was asked to clarify: {asked}")
    lines.append(f"The user's clarification: {reply}")
    lines += ["", "Answer the original question using this clarification."]
    return "\n".join(lines)


def clarification_for_model(answer: Answer) -> str:
    """What was asked, restated for the model. Account IDs found in the data are not passed on."""
    ids = answer.detail.get("account_ids")
    if ids:
        return (f"The account name {answer.detail.get('account_name')!r} belongs to {len(ids)} different account "
                "IDs. Which account ID do you mean?")
    return answer.message


def repair_message(failure: Exception) -> str:
    code = f" ({failure.code})" if isinstance(failure, QueryRejected) else ""
    return (
        f"The application did not accept that proposal{code}: {failure}\n\n"
        "Return one corrected JSON proposal for the same question. Keep every filter the user asked for. If the "
        "question cannot be expressed in the SQL this tool accepts, return \"unavailable\" with kind "
        "\"unsupported_operation\"; do not drop part of the question to make the query pass.")


def extract_json(reply: str) -> str:
    """Strip a Markdown code fence if the model added one. The content itself is not altered."""
    text = reply.strip()
    if text.startswith("```") and text.endswith("```") and "\n" in text:
        text = text[text.index("\n") + 1: -3].strip()
    return text


# --- provider adapter ---------------------------------------------------------

class AzureTransport:
    """Chat completions on an Azure AI Foundry deployment through the OpenAI SDK."""

    def __init__(self, settings: Settings):
        missing = settings.missing_model_settings()
        if missing:
            raise ConfigError(
                "missing model settings: " + ", ".join(missing) + ". Fill them in .env (see .env.example).")
        parts = urlsplit(settings.azure_endpoint)
        if parts.scheme not in ("https", "http") or not parts.netloc:
            raise ConfigError("AZURE_OPENAI_ENDPOINT must be a URL such as https://<resource>.openai.azure.com/")
        import openai

        self._openai = openai
        self._model = settings.azure_deployment
        common = {"api_key": settings.azure_api_key, "timeout": settings.llm_timeout_s, "max_retries": 1}
        origin = f"{parts.scheme}://{parts.netloc}"
        if settings.uses_v1_endpoint:
            self._client = openai.OpenAI(base_url=f"{origin}/openai/v1/", **common)
        else:
            # The SDK adds /openai/deployments/<deployment>/... itself, so only the resource origin is passed.
            self._client = openai.AzureOpenAI(
                azure_endpoint=origin, api_version=settings.azure_api_version, **common)

    @property
    def base_url(self) -> str:
        """Where requests are sent, for diagnostics and tests. Contains no credentials."""
        return str(self._client.base_url)

    def complete(self, messages: list[dict]) -> str:
        openai = self._openai
        try:
            response = self._client.chat.completions.create(
                model=self._model, messages=messages, response_format={"type": "json_object"})
        except openai.APITimeoutError:
            raise ModelError("provider_timeout", "the model did not respond before the timeout") from None
        except openai.APIConnectionError as exc:
            raise ModelError("provider_unreachable", f"could not reach the model endpoint: {exc}") from None
        except openai.AuthenticationError:
            raise ModelError("provider_auth", "the model endpoint rejected the API key") from None
        except openai.APIStatusError as exc:
            raise ModelError("provider_error", f"the model endpoint returned HTTP {exc.status_code}: "
                                               f"{_first_line(exc.message)}") from None
        except openai.OpenAIError as exc:
            raise ModelError("provider_error", f"{type(exc).__name__}: {_first_line(str(exc))}") from None
        choice = response.choices[0] if response.choices else None
        content = choice.message.content if choice else None
        if not content or not content.strip():
            reason = getattr(choice, "finish_reason", None) or "no choices returned"
            raise ModelError("empty_response", f"the model returned no text (finish reason: {reason})")
        return content


def _first_line(text: str, limit: int = 300) -> str:
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return line[:limit]


# --- asking -------------------------------------------------------------------

def ask(question: str, settings: Settings, transport: Transport,
        clarification: tuple[str | None, str] | None = None, origin: str = "model") -> Answer:
    """Interpret one question and answer it. The model gets one repair attempt, validated like the first."""
    try:
        report = engine.load_report(settings)
        if not settings.db_path.is_file():
            raise QueryFailed(f"database {settings.db_path} does not exist; run the build first")
    except QueryFailed as exc:
        return engine.error_answer(exc, None, question, origin)

    messages = [
        {"role": "system", "content": system_prompt(report)},
        {"role": "user", "content": user_message(question, clarification)},
    ]
    first_failure = None
    while True:
        try:
            reply = transport.complete(messages)
        except ModelError as exc:
            return Answer(kind="error", error_code=exc.code, message=str(exc), question=question, origin=origin)
        proposal = extract_json(reply)
        try:
            parsed, query = engine.prepare(proposal)
            answer = engine.answer_prepared(parsed, query, settings, question=question, origin=origin)
        except REPAIRABLE as exc:
            if first_failure is None:
                first_failure = exc
                messages += [{"role": "assistant", "content": reply},
                             {"role": "user", "content": repair_message(exc)}]
                continue
            answer = engine.error_answer(exc, proposal, question, origin)
            answer.detail.update(first_error=str(first_failure), model_reply=reply[:1000])
            return answer
        except engine.FAILURES as exc:      # timeout or denial: not something a rewrite should paper over
            return engine.error_answer(exc, proposal, question, origin)
        answer.repaired = first_failure is not None
        return answer
