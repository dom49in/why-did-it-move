# Why did it move?

An agent that answers one question analysts get asked every Monday: **"Why did this number change?"**
Ask it *"Why did gross margin fall in September versus August?"* and it queries your data, drills down until it
finds the segment that actually moved, explains *how* it moved (price, volume or unit cost), and hands back an
answer in which every number traces to a query you can re-run.

Built for the Open Agent Hackathon, track **"The Agent That Can Explain Why"**.

```
"Gross margin fell by 56,693.30 (-8.38%), from 676,566.20 in 2026-08 to 619,872.90 in 2026-09.
 The change is concentrated in product=Rain Jacket (58%, lift 3.03x). ..."      <- output_examples/output_1.json
```

## Quick start (no credentials needed)

Open **http://localhost:8000/** after `python run.py` for the web UI (a single page that calls `POST /run`).


```bash
pip install -r requirements.txt           # uvicorn + requests, nothing else
SAMPLE_MODE=true python run.py --input input_examples/example_1.json --output /tmp/answer.json
```

Or as a service:

```bash
SAMPLE_MODE=true python run.py             # serves on :8000
curl -s localhost:8000/health
curl -s -X POST localhost:8000/run -H 'content-type: application/json' \
     -d '{"user_query":"Why did gross margin fall in September versus August?"}'
```

Docker:

```bash
docker build -t why-did-it-move .
docker run --rm -p 8000:8000 -e SAMPLE_MODE=true why-did-it-move                 # sample mode
docker run --rm -p 8000:8000 --env-file .env why-did-it-move                     # live mode (default)
# Inside a container, Zetaris/Nightshift on your machine are reached at host.docker.internal, not localhost
```

Live mode (real Zetaris and Nightshift, the mode used for the recorded runs in `output_examples/live/`):

```bash
cp .env.example .env     # fill in the Zetaris and Nightshift values; keep SAMPLE_MODE=false
# load the data into Zetaris first: see "Load the data into Zetaris" below
python run.py --input input_examples/example_1.json --output /tmp/answer.json     # response shows "data_source": "zetaris"
```

`SAMPLE_MODE=true` above is only for trying the project without credentials. Never use it for a live run.

Tests (standard library only): `python -m unittest discover -s tests -t .`

### What "sample mode" is, honestly

`SAMPLE_MODE=true` swaps **only the data source**: the same SQL that would go to Zetaris runs against
`data/sample/sales_facts.csv` (synthetic, seeded by `scripts/make_sample_data.py`). If, in addition, **no Nightshift
credentials are set**, reasoning comes from `app/offline.py`, a deterministic rule set standing in for the model so a
judge can run the project from a clean clone. Every such run says so: `data_source: sample_data`,
`models_used: ["offline-rules"]`, `reasoning_mode: offline_rules`. It is refused outside sample mode, and there is
no silent fallback: with `SAMPLE_MODE=false`, a Nightshift or Zetaris outage is a structured error.
The files in `output_examples/` are real sample-mode runs; the live runs are in `output_examples/live/`.

### Second data source: a public web API, queried through Zetaris

Besides the sales table, the agent checks an outside series: USD/CAD exchange rates from the public Frankfurter API
(no key). **The app never calls the web.** You register the API in Zetaris as a REST table
(the statements are in `scripts/zetaris_setup_all.sql`), expose it as a view, and set `ZETARIS_FX_TABLE=sales.fx_rates_table`. The agent
then queries it with Zetaris SQL through the same adapter as the sales table. The Executor compares the
two months' averages and says whether the outside series is flat, broad or concentrated relative to the internal
finding; the Evaluator re-runs that query and flags any mismatch (`external_recomputed`). It reports correlation
context only, never a cause. If `ZETARIS_FX_TABLE` is unset, the step is skipped and the response says so.
In sample mode the series comes from `data/sample/fx_rates.csv`, which is **synthetic**, not real exchange rates.
The REST registration was run in the author's own Zetaris Docker instance, and the live runs in
`output_examples/live/` queried it (see "Load the data into Zetaris" below).

### Live mode

Zetaris has two ways in, picked by config. If `ZETARIS_BASE_URL` is set (the Zetaris UI address, for example
`http://localhost:3000`), the app uses the route Zetaris's own `getting-started` scripts use,
`POST /api/proxy/sql-editor/sqls/run-query` with `{sql, limit}`, authenticated by `ZETARIS_API_KEY` (created in the Zetaris
GUI) or `ZETARIS_USERNAME` plus `ZETARIS_PASSWORD`. Otherwise it calls the REST API at `ZETARIS_REST_URL`. Both are
written from Zetaris's public docs and repo. `ZETARIS_ORG_ID` is the numeric
org id; Zetaris's docs say to read it from the `X-Org-ID` header in your browser's Network panel while signed in.

