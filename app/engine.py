"""Turn a proposal into an answer: validate, execute, attach evidence and limitations.

Used by the CLI after the model responds and by the offline evals with
hand-written proposals. Nothing in here depends on which question was asked:
warnings, exclusions and evidence are derived from the validated query's view,
columns and filter.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from functools import lru_cache
from pathlib import Path

from app import schema
from app.answer import Answer, Note, compute_derived, format_derived, format_value, header
from app.config import Settings
from app.execute import Database, QueryDenied, QueryFailed, QueryTimeout, authorize
from app.proposal import Proposal, ProposalError, parse_proposal
from app.sqlguard import QueryRejected, ValidatedQuery, validate_query

_USD_COLUMNS = {
    "amount_usd_cents", "paid_revenue_cents", "net_revenue_cents", "refunds_cents", "exposure_cents",
    "lifetime_paid_revenue_cents", "lifetime_net_revenue_cents", "lifetime_refunds_cents", "lifetime_exposure_cents",
}
_INVOICE_TIME_COLUMNS = ("region", "plan", "industry", "account_name")


@lru_cache(maxsize=8)
def _load_report(path: str, _mtime: float) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_report(settings: Settings) -> dict:
    path = settings.report_path
    if not path.is_file():
        raise QueryFailed(f"quality report {path} does not exist; run the build first")
    return _load_report(str(path), path.stat().st_mtime)


# --- entry points -------------------------------------------------------------

def prepare(proposal) -> tuple[Proposal, ValidatedQuery | None]:
    """Shape-check a proposal and validate its SQL. Raises ProposalError or QueryRejected."""
    parsed = proposal if isinstance(proposal, Proposal) else parse_proposal(proposal)
    if parsed.action != "query":
        return parsed, None
    query = validate_query(parsed.sql, parsed.parameters, parsed.source_view, parsed.output_units, parsed.derived)
    return parsed, query


FAILURES = (ProposalError, QueryRejected, QueryTimeout, QueryDenied, QueryFailed)
_ERROR_CODES = {ProposalError: "invalid_proposal", QueryRejected: "rejected_query", QueryTimeout: "query_timeout",
                QueryDenied: "query_denied", QueryFailed: "sql_error"}


def proposed_sql(proposal) -> str:
    """The SQL text of a proposal in any form (dict, JSON text or parsed), for error reporting only."""
    if isinstance(proposal, str):
        try:
            proposal = json.loads(proposal)
        except ValueError:
            return ""
    sql = proposal.get("sql", "") if isinstance(proposal, dict) else getattr(proposal, "sql", "")
    return sql if isinstance(sql, str) else ""


def error_answer(exc: Exception, proposal, question: str | None = None, origin: str = "model") -> Answer:
    """Turn a validation or execution failure into an error answer. No number is reported."""
    detail = {"reject_code": exc.code} if isinstance(exc, QueryRejected) else {}
    return Answer(kind="error", error_code=_ERROR_CODES[type(exc)], message=str(exc), sql=proposed_sql(proposal),
                  detail=detail, origin=origin, question=question)


def run_proposal(proposal, settings: Settings, question: str | None = None, origin: str = "model") -> Answer:
    """Validate and answer one proposal. Every failure becomes an error answer; nothing is guessed."""
    try:
        parsed, query = prepare(proposal)
        return answer_prepared(parsed, query, settings, question=question, origin=origin)
    except FAILURES as exc:
        return error_answer(exc, proposal, question, origin)


def answer_prepared(parsed: Proposal, query: ValidatedQuery | None, settings: Settings,
                    question: str | None = None, origin: str = "model") -> Answer:
    """Answer an already validated proposal. May raise QueryFailed, QueryTimeout or QueryDenied."""
    base = {"origin": origin, "question": question}
    if parsed.action == "clarify":
        return Answer(kind="clarify", message=parsed.question, **base)
    if parsed.action == "explain":
        return Answer(kind="explain", message=parsed.explanation, detail={"source": parsed.source}, **base)
    if parsed.action == "unavailable":
        return Answer(kind="unavailable", message=parsed.reason,
                      detail={"kind": parsed.kind, "alternative": parsed.alternative}, **base)
    if parsed.action == "out_of_domain":
        return Answer(kind="out_of_domain", message=parsed.reason, **base)
    return _answer_query(parsed, query, settings, base)


# --- query answers ------------------------------------------------------------

def _answer_query(parsed: Proposal, query: ValidatedQuery, settings: Settings, base: dict) -> Answer:
    report = load_report(settings)
    view = query.view
    where = f" WHERE ({query.where_sql})" if query.where_sql else ""

    with Database(settings) as database:
        # An account name is not an identity: stop if a name filter spans several account IDs.
        if "account_id" not in (query.where_sql or "").lower():
            for name in query.account_name_filters:
                ids = [row[0] for row in database.query(
                    f"SELECT DISTINCT account_id FROM {view.name} WHERE account_name = :name ORDER BY account_id",
                    {"name": name}).rows]
                if len(ids) > 1:
                    shown = ", ".join(ids[:20]) + (f" and {len(ids) - 20} more" if len(ids) > 20 else "")
                    return Answer(
                        kind="clarify",
                        message=f"The name {name!r} appears on {len(ids)} different account IDs ({shown}). "
                                "Which account ID do you mean? Names are not merged into one customer.",
                        detail={"account_name": name, "account_ids": ids}, interpretation=parsed.interpretation,
                        sql=query.sql, parameters=query.parameters, source_view=view.name, **base)

        result = database.query(query.sql, query.parameters, row_cap=settings.row_cap)
        population = _population(database, query, where)
        returned_ids, in_returned, attribution = _attribution(database, query, where, result)
        lineage = _account_invoices(database) if view.name == "account_facts" else {}

    columns = query.output_columns
    aggregated = dict(zip(columns, query.aggregated_outputs))
    units = {name: query.output_units.get(name) for name in columns}
    derived = {
        item.name: [compute_derived(item.kind, row[columns.index(item.numerator)], row[columns.index(item.denominator)])
                    for row in result.rows]
        for item in query.derived
    }
    display_columns = [header(name, units[name]) for name in columns] + [item.name for item in query.derived]
    display_rows = [
        [format_value(value, units[name], aggregated[name]) for name, value in zip(columns, row)]
        + [format_derived(item.kind, derived[item.name][index]) for item in query.derived]
        for index, row in enumerate(result.rows)
    ]

    count = len(population)
    noun = "invoice" if view.name == "invoice_facts" else "account snapshot"
    scope = "matching the query's filter" if query.where_sql else "(no filter)"
    exclusions = _exclusions(query, report, population)
    notes = _notes(query, report, population, exclusions, result.truncated, settings.row_cap)

    answer = Answer(
        kind="result", interpretation=parsed.interpretation, source_view=view.name, sql=query.sql,
        parameters=query.parameters, columns=columns, rows=result.rows, units=units, derived=derived,
        display_columns=display_columns, display_rows=display_rows, truncated=result.truncated,
        population_count=count, population_label=f"{count:,} eligible {noun}{'' if count == 1 else 's'} {scope}",
        no_data=count == 0, partial=bool(exclusions["invoice_ids"]), returned_ids=returned_ids,
        exclusions=exclusions, notes=notes,
        sample_refs=(returned_ids or [row["id"] for row in population])[:5],
        input_sha256=report["input"]["sha256"], rules_version=report["rules_version"], **base,
    )
    answer.evidence_path = _write_evidence(settings, answer, query, report, population, in_returned, attribution,
                                           lineage)
    return answer


def _population(database: Database, query: ValidatedQuery, where: str) -> list[dict]:
    """Every row of the view that the query's own WHERE clause selects, before grouping or limits."""
    if query.view.name == "invoice_facts":
        sql = f"SELECT invoice_id, account_id, invoice_date_ambiguous, currency FROM invoice_facts{where} ORDER BY 1"
        keys = ("id", "account_id", "date_ambiguous", "currency")
    else:
        sql = ("SELECT account_id, has_attribute_disagreement, snapshot_date_ambiguous, csat_score IS NULL, "
               f"selected_invoice_id FROM account_facts{where} ORDER BY 1")
        keys = ("id", "disagreement", "date_ambiguous", "csat_unknown", "selected_invoice_id")
    return [dict(zip(keys, row)) for row in database.query(sql, query.parameters).rows]


