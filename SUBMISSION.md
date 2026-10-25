# Submission pack: "Why did it move?"

Track: **The Agent That Can Explain Why** · Open Agent Hackathon 2026 · Solo builder

> **Status (25 Oct).** Live runs are done: examples 1, 2 and 4 ran against real Zetaris, Nightshift and NVIDIA Nemotron
> (`output_examples/live/`, JSON plus trace). The code is pushed to https://github.com/dom49in/why-did-it-move.
> What is left before submitting:
> 1. **Confirm the repo is public** (open it in a private browser window) and that `.env`, `.memory` and `logs/` are not in it.
> 2. **Upload the demo video** (1-4 minutes, final cut 3:40) and **the slide deck** (12 slides, speaker notes included).
> 3. **Fill in the HackOS form**: description (§8), track (Track 2), the sponsor technology explanation (§3) and the
>    pre-existing components declaration (§4). Submit before **27 Oct 23:45 UTC** (7:45 PM Toronto), hard deadline.
> Dates confirmed from the Official Rules on HackOS: build window 22 Oct 00:00 to 27 Oct 23:45 UTC. Judging criteria:
> Impact 30, Technical 20, Innovation 15, Demo 15, Product & UX 10, Sponsor Tech 10.

## 1. Live verification (15 minutes)

```bash
cp .env.example .env     # set SAMPLE_MODE=false and fill: ZETARIS_REST_URL, ZETARIS_ORG_ID, ZETARIS_API_KEY,
                         #   ZETARIS_TABLE, METERLESS_BASE_URL, METERLESS_RUNTIME_TOKEN
# (NVIDIA key: Nightshift -> Providers tab ONLY. Never in .env.)
# ZETARIS_TABLE=sales.sales_facts and ZETARIS_FX_TABLE=sales.fx_rates_table (keep the `sales.` prefix).
# Load the data into Zetaris first: README, "Load the data into Zetaris (step by step)" (scripts/zetaris_setup_all.sql).
python run.py --input input_examples/example_1.json --output /tmp/live.json
python - <<'EOF'
import json; d = json.load(open("/tmp/live.json"))
print(d["data_source"], d["reasoning_mode"], d["models_used"], d["collaboration"]["final_checks"])
EOF
```

Expected: `zetaris nightshift [<model ids that Nightshift reports>]`. If it fails, the error is structured and says which stage:

| Symptom | Where to look |
|---|---|
| `data_error` at stage `zetaris` "without a list of rows" | Response shape differs from the assumed `{"data":[...]}`: `app/adapters/zetaris.py::_extract_rows` |
| `data_error` "Row keys … do not cover" | Column aliasing/casing: `app/data_access.py::_normalise` |
| SQL error from Zetaris | Dialect: `app/schema.py` (`SUM(CASE WHEN …)`, `GROUP BY`, date predicates in `_period_pred`) |
| `model_error` / bad JSON | `app/llm.py::chat_json` (one corrective retry); check `METERLESS_BASE_URL` ends in `/v1` |
| Evaluator keeps rejecting a real model's draft | Read `collaboration.evaluator_findings`; the checks are deterministic and the message says what failed |

Then copy the passing live response into `output_examples/live/` and keep the trace log; that is your strongest evidence.
Commit nothing from `.env`.

## 2. Requirement checklist (starter guide §22) with evidence