Set `SAMPLE_MODE=false` plus `ZETARIS_REST_URL` (or `ZETARIS_BASE_URL`), `ZETARIS_ORG_ID`, `ZETARIS_API_KEY`, `ZETARIS_TABLE`,
`METERLESS_BASE_URL`, `METERLESS_RUNTIME_TOKEN`. The table must expose the columns in `data/schema.json`
(`order_date, region, channel, product_category, product, revenue, cost, units`); edit that file to map your own.
The default mode (`SAMPLE_MODE` unset) is the live one.
### Load the data into Zetaris (step by step)

Zetaris cannot read a file on your laptop, so the data is loaded with **SQL** that you paste into the Zetaris SQL
Editor. The statements are in `scripts/zetaris_setup_all.sql`. They create two things in a Zetaris container named
`sales`: the sales table, `sales.sales_facts` (the synthetic data from `data/sample/sales_facts.csv`, 1,440 rows), and
the exchange-rate table, `sales.fx_rates_table` (USD/CAD from the public Frankfurter API, registered as a REST table;
no API key).

1. **Start Zetaris** (Docker, from Zetaris Cloud, per the hackathon Participant Setup Guide) and sign in to its UI.
2. **Open the SQL Editor** in the Zetaris UI.
3. **Run the statements in `scripts/zetaris_setup_all.sql` one at a time, in order.** Stop at the first failure and read
   the message. The sales data goes first, then the exchange-rate API. The `sales` container is created once, at
   the top (there is no `IF NOT EXISTS`), so if it already exists in your Zetaris, skip that first statement.
4. **Check the sales table:** `SELECT COUNT(*), MIN(order_date), MAX(order_date) FROM sales.sales_facts;`
   should return **1440** rows, from 2026-05-03 to September 2026.
5. **Check the exchange-rate table:** `SELECT COUNT(*) FROM sales.fx_rates_table;` should return one row per business day.
6. **Set `.env`** (copy `.env.example`): `SAMPLE_MODE=false`, `ZETARIS_TABLE=sales.sales_facts` and
   `ZETARIS_FX_TABLE=sales.fx_rates_table` (keep the `sales.` prefix), plus your Zetaris address, `ZETARIS_ORG_ID` and
   `ZETARIS_API_KEY` as described above.
7. **Run one live question** to confirm the connection:
   `python run.py --input input_examples/example_1.json --output /tmp/answer.json`. The response should show
   `"data_source": "zetaris"`, and every evidence row should say `"source": "zetaris"`.

Your own data: expose a table or view with the columns in `data/schema.json` and point `ZETARIS_TABLE` at it.

The NVIDIA key is entered **only** in Nightshift's Providers tab; this app never sees it.

## How it works

```
 user_query ─▶ PlannerAgent ──plan + H-MEM hints──▶ ExecutorAgent ──draft + evidence──▶ EvaluatorAgent
                  ▲  (recall / store)                 │  Markovian loop                    │ re-queries & re-checks
                  │                                   ▼  probe → descend → …               │
                  │                              Zetaris SQL (whitelisted, read-only)      │
                  └──────── critique ◀── revise ◀───────────────────────────────────────────┘
                              (up to 2 revisions; unresolved issues are reported, never hidden)
```

| Piece | What it does |
|---|---|
| **PlannerAgent** | Resolves metric and periods from the question and the data's date range, recalls past findings from H-MEM (as *hints only*), proposes hypotheses (dimensions to test). |
| **ExecutorAgent** | Runs the investigation as a Markovian loop. Each step the model sees only a bounded carryover state plus the latest observation, and chooses `probe` or `descend`. Then explains *mechanism* for each finding and drafts claims. |
| **EvaluatorAgent** | Independently re-runs and re-checks the draft (below). Issues a critique; the Executor revises; repeat. |
| **Zetaris** | The data layer. Every number comes from a SQL statement sent to it. |
| **Nightshift** | The model provider (`meterless/auto`). The model that actually handled each call (`response.model`) is written to every log line and `models_used`. |

**The LLM never writes SQL.** SQL is built from the whitelisted schema (`app/schema.py`), literals are escaped,
and `app/sqlguard.py` rejects anything but a single `SELECT` before it can leave the process.

### Why the analysis is not shallow