def _account_invoices(database: Database) -> dict[str, list[str]]:
    invoices: dict[str, list[str]] = {}
    for account_id, invoice_id in database.query("SELECT account_id, invoice_id FROM invoices ORDER BY 1, 2").rows:
        invoices.setdefault(account_id, []).append(invoice_id)
    return invoices


def _attribution(database: Database, query: ValidatedQuery, where: str, result):
    """Work out which evaluated records belong to the rows actually returned.

    Returns (returned IDs or None, set of IDs in returned rows/groups or None, description).
    """
    id_column = query.view.id_column
    lowered = [text.strip().lower() for text in query.select_sql]
    if not query.has_aggregate and not query.group_sql:
        if id_column in lowered and not query.is_distinct:
            ids = [row[lowered.index(id_column)] for row in result.rows]
            return ids, set(ids), "returned rows are identified by their ID"
        return None, None, ("the result does not include the record ID, so evidence covers the evaluated "
                            "population, not individual returned rows")
    if not query.group_sql:
        return None, None, "one aggregate over the whole evaluated population"
    if not (query.has_having or query.limit is not None or result.truncated):
        return None, None, "every group in the evaluated population is returned"

    # Map each GROUP BY expression to an output column so returned groups can be matched to records.
    names = [name.lower() for name in query.output_columns]
    expressions, indexes = [], []
    for text in query.group_sql:
        key = text.strip().lower()
        if query.view.column(key):
            index = lowered.index(key) if key in lowered else None
            expression = text
        elif key in names:
            index = names.index(key)
            expression = query.select_sql[index]
        else:
            index = lowered.index(key) if key in lowered else None
            expression = text
        if index is None:
            return None, None, ("a GROUP BY expression is not in the output, so evidence covers the evaluated "
                                "population, not individual returned groups")
        expressions.append(expression)
        indexes.append(index)
    try:
        rows = database.query(
            f"SELECT {id_column}, {', '.join(expressions)} FROM {query.view.name}{where}", query.parameters).rows
    except QueryFailed:
        return None, None, "group membership could not be derived, so evidence covers the evaluated population"
    returned_keys = {tuple(row[index] for index in indexes) for row in result.rows}
    members = {row[0] for row in rows if tuple(row[1:]) in returned_keys}
    return None, members, "records are marked when they belong to a returned group"


