"""Validate a proposed query before it can run (specs/solution.md section 6).

The whole statement is parsed with SQLGlot and every node of the syntax tree
is checked against an allowlist. The supported grammar is one SELECT over one
approved view. Joins, subqueries, CTEs, set operations, window functions,
writes and anything else not listed are rejected.

This is a validation layer. It does not prove the query means what the user
asked; the read-only connection in `app.execute` is the second layer.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date

import sqlglot
from sqlglot import exp
from sqlglot.dialects.dialect import Dialect
from sqlglot.errors import SqlglotError
from sqlglot.tokens import TokenType

from app import schema
from app.proposal import Derived

logging.getLogger("sqlglot").setLevel(logging.ERROR)
_SQLITE = Dialect.get_or_raise("sqlite")


class QueryRejected(Exception):
    """The proposed query is outside the supported, safe grammar."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class ValidatedQuery:
    sql: str
    parameters: dict
    view: schema.View
    output_columns: list[str]                     # names as they will appear in the result
    output_units: dict[str, str | None]           # inferred from the syntax tree, not from the model
    declared_units: dict[str, str]
    select_sql: list[str]                         # expression text of each output, without its alias
    aggregated_outputs: list[bool]
    referenced_columns: set[str]
    where_sql: str | None
    group_sql: list[str]
    has_aggregate: bool
    has_having: bool
    limit: int | None
    is_distinct: bool
    account_name_filters: list[str] = field(default_factory=list)
    derived: tuple[Derived, ...] = ()


def _classes(*names: str) -> tuple[type, ...]:
    return tuple(getattr(exp, name) for name in names if hasattr(exp, name))


# Every node in the tree must be exactly one of these types.
_ALLOWED = set(_classes(
    "Select", "From", "Table", "Identifier", "Column", "Alias", "Where", "Group", "Having", "Order", "Ordered",
    "Limit", "Distinct", "Literal", "Placeholder", "Null", "Boolean", "And", "Or", "Not", "Paren",
    "EQ", "NEQ", "GT", "GTE", "LT", "LTE", "In", "Between", "Is", "Like",
    "Case", "If", "Add", "Sub", "Mul", "Div", "Mod", "Neg", "DPipe",
    "Count", "Sum", "Avg", "Min", "Max", "Star",
    "Coalesce", "Nullif", "Round", "Abs", "Cast", "DataType", "Substring", "Upper", "Lower", "Length",
    "Least", "Greatest", "TimeToStr", "TsOrDsToTimestamp", "Anonymous",
))
# Functions SQLGlot has no dedicated node for.
_APPROVED_ANONYMOUS = {"date", "julianday", "strftime"}

# Constructs that get a specific explanation instead of "unsupported syntax".
_NAMED = (
    (_classes("Join"), "join", "joins are not supported; each query reads one view"),
    (_classes("With", "CTE"), "cte", "CTEs are not supported"),
    (_classes("Union", "Intersect", "Except"), "set_operation", "UNION, INTERSECT and EXCEPT are not supported"),
    (_classes("Window", "WindowSpec"), "window", "window functions are not supported"),
    (_classes("Subquery", "Exists"), "subquery", "subqueries are not supported"),
)
_ROOT_ARGS = {"expressions", "from", "from_", "where", "group", "having", "order", "limit", "distinct"}
_AGGREGATES = _classes("Count", "Sum", "Avg", "Min", "Max")
_COMPARISONS = _classes("EQ", "NEQ", "GT", "GTE", "LT", "LTE", "Like", "In", "Between")
_CONDITIONS = _COMPARISONS + _classes("Is")
_CLAUSES = {
    TokenType.SELECT: "select", TokenType.FROM: "from", TokenType.WHERE: "where", TokenType.GROUP_BY: "group",
    TokenType.HAVING: "having", TokenType.ORDER_BY: "order", TokenType.LIMIT: "limit",
}
_DATE_COLUMNS = {"invoice_date", "signup_date", "selected_invoice_date"}
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _reject(code: str, message: str):
    raise QueryRejected(code, message)


def _has_aggregate(node: exp.Expression) -> bool:
    return any(isinstance(n, _AGGREGATES) for n in node.walk())


def _key(node: exp.Expression) -> str:
    return node.sql(dialect="sqlite").lower()


# --- clause text --------------------------------------------------------------