* **Lift, not just share.** A segment holding 40% of the change is not a driver if it holds 40% of the base.
  A driver must move faster than its size predicts (`share of change ÷ share of base`).
* **Probe, then descend.** Every open dimension is probed first; the agent descends into the most concentrated one,
  and prefers a child (product) over its parent (category) when the child explains more.
* **Mechanism, exactly.** For gross margin the change is split by an identity, not an estimate:
  `Δ = volume + price + unit-cost` effects. In the sample data this surfaces the seeded Rain Jacket cost shock
  (average unit cost +20.0%, accounting for -28,198 of the -33,017 change) rather than just "Rain Jacket fell".

### What the Evaluator verifies (deterministically, whichever model wrote the draft)

numbers recomputed from fresh queries · breakdowns sum to an independent total · mechanism effects add up ·
no truncated result sets (row cap) · every number in the prose is a rounding of a number in the evidence ·
claim direction matches the data · every finding is cited · cited drivers cover ≥ 60% of the change.
Failed checks produce a critique, a revision, and a re-review; anything still failing after two revisions
is returned in `collaboration.unresolved_issues` and `feedback_loop_completed` is `false`.

## AI and model usage

* **Models:** all product model calls go through Meterless Nightshift (`meterless/auto`) and the model that handled each call is
  logged (`models_used`, every log line). Roles: Planner hypotheses, Executor step decisions and drafts, Evaluator model critic.
  No embedding model: H-MEM uses a hashed bag-of-words vector.
* **NVIDIA / Nemotron:** this app has no NVIDIA-specific code. NVIDIA models contribute only if your Nightshift routes to them; read
  `models_used` from a live run before claiming it, because `meterless/auto` may route elsewhere.
* **Agent framework:** none; a small custom stack. **Libraries:** Python standard library, `uvicorn` (BSD-3), `requests` (Apache-2.0).
* **Meterless engines:** the open-source specs (Apache-2.0) were implemented in Python for this project; no engine code was copied.
* **Coding tool:** built with Claude Code. Cursor was not used. All generated code was run and tested (108 tests).
* **Data:** `data/sample/sales_facts.csv` is synthetic, generated by `scripts/make_sample_data.py`.
* **Sample mode:** `SAMPLE_MODE=true` is for offline testing and says so in every output; the default mode is live.

## Meterless engines

### Markovian (`app/engines/markovian.py`), load-bearing
Chunked reasoning with **bounded carryover** (spec §6, `COMPLETED / REMAINING / DECISIONS / CONTEXT`), the
compression cascade (explicit → model → heuristic → truncation) and the §6.3 efficiency math.
It is the control loop of the investigation: without it there is no investigation. Defaults here: chunk 3000 tokens,
max 12 chunks, carryover 256 tokens, overlap 0 (non-zero overlap is rejected, per the spec).
`engines.markovian` in each response reports per-step prompt sizes (flat, ≈1.6–2.7k chars over 7–9 steps, while
a replay-everything prompt grows each step), the cascade level used per step, and two efficiency figures:
the spec §6.3 figure (≈10–27% on the examples) and one using the *actual* carryover size (≈30–44%).
Token counts are **estimated** (≈ chars/4) and labelled `token_label: estimated` unless Nightshift returns `usage`.

### H-MEM (`app/engines/hmem.py`), second engine
Verified drivers and ruled-out dimensions are stored as memories (mandatory `source`, three layers, supersede
links). The Planner recalls them with the spec's hybrid score
(`.35 semantic + .20 keyword + .10 tag + .10 domain + .15 entity + .05 layer + .05 recency − .20 superseded`,
× confidence), and after each run the Evaluator's findings give **feedback** (`helpful +0.05`, `not_helpful −0.03`,
`wrong −0.20`) to every memory that was recalled. A hash-chained trust ledger records each create/read/update/
feedback, and `/memory/sleep` previews before it executes (with backup and restore). Recalled memories are hints:
the agent still queries, and a hint that the data contradicts is marked `not_helpful`.
See `output_examples/output_4.json` (a reworded repeat of example 1): 3 memories recalled, all 3 earned `helpful`.

### Not used
World Model and Scout Intent are not used.

## API

| Route | |
|---|---|
| `POST /run` | `{"user_query": "...", "request_id"?: "...", "context"?: {}}` → full response (below). 422 on bad input, 502 on Nightshift/Zetaris failure. |
| `GET /health` | status, `sample_mode`, `reasoning_mode` |
| `GET /memory` | H-MEM contents and ledger verification |
| `POST /memory/feedback` | `{"memory_id", "signal": "helpful" \| "not_helpful" \| "wrong"}` |
| `POST /memory/sleep` | preview; `{"apply": true}` to execute (with backup) |
| `GET /engine/markovian` | cumulative Markovian stats across runs |

