# CloudNova CLI: development plan

Drafted 10 November 2026 against spec v0.2 (`CloudNova_Solution_Spec.md`, to become `specs/solution.md`). Nothing is built yet. This file is the shared tracker: I update the status table and notes at every checkpoint.

## How we work

- We go one checkpoint at a time. Each checkpoint ends with a gate (a command and the result it must show) and one or more small commits.
- Commits for a checkpoint carry its prefix, so `git log --grep '^cp3'` lists checkpoint 3. The gate result is recorded in the notes below.
- On 10 November you asked me to run all checkpoints without stopping in between. The gates still apply: a checkpoint is marked done only when its gate is met.
- If the code disagrees with a reference value in the spec, I stop and show the difference with the records involved. I do not change an expected value to make a case pass.
- I commit locally only. I do not create a remote or push.

## Status

| # | Checkpoint | Gate, in short | Status |
| --- | --- | --- | --- |
| 0 | Repository and spec | Spec is the first commit; CSV hash unchanged | Done |
| 1 | Eval fixtures and runner skeleton | Runner lists every case; all fail or skip | Not started |
| 2 | Field normalization | Parsing, FX and date fixtures pass | Not started |
| 3 | Conflict policy, tables, quality report | 5,125 = 109 + 2 + 28 + 4,986 | Not started |
| 4 | Views and column dictionary | Six reference results match to the cent | Not started |
| 5 | Query guard and read-only execution | Query-boundary cases pass; database unchanged | Not started |
| 6 | Answers, warnings and evidence | Baseline answers carry the right caveats and IDs | Not started |
| 7 | Model adapter and CLI | Clear errors without a key; live cases still skip | Not started |
| 8 | Live evaluation (needs your Azure details) | Baseline, unseen, ambiguous and missing-data cases run | Not started |
| 9 | README, diagram, prompt log | Setup works from a fresh clone | Not started |

## Checkpoint notes

Gate results and deviations from this plan, recorded as each checkpoint closes.

- **Checkpoint 0.** The spec (`specs/solution.md`) is the first commit. The CSV moved to `data/cloudnova_invoices.csv` with SHA-256 `f542bc1f...9c89d84` unchanged. The repository already existed as an empty `main` branch when I started, so I did not run `git init`. Installed versions are `openai` 3.49.0 and `sqlglot` 30.31.0 on Python 3.11.13 with SQLite 3.50.2.

## What changed from the first plan

The first plan followed spec v0.1: the model picked one of eight fixed metrics and the application filled in a SQL template. Spec v0.2 removes that. The six stakeholder questions are now baseline evaluations, and the product must answer new combinations of fields, filters, periods, groupings and rankings.

The model now proposes SQL over two documented views. The application decides whether that SQL may run.

## Design in brief

**Build path**

1. Read the raw CSV, normalize each field and record quality flags.
2. Collapse duplicates and apply the four-step conflict policy.
3. Write the `invoices` and `accounts` tables, then the `invoice_facts` and `account_facts` views.
4. Write the quality report with lineage, exclusions and assumptions.

**Question path**

1. The model receives the view schemas, the column dictionary, the business definitions, the date coverage and a data-quality summary. It never receives the CSV or email values.
2. It returns one typed proposal: `query`, `clarify`, `explain`, `unavailable` or `out_of_domain`.
3. A `query` proposal is parsed with SQLGlot and checked against the approved views, columns, functions and grammar.
4. Approved SQL runs on a read-only connection with an authorizer, a deadline and a detail-row cap.
5. The application formats numbers, attaches warnings chosen by the views, columns and period used, and writes the evidence export.

**Modules**

| File | Responsibility |
| --- | --- |
| `app/config.py` | Paths, prices, FX rates, aliases, approved functions, row cap, timeout, provider settings |
| `app/normalize.py` | Parsers for amounts, dates, categories, booleans and optional fields |
| `app/resolve.py` | Duplicate collapse and the conflict policy |
| `app/build.py` | Tables, views and the quality report |
| `app/schema.py` | View definitions and the column dictionary, shared by the prompt and the validator |
| `app/proposal.py` | Shape checks for the typed proposal |
| `app/sqlguard.py` | SQLGlot syntax-tree validation |
| `app/execute.py` | Read-only connection, authorizer, deadline, row cap |
| `app/answer.py` | Formatting, ratios, warnings, exclusions, evidence |
| `app/llm.py` | Prompt assembly, Azure adapter, the single repair attempt |
| `app/__main__.py` | `--demo` and `--ask` |
| `evals/` | Fixtures, cases and the runner |

**Environment:** Python 3.11 in a `.venv` created with `uv`. Dependencies are the `openai` SDK (pointed at your Azure endpoint) and `sqlglot`, pinned to the versions actually tested. Everything else is standard library.

**Layout:** the spec moves to `specs/solution.md`, the CSV to `data/cloudnova_invoices.csv` and the briefing to `data/BUSINESS_CONTEXT.md`, with bytes unchanged. The take-home brief stays uncommitted. Generated files go to `out/`, which is gitignored.

## Spec points I had to interpret

These are the places where spec v0.2 leaves the mechanism open. Each is my proposal; say if you want a different approach.