def _segments(tokens) -> dict[str, list]:
    """Split the token stream into top-level clauses (parenthesis depth 0)."""
    depth, current, out = 0, None, {}
    for token in tokens:
        if token.token_type == TokenType.L_PAREN:
            depth += 1
        elif token.token_type == TokenType.R_PAREN:
            depth -= 1
        elif depth == 0 and token.token_type in _CLAUSES:
            current = _CLAUSES[token.token_type]
            out[current] = []
            continue
        if current:
            out[current].append(token)
    return out


def _split_commas(tokens) -> list[list]:
    depth, parts = 0, [[]]
    for token in tokens:
        if token.token_type == TokenType.L_PAREN:
            depth += 1
        elif token.token_type == TokenType.R_PAREN:
            depth -= 1
        elif depth == 0 and token.token_type == TokenType.COMMA:
            parts.append([])
            continue
        parts[-1].append(token)
    return parts


def _text(sql: str, tokens) -> str:
    return sql[tokens[0].start: tokens[-1].end + 1] if tokens else ""


def _strip_alias(tokens) -> list | None:
    """Tokens before the last top-level AS, or None when the alias is not written with AS."""
    depth = 0
    cut = None
    for index, token in enumerate(tokens):
        if token.token_type == TokenType.L_PAREN:
            depth += 1
        elif token.token_type == TokenType.R_PAREN:
            depth -= 1
        elif depth == 0 and token.token_type == TokenType.ALIAS:
            cut = index
    return tokens[:cut] if cut is not None else None


# --- units --------------------------------------------------------------------

def infer_unit(node: exp.Expression, view: schema.View) -> str | None:
    """Unit of an output expression, derived from the columns and functions it uses."""
    if isinstance(node, (exp.Alias, exp.Paren, exp.Neg, exp.Abs, exp.Round, exp.Min, exp.Max)):
        return infer_unit(node.this, view)
    if isinstance(node, exp.Column):
        column = view.column(node.name)
        return column.unit if column else None
    if isinstance(node, exp.Count):
        return schema.COUNT
    if isinstance(node, exp.Sum):
        inner = infer_unit(node.this, view)
        return schema.COUNT if inner == schema.FLAG else inner
    if isinstance(node, exp.Coalesce):
        return infer_unit(node.this, view)
    if isinstance(node, (exp.Add, exp.Sub)):
        left, right = infer_unit(node.this, view), infer_unit(node.expression, view)
        return left if left == right else None
    if isinstance(node, exp.Case):
        results = [branch.args.get("true") for branch in node.args.get("ifs", [])]
        if node.args.get("default") is not None:
            results.append(node.args["default"])
        measured = {infer_unit(r, view) for r in results if not isinstance(r, (exp.Literal, exp.Null))}
        if not measured:
            # CASE WHEN ... THEN 1 ELSE 0 END is an indicator; summing it gives a count.
            return schema.FLAG if all(isinstance(r, exp.Null) or r.is_int for r in results) else None
        return measured.pop() if len(measured) == 1 else None
    return None


def _declared_class(text: str) -> str | None:
    text = text.lower().replace("_", " ")
    if "basis" in text:
        return schema.BASIS_POINTS
    if "cent" in text:
        return schema.LOCAL_CENTS if "local" in text else schema.USD_CENTS
    return None


# --- validation ---------------------------------------------------------------

