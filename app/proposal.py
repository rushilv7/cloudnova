"""The typed proposal a model (or a hand-written eval) hands to the application.

A proposal is data, not an instruction. `parse_proposal` checks its shape and
rejects unknown fields; `app.sqlguard` then checks any SQL it contains.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field

ACTIONS = ("query", "clarify", "explain", "unavailable", "out_of_domain")
DERIVED_KINDS = ("usd_average", "percentage", "ratio")
UNAVAILABLE_KINDS = ("missing_data", "unsupported_operation")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# action -> (required string fields, optional fields)
_FIELDS = {
    "query": (("sql", "source_view", "interpretation"), ("parameters", "output_units", "derived")),
    "clarify": (("question",), ()),
    "explain": (("explanation", "source"), ()),
    "unavailable": (("reason", "kind"), ("alternative",)),
    "out_of_domain": (("reason",), ()),
}


class ProposalError(ValueError):
    """The proposal is not in the agreed shape."""


@dataclass(frozen=True)
class Derived:
    name: str
    kind: str
    numerator: str
    denominator: str


@dataclass(frozen=True)
class Proposal:
    action: str
    sql: str = ""
    source_view: str = ""
    interpretation: str = ""
    parameters: dict = field(default_factory=dict)
    output_units: dict = field(default_factory=dict)
    derived: tuple[Derived, ...] = ()
    question: str = ""
    explanation: str = ""
    source: str = ""
    reason: str = ""
    kind: str = ""
    alternative: str = ""


def _text(obj: dict, name: str) -> str:
    value = obj.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ProposalError(f"'{name}' must be a non-empty string")
    return value.strip()


def _parameters(value) -> dict:
    if not isinstance(value, dict):
        raise ProposalError("'parameters' must be an object of name -> value")
    for name, item in value.items():
        if not isinstance(name, str) or not _IDENTIFIER.fullmatch(name):
            raise ProposalError(f"parameter name {name!r} is not a plain identifier")
        if isinstance(item, bool) or not isinstance(item, (str, int, float)):
            raise ProposalError(f"parameter {name!r} must be a string or a number")
        if isinstance(item, float) and not math.isfinite(item):
            raise ProposalError(f"parameter {name!r} must be a finite number")
    return dict(value)


def _derived(value) -> tuple[Derived, ...]:
    if not isinstance(value, list):
        raise ProposalError("'derived' must be a list")
    items = []
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {"name", "kind", "numerator", "denominator"}:
            raise ProposalError("each derived entry needs exactly: name, kind, numerator, denominator")
        item = Derived(*(_text(entry, key) for key in ("name", "kind", "numerator", "denominator")))
        if item.kind not in DERIVED_KINDS:
            raise ProposalError(f"derived kind {item.kind!r} is not one of {', '.join(DERIVED_KINDS)}")
        if not _IDENTIFIER.fullmatch(item.name):
            raise ProposalError(f"derived name {item.name!r} is not a plain identifier")
        items.append(item)
    return tuple(items)


def parse_proposal(obj) -> Proposal:
    """Validate the shape of a proposal given as a dict or a JSON string."""
    if isinstance(obj, str):
        try:
            obj = json.loads(obj)
        except json.JSONDecodeError as exc:
            raise ProposalError(f"proposal is not valid JSON: {exc.msg}") from None
    if not isinstance(obj, dict):
        raise ProposalError("proposal must be a JSON object")

    action = obj.get("action")
    if action not in ACTIONS:
        raise ProposalError(f"'action' must be one of {', '.join(ACTIONS)}; got {action!r}")
    required, optional = _FIELDS[action]
    unknown = sorted(set(obj) - {"action", *required, *optional})
    if unknown:
        raise ProposalError(f"unknown field(s) for action '{action}': {', '.join(unknown)}")

    values = {name: _text(obj, name) for name in required}
    if action == "query":
        values["parameters"] = _parameters(obj.get("parameters", {}))
        units = obj.get("output_units", {})
        if not isinstance(units, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in units.items()):
            raise ProposalError("'output_units' must be an object of column name -> unit text")
        values["output_units"] = dict(units)
        values["derived"] = _derived(obj.get("derived", []))
    if action == "unavailable":
        if values["kind"] not in UNAVAILABLE_KINDS:
            raise ProposalError(f"'kind' must be one of {', '.join(UNAVAILABLE_KINDS)}")
        if "alternative" in obj:
            if not isinstance(obj["alternative"], str):
                raise ProposalError("'alternative' must be a string")
            values["alternative"] = obj["alternative"].strip()
    return Proposal(action=action, **values)