| Requirement | Status | Evidence |
|---|---|---|
| Entry point documented | ✅ | `python run.py --input FILE`, `POST /run` (README) |
| requirements.txt, .env.example, README.md | ✅ | repo root; `.env.example` has blank secrets (tested) |
| ≥3 input examples, ≥3 real output examples | ✅ (sample-mode) | `input_examples/` ×4, `output_examples/` ×4 with traces; produced by `run.py`, checked against the CSV. Live: `output_examples/live/` has examples 1, 2 and 4 (`example_N_live.json` + `example_N_live_trace.jsonl`; traces `f0cf84f6`, `e85e897a`, `b244ab77`). |
| No real .env / keys committed | ✅ | `tests/test_hygiene.py` scans for key shapes; `.gitignore` covers `.env` |
| Repository is public | ✅ pushed, ☐ confirm public | https://github.com/dom49in/why-did-it-move (open in a private window to confirm) |
| Zetaris is the data layer, real role | ✅ verified live | every number is a SQL result via `app/adapters/zetaris.py`; both live runs show `"source": "zetaris"` on all 12 queries, incl. the public FX web API registered as a Zetaris REST table |
| ≥1 Meterless Engine in a working feature, with a test | ✅ | Markovian (control loop) + H-MEM (memory loop); `tests/test_markovian.py`, `tests/test_hmem.py`. Python ports, **TS conformance not run** |
| Nightshift is the primary AI provider | ✅ verified live | all model calls in `app/llm.py` → `{METERLESS_BASE_URL}/chat/completions`, `meterless/auto`; live traces show `reasoning: nightshift` and 12 model steps per run |
| NVIDIA models through Nightshift | ✅ verified live | `models_used: ["nvidia/nemotron-3.5-lightning-30b-a3b"]` in both live responses; the model is logged on every model step |
| ≥2 agents with distinct roles | ✅ | Planner, Executor, Evaluator |
| ≥1 feedback loop; Evaluator critiques; output revised/approved | ✅ | `output_1.json` (`critiques_issued: 1`, `revision_count: 1`); `trace` has `critique_issued` / `revise_draft` / `approve_draft` |
| Logs in `logs/` and stdout, agent names, actions, handoffs, status, model | ✅ | `logs/trace-*.jsonl`; `tests/test_pipeline_sample.py::test_logs_written…`, `tests/test_api.py::test_cli…` |
| No prewritten answers; examples not read at runtime | ✅ | `tests/test_hygiene.py` (no business facts in `app/`, `output_examples` never referenced) |
| SAMPLE_MODE uses sample *inputs* only | ✅ | same SQL runs on the CSV; answers computed per query |
| Different inputs → different outputs | ✅ | outputs 1-3 differ in metric, periods, drivers (Rain Jacket / Quebec-Apparel / Quebec-Wholesale) |
| Sample mode works without Zetaris, Nightshift, credentials | ✅ | quick start; Docker `-e SAMPLE_MODE=true` (Docker itself **not built here**) |
| Docker build/run documented | ✅ docs / ☐ built | `Dockerfile`; run `docker build` once |
| Demo video 1-4 min | ✅ built, ☐ upload | 3:40, follows the deck flow and labels examples 1, 2 and 4; upload to HackOS |
| Slide deck | ✅ built | 12 slides with speaker notes (Slides artifact); repository URL on the last slide |
| Sponsor technology explanation | ✅ | §3 |
| Pre-existing components declared | ✅ | §4 (**confirm the line about the starting scaffold**) |

## 3. Sponsor technology explanation

**Zetaris: the data layer.** Every figure in an answer is the result of a SQL statement sent to Zetaris
through its SQL endpoint (REST API or the UI proxy route, chosen by config; API key and org id). The agent needs *live aggregation*
(totals and breakdowns by dimension, in scope, per period), which is precisely what a SQL layer over the company's
tables provides; a "why" agent that works from a pasted CSV cannot be pointed at the real business. SQL is built from a
whitelisted schema, escaped, and rejected unless it is one `SELECT` (the model never writes SQL). Row caps are
detected: a truncated result is flagged, fails the Evaluator's checks, and is surfaced in `caveats`, never silently analysed.

**Meterless Markovian: the investigation loop.** A "why" question is an open-ended drill-down: probe each dimension,
descend into the one that is concentrated, probe again inside that scope. A naive agent replays every query result into
the next prompt, so context grows with each step. Here, each step's prompt is the system prompt, a ≤256-token carryover
(`COMPLETED / REMAINING / DECISIONS / CONTEXT`) and only the latest observation; the state transition happens in code
and the model chooses the next action. Prompt size stays flat (≈1.6-2.7k characters across 7-9 steps) and the
spec's cascade (explicit → model → heuristic → truncation) degrades gracefully if the state outgrows its budget.
Without it there is no investigation; it is the control loop, not a decoration.

**Meterless H-MEM: compounding knowledge.** Verified drivers and ruled-out dimensions are stored as memories with
provenance. The next, related question starts with hints ("last time, Rain Jacket drove 58% and channel was ruled out"),
the Planner orders hypotheses accordingly, and the Evaluator's verdict feeds back as `helpful` / `not_helpful` /
`wrong`, shifting confidence by the spec's deltas. A hash-chained ledger makes every change auditable.
Memory is advisory only: numbers always come from fresh queries, and a stale memory is demoted when the data contradicts it.

**Meterless Nightshift: the model provider.** Planner hypotheses, step decisions, draft explanations and a model critic
pass go through Nightshift (`meterless/auto`) using only the Runtime token. `response.model` is recorded in every log
line and in `models_used`, which is how NVIDIA contribution is shown rather than asserted. In the live runs `models_used` is
`["nvidia/nemotron-3.5-lightning-30b-a3b"]` and `reasoning_mode` is `nightshift`. The NVIDIA key lives in
Nightshift's Providers tab, never in this app.

**Not used:** World Model, Scout Intent.

### Local-first and privacy design (15% of judging)
* The model sees **aggregates** (member names, base/compare sums, shares), never raw rows; raw rows never leave Zetaris.
* SQL is read-only by construction (builders + guard + tests that a `DELETE` never reaches the wire).
* Memory is a local JSON file plus a local ledger (`MEMORY_DIR`); nothing is sent to third parties; no telemetry.
* With Nightshift on local hardware the whole path (data → model → memory) stays on the customer's machines.
* Secrets only via environment; `.env` is git-ignored; the app holds only the Nightshift Runtime token.

