# Prompt and steering log

This log covers the Cursor agent session that built this repository on 10 November 2026. It records the prompts as they were given, the corrections that changed the design, and the points where the agent's first attempt was wrong.

The spec was drafted in an earlier conversation that this session could not see. Section 6 is reserved for those decisions and is to be filled in by the author. Nothing in this file is reconstructed.

## 1. How the agent was configured

- **`AGENTS.md`** at the repository root holds the standing rules. It was written at checkpoint 0 from spec section 9: read the spec first, keep the raw input read-only, keep metric logic in code and SQL, no handler per question, never change an expected value to make a case pass, never present a hand-written or stubbed result as a model response, no secrets in git.
- **`specs/solution.md`** (v0.2) was the governing document and the first commit.
- **`docs/development-plan.md`** was the shared tracker. Each checkpoint has a gate, and the notes record the result and any deviation.

## 2. Prompts given to the agent

These are verbatim.

**Prompt 1: ask for a plan before any code**

> Please read CloudNova_Solution_Spec.md. We will be building a local python cli that cleans ledger records from cloudnova_invoices (1).csv, and be able to answer stakeholder questions using natural language. If there are things from the spec that are unclear, you may reference BUSINESS_CONTEXT (1).md amd Take-Home_ FDE (Data, ML & AI).md to understand further. Given the spec and the rest of the references, I want to see a plan put forward for the development of the solution. Once I review the plan, we can continue building it out

The agent asked two questions before planning: which model provider to use, and whether to initialise git. The answers:

> It will be Azure AI foundry, but seeing how theres no .env file yet you can go ahead and create it and I will fill in the key details after once ready to test

> Initialise git and commit in small steps as I build, spec first

**Prompt 2: correction to the plan**

> I would readjust the plan slightly since it assumes it must only answer the 6 stakeholder questions. I even readjusted the spec to make sure it is robust enough to answer questions that may be considered outside the current scope. Aside from that I would like a step by step development plan put forward in the folder so that we can follow the development together and continue in checkpoints and commits.

**Prompt 3: build**

> Implement the plan as specified, it is attached for your reference. Do NOT edit the plan file itself.
>
> To-do's from the plan have already been created. Do not create them again. Mark them as in_progress as you work, starting with the first one. Don't stop until you have completed all the to-dos.

**Prompt 4: finish docs and publish**

> Please complete checkpoint 9 and push the repo

Checkpoint 9 had already landed architecture, the prompt log, `--show-prompt` and the example proposal; this prompt closed the remaining gate item (`README.md`), marked the checkpoint done in the development plan, and pushed to `origin`.

## 3. Corrections from the author that changed the design

| Correction | What the agent had done | What changed |
| --- | --- | --- |
| The plan must not assume only six questions (prompt 2) | The first plan followed spec v0.1: the model chose one of eight fixed metrics and the application filled in a SQL template. | The fixed metrics were dropped. The model proposes SQL over two documented views, and the application validates it. The six questions became baseline evaluations. Unseen-question cases were added to the evals. |
| Put the plan in the repository, in checkpoints (prompt 2) | The first plan was a Cursor plan file outside the repository. | `docs/development-plan.md` with ten checkpoints, a gate for each, and a commit prefix per checkpoint. |
| Commit in small steps, spec first | Not applicable; this was set before any commit. | The spec is the first commit. The raw data and briefing are the second, unmodified. |

## 4. Where the agent's own first attempt was wrong

Most were caught by reading output or by probing a library or a second safety layer before relying on it. The earlier part of the session was compacted, so rows for checkpoints 2 to 5 come from its summary.

