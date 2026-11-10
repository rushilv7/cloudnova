"""Model path and CLI, exercised offline with a stub in place of the model.

Nothing here calls a model. `Stub` returns replies written by hand in this
file, and every answer produced through it carries the origin "stub transport"
so that it cannot be mistaken for a model response. These cases test what the
application does with a reply: repair, refusal, error handling and the prompt
it would send. Whether a real model interprets questions correctly is tested
only in cases_live.py.
"""

import contextlib
import io
import json
import re
from pathlib import Path

from evals import expected as E
from evals.harness import FIXTURES, case, check, eq, fixture, full, report, scratch

ORIGIN = "stub transport"
COUNT_INVOICES = {"action": "query", "source_view": "invoice_facts", "interpretation": "Number of eligible invoices",
                  "sql": "SELECT COUNT(*) AS invoices FROM invoice_facts", "parameters": {}}
UNKNOWN_COLUMN = {"action": "query", "source_view": "invoice_facts", "interpretation": "Gross margin",
                  "sql": "SELECT SUM(gross_margin_cents) AS margin FROM invoice_facts", "parameters": {}}


class Stub:
    """Scripted replies standing in for a model. Records every message list it is sent."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    def complete(self, messages: list[dict]) -> str:
        self.calls.append([dict(message) for message in messages])
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, str) else json.dumps(reply)


def _ask(settings, stub, question="How many invoices are there?", **extra):
    from app import llm

    return llm.ask(question, settings, stub, origin=ORIGIN, **extra)


def _cli(argv: list[str], env: dict) -> tuple[int, str, str]:
    from app.__main__ import main

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        status = main(argv, env=env)
    return status, out.getvalue(), err.getvalue()


# --- replies ------------------------------------------------------------------

@case("model_malformed_reply", "model", "Stub: a reply that is not a proposal is rejected after one repair attempt")
def model_malformed_reply():
    stub = Stub("I think the answer is 42.", "Still about 42.", COUNT_INVOICES)
    a = _ask(fixture("unseen"), stub)
    eq(a.kind, "error", "answer kind")
    eq(a.error_code, "invalid_proposal", "error code")
    eq(len(stub.calls), 2, "model calls (the first and exactly one repair)")
    check(not a.rows and not a.evidence_path, "no result may accompany the error")
    check("No number is reported" in a.text() and "42" not in a.text().split("Model reply")[0],
          "the model's own figure is not presented as an answer")
    check(f"{ORIGIN} (not a model response)" in a.text(), "stub output is labelled")


@case("model_repair_succeeds", "model", "Stub: a rejected query is sent back once with the reason, then validated again")
def model_repair_succeeds():
    stub = Stub(UNKNOWN_COLUMN, COUNT_INVOICES)
    a = _ask(fixture("unseen"), stub)
    eq(a.kind, "result", "answer kind")
    eq(a.rows, [(E.UNSEEN["invoice_count"],)], "result of the repaired query")
    eq(a.repaired, True, "repair is disclosed")
    check("used its one repair attempt" in a.text(), "repair is stated in the answer text")
    second = stub.calls[1]
    eq([m["role"] for m in second], ["system", "user", "assistant", "user"], "repair conversation")
    check("unknown_column" in second[-1]["content"] and "gross_margin_cents" in second[-1]["content"],
          "the repair request carries the rejection reason")


@case("model_repair_limit", "model", "Stub: a second rejected query ends in an error; there is no third attempt")
def model_repair_limit():
    stub = Stub(UNKNOWN_COLUMN, {**UNKNOWN_COLUMN, "sql": "SELECT invoice_id FROM invoices"}, COUNT_INVOICES)
    a = _ask(fixture("unseen"), stub)
    eq(a.kind, "error", "answer kind")
    eq(a.error_code, "rejected_query", "error code")
    eq(a.detail.get("reject_code"), "unapproved_view", "the second rejection is the one reported")
    eq(len(stub.calls), 2, "model calls")
    eq(len(stub.replies), 1, "the third scripted reply was never requested")
    check(not a.rows, "no rows")


@case("model_provider_failure", "model", "Stub: a provider failure becomes a clear error and is not retried as a repair")
def model_provider_failure():
    from app.llm import ModelError

    stub = Stub(ModelError("provider_unreachable", "could not reach the model endpoint: connection refused"))
    a = _ask(fixture("unseen"), stub)
    eq((a.kind, a.error_code), ("error", "provider_unreachable"), "kind and code")
    eq(len(stub.calls), 1, "model calls")
    check("could not reach the model endpoint" in a.text() and "No number is reported" in a.text(), "error text")

    stub = Stub(UNKNOWN_COLUMN, ModelError("provider_timeout", "the model did not respond before the timeout"))
    a = _ask(fixture("unseen"), stub)
    eq((a.kind, a.error_code), ("error", "provider_timeout"), "failure during the repair call")


@case("model_execution_failure_not_repaired", "model", "Stub: a query stopped by the deadline is reported, not rewritten")
def model_execution_failure_not_repaired():
    stub = Stub({**COUNT_INVOICES, "source_view": "account_facts",
                 "sql": "SELECT COUNT(*) AS accounts FROM account_facts"}, COUNT_INVOICES)
    a = _ask(full(query_timeout_s=1e-9), stub)
    eq((a.kind, a.error_code), ("error", "query_timeout"), "kind and code")
    eq(len(stub.calls), 1, "model calls")


@case("model_non_query_actions", "model", "Stub: clarify, explain, unavailable and out-of-domain replies run no query")
def model_non_query_actions():
    replies = [
        ({"action": "clarify", "question": "Best by revenue or by MRR?"}, "clarify", "Clarification needed"),
        ({"action": "explain", "explanation": "Annual invoices map to the same MRR.", "source": "customer briefing"},
         "explain", "No query was run"),
        ({"action": "unavailable", "kind": "missing_data", "reason": "There is no cost data."},
         "unavailable", "the data needed is not in the ledger"),
        ({"action": "unavailable", "kind": "unsupported_operation", "reason": "Needs a window function.",
          "alternative": "Ask for one month."}, "unavailable", "not supported in this version"),
        ({"action": "out_of_domain", "reason": "Not about invoices."}, "out_of_domain", "invoices and accounts only"),
    ]
    for reply, kind, phrase in replies:
        stub = Stub(reply)
        a = _ask(fixture("unseen"), stub)
        eq(a.kind, kind, f"answer kind for {reply['action']}")
        check(not a.rows and not a.sql and not a.evidence_path, f"{kind}: no query, rows or evidence")
        check(phrase in a.text(), f"{kind}: text should contain {phrase!r}; got {a.text()!r}")
        eq(len(stub.calls), 1, "model calls")

    # An unavailable reply cannot smuggle a figure in through an extra field.
    smuggled = {"action": "unavailable", "kind": "missing_data", "reason": "No costs.", "estimate_pct": 41.5}
    a = _ask(fixture("unseen"), Stub(smuggled, smuggled))
    eq((a.kind, a.error_code), ("error", "invalid_proposal"), "extra fields are rejected")


@case("model_fenced_reply", "model", "Stub: a proposal wrapped in a Markdown code fence is still read")
def model_fenced_reply():
    a = _ask(fixture("unseen"), Stub("```json\n" + json.dumps(COUNT_INVOICES, indent=2) + "\n```"))
    eq(a.kind, "result", "answer kind")
    eq(a.repaired, False, "no repair was needed")


@case("model_clarification_round", "model", "Stub: the user's reply is sent with the original question; found IDs are not")
def model_clarification_round():
    from app import llm

    s = fixture("unseen")
    question = "Who are our best customers?"
    stub = Stub({"action": "clarify", "question": "Best by which measure?"},
                {"action": "query", "source_view": "account_facts", "interpretation": "Accounts by lifetime net revenue",
                 "sql": "SELECT account_id, lifetime_net_revenue_cents FROM account_facts "
                        "ORDER BY lifetime_net_revenue_cents DESC, account_id LIMIT 3", "parameters": {}})
    first = _ask(s, stub, question)
    eq(first.kind, "clarify", "first answer")
    second = _ask(s, stub, question, clarification=(llm.clarification_for_model(first), "by lifetime revenue"))
    eq(second.kind, "result", "second answer")
    sent = stub.calls[1][-1]["content"]
    check(all(part in sent for part in (question, "Best by which measure?", "by lifetime revenue")),
          f"the follow-up must restate the question, the clarification and the reply; sent {sent!r}")

    # A clarification raised by the application (a name shared by several accounts) lists the IDs to the
    # user, but the IDs found in the data are not passed on to the model.
    named = {"action": "query", "source_view": "invoice_facts", "interpretation": "Invoices for Stark Labs",
             "sql": "SELECT invoice_id FROM invoice_facts WHERE account_name = :name", "parameters": {"name": "Stark Labs"}}
    a = _ask(s, Stub(named), "List the invoices for Stark Labs.")
    eq(a.kind, "clarify", "shared account name")
    eq(a.detail.get("account_ids"), E.UNSEEN["stark_labs_account_ids"], "candidate IDs shown to the user")
    for_model = llm.clarification_for_model(a)
    check("Stark Labs" in for_model and "ACC-" not in for_model, f"model-facing text: {for_model!r}")


# --- prompt -------------------------------------------------------------------

@case("prompt_contract", "model", "The prompt carries the schema and coverage, and no records, eval questions or results")
def prompt_contract():
    from app import llm, schema
    from evals import cases_live

    s = full()
    quality = report(s)
    prompt = llm.system_prompt(quality)
    for view in schema.VIEWS.values():
        check(f"### {view.name}" in prompt, f"view {view.name} is described")
        for column in view.columns:
            check(f"- {column.name} (" in prompt, f"column {view.name}.{column.name} is described")
            for value in column.values:
                check(f"'{value}'" in prompt, f"allowed value {value!r} of {column.name} is listed")
    for key in ("invoice_date_min", "invoice_date_max"):
        check(quality["profile"][key] in prompt, f"date coverage {key} is stated")
    check("JSON" in prompt, "the prompt asks for JSON (required by the provider's JSON mode)")

    # No ledger records: IDs, email addresses or account names.
    check(not re.search(r"\b(ACC|INV)-\d", prompt), "the prompt contains an account or invoice ID")
    check("@" not in prompt, "the prompt contains an email address")
    names = {row["account_name"] for row in _raw_rows(s) if row["account_name"].strip()}
    leaked = sorted(name for name in names if name in prompt)
    check(not leaked, f"account names from the CSV appear in the prompt: {leaked[:5]}")

    # No eval question and no reference result, so live cases cannot be answered by copying.
    for question in cases_live.ALL_QUESTIONS:
        check(question not in prompt, f"eval question appears in the prompt: {question!r}")
    r = E.REFERENCE
    figures = [r["paid_2024_cents"], r["refunds_cents"], r["exposure_cents"], r["paid_2024_disputed_additional_cents"],
               *(cents for _id, cents, _csat, _low in r["top_five"])]
    for cents in figures:
        for text in (str(cents), f"{cents / 100:,.2f}", f"{cents / 100:.2f}"):
            check(text not in prompt, f"reference result {text} appears in the prompt")
    for text in ("6,164.03", "6164.03", "21.85", "18.79"):
        check(text not in prompt, f"reference result {text} appears in the prompt")


def _raw_rows(settings) -> list[dict]:
    from app.build import read_csv

    rows, _sha256 = read_csv(settings.csv_path)
    return rows


@case("prompt_examples_valid", "model", "Every example and function in the prompt passes the guard and runs read-only")
def prompt_examples_valid():
    from app import engine, llm
    from app.execute import run_query
    from app.proposal import parse_proposal
    from app.sqlguard import validate_query

    s = full()
    for question, proposal in llm.EXAMPLES:
        parsed = parse_proposal(proposal)
        if parsed.action == "query":
            a = engine.run_proposal(proposal, s, question=question, origin="prompt example")
            eq(a.kind, "result", f"example {question!r}: {a.message}")
    for shown, expression in (*llm.SQL_FUNCTIONS, ("quarter bucket", llm.QUARTER_EXPRESSION)):
        parameters = {"text": "x"} if ":text" in expression else {}
        try:
            query = validate_query(f"SELECT {expression} AS value FROM invoice_facts ORDER BY invoice_id LIMIT 1",
                                   parameters, "invoice_facts")
            run_query(s, query.sql, query.parameters)
        except Exception as exc:
            raise AssertionError(f"the prompt offers {shown}, but {expression} fails: {type(exc).__name__}: {exc}")


# --- the live checkers themselves ---------------------------------------------

def _q(view: str, sql: str, parameters: dict | None = None, derived: list | None = None) -> dict:
    return {"action": "query", "source_view": view, "interpretation": "hand-written", "sql": sql,
            "parameters": parameters or {}, "derived": derived or []}


_AVG_MRR = [{"name": "average_mrr_usd", "kind": "usd_average", "numerator": "mrr_cents", "denominator": "accounts"}]
_CHURN_PCT = [{"name": "churn_pct", "kind": "percentage", "numerator": "churned_accounts", "denominator": "accounts"}]
_TOP_FIVE = ("SELECT account_id, lifetime_net_revenue_cents, csat_score, low_csat_flag FROM account_facts "
             "ORDER BY lifetime_net_revenue_cents {direction}, account_id LIMIT 5")

# checker name -> (a correct hand-written proposal, a plausible wrong one the checker must refuse)
CHECKER_PROPOSALS = {
    "paid_2024": (
        _q("invoice_facts", "SELECT SUM(paid_revenue_cents) AS paid_revenue_cents FROM invoice_facts WHERE status = "
           ":paid AND invoice_date >= :start AND invoice_date < :end",
           {"paid": "paid", "start": "2024-01-01", "end": "2025-01-01"}),
        # Net revenue instead of paid-only revenue.
        _q("invoice_facts", "SELECT SUM(net_revenue_cents) AS net_revenue_cents FROM invoice_facts WHERE status IN "
           "(:paid, :refunded) AND invoice_date >= :start AND invoice_date < :end",
           {"paid": "paid", "refunded": "refunded", "start": "2024-01-01", "end": "2025-01-01"}),
    ),
    "region_mrr": (
        _q("account_facts", "SELECT region, SUM(mrr_cents) AS mrr_cents, COUNT(*) AS accounts FROM account_facts "
           "WHERE churned = 0 GROUP BY region ORDER BY SUM(mrr_cents) * 1.0 / COUNT(*) DESC, region", derived=_AVG_MRR),
        # Churned accounts left in the denominator.
        _q("account_facts", "SELECT region, SUM(mrr_cents) AS mrr_cents, COUNT(*) AS accounts FROM account_facts "
           "GROUP BY region ORDER BY SUM(mrr_cents) * 1.0 / COUNT(*) DESC, region", derived=_AVG_MRR),
    ),
    "refunds": (
        _q("invoice_facts", "SELECT SUM(refunds_cents) AS refunds_cents FROM invoice_facts WHERE status = :refunded",
           {"refunded": "refunded"}),
        # Right total, but the population is every invoice instead of the refunded ones.
        _q("invoice_facts", "SELECT SUM(refunds_cents) AS refunds_cents FROM invoice_facts"),
    ),
    "churn": (
        _q("account_facts", "SELECT plan, SUM(churned) AS churned_accounts, COUNT(*) AS accounts FROM account_facts "
           "WHERE plan IN (:a, :b) GROUP BY plan ORDER BY plan", {"a": "enterprise", "b": "starter"}, _CHURN_PCT),
        # Invoices counted as accounts.
        _q("invoice_facts", "SELECT plan, COUNT(DISTINCT CASE WHEN churned = 1 THEN account_id END) AS "
           "churned_accounts, COUNT(*) AS accounts FROM invoice_facts WHERE plan IN (:a, :b) GROUP BY plan",
           {"a": "enterprise", "b": "starter"}, _CHURN_PCT),
    ),
    "top_five": (
        _q("account_facts", _TOP_FIVE.format(direction="DESC")),
        _q("account_facts", _TOP_FIVE.format(direction="ASC")),
    ),
    "exposure": (
        _q("invoice_facts", "SELECT COUNT(*) AS invoices, SUM(exposure_cents) AS exposure_cents FROM invoice_facts "
           "WHERE status IN (:pending, :failed)", {"pending": "pending", "failed": "failed"}),
        # Pending only.
        _q("invoice_facts", "SELECT COUNT(*) AS invoices, SUM(exposure_cents) AS exposure_cents FROM invoice_facts "
           "WHERE status = :pending", {"pending": "pending"}),
    ),
}


@case("live_checkers_discriminate", "model",
      "Stub: the live baseline checkers accept a correct hand-written proposal and refuse a wrong one")
def live_checkers_discriminate():
    from evals import cases_live

    s = full()
    checkers = dict(cases_live._BASELINE_CHECKS)
    eq(sorted(checkers), sorted(CHECKER_PROPOSALS), "every baseline checker is covered")
    for name, (right, wrong) in CHECKER_PROPOSALS.items():
        checkers[name](_ask(s, Stub(right)))
        try:
            checkers[name](_ask(s, Stub(wrong)))
        except AssertionError:
            continue
        raise AssertionError(f"the {name} checker accepted a wrong proposal: {wrong['sql']}")


# --- provider settings and CLI ------------------------------------------------

@case("azure_endpoint_forms", "model", "Endpoint settings are checked and normalized without contacting the provider")
def azure_endpoint_forms():
    from app.config import ConfigError, load_settings
    from app.llm import AzureTransport

    def transport(**values):
        env = {"AZURE_OPENAI_API_KEY": "not-a-real-key", "AZURE_OPENAI_DEPLOYMENT": "deployment-name", **values}
        return AzureTransport(load_settings(env=env))

    classic = transport(AZURE_OPENAI_ENDPOINT="https://example.openai.azure.com/",
                        AZURE_OPENAI_API_VERSION="2024-10-21")
    check(classic.base_url.startswith("https://example.openai.azure.com/openai"), classic.base_url)
    pasted = transport(
        AZURE_OPENAI_ENDPOINT="https://example.openai.azure.com/openai/deployments/x/chat/completions?api-version=1",
        AZURE_OPENAI_API_VERSION="2024-10-21")
    eq(pasted.base_url, classic.base_url, "a pasted full request URL is reduced to the resource endpoint")
    v1 = transport(AZURE_OPENAI_ENDPOINT="https://example.services.ai.azure.com/openai/v1")
    eq(v1.base_url, "https://example.services.ai.azure.com/openai/v1/", "v1 endpoint needs no API version")

    for label, values in (
        ("nothing set", {}),
        ("no API version for a classic endpoint", {"AZURE_OPENAI_ENDPOINT": "https://example.openai.azure.com/"}),
        ("endpoint is not a URL", {"AZURE_OPENAI_ENDPOINT": "example.openai.azure.com",
                                   "AZURE_OPENAI_API_VERSION": "2024-10-21"}),
    ):
        try:
            if label == "nothing set":
                AzureTransport(load_settings(env={}))
            else:
                transport(**values)
        except ConfigError as exc:
            check("not-a-real-key" not in str(exc), "the error must not echo the key")
            continue
        raise AssertionError(f"expected a configuration error when {label}")


@case("cli_requires_credentials", "model", "--ask and --demo without credentials exit 2 with a clear message and no answer")
def cli_requires_credentials():
    out_dir = scratch("cli-nokey")
    env = {"CLOUDNOVA_CSV": str(FIXTURES / "unseen.csv"), "CLOUDNOVA_OUT_DIR": str(out_dir)}
    for argv in (["--ask", "How many invoices are there?"], ["--demo"]):
        status, out, err = _cli(argv, env)
        eq(status, 2, f"exit status of {argv[0]}")
        for name in ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_DEPLOYMENT",
                     "AZURE_OPENAI_API_VERSION"):
            check(name in err, f"{argv[0]}: the message should name {name}; got {err!r}")
        check("No model was called" in err, "the message says no model was called")
        check("Result" not in out and "USD" not in out, f"{argv[0]}: no answer may be printed; got {out!r}")


@case("cli_build_and_proposal", "model", "--build reports the reconciliation; --proposal runs a labelled hand-written proposal")
def cli_build_and_proposal():
    out_dir = scratch("cli-build")
    env = {"CLOUDNOVA_CSV": str(FIXTURES / "duplicates.csv"), "CLOUDNOVA_OUT_DIR": str(out_dir)}
    d = E.DUPLICATES
    status, out, _err = _cli(["--build"], env)
    eq(status, 0, "exit status of --build")
    duplicates = d["exact_duplicate_rows"] + d["normalized_duplicate_rows"]
    want = (f"{d['input_rows']} input rows = {duplicates} duplicate rows removed + {d['rejected_invalid_rows']} "
            f"invalid rows rejected + {d['held_conflict_rows']} rows held as disputed "
            f"({len(d['held_conflict_invoice_ids'])} invoice IDs) + {len(d['eligible_invoice_ids'])} eligible invoices")
    check(want in out, f"build summary should contain {want!r}; got {out!r}")

    proposal = out_dir / "proposal.json"
    proposal.write_text(json.dumps({
        "action": "query", "source_view": "invoice_facts", "interpretation": "Paid revenue, all dates",
        "sql": "SELECT SUM(paid_revenue_cents) AS paid_revenue_cents FROM invoice_facts WHERE status = :status",
        "parameters": {"status": "paid"}}), encoding="utf-8")
    status, out, _err = _cli(["--proposal", str(proposal)], env)
    eq(status, 0, "exit status of --proposal")
    check("hand-written proposal (not a model response)" in out, "the output is labelled as not from a model")
    check(f"USD {d['paid_revenue_cents'] / 100:,.2f}" in out, f"paid revenue should be shown; got {out!r}")

    proposal.write_text(json.dumps({"action": "query", "source_view": "invoice_facts", "interpretation": "x",
                                    "sql": "DELETE FROM invoices", "parameters": {}}), encoding="utf-8")
    status, out, _err = _cli(["--proposal", str(proposal)], env)
    eq(status, 1, "exit status for a rejected proposal")
    check("No number is reported" in out, "rejection text")
    eq(Path(out_dir / "cloudnova.db").is_file(), True, "database still present")

    status, out, _err = _cli(["--show-prompt"], env)
    eq(status, 0, "exit status of --show-prompt")
    check("## The two views" in out and "### account_facts" in out, "the prompt is printed")