def validate_query(sql: str, parameters: dict, source_view: str, output_units: dict | None = None,
                   derived: tuple[Derived, ...] = ()) -> ValidatedQuery:
    """Return a ValidatedQuery or raise QueryRejected with a reason the model can act on."""
    view = schema.VIEWS.get(source_view)
    if view is None:
        _reject("unapproved_view", f"source_view must be one of {', '.join(schema.VIEWS)}")
    if not isinstance(sql, str) or not sql.strip():
        _reject("parse_error", "the query is empty")
    sql = sql.strip()
    if sql.endswith(";"):
        sql = sql[:-1].rstrip()

    try:
        tokens = _SQLITE.tokenize(sql)
        statements = [s for s in sqlglot.parse(sql, read="sqlite") if s is not None]
    except SqlglotError as exc:
        _reject("parse_error", f"the query could not be parsed: {str(exc).splitlines()[0]}")
    if any(token.token_type == TokenType.SEMICOLON for token in tokens) or len(statements) > 1:
        _reject("multiple_statements", "exactly one statement is allowed")
    if any(token.comments for token in tokens):
        _reject("comment", "comments are not allowed in the query")
    if not statements:
        _reject("parse_error", "the query is empty")
    root = statements[0]

    # Structure.
    for classes, code, message in _NAMED:
        if isinstance(root, classes) or root.find(*classes):
            _reject(code, message)
    if type(root) is not exp.Select:
        _reject("not_select", "only a single SELECT statement is allowed")
    if any(node is not root and isinstance(node, exp.Select) for node in root.walk()):
        _reject("subquery", "subqueries are not supported")
    extra = sorted(key for key, value in root.args.items() if value and key not in _ROOT_ARGS)
    if extra:
        _reject("unsupported_syntax", f"unsupported clause(s): {', '.join(extra)}")

    for node in root.walk():
        if type(node) not in _ALLOWED:
            if isinstance(node, exp.Func):
                _reject("unapproved_function", f"function {node.sql_name()} is not approved")
            _reject("unsupported_syntax", f"unsupported syntax: {type(node).__name__}")
        if isinstance(node, exp.Anonymous) and node.name.lower() not in _APPROVED_ANONYMOUS:
            _reject("unapproved_function", f"function {node.name} is not approved")
        if isinstance(node, exp.Star) and not isinstance(node.parent, exp.Count):
            _reject("unsupported_syntax", "select columns by name; * is only allowed inside COUNT(*)")

    # Source.
    tables = list(root.find_all(exp.Table))
    if len(tables) != 1:
        _reject("unapproved_view", f"the query must read from {view.name}")
    table = tables[0]
    if table.name.lower() != view.name or table.db or table.catalog:
        _reject("unapproved_view", f"the query must read from {view.name} (the declared source_view), unqualified")
    if table.alias:
        _reject("unsupported_syntax", "do not alias the view")

    # Outputs.
    names, expressions = [], []
    for item in root.expressions:
        if isinstance(item, exp.Alias):
            names.append(item.alias)
            expressions.append(item.this)
        elif isinstance(item, exp.Column):
            names.append(item.name)
            expressions.append(item)
        else:
            _reject("unsupported_syntax", "give every computed output a name with AS")
    if not names:
        _reject("parse_error", "the query selects nothing")
    if len({name.lower() for name in names}) != len(names):
        _reject("unsupported_syntax", "output column names must be unique")
    aliases = {item.alias.lower() for item in root.expressions if isinstance(item, exp.Alias)}

    # Columns.
    referenced: set[str] = set()
    for column in root.find_all(exp.Column):
        if column.table and column.table.lower() != view.name:
            _reject("unknown_column", f"unknown qualifier {column.table!r}")
        name = column.name.lower()
        if view.column(name):
            referenced.add(name)
        elif name in aliases and column.find_ancestor(exp.Group, exp.Order, exp.Having):
            continue
        else:
            _reject("unknown_column", f"{view.name} has no column {column.name!r}")
    for name, expression in zip(names, expressions):
        shadows = view.column(name) and not _has_aggregate(expression)
        if shadows and not (isinstance(expression, exp.Column) and expression.name.lower() == name.lower()):
            _reject("unsupported_syntax", f"alias {name!r} is also a column of {view.name}; choose another name")

    # Parameters.
    used = set()
    for placeholder in root.find_all(exp.Placeholder):
        if placeholder.this is None:
            _reject("parameter", "use named parameters such as :start, not ?")
        used.add(placeholder.name)
    if used != set(parameters):
        missing, unused = sorted(used - set(parameters)), sorted(set(parameters) - used)
        _reject("parameter", f"parameters do not match the query (missing: {missing}; unused: {unused})")

    # Filter values.
    for literal in root.find_all(exp.Literal):
        if literal.is_string and isinstance(literal.parent, _COMPARISONS):
            _reject("literal_filter", f"bind the value {literal.this!r} as a named parameter")
    name_filters = _check_comparisons(root, view, parameters)

    # Grouping.
    group = root.args.get("group")
    group_nodes = list(group.expressions) if group else []
    if any(isinstance(node, exp.Literal) for node in group_nodes):
        _reject("unsupported_syntax", "group by column or alias name, not by position")
    has_aggregate = any(_has_aggregate(e) for e in expressions) or root.args.get("having") is not None
    if has_aggregate or group_nodes:
        group_keys = {_key(node) for node in group_nodes}
        for name, expression in zip(names, expressions):
            if _has_aggregate(expression) or not expression.find(exp.Column):
                continue
            if name.lower() not in group_keys and _key(expression) not in group_keys:
                _reject("bare_column", f"output {name!r} is neither aggregated nor in GROUP BY")

    # Money and grain.
    _check_measures(root, view, expressions, group_nodes, names)

    limit = None
    if root.args.get("limit") is not None:
        node = root.args["limit"].expression
        if not (isinstance(node, exp.Literal) and node.is_int and int(node.this) >= 1):
            _reject("limit", "LIMIT must be a positive integer literal")
        limit = int(node.this)

    # Clause text, taken from the query as written.
    segments = _segments(tokens)
    select_tokens = [t for t in segments.get("select", []) if t.token_type != TokenType.DISTINCT]
    items = _split_commas(select_tokens)
    consistent = (
        len(items) == len(root.expressions)
        and (root.args.get("where") is not None) == ("where" in segments)
        and bool(group_nodes) == ("group" in segments)
    )
    if not consistent:
        _reject("unsupported_syntax", "the query's clauses could not be located")
    select_sql = []
    for item_tokens, item in zip(items, root.expressions):
        if isinstance(item, exp.Alias):
            item_tokens = _strip_alias(item_tokens)
            if item_tokens is None:
                _reject("unsupported_syntax", "write output names with AS")
        select_sql.append(_text(sql, item_tokens))
    group_sql = [_text(sql, part) for part in _split_commas(segments["group"])] if group_nodes else []

    # Units and derived values.
    units = {name: infer_unit(expression, view) for name, expression in zip(names, expressions)}
    declared = dict(output_units or {})
    for name, text in declared.items():
        if name not in units:
            _reject("output_units", f"output_units names {name!r}, which is not an output column")
        claimed = _declared_class(text)
        if units[name] is not None and claimed is not None and claimed != units[name]:
            _reject("output_units", f"{name!r} is {units[name]}, not {text!r}")
        if units[name] is not None and claimed is None and units[name] in schema.MONEY_UNITS:
            _reject("output_units", f"{name!r} is {units[name]}, not {text!r}")
    _check_derived(derived, names, units)

    return ValidatedQuery(
        sql=sql, parameters=dict(parameters), view=view, output_columns=names, output_units=units,
        declared_units=declared, select_sql=select_sql,
        aggregated_outputs=[_has_aggregate(e) for e in expressions], referenced_columns=referenced,
        where_sql=_text(sql, segments["where"]) if "where" in segments else None, group_sql=group_sql,
        has_aggregate=has_aggregate, has_having=root.args.get("having") is not None, limit=limit,
        is_distinct=root.args.get("distinct") is not None, account_name_filters=name_filters, derived=tuple(derived),
    )