# --- exclusions ---------------------------------------------------------------

def _candidate_database(report: dict) -> sqlite3.Connection:
    """Disputed candidates in a private in-memory database with the same invoice_facts definition."""
    conn = sqlite3.connect(":memory:")
    conn.execute(schema.invoices_ddl(primary_key=False))
    conn.execute(schema.invoice_facts_ddl())
    insert = (f"INSERT INTO invoices ({', '.join(schema.INVOICE_COLUMNS)}) "
              f"VALUES ({', '.join(':' + c for c in schema.INVOICE_COLUMNS)})")
    for conflict in report["conflicts"]:
        for candidate in conflict["candidates"]:
            conn.execute(insert, {**candidate, "source_row": candidate["source_rows"][0],
                                  "quality_flags": ",".join(candidate["flags"])})
    conn.set_authorizer(authorize)
    return conn


def _exclusions(query: ValidatedQuery, report: dict, population: list[dict]) -> dict:
    """Which excluded records could have mattered to this query."""
    conflicts = report["conflicts"]
    total = len(conflicts)
    out = {"relevance": "none", "invoice_ids": [], "candidates": {}, "possible_additional_usd_cents": None,
           "accounts_without_snapshot": 0, "affected_account_ids": [], "lines": []}
    if not total:
        return out

    if query.view.name == "invoice_facts":
        # Re-run the query's own filter over the disputed candidates.
        where = f" WHERE ({query.where_sql})" if query.where_sql else ""
        conn = _candidate_database(report)
        try:
            rows = conn.execute(
                f"SELECT invoice_id, status, amount_usd_cents FROM invoice_facts{where} ORDER BY 1, 2",
                query.parameters).fetchall()
        except sqlite3.Error:
            out["relevance"] = "unknown"
            out["lines"].append(f"{total} invoice IDs are excluded because source systems disagree; whether they "
                                "match this filter could not be determined.")
            return out
        finally:
            conn.close()
        matched: dict[str, list[tuple]] = {}
        for invoice_id, status, usd in rows:
            matched.setdefault(invoice_id, []).append((status, usd))
        out["relevance"] = "evaluated"
        out["invoice_ids"] = sorted(matched)
        out["candidates"] = {key: [{"status": s, "amount_usd_cents": usd} for s, usd in value]
                             for key, value in matched.items()}
        if matched and all(len(value) == 1 for value in matched.values()):
            out["possible_additional_usd_cents"] = sum(value[0][1] for value in matched.values())
        if matched:
            line = (f"{len(matched)} of {total} disputed invoice IDs match this filter under at least one candidate "
                    f"status and are excluded: {', '.join(sorted(matched))}.")
            if out["possible_additional_usd_cents"] is not None:
                line += (" If their matching candidates were accepted they would add "
                         f"{format_value(out['possible_additional_usd_cents'], schema.USD_CENTS, True)} "
                         "of recorded amount.")
            out["lines"].append(line)
        else:
            out["lines"].append(f"none of the {total} disputed invoice IDs match this filter.")
        return out

    # Account view: a disputed invoice can remove an account or change its snapshot and totals.
    without = report["accounts_without_snapshot"]
    in_scope = {row["id"] for row in population}
    affected = sorted({a for conflict in conflicts for a in conflict["account_ids"]} & in_scope)
    out["relevance"] = "unknown"
    out["accounts_without_snapshot"] = len(without)
    out["affected_account_ids"] = affected
    if without:
        out["lines"].append(f"{len(without)} accounts have only disputed invoices and have no snapshot; whether they "
                            "would match this filter is unknown.")
    if affected:
        out["lines"].append(f"{len(affected)} evaluated account(s) also have a disputed invoice that is left out of "
                            f"their snapshot choice and lifetime totals: {', '.join(affected)}.")
    return out


