"""Business rules and runtime settings.

Business rules (prices, FX, aliases) are constants here so that cleaning, the
views and the prompt all read one definition. Runtime settings (paths, limits,
provider) come from the environment or `.env`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Mapping

ROOT = Path(__file__).resolve().parent.parent

# Bump when any rule below, the conflict policy or a view definition changes.
# Reference results in evals/expected.py must then be rebaselined.
RULES_VERSION = "2026-11-10.1"

# --- business rules (specs/solution.md sections 3 and 4) ----------------------

PRICE_USD_CENTS = {"starter": 4_900, "pro": 9_900, "enterprise": 29_900}
FX_TO_USD = {"USD": Decimal("1"), "EUR": Decimal("1.08"), "GBP": Decimal("1.27")}
CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP"}
REGIONS = ("NA", "EMEA", "APAC", "LATAM")
LOW_CSAT_MAX = 2

PLAN_ALIASES = {
    "starter": "starter", "start": "starter", "tier 1": "starter",
    "pro": "pro", "professional": "pro", "tier 2": "pro",
    "enterprise": "enterprise", "ent": "enterprise", "tier 3": "enterprise",
}
STATUS_ALIASES = {
    "paid": "paid", "complete": "paid", "completed": "paid", "success": "paid",
    "pending": "pending", "in_review": "pending", "awaiting": "pending",
    "failed": "failed", "declined": "failed", "error": "failed",
    "refunded": "refunded", "refund": "refunded", "charged_back": "refunded",
    "void": "void", "cancelled": "void", "canceled": "void",
}
BILLING_CYCLE_ALIASES = {"monthly": "monthly", "annual": "annual", "annually": "annual", "yearly": "annual"}
CHURN_ALIASES = {"true": 1, "yes": 1, "1": 1, "y": 1, "false": 0, "no": 0, "0": 0, "n": 0}
PAYMENT_METHOD_ALIASES = {
    "credit card": "credit_card", "credit_card": "credit_card",
    "ach": "ACH",
    "wire": "wire", "wire transfer": "wire",
    "invoice": "invoice",
}

PLANS = ("starter", "pro", "enterprise")
STATUSES = ("paid", "pending", "failed", "refunded", "void")
BILLING_CYCLES = ("monthly", "annual")
CURRENCIES = tuple(FX_TO_USD)
PAYMENT_METHODS = ("credit_card", "ACH", "wire", "invoice")

# --- runtime settings ---------------------------------------------------------


class ConfigError(Exception):
    """A setting is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    csv_path: Path
    out_dir: Path
    row_cap: int = 200
    query_timeout_s: float = 5.0
    llm_timeout_s: float = 60.0
    azure_endpoint: str = ""
    azure_api_key: str = ""
    azure_deployment: str = ""
    azure_api_version: str = ""

    @property
    def db_path(self) -> Path:
        return self.out_dir / "cloudnova.db"

    @property
    def report_path(self) -> Path:
        return self.out_dir / "quality_report.json"

    @property
    def evidence_dir(self) -> Path:
        return self.out_dir / "evidence"

    @property
    def uses_v1_endpoint(self) -> bool:
        """True for endpoints of the form https://<resource>/openai/v1/, which take no API version."""
        return "/openai/v1" in self.azure_endpoint

    def missing_model_settings(self) -> list[str]:
        required = {
            "AZURE_OPENAI_ENDPOINT": self.azure_endpoint,
            "AZURE_OPENAI_API_KEY": self.azure_api_key,
            "AZURE_OPENAI_DEPLOYMENT": self.azure_deployment,
        }
        if not self.uses_v1_endpoint:
            required["AZURE_OPENAI_API_VERSION"] = self.azure_api_version
        return [name for name, value in required.items() if not value]


def read_dotenv(path: Path) -> dict[str, str]:
    """Read KEY=VALUE lines. Blank lines and # comments are ignored."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _resolve(path: str | Path) -> Path:
    path = Path(path)
    return path if path.is_absolute() else ROOT / path


def _number(env: Mapping[str, str], name: str, default, convert):
    raw = env.get(name, "")
    if raw == "":
        return default
    try:
        return convert(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from None


def load_settings(env: Mapping[str, str] | None = None, **overrides) -> Settings:
    """Build settings from `.env` and the process environment (the environment wins).

    Pass `env` to use only that mapping. Keyword overrides replace individual fields.
    """
    if env is None:
        env = {**read_dotenv(ROOT / ".env"), **os.environ}
    values = {
        "csv_path": _resolve(env.get("CLOUDNOVA_CSV") or "data/cloudnova_invoices.csv"),
        "out_dir": _resolve(env.get("CLOUDNOVA_OUT_DIR") or "out"),
        "row_cap": _number(env, "CLOUDNOVA_ROW_CAP", 200, int),
        "query_timeout_s": _number(env, "CLOUDNOVA_QUERY_TIMEOUT_S", 5.0, float),
        "llm_timeout_s": _number(env, "CLOUDNOVA_LLM_TIMEOUT_S", 60.0, float),
        "azure_endpoint": env.get("AZURE_OPENAI_ENDPOINT", "").strip(),
        "azure_api_key": env.get("AZURE_OPENAI_API_KEY", "").strip(),
        "azure_deployment": env.get("AZURE_OPENAI_DEPLOYMENT", "").strip(),
        "azure_api_version": env.get("AZURE_OPENAI_API_VERSION", "").strip(),
    }
    values.update(overrides)
    values["csv_path"] = _resolve(values["csv_path"])
    values["out_dir"] = _resolve(values["out_dir"])
    settings = Settings(**values)
    if settings.row_cap < 1:
        raise ConfigError("CLOUDNOVA_ROW_CAP must be at least 1")
    if settings.query_timeout_s <= 0 or settings.llm_timeout_s <= 0:
        raise ConfigError("timeouts must be greater than zero")
    return settings
