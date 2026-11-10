"""Shared plumbing for eval cases: registry, assertions and fixture builds.

Application modules are imported inside functions so that a missing or broken
module fails the individual case instead of crashing the runner.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


class XFail(Exception):
    """The case hit its documented, expected limitation."""


class Skip(Exception):
    """The case could not run (for example, no credentials)."""


@dataclass
class Case:
    id: str
    group: str
    description: str
    fn: Callable[[], None]
    live: bool = False


REGISTRY: list[Case] = []


def case(case_id: str, group: str, description: str, live: bool = False):
    def register(fn: Callable[[], None]) -> Callable[[], None]:
        REGISTRY.append(Case(case_id, group, description, fn, live))
        return fn

    return register


def eq(actual, expected, label: str) -> None:
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def check(condition, message: str) -> None:
    if not condition:
        raise AssertionError(message)


# --- builds -----------------------------------------------------------------

_TMP = Path(tempfile.mkdtemp(prefix="cloudnova-evals-"))
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
_BUILT: dict[str, object] = {}


def _settings(csv_path: Path, out_dir: Path, **overrides):
    from app.config import load_settings

    # env={} keeps evals independent of the developer's .env and shell.
    return load_settings(env={}, csv_path=csv_path, out_dir=out_dir, **overrides)


def _build(key: str, csv_path: Path):
    if key not in _BUILT:
        from app.build import build

        settings = _settings(csv_path, _TMP / key)
        build(settings)
        _BUILT[key] = settings
    return _BUILT[key]


def fixture(name: str, **overrides):
    """Build evals/fixtures/<name>.csv once and return its settings."""
    base = _build(f"fixture-{name}", FIXTURES / f"{name}.csv")
    return _settings(base.csv_path, base.out_dir, **overrides) if overrides else base


def full(**overrides):
    """Build the supplied CSV once and return its settings."""
    base = _build("full", ROOT / "data" / "cloudnova_invoices.csv")
    return _settings(base.csv_path, base.out_dir, **overrides) if overrides else base


def report(settings) -> dict:
    return json.loads(Path(settings.report_path).read_text(encoding="utf-8"))


def scalar(settings, sql: str, params: dict | tuple = ()):
    """Read one value straight from the built database (test inspection only)."""
    rows = table(settings, sql, params)
    return rows[0][0] if rows else None


def table(settings, sql: str, params: dict | tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True)
    try:
        return [tuple(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def file_sha256(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(settings, proposal: dict, question: str | None = None):
    """Run a hand-written proposal through validation, execution and answering."""
    from app.engine import run_proposal

    return run_proposal(proposal, settings, question=question, origin="hand-written proposal")


def query(sql: str, parameters: dict | None = None, view: str = "invoice_facts", **extra) -> dict:
    """Build a hand-written query proposal."""
    proposal = {
        "action": "query",
        "source_view": view,
        "interpretation": extra.pop("interpretation", "hand-written eval proposal"),
        "sql": sql,
        "parameters": parameters or {},
        "output_units": extra.pop("output_units", {}),
    }
    proposal.update(extra)
    return proposal
