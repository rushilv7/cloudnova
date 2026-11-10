# CloudNova ledger CLI

Local Python CLI that cleans the CloudNova invoice export, builds two SQLite analytical views, and answers natural-language questions about revenue, accounts and exposure. An Azure AI Foundry model proposes SQL; the application validates it and runs it read-only. Metric definitions live in code and views, not in the prompt.

Governing document: [`specs/solution.md`](specs/solution.md). How it is wired: [`docs/architecture.md`](docs/architecture.md). Real prompts and corrections from this build: [`docs/prompts.md`](docs/prompts.md).

## Requirements

- Python 3.11+ with SQLite
- An Azure AI Foundry (or Azure OpenAI) chat deployment, for `--ask`, `--demo` and live evals

Offline cleaning and `python -m evals` need no credentials.

## Setup

```bash
git clone https://github.com/rushil-cog/cloudnova.git
cd cloudnova

python3.11 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env: endpoint, API key, deployment name, and API version when required
```

`.env` is gitignored. Leave the Azure fields blank to use `--build`, `--proposal`, `--show-prompt` and offline evals only.

Tested with `openai==3.49.0` and `sqlglot==30.31.0` on Python 3.11.13 / SQLite 3.50.2.

## Commands

```bash
# Clean the CSV; write out/cloudnova.db and out/quality_report.json
python -m app --build

# Answer one question through the configured model
python -m app --ask "What was our paid revenue in 2024?"

# Build, then run the six stakeholder questions through the model
python -m app --demo

# Offline checks (no model): fixtures, guard, reference totals
python -m evals

# Same plus live interpretation against the deployment
python -m evals --live
```

Useful without a model:

```bash
# Print the generated system prompt (builds the DB if needed)
python -m app --show-prompt

# Run a hand-written JSON proposal; labelled as not a model response
python -m app --proposal docs/example-proposal.json
```

If the tool asks for clarification and stdin is not a TTY, pass the reply:

```bash
python -m app --ask "Who are our best customers?" --reply "by paid revenue in 2024"
```

Exit status: `0` on answer / clarification / stated limitation; `1` on an error answer; `2` on configuration or build failure. Missing credentials print which settings are absent and exit `2` without calling the model.

## What you get back

For a query answer: interpreted scope, formatted result, executed SQL and parameters, evaluated population, disputed-invoice exclusions when they match the filter, caveats chosen from the views and columns used, sample invoice IDs, and an evidence JSON under `out/evidence/`.

Non-query actions return a clarification, an explanation, an unavailable reason (`missing_data` or `unsupported_operation`), or `out_of_domain`. The application never invents a number for those.

## Data and outputs

| Path | Role |
| --- | --- |
| `data/cloudnova_invoices.csv` | Raw export (read-only; never edit) |
| `data/BUSINESS_CONTEXT.md` | Customer briefing (read-only) |
| `out/cloudnova.db` | Eligible invoices, account snapshots, two views |
| `out/quality_report.json` | Lineage, exclusions, counts |
| `out/evidence/*.json` | Evaluated population with source-row references |

Generated files under `out/` are gitignored and rebuilt from the CSV.

## Limitations (honest)

- Fourteen disputed invoice IDs are held out of modeled totals; answers that touch them are labelled eligible subtotals.
- Account attributes and MRR use a latest-invoice snapshot estimate when history disagrees.
- Foreign-currency conversion follows the briefing; many recorded FX amounts already look like list-price × rate (see the quality report and answer caveats).
- Supported SQL is one `SELECT` over one approved view. Joins, CTEs, window functions and subqueries are refused as unsupported operations, not as missing data.
- Costs, payment dates and churn reasons are absent from the ledger; margin and causal churn questions are declined.

## Project layout

```
app/           cleaning, views, SQL guard, execution, prompt, CLI
evals/         fixtures, cases, offline and live runners
specs/         solution specification
docs/          architecture, development plan, prompt log
data/          raw CSV and briefing
```