def _comparison_pairs(root: exp.Expression):
    """Yield (operator node, column node, value node) for column-versus-value comparisons."""
    for node in root.find_all(*_COMPARISONS):
        if isinstance(node, exp.In):
            values = node.expressions
        elif isinstance(node, exp.Between):
            values = [node.args.get("low"), node.args.get("high")]
        else:
            values = [node.expression]
        left = node.this
        if isinstance(left, exp.Column):
            for value in values:
                yield node, left, value
        elif not isinstance(node, (exp.In, exp.Between)) and isinstance(node.expression, exp.Column):
            yield node, node.expression, left


def _check_comparisons(root: exp.Expression, view: schema.View, parameters: dict) -> list[str]:
    """Check values compared with columns: canonical categories, matching types, ISO dates."""
    name_filters: list[str] = []
    for operator, column_node, value_node in _comparison_pairs(root):
        column = view.column(column_node.name)
        if column is None:
            continue
        if isinstance(value_node, exp.Placeholder):
            value, label = parameters[value_node.name], f"parameter :{value_node.name}"
        elif isinstance(value_node, exp.Literal) and not value_node.is_string:
            value, label = (int(value_node.this) if value_node.is_int else float(value_node.this)), "literal"
        else:
            continue
        if isinstance(operator, exp.Like):
            if not isinstance(value, str):
                _reject("parameter_type", f"{label} must be text for LIKE")
            continue
        if column.values and isinstance(operator, (exp.EQ, exp.NEQ, exp.In)):
            if value not in column.values:
                _reject("enum_value", f"{label} = {value!r} is not a value of {column.name}; "
                                      f"use one of: {', '.join(column.values)}")
        elif column.type == "INTEGER":
            if isinstance(value, str):
                _reject("parameter_type", f"{label} must be a number to compare with {column.name}")
        else:
            if not isinstance(value, str):
                _reject("parameter_type", f"{label} must be text to compare with {column.name}")
            if column.name in _DATE_COLUMNS:
                try:
                    if not _ISO_DATE.fullmatch(value):
                        raise ValueError
                    date.fromisoformat(value)
                except ValueError:
                    _reject("parameter_type", f"{label} = {value!r} must be a date written YYYY-MM-DD")
        if column.name == "account_name" and isinstance(operator, (exp.EQ, exp.In)) and isinstance(value, str):
            name_filters.append(value)
    return name_filters