# --- limitations --------------------------------------------------------------

def _notes(query: ValidatedQuery, report: dict, population: list[dict], exclusions: dict, truncated: bool,
           row_cap: int) -> list[Note]:
    """Limitations chosen by the view, the columns used and the filter, not by the wording of the question."""
    notes: list[Note] = []
    used = query.referenced_columns
    selected = " ".join(query.select_sql).lower()
    where = (query.where_sql or "").lower()
    profile, count = report["profile"], len(population)
    coverage = f"{profile['invoice_date_min']} to {profile['invoice_date_max']}"
    fx = report["fx_discrepancy"]
    fx_text = (
        "USD values follow the briefing (EUR x 1.08, GBP x 1.27). In this ledger "
        f"{fx['amount_equals_usd_list_price_times_rate']:,} of {fx['foreign_currency_non_refund_invoices']:,} "
        "foreign-currency non-refund invoices already equal the USD list price times the rate, so converted "
        "foreign-currency amounts may be overstated. This is unresolved with finance.")

    def add(code: str, text: str) -> None:
        notes.append(Note(code, text))

    if truncated:
        add("truncated", f"Only the first {row_cap} rows are shown; the query matched more. Totals are not affected "
                         "by this cap.")

    if query.view.name == "invoice_facts":
        if exclusions["invoice_ids"]:
            add("invoice_conflicts", "This is an eligible subtotal, not a complete ledger total: "
                f"{len(exclusions['invoice_ids'])} disputed invoice(s) matching this filter are left out "
                "(listed under Exclusions).")
        elif exclusions["relevance"] == "unknown":
            add("invoice_conflicts", "Disputed invoices may be missing from this result (see Exclusions).")
        if "invoice_date" not in where:
            add("all_dates", f"No invoice-date filter: this covers all available invoice dates, {coverage}, "
                             "including dates later than today. The last date is a coverage label, not a proven "
                             "export date.")
        ambiguous = sum(1 for row in population if row["date_ambiguous"])
        if ambiguous and "invoice_date" in used:
            add("date_ambiguity", f"{ambiguous:,} of the {count:,} evaluated invoices have a DD-MM-YYYY date that "
                                  "could also be read as MM-DD-YYYY. They are read as day-month, which decides the "
                                  "period they fall in.")
        foreign = sum(1 for row in population if row["currency"] != "USD")
        if foreign and used & _USD_COLUMNS:
            add("fx_discrepancy", f"{foreign:,} of the {count:,} evaluated invoices are in EUR or GBP. " + fx_text)
        if "net_revenue_cents" in used:
            add("revenue_definition", "Revenue here means paid invoices plus negative refunds, dated by invoice "
                                      "issue date. This is the ledger definition, not deferred-revenue accounting.")
        elif "paid_revenue_cents" in used:
            add("revenue_definition", "Paid invoices only, dated by invoice issue date; refunds are not subtracted.")
        filters_stuck = "status" in used and {"pending", "failed"} & {
            value for value in query.parameters.values() if isinstance(value, str)}
        if "exposure_cents" in used or filters_stuck:
            add("status_exposure", "Pending and failed are payment statuses. No due dates exist, so days overdue "
                                   "cannot be established.")
        time_columns = [name for name in _INVOICE_TIME_COLUMNS if name in used]
        if time_columns:
            add("invoice_time_attributes", f"{', '.join(time_columns)} are as recorded on each invoice. They can "
                                           "differ from the account's latest values in account_facts.")
        if used & {"csat_score", "support_tickets", "churned"}:
            add("repeated_account_values", "churned, csat_score and support_tickets are account-level values "
                                           "repeated on each invoice; use account_facts to count accounts.")
        return notes

    add("account_estimate", "Account attributes, MRR and churn come from each account's latest eligible invoice. "
                            "They are latest-invoice estimates, not verified current subscription state.")
    disagreeing = sum(1 for row in population if row["disagreement"])
    if disagreeing:
        add("account_identity", f"{disagreeing:,} of the {count:,} evaluated accounts have invoices that disagree on "
                                "name, region, industry, signup date or churn flag. Accounts are grouped by account "
                                "ID only.")
    ambiguous = sum(1 for row in population if row["date_ambiguous"])
    if ambiguous:
        add("date_ambiguity", f"For {ambiguous:,} of the {count:,} evaluated accounts an ambiguous DD-MM-YYYY "
                              "invoice date could change which invoice is the latest.")
    if exclusions["lines"]:
        add("accounts_excluded", "Disputed invoices are left out of account snapshots and lifetime totals "
                                 "(affected accounts are listed under Exclusions).")
    if "mrr_cents" in used:
        add("mrr_definition", "MRR is list price x seats x (1 - discount) for accounts not flagged churned, and 0 "
                              "otherwise. Annual invoices are neither divided by 12 nor counted twice.")
    if "churned" in used:
        if "churned" in selected:
            add("churn_proportion", "Churn here is the share of account snapshots flagged churned. It is an "
                                    "observed proportion, not a churn rate for a period: no churn dates exist.")
        else:
            add("activity_proxy", "Active means the latest invoice's churn flag is false. Payment status is not "
                                  "used to decide activity.")
    if used & {"csat_score", "low_csat_flag"}:
        unknown = sum(1 for row in population if row["csat_unknown"])
        if unknown:
            add("csat_unknown", f"CSAT is missing for {unknown:,} of the {count:,} evaluated accounts. Missing is "
                                "shown as Unknown and is not treated as satisfied or dissatisfied.")
    lifetime = sorted(name for name in used if name.startswith("lifetime_"))
    if lifetime:
        add("all_dates", f"{', '.join(lifetime)} cover all available invoice dates, {coverage}, not a selected "
                         "period.")
        if used & _USD_COLUMNS:
            add("fx_discrepancy", "Lifetime totals include EUR and GBP invoices. " + fx_text)
    if "lifetime_net_revenue_cents" in used:
        add("revenue_definition", "Revenue here means paid invoices plus negative refunds.")
    if used & {"churned", "csat_score", "low_csat_flag", "support_tickets"}:
        add("descriptive_only", "These figures describe the ledger. They cannot show what caused churn, "
                                "satisfaction or support volume.")
    return notes


