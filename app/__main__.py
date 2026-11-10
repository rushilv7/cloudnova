"""Command-line interface.

    python -m app --build              clean the CSV and write the database and quality report
    python -m app --ask "QUESTION"     answer one question through the configured model
    python -m app --demo               build, then run the six stakeholder questions through the model
    python -m app --proposal FILE      run a hand-written JSON proposal (no model; labelled as such)

Exit status: 0 when every question got an answer, a clarification or a stated
limitation; 1 when any question ended in an error; 2 for configuration or
build problems.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Mapping

from app import build as builder
from app import engine, llm
from app.config import ConfigError, Settings, load_settings

# The six questions from the customer briefing, verbatim. They are inputs to the same path as any
# other question: nothing in the application recognizes them.
DEMO_QUESTIONS = (
    "What was our total recognized revenue in USD for paid invoices in 2024?",
    "Which region has the highest average MRR per account?",
    "How much have we given back in refunds?",
    "What's the churn rate among Enterprise accounts vs Starter?",
    "List the top 5 accounts by revenue, and flag any with CSAT ≤ 2.",
    "How many invoices are stuck in pending/failed and what's the exposed $?",
)
_RULE = "=" * 100


def build_summary(settings: Settings, report: dict) -> str:
    c = report["counts"]
    return "\n".join([
        f"Built {settings.db_path} from {settings.csv_path} (SHA-256 {report['input']['sha256'][:12]}...)",
        f"  {c['input_rows']:,} input rows = {c['duplicate_rows_removed']:,} duplicate rows removed "
        f"+ {c['rejected_invalid_rows']:,} invalid rows rejected "
        f"+ {c['held_conflict_rows']:,} rows held as disputed ({c['held_conflict_invoice_ids']:,} invoice IDs) "
        f"+ {c['eligible_invoices']:,} eligible invoices",
        f"  {c['accounts']:,} account snapshots; {c['accounts_without_snapshot']:,} accounts have only disputed "
        "invoices and no snapshot",
        f"  Quality report with lineage and exclusions: {settings.report_path}",
    ])


def _ensure_built(settings: Settings) -> None:
    if not builder.is_current(settings):
        print(build_summary(settings, builder.build(settings)))
        print()


def _ask_and_print(question: str, settings: Settings, transport, reply: str | None, interactive: bool) -> bool:
    """Answer one question, handling at most one clarification reply. Returns False on an error answer."""
    if reply:
        # The clarification was given up front, so it goes to the model together with the question.
        answer = llm.ask(question, settings, transport, clarification=(None, reply))
        print(f"Clarification given: {reply}")
        print(answer.text())
        return answer.kind != "error"

    answer = llm.ask(question, settings, transport)
    print(answer.text())
    if answer.kind != "clarify":
        return answer.kind != "error"
    if not interactive:
        print('To answer, run again with: --ask "<the question>" --reply "<your answer>"')
        return True
    try:
        reply = input("Your reply (Enter to skip): ").strip()
    except EOFError:
        reply = ""
    if not reply:
        return True
    print()
    answer = llm.ask(question, settings, transport, clarification=(llm.clarification_for_model(answer), reply))
    print(answer.text())
    return answer.kind != "error"


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app", description="Clean the CloudNova ledger and answer "
                                     "questions about it.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--build", action="store_true", help="clean the CSV and write the database and quality report")
    mode.add_argument("--ask", metavar="QUESTION", help="answer one question through the configured model")
    mode.add_argument("--demo", action="store_true", help="build, then run the six stakeholder questions")
    mode.add_argument("--proposal", metavar="FILE", help="run a hand-written JSON proposal without a model")
    parser.add_argument("--reply", metavar="TEXT", help="with --ask: your answer if the tool asks for clarification")
    args = parser.parse_args(argv)

    try:
        settings = load_settings(env)
        if args.build:
            print(build_summary(settings, builder.build(settings)))
            return 0

        if args.proposal:
            _ensure_built(settings)
            try:
                text = Path(args.proposal).read_text(encoding="utf-8")
            except OSError as exc:
                raise ConfigError(f"cannot read proposal file {args.proposal}: {exc}") from None
            answer = engine.run_proposal(text, settings, origin="hand-written proposal")
            print(answer.text())
            return 0 if answer.kind != "error" else 1

        # Check credentials before doing any work, so a missing key is the first thing reported.
        transport = llm.AzureTransport(settings)
        if args.ask:
            _ensure_built(settings)
            ok = _ask_and_print(args.ask, settings, transport, args.reply, sys.stdin.isatty())
            return 0 if ok else 1

        print(build_summary(settings, builder.build(settings)))
        print(f"Model deployment: {settings.azure_deployment}. Each answer below comes from a live model call.")
        failed = 0
        for number, question in enumerate(DEMO_QUESTIONS, start=1):
            print(f"\n{_RULE}\n[{number}/{len(DEMO_QUESTIONS)}]")
            if not _ask_and_print(question, settings, transport, None, False):
                failed += 1
        print(f"\n{_RULE}\n{len(DEMO_QUESTIONS)} questions asked; {failed} ended in an error.")
        return 1 if failed else 0
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        print("No model was called and no answer is shown. `python -m app --build` and `python -m evals` "
              "work without credentials.", file=sys.stderr)
        return 2
    except builder.BuildError as exc:
        print(f"Build error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