1. **Ratios.** The spec wants monetary averages and percentages computed from integer sums and counts with decimal arithmetic, but the proposal format has no field for that. I add an optional `derived` list to a query proposal, naming a numerator column, a denominator column and the kind (USD average or percentage). The application does the division. The validator rejects `AVG` over a cents column and division of aggregates in SQL.
2. **Account names that map to several IDs.** The model cannot detect this because it sees no data values. The application checks any equality filter on `account_name` before running the query. If the name matches more than one account ID, it returns a clarification listing the IDs.
3. **Whether excluded invoices affect an answer.** The unresolved candidates go into a table the model cannot reach. For an `invoice_facts` query, the application reruns the query's own filter over those candidates and reports the disputed invoice IDs that match. For an `account_facts` query, it reports the accounts with no snapshot as relevance unknown.
4. **Expected values for unseen questions.** Data logic is checked offline against a small fixture ledger whose grouped totals I calculate by hand, with the arithmetic written out for you to check. Live cases on the full file compare the model's result with hand-written reference SQL over the same views, so they test interpretation and not the cleaning rules again.
5. **Held-out questions.** The spec asks for these to be chosen after implementation. I will ask you for at least three at checkpoint 8, so the prompt is not tuned to questions I wrote myself.
6. **Clarification in the CLI.** `--ask` prints the clarifying question. In a terminal it waits for one reply and reruns with the original question plus your answer.

## Checkpoints

### 0. Repository and spec

- Run `git init`. Move the spec, CSV and briefing into `specs/` and `data/`. Add `.gitignore`.
- Commit the spec alone as the first commit, then the data and briefing, then the project setup.
- Project setup is `AGENTS.md` with the rules from spec section 9, this plan, `.env.example`, a placeholder `.env` that is gitignored, `requirements.txt` and the venv.
- The first spec commit will be v0.2, because git history starts now. The move from v0.1's fixed templates is recorded in `docs/prompts.md`, not as an invented earlier commit.
- **Gate:** `git log` shows the spec first, and the CSV SHA-256 is still `f542bc1f...9c89d84`.

### 1. Eval fixtures and runner skeleton

- Write a small fixture ledger covering every case in spec section 8 that has no model in it: paid versus net, FX and parsing, annual MRR and account grain, dates, duplicates, churn and missing CSAT.
- Add fixture rows that support an unseen breakdown, an account analysis and a record lookup.
- Record the full-file reference results from spec section 5 as regression cases.
- Add the runner with PASS, FAIL, XFAIL and SKIP and a nonzero exit on unexpected failures.
- **Gate:** `python -m evals` lists every case. Data cases fail because nothing is implemented, and live cases skip.

### 2. Field normalization

- Implement `app/config.py` and `app/normalize.py` to spec section 3.
- **Gate:** the parsing, FX and date cases pass, including `05-01-2024` as 5 January with an ambiguity flag and both year boundaries.

### 3. Conflict policy, tables and quality report

- Implement `app/resolve.py` and the table half of `app/build.py`.
- **Gate:** the duplicate fixture passes. On the full file the build reports 109 duplicate rows removed, 2 rejected, 28 held across 14 invoice IDs, 4,986 eligible invoices and 3,803 accounts. Two builds from the same input give identical business outputs.

### 4. Views and column dictionary

- Implement `invoice_facts`, `account_facts` and the column dictionary in `app/schema.py`.
- **Gate:** the paid-versus-net, MRR and churn fixtures pass. Hand-written SQL over the views reproduces all six reference results and the top-five table.

### 5. Query guard and read-only execution

- Implement `app/proposal.py`, `app/sqlguard.py` and `app/execute.py`.
- **Gate:** the query-boundary cases pass. Hidden writes, multiple statements, joins, subqueries, unapproved views or functions and injection through parameters are all rejected. The deadline and row cap work, an aggregate is not truncated by the cap, and the database file hash is unchanged afterwards.

### 6. Answers, warnings and evidence

- Implement `app/answer.py`: deterministic formatting, derived ratios, warnings, exclusion relevance and the evidence export.
- **Gate:** the six baseline answers, run from hand-written proposals, show the expected caveats. The 2024 paid answer is labelled an eligible subtotal, names the three disputed invoice IDs and exports 888 contributing invoices. The unseen-shape fixture cases pass.
- These runs use proposals I write by hand and are labelled that way. They are not model responses.

### 7. Model adapter and CLI

- Implement `app/llm.py` and `app/__main__.py`: the prompt, the Azure adapter, the five actions, the account-name check, the clarification reply and one repair attempt.
- **Gate:** without credentials, `--ask` and `--demo` exit with a clear configuration error. Malformed model output and provider failure are exercised through a stub transport, labelled as a stub. Live cases still skip.
- The model path is untested against a real model until checkpoint 8.

### 8. Live evaluation

- Needs your Azure endpoint, key, deployment name and API version in `.env`, and at least three held-out questions from you.
- Run `python -m evals --live` and `python -m app --demo`.
- Cases: the six baselines with one paraphrase each, three or more unseen answerable questions, "best customers", gross margin, churn causes, weather, and the monthly-churn question as the documented XFAIL.
- **Gate:** every case reports a status with a reason. I fix what is a defect and record the failures that remain. I do not add a handler for any individual question.

### 9. README, diagram and prompt log

- Write `README.md`, `docs/architecture.md` matching the code as built, and `docs/prompts.md` with the real prompts and corrections from this build.
- The spec was drafted in a conversation I cannot see, so `docs/prompts.md` gets a marked section for you to add the FX-direction and duplicate-precedence decisions.
- **Gate:** the setup and demo commands in the README work in a fresh clone.

## What I need from you

- **Now:** approval of this plan, or changes to the interpretations above.
- **Before checkpoint 8:** the Azure details in `.env`. I am assuming an Azure OpenAI-style chat deployment. If your Foundry deployment is a different model family, the client setup in `app/llm.py` changes and I will adjust when I see the endpoint.
- **At checkpoint 8:** three or more held-out questions.