def _value_columns(aggregate: exp.Expression, view: schema.View):
    """Columns an aggregate adds up, ignoring columns that only appear in conditions."""
    for column in aggregate.find_all(exp.Column):
        parent, in_condition = column.parent, False
        while parent is not None and parent is not aggregate:
            if isinstance(parent, _CONDITIONS):
                in_condition = True
                break
            parent = parent.parent
        entry = view.column(column.name)
        if entry and not in_condition:
            yield entry


def _check_measures(root, view, expressions, group_nodes, names) -> None:
    """Mechanical checks that keep business arithmetic in the views and in application code."""
    for aggregate in root.find_all(exp.Avg):
        if any(column.unit in schema.MONEY_UNITS for column in _value_columns(aggregate, view)):
            _reject("ratio_in_sql", "do not AVG money in SQL; return SUM and COUNT and declare a 'usd_average' "
                                    "in derived")
    for expression in expressions:
        for division in expression.find_all(exp.Div):
            money = any((c := view.column(col.name)) and c.unit in schema.MONEY_UNITS
                        for col in division.find_all(exp.Column))
            if money or _has_aggregate(division):
                _reject("ratio_in_sql", "do not divide in output columns; return the numerator and denominator "
                                        "and declare the ratio in derived")
    if view.name == "invoice_facts":
        for aggregate in root.find_all(exp.Sum, exp.Avg):
            repeated = [column.name for column in _value_columns(aggregate, view) if column.per_account]
            if repeated:
                _reject("repeated_account_value", f"{', '.join(repeated)} is an account-level value repeated on "
                        "every invoice; aggregate it in account_facts instead")
    grouped = {node.name.lower() for node in group_nodes if isinstance(node, exp.Column)}
    for name, expression in zip(names, expressions):
        if name.lower() in grouped and isinstance(expression, exp.Column):
            grouped.add(expression.name.lower())
    where = root.args.get("where")
    filtered = where is not None and any(
        isinstance(node, exp.EQ) and isinstance(node.this, exp.Column) and node.this.name.lower() == "currency"
        for node in where.find_all(exp.EQ))
    for aggregate in root.find_all(exp.Sum, exp.Min, exp.Max):
        local = any(column.unit == schema.LOCAL_CENTS for column in _value_columns(aggregate, view))
        if local and "currency" not in grouped and not filtered:
            _reject("mixed_currency", "amount_local_cents mixes currencies; group by or filter on currency, or use "
                                      "amount_usd_cents")


def _check_derived(derived, names, units) -> None:
    seen = set(names)
    for item in derived:
        if item.name in seen:
            _reject("derived", f"derived name {item.name!r} is already used")
        seen.add(item.name)
        for part in (item.numerator, item.denominator):
            if part not in names:
                _reject("derived", f"derived {item.name!r} refers to {part!r}, which is not an output column")
        numerator, denominator = units[item.numerator], units[item.denominator]
        if item.kind == "usd_average":
            if numerator != schema.USD_CENTS or denominator != schema.COUNT:
                _reject("derived", f"usd_average {item.name!r} needs a USD-cents numerator and a count denominator")
        elif numerator is not None and denominator is not None and numerator != denominator:
            _reject("derived", f"{item.kind} {item.name!r} divides {numerator} by {denominator}; "
                               "both sides must have the same unit")