## 4. Declaration of pre-existing components

* **Meterless engine specifications and reference implementations** (Apache-2.0, `github.com/Meterless/Meterless`, commit
  `1742dec`). The TypeScript references were **not copied into this project**: they were run (read-only) only to generate
  the numeric test vectors in `tests/fixtures/markovian_reference_vectors.json`. `app/engines/markovian.py` and
  `app/engines/hmem.py` are written for this project from the specs and are Python *subsets* (see README "Deviations").
* **Open-source libraries:** Python standard library, `uvicorn`, `requests`.
* **Data:** `data/sample/sales_facts.csv` is synthetic, generated by `scripts/make_sample_data.py` with a fixed seed; it
  contains deliberately seeded shocks (e.g. a Rain Jacket cost shock in September) so findings can be checked.
* **Starting scaffold and prepared data:** the project began from the Meterless agentathon starter, and a partial project
  already existed in the working directory when this session began (`app/config.py`, `errors.py`, `sqlguard.py`,
  `sample_store.py`, `tracing.py`, `scripts/make_sample_data.py`, the CSV). Those files were kept; everything else
  (agents, pipeline, both engines, analysis, offline rules, adapters, API, tests, docs, the live-run evidence) was written
  or rewritten during the build window. The sales data loaded into Zetaris comes from that CSV via
  `scripts/zetaris_setup_all.sql`. **Confirm and edit this bullet to state who wrote the scaffold and the data, and when,
  before pasting it into the form.**
* **AI assistance:** developed with Claude Code (vibe coding is permitted by the guide). All code is run and tested; none
  of it is copied model output that was not executed.
* Cursor was not used.

## 5. Judging criteria, honestly

| Criterion | Weight | Case | Gap |
|---|---:|---|---|
| Usefulness | 30 | Every analyst, finance lead and PM is asked "why did it move?"; today it's hours of pivot tables. Answers are checkable: each claim cites a finding and the SQL behind it. | Month-over-month, one fact table, three metrics. |
| Engine usage | 25 | Markovian runs the loop; H-MEM recall → feedback → supersede visibly changes behaviour (`output_4.json`). Both tested, formulas cross-checked. | Python ports; conformance suites not run; World Model/Scout unused. |
| Local-first / privacy | 15 | §3. | Memory file isn't encrypted. |
| Demo quality | 15 | §6, built on a live run. | Not recorded yet. |
| Creativity | 10 | Lift test and exact price/volume/cost identity find the *mechanism*, not just the biggest bar. Evaluator re-derives numbers instead of trusting the Executor. | |
| Documentation | 5 | README with verified/unverified section, this pack. | |

## 6. Demo video script (limit 4:00)

> The final video (3:40, synthetic voiceover) follows the deck's flow instead of this older script: problem and solution, what the user sees, architecture, the loop on examples 1 and 2, Zetaris, Nightshift and NVIDIA, Markovian, H-MEM on example 4, then limits and next steps. The table below is the original outline.

| Time | Show | Say |
|---|---|---|
| 0:00 | Slack-style question on screen: "Why did gross margin fall in September?" | "Every Monday someone asks this. The answer is usually a day of pivot tables. Meet the agent that explains why." |
| 0:15 | Terminal: `python run.py --input …` with the JSONL trace scrolling | "Three agents, real data from Zetaris, models via Nightshift. Watch the trace: Planner, Executor, Evaluator." |
| 0:35 | Highlight `markov_transition` lines and the flat `prompt_chars` | "The Executor drills region, channel, product. Markovian keeps the prompt flat, about two thousand characters every step, instead of replaying every result." |
| 1:00 | The drill-down result: `product = Rain Jacket`, 58%, lift 3.0x | "It doesn't stop at the biggest bar. Lift asks whether a segment moved faster than its size predicts." |
| 1:20 | `mechanism`: unit cost +20.0%, effects volume −4,346 · price −473 · unit cost −28,198 | "And how it moved: volume, price, unit cost, an exact identity. Unit cost is up twenty percent, and that is almost all of it." |
| 1:45 | Show the `critique` line and the revised draft | "The Evaluator re-ran the queries, found the first draft explained only 58%, and sent it back. The Executor added the next drivers." |
| 2:10 | `final_checks` all true; open `evidence` and run one SQL in Zetaris | "Every number is recomputed, reconciled to an independent total, and traceable to a query." |
| 2:30 | Ask the reworded question; show `memory.recalled` + `feedback_applied` | "Ask it again differently: H-MEM recalls last time's finding, then marks it helpful because today's data confirmed it." |
| 2:50 | `models_used` line; README "Verified vs. not verified" | "The model that answered is logged on every call. And the README says plainly what is and isn't verified." |