# --- evidence -----------------------------------------------------------------

def _write_evidence(settings: Settings, answer: Answer, query: ValidatedQuery, report: dict, population: list[dict],
                    in_returned: set | None, attribution: str, account_invoices: dict) -> str:
    """Write every evaluated record with its raw-row lineage. Samples in the answer do not prove a total."""
    lineage = report["lineage"]
    records = []
    for row in population:
        if query.view.name == "invoice_facts":
            record = {"invoice_id": row["id"], "account_id": row["account_id"], "source_rows": lineage[row["id"]]}
        else:
            record = {
                "account_id": row["id"],
                "selected_invoice_id": row["selected_invoice_id"],
                "invoices": [{"invoice_id": invoice_id, "source_rows": lineage[invoice_id]}
                             for invoice_id in account_invoices.get(row["id"], [])],
            }
        if in_returned is not None:
            key = "in_returned_group" if query.group_sql else "in_returned_rows"
            record[key] = row["id"] in in_returned
        records.append(record)

    document = {
        "question": answer.question,
        "proposal_source": answer.origin,
        "interpretation": answer.interpretation,
        "source_view": query.view.name,
        "sql": query.sql,
        "parameters": query.parameters,
        "input": {"file": report["input"]["file"], "sha256": report["input"]["sha256"]},
        "rules_version": report["rules_version"],
        "population_definition": (
            f"rows of {query.view.name} selected by the query's own WHERE clause, before GROUP BY, HAVING, "
            "ORDER BY, LIMIT and the display row cap"),
        "population_count": len(records),
        "attribution": attribution,
        "source_rows_note": "source_rows are 1-based data rows of the input CSV (row 1 follows the header); "
                            "the first is the representative, the rest are collapsed duplicates",
        "exclusions": {key: value for key, value in answer.exclusions.items() if key != "lines"},
        "exclusion_notes": answer.exclusions.get("lines", []),
        "population": records,
    }
    digest = hashlib.sha256(json.dumps(
        [query.sql, query.parameters, report["input"]["sha256"], report["rules_version"]],
        sort_keys=True).encode("utf-8")).hexdigest()[:16]
    settings.evidence_dir.mkdir(parents=True, exist_ok=True)
    path = settings.evidence_dir / f"{query.view.name}-{digest}.json"
    path.write_text(json.dumps(document, indent=1) + "\n", encoding="utf-8")
    return str(path)
