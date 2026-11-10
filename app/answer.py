"""The answer object and its deterministic formatting (specs/solution.md section 6).

Numbers are formatted here from query results. The model never formats or
restates a figure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from app import schema

_CENT = Decimal("0.01")


@dataclass(frozen=True)
class Note:
    code: str
    text: str


@dataclass
class Answer:
    kind: str                                  # result, clarify, explain, unavailable, out_of_domain or error
    origin: str = ""                           # who produced the proposal: model, hand-written proposal, stub
    question: str | None = None
    message: str = ""                          # clarifying question, explanation, reason or error text
    error_code: str = ""
    detail: dict = field(default_factory=dict)
    interpretation: str = ""
    source_view: str = ""
    sql: str = ""
    parameters: dict = field(default_factory=dict)
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)            # raw query values
    units: dict = field(default_factory=dict)
    derived: dict = field(default_factory=dict)                # name -> list of Decimal or None, one per row
    display_columns: list[str] = field(default_factory=list)
    display_rows: list[list[str]] = field(default_factory=list)
    truncated: bool = False
    population_count: int | None = None
    population_label: str = ""
    no_data: bool = False
    partial: bool = False
    returned_ids: list[str] | None = None
    exclusions: dict = field(default_factory=dict)
    notes: list[Note] = field(default_factory=list)
    sample_refs: list[str] = field(default_factory=list)
    evidence_path: str | None = None
    input_sha256: str = ""
    rules_version: str = ""
    repaired: bool = False                     # the model needed its one repair attempt

    @property
    def warning_codes(self) -> set[str]:
        return {note.code for note in self.notes}

    def text(self) -> str:
        return render(self)


# --- values -------------------------------------------------------------------

def compute_derived(kind: str, numerator, denominator) -> Decimal | None:
    """Finish a ratio with decimal arithmetic. None means unavailable (missing value or zero denominator)."""
    if numerator is None or not denominator:
        return None
    ratio = Decimal(numerator) / Decimal(denominator)
    if kind == "usd_average":
        ratio /= 100          # cents per count -> USD per count
    elif kind == "percentage":
        ratio *= 100
    return ratio.quantize(_CENT, rounding=ROUND_HALF_UP)


def format_derived(kind: str, value: Decimal | None) -> str:
    if value is None:
        return "unavailable"
    if kind == "usd_average":
        return f"USD {value:,.2f}"
    if kind == "percentage":
        return f"{value:,.2f}%"
    return f"{value:,.2f}"


def format_value(value, unit: str | None, aggregated: bool) -> str:
    if value is None:
        return "no data" if aggregated else "Unknown"
    if unit == schema.USD_CENTS:
        return f"USD {Decimal(value) / 100:,.2f}"
    if unit == schema.LOCAL_CENTS:
        return f"{Decimal(value) / 100:,.2f}"
    if unit == schema.BASIS_POINTS:
        return f"{Decimal(value) / 100:,.2f}%"
    if unit == schema.FLAG and value in (0, 1):
        return "Yes" if value else "No"
    if unit == schema.COUNT:
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:,.2f}"
    return str(value)


def header(name: str, unit: str | None) -> str:
    if unit == schema.USD_CENTS:
        return (name[:-6] if name.endswith("_cents") else name) + " (USD)"
    if unit == schema.LOCAL_CENTS:
        return (name[:-6] if name.endswith("_cents") else name) + " (invoice currency)"
    if unit == schema.BASIS_POINTS:
        return (name[:-3] if name.endswith("_bp") else name) + " (%)"
    return name


# --- text ---------------------------------------------------------------------

def _table(columns: list[str], rows: list[list[str]]) -> list[str]:
    widths = [max(len(str(cell)) for cell in column) for column in zip(columns, *rows)] if columns else []

    def line(cells) -> str:
        return "  " + " | ".join(str(cell).ljust(width) for cell, width in zip(cells, widths)).rstrip()

    return [line(columns), "  " + "-+-".join("-" * width for width in widths), *[line(row) for row in rows]]


def render(answer: Answer) -> str:
    out: list[str] = []
    if answer.question:
        out.append(f"Question: {answer.question}")
    if answer.origin and answer.origin != "model":
        out.append(f"Proposal source: {answer.origin} (not a model response)")

    if answer.kind == "error":
        out.append(f"Error ({answer.error_code}): {answer.message}")
        if answer.detail.get("first_error"):
            out.append("The model's first proposal was also rejected, so its one repair attempt is used up. "
                       f"First rejection: {answer.detail['first_error']}")
        if answer.sql:
            out.append(f"Proposed SQL (not executed): {answer.sql}")
        elif answer.detail.get("model_reply"):
            out.append(f"Model reply (not used): {answer.detail['model_reply']}")
        out.append("No number is reported.")
        return "\n".join(out)
    if answer.repaired:
        out.append("Note: the model's first proposal was rejected and it used its one repair attempt.")
    if answer.kind == "clarify":
        out.append(f"Clarification needed: {answer.message}")
        return "\n".join(out)
    if answer.kind == "explain":
        out.append("Explanation (written by the model from the rules in its prompt, not computed from the data):")
        out.append(f"  {answer.message}")
        out.append(f"Source named by the model: {answer.detail.get('source', '')}")
        out.append("No query was run.")
        return "\n".join(out)
    if answer.kind == "unavailable":
        label = {"missing_data": "the data needed is not in the ledger",
                 "unsupported_operation": "the query operation is not supported in this version"}
        out.append(f"Cannot answer: {label.get(answer.detail.get('kind'), 'unavailable')}.")
        out.append(f"Reason: {answer.message}")
        if answer.detail.get("alternative"):
            out.append(f"A supported alternative (not the answer to your question): {answer.detail['alternative']}")
        out.append("No number is reported.")
        return "\n".join(out)
    if answer.kind == "out_of_domain":
        out.append("This tool answers questions about CloudNova invoices and accounts only.")
        out.append(f"Reason: {answer.message}")
        return "\n".join(out)

    out.append(f"Interpretation: {answer.interpretation}")
    view = schema.VIEWS[answer.source_view]
    out.append(f"Source: {view.name} ({view.grain})")
    out.append("")
    title = "Result"
    if answer.partial:
        title += " (eligible subtotal: disputed invoices are excluded, see below)"
    if answer.no_data:
        title += " (no data: nothing matches this filter)"
    out.append(title + ":")
    out.extend(_table(answer.display_columns, answer.display_rows))
    if answer.truncated:
        out.append(f"  ... only the first {len(answer.rows)} rows are shown.")
    out.append("")
    out.append(f"Evaluated population: {answer.population_label}")
    for line in answer.exclusions.get("lines", []):
        out.append(f"Exclusions: {line}")
    if answer.notes:
        out.append("Limitations and assumptions:")
        out.extend(f"  - {note.text}" for note in answer.notes)
    out.append("")
    out.append(f"SQL: {answer.sql}")
    out.append(f"Parameters: {answer.parameters}")
    if answer.sample_refs:
        out.append(f"Record references (sample): {', '.join(answer.sample_refs)}")
    if answer.evidence_path:
        out.append(f"Evidence file (all evaluated records with raw-row lineage): {answer.evidence_path}")
    out.append(f"Data: input SHA-256 {answer.input_sha256[:12]}..., rules version {answer.rules_version}")
    return "\n".join(out)