Do not show or mention a number you haven't seen on screen in the live run; if the live numbers differ from this script, use the live ones.

## 7. Slide outline (original 8-slide plan; the built deck has 12 slides, with speaker notes)

1. Title and one-line promise. 2. The problem (Monday question; cost of the manual answer). 3. Demo screenshot: question → answer with the cited driver. 4. Architecture (Planner / Executor / Evaluator + Zetaris, Nightshift, Markovian, H-MEM). 5. Why the answer is trustworthy (lift, mechanism, deterministic checks, critique loop). 6. Meterless engines: Markovian flat prompt chart + H-MEM recall/feedback. 7. Privacy and local-first, plus verified vs. unverified. 8. What's next (more metrics, World Model for forecasts, Scout Intent for proactive alerts).

## 8. Project description for the submission form (1,964 characters; limit 2,000)

```
Why did it move? is an agent that explains why a business metric changed, and proves its answer.

THE PROBLEM
A dashboard shows that a number moved, but not why. Finding the cause takes an analyst hours of slicing data, an AI that simply guesses cannot be trusted with the numbers, and a pasted answer cannot be re-run.

THE SOLUTION
Ask a plain-English question, such as "Why did gross margin fall in September?" Three agents work in a loop. The Planner recalls memory and picks hypotheses. The Executor probes the data, drills into the biggest segment, splits the change into volume, price and unit cost, and drafts an answer. The Evaluator re-queries every number and either approves or sends the draft back with specific issues. The model never writes SQL or does arithmetic: code builds whitelisted, read-only queries and computes the numbers, so every figure traces to a query. The answer also states what was ruled out, such as currency, channel and region.

SPONSOR TECHNOLOGY
- Zetaris is the data layer: the sales table and a public exchange-rate web API are both queried through it by SQL.
- Meterless Nightshift is the only route to a model.
- NVIDIA Nemotron (nemotron-3.5-lightning-30b-a3b) handled every model step, named in each response and trace.
- Meterless Markovian runs the 7-step investigation with a bounded prompt.
- Meterless H-MEM recalled 3 of 11 memories on a reworded repeat question, all helpful.

EVIDENCE
Three live runs (two distinct questions) show real loopbacks: one run's first draft was rejected for covering only 58% of the change and approved after revision; another had two rejections before approval. JSON responses and JSONL traces are in the repository.

HONEST LIMITS
The engines are Python ports of the specs, and the TypeScript conformance suites were not run. Runs take 8 to 19 minutes on one lane. It finds which segments moved and how, not the business cause.

Track 2: The Agent That Can Explain Why. Team DMax.
```

## 9. Final audit before pressing submit

```bash
python -m unittest discover -s tests -t .       # all green
git ls-files | grep -E '(^|/)\.env$' ; echo "(nothing above = good)"
grep -rIE 'nvapi-|sk-[A-Za-z0-9]{20}' . --exclude-dir=.git ; echo "(nothing above = good)"
docker build -t wdim . && docker run --rm -e SAMPLE_MODE=true -p 8000:8000 wdim     # first real Docker run
```


## Live-run evidence (added after running against Zetaris and Nightshift)

| Run | Trace | What it shows |
|---|---|---|
| Example 1 | `trace-20261025-f0cf84f6` | Zetaris + FX source, Nightshift/Nemotron, Markovian (7 steps, max prompt 2,507 chars), Evaluator loop (critique, revise, approve; coverage 0.582 to 0.876), H-MEM recall/feedback/store |
| Example 2 (a different metric and months: revenue, Jul to Aug) | `trace-20261025-e85e897a` | New question, so H-MEM correctly recalled 0 of 17 memories. 9 Markovian steps, descent from region into product_category. Two rejections and two revisions: the Evaluator caught numbers not in the evidence and a percent misstated by a factor of 100, then a unit-cost effect wrongly attributed to revenue; approved on the third review. The FX check returned `concentrated` (not flat): -1.44%, but the change sits in Quebec, so it is not the main explanation |
| Example 4 (reworded repeat of example 1) | `trace-20261025-b244ab77` | H-MEM recalled 3 of 11 memories, all written by the example 1 run, rated them helpful and stored 6 more; same loop and checks passed again |

Limits, stated plainly: the engines are spec-aligned Python (Meterless TS conformance suites were not run); Markovian efficiency figures are the spec formula and labelled estimated (measured prompt tokens did not fall step over step), so the claim is a bounded prompt, not a token saving; recall scores (0.25 to 0.29) sit just above the 0.25 threshold and recalled memories are hints only, every number is re-verified; only examples 1, 2 and 4 were run live (3 is sample-mode); a live run takes 8 to 19 minutes (example 2 took 19) on one Nightshift lane; Docker build and Python 3.11 were not tested.
