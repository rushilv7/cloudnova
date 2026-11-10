"""Eval runner.

    python -m evals            offline cases; live cases are reported as SKIP
    python -m evals --live     also runs cases that call the configured model
    python -m evals --list     list cases without running them
    python -m evals -k TEXT    run only cases whose id or group contains TEXT

Exit status is nonzero when any case reports FAIL. SKIP is not a pass.
"""

from __future__ import annotations

import argparse
import importlib
import sys
import traceback

from evals import harness

CASE_MODULES = [
    "evals.cases_normalize",
    "evals.cases_pipeline",
    "evals.cases_reference",
    "evals.cases_query",
    "evals.cases_guard",
    "evals.cases_model",
    "evals.cases_live",
]


def _load_cases() -> list[str]:
    problems = []
    for name in CASE_MODULES:
        try:
            importlib.import_module(name)
        except ModuleNotFoundError as exc:
            if exc.name == name:
                continue  # case module not written yet
            problems.append(f"{name}: {exc}")
        except Exception as exc:  # a broken case module must not hide the others
            problems.append(f"{name}: {type(exc).__name__}: {exc}")
    return problems


def _live_ready() -> tuple[bool, str]:
    try:
        from app.config import load_settings

        missing = load_settings().missing_model_settings()
    except Exception as exc:
        return False, f"cannot load model settings ({type(exc).__name__}: {exc})"
    if missing:
        return False, "missing credentials: " + ", ".join(missing)
    return True, ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals", description=__doc__.split("\n")[0])
    parser.add_argument("--live", action="store_true", help="run cases that call the configured model")
    parser.add_argument("--list", action="store_true", help="list cases and exit")
    parser.add_argument("-k", dest="keyword", default="", help="only cases whose id or group contains this text")
    parser.add_argument("-v", "--verbose", action="store_true", help="print tracebacks for failures")
    args = parser.parse_args(argv)

    problems = _load_cases()
    cases = [c for c in harness.REGISTRY if args.keyword in c.id or args.keyword in c.group]

    if args.list:
        for c in cases:
            print(f"{'live' if c.live else 'offline':7} [{c.group}] {c.id}: {c.description}")
        print(f"{len(cases)} cases")
        return 0

    live_ok, live_reason = (False, "live mode not requested (use --live)")
    if args.live:
        live_ok, live_reason = _live_ready()

    counts = {"PASS": 0, "FAIL": 0, "XFAIL": 0, "SKIP": 0}
    for c in cases:
        status, reason = "PASS", ""
        if c.live and not live_ok:
            status, reason = "SKIP", live_reason
        else:
            try:
                c.fn()
            except harness.XFail as exc:
                status, reason = "XFAIL", str(exc)
            except harness.Skip as exc:
                status, reason = "SKIP", str(exc)
            except AssertionError as exc:
                status, reason = "FAIL", str(exc) or "assertion failed"
                if args.verbose:
                    traceback.print_exc()
            except Exception as exc:
                status, reason = "FAIL", f"{type(exc).__name__}: {exc}"
                if args.verbose:
                    traceback.print_exc()
        counts[status] += 1
        print(f"{status:5} [{c.group}] {c.id}: {c.description}")
        if reason:
            print(f"        reason: {reason}")

    for problem in problems:
        counts["FAIL"] += 1
        print(f"FAIL  [runner] could not load case module {problem}")

    print()
    print(
        f"{len(cases)} cases: {counts['PASS']} passed, {counts['FAIL']} failed, "
        f"{counts['XFAIL']} expected failures, {counts['SKIP']} skipped"
    )
    if counts["SKIP"]:
        print("Skipped cases did not run and are not passes.")
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