Response: `result` (`summary`, `total_change`, `findings` with mechanism and sources, `claims`, `ruled_out`,
`coverage`, `confidence`, `caveats`, `memory`, `evidence` = every SQL run), `collaboration` (critiques, revisions,
`final_checks`), `engines`, `models_used`, `data_source`, `reasoning_mode`, `log_path`.

## Logs

Every agent action, handoff, query, model call and engine transition is a JSON line in `logs/trace-<id>.jsonl`
**and** on stdout (`LOG_STDOUT=false` to silence): `timestamp, trace_id, agent_name, action, input_summary,
output_summary, target_agent, model, confidence, retry_count, status`, plus engine fields. Sample traces:
`output_examples/logs/`.

## Verified vs. not verified

Verified here (see `tests/`, 108 tests):
* Findings equal ground truth computed independently from the CSV (totals, top driver, price/volume/cost split).
* Markovian efficiency math and config validation match vectors generated by running the **real TypeScript
  reference** (`tests/fixtures/markovian_reference_vectors.json`, `scripts/gen_markovian_vectors.mjs`).
* H-MEM formulas, deltas, supersede, ledger tamper detection, sleep/restore.
* The Zetaris and Nightshift HTTP paths (headers, body, retries, row shapes, row cap, model logging, auth failure)
  **against local stand-ins in `tests/helpers.py`**, which exist only in the tests.
* Truncated data is detected and surfaced rather than analysed as if complete.

**Not verified. Please read:**
* **Live runs:** Zetaris and Nightshift (NVIDIA Nemotron) were exercised in three live runs, examples 1, 2 and 4
  (`output_examples/live/`, JSON plus trace). Example 3 was not run live, and only one Zetaris access route was exercised. If your Zetaris
  dialect or response differs, `app/adapters/zetaris.py` and `app/data_access.py` are the two places to adjust.
* The model-driven path ran live on one Nightshift lane (NVIDIA Nemotron) for examples 1, 2 and 4. It is slow
  (8 to 19 minutes a run) and the sample is small, so behaviour on other questions is untested; the code has one
  corrective retry and deterministic Evaluator checks as a backstop, not a guarantee.
* **Docker image not built** (no Docker daemon in the build environment); `Dockerfile` is simple but unrun.
* Python 3.11 not run (tests ran on 3.13).
* **TypeScript conformance suites for Markovian and H-MEM were not run against the Python ports.** Numeric contracts
  are cross-checked for Markovian only (above); no conformance is claimed for either engine.

## Deviations and limits

* H-MEM retrieval threshold is **0.25**, not the spec's 0.35 (`HMEM_THRESHOLD`). Memories are short and machine-written and
  questions rarely name an entity, so the best relevant memory scores ≈0.29. The embedding is a hashed bag-of-words,
  so recall works for close rewordings ("versus" → "compared to") and misses looser ones ("the drop in profit").
* H-MEM LLM-based mining, dreaming, conflict detection and sleep synthesis are not implemented.
* Offline rules perform one investigation strategy deterministically; a real model may choose differently.
* In the shipped examples only example 1 triggered a critique→revision round (the coverage check). The loop is real
  code exercised by the tests, but not every question needs a revision.
* Explains **what** moved and **how**, not **why in the business sense**: it cannot know about a campaign or a
  supplier change that is not in the data, and says so in `caveats`.
* One metric family (revenue, gross margin, units), month-over-month comparisons, one fact table.

## Layout

```
run.py                 entry point: --input FILE [--output FILE]  or  ASGI service (POST /run)
app/agents.py          PlannerAgent, ExecutorAgent, EvaluatorAgent
app/pipeline.py        orchestration + feedback loop
app/engines/           markovian.py, hmem.py (Python ports of the Meterless specs)
app/analysis.py        contributions, lift, mechanism, grounding and direction checks
app/schema.py          whitelisted SQL builders      app/sqlguard.py   read-only guard
app/llm.py             Nightshift client             app/offline.py    sample-mode stand-in (never used live)
app/adapters/zetaris.py, app/data_access.py, app/sample_store.py
data/                  schema.json, sample/sales_facts.csv      scripts/  data generator, TS vector generator
input_examples/  output_examples/  tests/  SUBMISSION.md
```

See [SUBMISSION.md](SUBMISSION.md) for the sponsor-technology explanation, the pre-existing-components declaration,
the rules checklist, the demo-video script and the slide outline.