| Checkpoint | Problem | How it was caught | Fix |
| --- | --- | --- | --- |
| 2 | Integer fields (seats, support tickets) were checked with `str.isdigit()`, which accepts non-ASCII digits. | The session summary records the fix but not how it was noticed. | Explicit `[0-9]` patterns. |
| 3 | The first duplicate resolver assigned a disposition to every row and then relabelled some of them. | Reading the draft back; the logic was hard to follow. | Rewritten so the smallest row of a group takes the outcome and later rows are duplicates. |
| 4 | The spec's account disagreement counts (980, 976, 774) did not match the build (978, 974, 772). | Comparison with the spec's section 2 table. | No expected value was changed. The spec's numbers were counted before disputed invoices were excluded, and the difference is recorded in the plan notes. |
| 5 | The installed libraries were newer than assumed (`openai` 3.49, `sqlglot` 30.31). | Probing the real API before writing code against it. | Code uses the verified names, for example SQLGlot's `from_` argument key. |
| 6 | The plan said disputed candidates would go into a third table. | The spec allows two tables. | Candidates stay in the quality report and are loaded into a private in-memory database to test a query's filter. |
| 6 | Answers printed the exclusion sentence twice. | Reading the rendered answers after all cases had passed. | The caveat now points to the Exclusions line. |
| 7 | `IIF(...)` passed the SQL guard but was denied by the SQLite authorizer. | Running every function the prompt was about to offer through both layers. | The guard rejects it with advice to use `CASE`. |
| 7 | Money arithmetic with no inferable unit, such as `amount_usd_cents * seats`, was accepted and would be shown as a bare number of cents. | Same probe. | The guard rejects it. Money times a plain number keeps its unit. |
| 7 | The live checkers had never run, because there were no credentials. | Noticing that they could be broken without anyone knowing. | `live_checkers_discriminate` feeds each checker one correct and one wrong hand-written proposal. |

## 5. The prompt the model receives at run time

The prompt is generated by `system_prompt` in `app/llm.py`. Print it with:

```bash
python -m app --show-prompt
```

It is built from the code, so it cannot drift from what the application enforces:

- The two views, their columns, units and allowed values come from `app/schema.py`.
- Prices, FX rates and the low-CSAT threshold come from `app/config.py`.
- Row counts, date coverage and the data-quality summary come from the quality report.

Decisions made while writing it:

- **The model is told it never sees data and must not state figures.** Every number the user reads comes from an executed query.
- **Business definitions are stated as defaults**, such as revenue meaning paid plus negative refunds and average MRR being taken over active accounts. The model selects view columns; it does not rebuild the logic.
- **Filters belong in `WHERE`.** The application derives the evaluated population, the evidence file and the disputed-invoice check from the query's `WHERE` clause, so the prompt says a measure that is zero for other rows is not a substitute for the filter.
- **A relative period asks for clarification.** The ledger has no as-of date and runs to 2027-07-23, so "last year" has no anchor.
- **No eval question appears in the prompt**, and neither does any reference result. The seven worked examples are different questions. An offline case (`prompt_contract`) enforces this.
- **The prompt does list what the ledger lacks** (costs, payment dates, churn dates). The gross-margin and churn-cause live cases therefore test that the model follows the contract, not that it discovers the gap unaided.

When a proposal is rejected, the model gets one repair message:

> The application did not accept that proposal (*code*): *reason*
>
> Return one corrected JSON proposal for the same question. Keep every filter the user asked for. If the question cannot be expressed in the SQL this tool accepts, return "unavailable" with kind "unsupported_operation"; do not drop part of the question to make the query pass.

**First live run (checkpoint 8, 10 November 2026).** Credentials were filled in `.env` and the Foundry v1 endpoint plus deployment `cloudnova` were used. `python -m app --demo` and `python -m evals --live` both completed with **0 unexpected failures**. No prompt edit was required after the first real replies. Remaining open item: held-out questions (`HELD_OUT` still empty) still need to be supplied by the reviewer.

## 6. Decisions made while drafting the spec (to be completed by the author)

Spec section 9 asks for these steering decisions to be recorded. They were made before this session, in a conversation the agent could not see. The spec states the outcomes; the prompts and reasoning behind them belong here.

- **Replacing the fixed-template design (spec v0.1 to v0.2).** Outcome in the spec: "This draft replaces fixed question templates with general querying over documented data and business rules." *Author: add the prompt or reasoning that led to this.*
- **Keeping the briefing's FX direction.** Outcome in the spec: "Follow the briefing's conversion direction, retain actual recorded amounts, and report the discrepancy." *Author: add the prompt or reasoning.*
- **Rejecting an unsupported duplicate precedence.** Outcome in the spec: "Do not prefer paid, void, or CSV order as evidence of truth." *Author: add the prompt or reasoning.*
- **Time spent on profiling and the spec.** *Author: add the figure.* Git shows the build commits only, starting at 13:35 on 10 November 2026.
