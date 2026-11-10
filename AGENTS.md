# Instructions for AI coding agents

This repository is a local Python CLI that cleans the CloudNova invoice ledger and answers natural-language questions about it. The governing document is `specs/solution.md`.

## Before changing anything

- Read `specs/solution.md` in full. If the code and the spec disagree, stop and report it; do not pick one silently.
- Read `docs/development-plan.md` for the current checkpoint and its gate.

## Rules

- **Raw input is read-only.** Never edit `data/cloudnova_invoices.csv` or `data/BUSINESS_CONTEXT.md`. Cleaning writes new outputs under `out/` only.
- **Metric logic lives in code and SQL views.** Revenue eligibility, FX conversion, MRR and the account denominator are defined in `app/` and the views. The model must not redefine them in a prompt or in generated SQL.
- **Unseen questions are answered through the schema.** Do not add a switch over the six stakeholder questions, a handler per question, or a stored answer. A question being absent from the sample list is never a reason to reject it.
- **Report limitations accurately.** Distinguish missing data from an unsupported query operation. Return a clarification, an explanation or an unavailable response instead of a guessed number.
- **Never change an expected value to make a case pass.** Expected values live in `evals/expected.py` and come from the spec or from hand calculation. If the implementation disagrees, report the difference with the records involved.
- **Never present a stored or hand-written result as a model response.** Offline runs that use hand-written proposals or a stub transport are labelled as such.
- **Model output is never executed directly.** Every proposed query passes `app/sqlguard.py` and runs through the read-only connection in `app/execute.py`.
- **No secrets in git.** Credentials go in `.env`, which is ignored. Keep `.env.example` current.

## Working practice

- Small commits, one concern each, with the checkpoint prefix (`cp3: ...`).
- Run `python -m evals` before every commit that touches `app/` or `evals/`.
- Update the status table in `docs/development-plan.md` when a checkpoint's gate is met.
- Record real prompts and corrections in `docs/prompts.md`. Do not reconstruct history that did not happen.
- Use the standard library where it is sufficient. The only third-party packages are the model SDK and the SQL parser, pinned in `requirements.txt`.
