"""Planner, Executor and Evaluator agents and the collaboration between them.

Planner   : turns the question into a structured plan (metric, periods, ordered hypotheses), recalls H-MEM
            memories as hints, and finalises (scores recalled memories against today's data, mines new ones).
Executor  : runs the Markovian investigation (model picks the next dimension; SQL runs through Zetaris),
            writes the draft, and revises it when the Evaluator pushes back.
Evaluator : independently re-checks every number against the stored query rows, checks that sums reconcile to
            the independent total, that nothing was truncated, that every number in the text is grounded in the
            evidence and every direction is right, then asks a model to critique unsupported statements.
            It can send work back.
"""
from __future__ import annotations

import json

from . import analysis, data_access, llm
from . import schema as schema_mod
from .analysis import Evidence
from .engines.markovian import MarkovianEngine, MarkovState, scope_key
from .errors import LLMError, NoDataError, RequestError
from .memory import InvestigationMemory
from .tracing import Tracer, short

MIN_COVERAGE = 0.60
SUM_TOLERANCE = 0.02

PLAN_SYSTEM = (
    "ROLE: planner\n"
    "You plan root-cause investigations of a business metric change using only the provided schema.\n"
    "Reply with ONLY a JSON object:\n"
    '{"metric": "<one of the metrics>", "base_period": "YYYY-MM", "compare_period": "YYYY-MM", '
    '"hypotheses": [{"dimension": "<one of the dimensions>", "rationale": "<max 15 words>"}], '
    '"success_criteria": "<one sentence>"}\n'
    "- base_period is the earlier/reference month; compare_period is the month whose change is being explained.\n"
    "- Both periods must fall inside DATA_RANGE. 'Latest month' means the month of the last date in DATA_RANGE.\n"
    "- Order hypotheses from most to least likely to concentrate the change. PAST_MEMORY_JSON lists earlier "
    "confirmed drivers and ruled-out dimensions: test confirmed ones first and ruled-out ones last, but they are "
    "hints only and will be re-checked against the data.\n"
    '- If the question cannot be answered with these metrics and dimensions, reply '
    '{"unsupported": true, "reason": "<why>"}.'
)

DRAFT_SYSTEM = (
    "ROLE: executor-drafter\n"
    "You explain WHERE and HOW a business metric changed, using only EVIDENCE_JSON.\n"
    'Reply with ONLY a JSON object: {"summary": "<2-4 sentences>", '
    '"claims": [{"finding_id": "<an id from findings>", "statement": "<one sentence>"}]}\n'
    "Rules:\n"
    "- Every finding needs at least one claim citing its id, stating direction and size using the evidence numbers.\n"
    "- Mention dimensions that were ruled out as diffuse or proportional when it helps the reader.\n"
    "- Each finding has a lift (share of the change divided by share of the base). Lift >= 1.5 means a real driver; "
    "lift < 1.5 means it moved roughly in line with its size, so call it a proportional mover, never a driver.\n"
    "- Each finding's mechanism splits its delta exactly into volume, price and unit_cost effects: say which effect "
    "dominates and quote the unit cost / price / units change percentages.\n"
    "- EVIDENCE_JSON.external, when not null, is a market-wide context test from a second data source. Include its "
    "statement in one sentence with its verdict unchanged; it is correlation context, never a proven cause.\n"
    "- Never invent causes outside the data (pricing decisions, campaigns, weather...). You may say what the data "
    "cannot tell.\n"
    "- Do not use any number that is not in the evidence (you may round it). If there are no findings, say the "
    "change is broad-based."
)

CRITIC_SYSTEM = (
    "ROLE: evaluator-critic\n"
    "You review a draft explanation of a metric change against EVIDENCE_JSON. Flag only MATERIAL problems: "
    "numbers that contradict the evidence, direction stated wrongly, causes asserted that the data cannot support, "
    "or conclusions not backed by a cited finding. Do not nitpick style.\n"
    'Reply with ONLY a JSON object: {"requires_revision": true|false, "issues": ["<specific issue>", ...]}.'
)


def scope_text(scope: list[dict]) -> str:
    return " AND ".join(f"{s['dimension']}={s['member']}" for s in scope)


class PlannerAgent:
    name = "PlannerAgent"

    def __init__(self, tracer: Tracer, memory: InvestigationMemory):
        self.tracer, self.memory = tracer, memory

    def plan(self, user_query: str, context: dict, schema: dict, evidence: Evidence) -> dict:
        sql = schema_mod.range_sql(schema)
        rows, source, truncated = data_access.query(sql, ["min_date", "max_date"])
        evidence.add(sql, rows, source, kind="range", truncated=truncated)
        if not rows or not rows[0].get("min_date"):
            raise NoDataError("The data source returned no rows.", stage="plan")
        min_date, max_date = str(rows[0]["min_date"])[:10], str(rows[0]["max_date"])[:10]
        self.tracer.event(self.name, "query_data", input_summary="data range", source=source,
                          output_summary=f"{min_date} .. {max_date}")

        recall = self.memory.recall(user_query)
        hints = self.memory.hints(recall)
        self.tracer.event(self.name, "memory_recall", engine="H-MEM", input_summary=user_query,
                          output_summary=f"{len(hints)} recalled (hints only): {recall['trace']['retrievalReason']}",
                          memory_ids=[h["id"] for h in hints], scores=[h["score"] for h in hints])
        user = (f"QUESTION: {user_query}\n"
                f"SCHEMA_JSON:\n{json.dumps({'metrics': sorted(schema['metrics']), 'dimensions': schema['dimensions']})}\n"
                f"DATA_RANGE: {min_date} to {max_date}\n"
                f"PAST_MEMORY_JSON:\n{json.dumps(hints)}")
        obj, meta = llm.chat_json([{"role": "system", "content": PLAN_SYSTEM}, {"role": "user", "content": user}])
        if obj.get("unsupported"):
            raise RequestError(f"This question can't be answered from the available data: {obj.get('reason', '')}",
                               stage="plan", details={"metrics": sorted(schema["metrics"])})
        missing = [k for k in ("metric", "base_period", "compare_period") if k not in obj]
        if missing:
            raise LLMError(f"Plan is missing {missing}", stage="plan")
        metric, base, comp = obj["metric"], obj["base_period"], obj["compare_period"]
        if metric not in schema["metrics"]:
            raise RequestError(f"Unsupported metric {metric!r}.", stage="plan",
                               details={"metrics": sorted(schema["metrics"])})
        schema_mod.period_bounds(base)
        schema_mod.period_bounds(comp)
        if base == comp:
            raise RequestError("Base and compare periods must differ.", stage="plan")
        for p in (base, comp):
            if not (min_date[:7] <= p <= max_date[:7]):
                raise NoDataError(f"Period {p} is outside the data range {min_date}..{max_date}.", stage="plan")
        hyps = []
        for h in obj.get("hypotheses") or []:
            if isinstance(h, dict) and h.get("dimension") in schema["dimensions"]:
                hyps.append({"dimension": h["dimension"], "rationale": str(h.get("rationale", ""))[:120]})
        plan = {"metric": metric, "base_period": base, "compare_period": comp, "hypotheses": hyps,
                "success_criteria": str(obj.get("success_criteria", ""))[:300], "data_range": [min_date, max_date],
                "memory_recalled": recall["memories"], "memory_trace": recall["trace"]}
        self.tracer.event(self.name, "create_plan", input_summary=user_query, model=meta["model"],
                          retry_count=meta["retries"], target_agent="ExecutorAgent",
                          output_summary=f"{metric} {base} -> {comp}; hypotheses: {[h['dimension'] for h in hyps]}")
        return plan

    def finalise(self, plan: dict, state: MarkovState, draft: dict, evaluation: dict, evidence: Evidence,
                 revision_count: int, unresolved: bool) -> dict:
        total = evidence.queries[evidence.first("total")]["rows"][0]
        base_t, comp_t = analysis.num(total["base_value"]), analysis.num(total["compare_value"])
        delta = comp_t - base_t
        findings = [{
            "id": f["id"], "level": f["level"], "dimension": f["dimension"], "member": f["member"],
            "scope": scope_text(f["scope"]) or "all data",
            "base_value": f["base"], "compare_value": f["compare"], "delta": f["delta"],
            "share_of_scope_change": f["share_of_scope"], "share_of_total_change": f["share_of_total"],
            "lift_vs_base_share": f.get("lift"),
            "mechanism": f.get("mechanism"),
            "sources": [f["query_id"], *([f["mechanism_query_id"]] if f.get("mechanism_query_id") else [])],
        } for f in state.findings]
        coverage = evaluation["coverage"]
        truncated = any(q["truncated"] for q in evidence.queries.values())
        if not state.findings:
            confidence = 0.25
        else:
            confidence = 0.35 + 0.5 * min(max(coverage, 0.0), 1.0) + (-0.2 if unresolved else 0.1)
        if truncated:
            confidence -= 0.15
        confidence = round(min(max(confidence, 0.05), 0.95), 2)
        caveats = ["The decomposition shows which segments moved and by how much; it cannot identify business "
                   "causes (pricing, campaigns, supply) that are not in the data."]
        if state.findings and coverage < MIN_COVERAGE:
            caveats.append(f"The identified drivers account for {coverage:.0%} of the total change.")
        if not state.findings:
            caveats.append("No single segment concentrated the change; it appears broad-based.")
        if unresolved:
            caveats.append("The Evaluator still had open issues when the revision limit was reached.")
        if truncated:
            caveats.append("At least one query hit the row cap, so its result may be incomplete.")
        if state.stopped == "immaterial_change":
            caveats.append("The change between the two periods is immaterial.")

        # H-MEM: first score what was recalled against today's data, then mine today's verified findings.
        analysed = {(q["dimension"], "|".join(f"{s['dimension']}={s['member']}" for s in q["scope"]) or "(all)")
                    for q in evidence.queries.values() if q["kind"] == "breakdown"}
        recalled = plan.get("memory_recalled", [])
        fb = self.memory.feedback_on_outcome(recalled, plan["metric"], state.findings, state.ruled_out, analysed)
        for item in fb:
            self.tracer.event(self.name, "memory_feedback", engine="H-MEM", input_summary=f"prior {item['memory_id']}",
                              output_summary=f"{item['signal']} -> confidence {item['confidence']}",
                              status="success")
        stored = self.memory.store_run(plan, state.findings, state.ruled_out, unresolved)
        self.tracer.event(self.name, "memory_store", engine="H-MEM", input_summary="verified findings and ruled-out dimensions",
                          output_summary=f"{len(stored)} memories written (same-key memories superseded, not deleted)",
                          memory_ids=stored)

        result = {
            "metric": plan["metric"], "base_period": plan["base_period"], "compare_period": plan["compare_period"],
            "total_change": {"base": round(base_t, 2), "compare": round(comp_t, 2), "delta": round(delta, 2),
                             "pct": None if abs(base_t) < 1e-9 else round(delta / base_t * 100, 2)},
            "summary": draft["summary"],
            "claims": draft["claims"],
            "findings": findings,
            "ruled_out": state.ruled_out,
            "coverage": coverage,
            "confidence": confidence,
            "caveats": caveats,
            "memory": {
                "recalled": [{"id": m["id"], "score": m["score"], "content": m["content"]} for m in recalled],
                "retrieval_trace": plan.get("memory_trace"),
                "feedback_applied": fb,
                "stored": stored,
            },
            "evidence": evidence.public(),
        }
        self.tracer.event(self.name, "final_response", target_agent="User", confidence=confidence,
                          input_summary=f"evaluation: {len(evaluation['issues'])} open issue(s), "
                                        f"{revision_count} revision(s)",
                          output_summary=draft["summary"])
        return result


class ExecutorAgent:
    name = "ExecutorAgent"

    def __init__(self, tracer: Tracer, engine: MarkovianEngine, evidence: Evidence, schema: dict):
        self.tracer, self.engine, self.evidence, self.schema = tracer, engine, evidence, schema
        self._plan: dict = {}
        self.external_raw: dict | None = None      # one-row result of the external-source query (via Zetaris)
        self.external_qid: str | None = None

    # -- investigation (Markovian loop) ----------------------------------------------------------
    def investigate(self, user_query: str, plan: dict) -> MarkovState:
        metric, base, comp = plan["metric"], plan["base_period"], plan["compare_period"]
        self._plan = plan
        sql = schema_mod.total_sql(self.schema, metric, base, comp)
        rows, source, truncated = data_access.query(sql, ["base_value", "compare_value"])
        qid = self.evidence.add(sql, rows, source, kind="total", truncated=truncated)
        base_t = analysis.num(rows[0]["base_value"]) if rows else 0.0
        comp_t = analysis.num(rows[0]["compare_value"]) if rows else 0.0
        if base_t == 0 and comp_t == 0:
            raise NoDataError(f"No {metric} data for {base} or {comp}.", stage="investigate")
        total_delta = comp_t - base_t
        obs = {"type": "total", "base_total": round(base_t, 2), "compare_total": round(comp_t, 2),
               "delta": round(total_delta, 2),
               "pct_change": None if abs(base_t) < 1e-9 else round(total_delta / base_t * 100, 2), "query_id": qid}
        self.tracer.event(self.name, "query_data", source=source, input_summary=f"total {metric} {base} vs {comp}",
                          output_summary=f"base={obs['base_total']} compare={obs['compare_total']} "
                                         f"delta={obs['delta']}")
        state = self.engine.init_state(user_query, metric, base, comp, total_delta, plan["hypotheses"],
                                       list(self.schema["dimensions"]), self.schema.get("hierarchies"))
        if abs(total_delta) <= 0.001 * max(abs(base_t), 1e-9):
            state.stopped = "immaterial_change"
        probes: dict[tuple[str, str], dict] = {}     # (scope, dimension) -> observation, reused by `descend`

        while True:
            stop, reason = self.engine.should_stop(state)
            if stop:
                state.stopped = state.stopped or reason
                break
            messages = self.engine.build_step_prompt(state, obs)
            action = self._decide(messages, state)
            kind = action["action"]
            if kind == "finish":
                self.engine.record_step(0, {"type": "finish"}, action)
                state.stopped = "model_finished"
                break
            dim = action["dimension"]
            key = (scope_key(state.scope), dim)
            if kind == "probe":
                sql = schema_mod.breakdown_sql(self.schema, dim, metric, base, comp, state.scope)
                rows, source, truncated = data_access.query(sql, ["member", "base_value", "compare_value"])
                qid = self.evidence.add(sql, rows, source, dimension=dim, scope=state.scope, truncated=truncated)
                summary = analysis.summarise(rows)
                obs = analysis.observation(dim, state.scope, summary, qid, raw_chars=len(json.dumps(rows, default=str)))
                probes[key] = obs
                self.engine.record_step(obs["raw_chars"], obs, action)
                self.tracer.event(self.name, "query_data", source=source,
                                  input_summary=f"probe {dim}, scope={scope_text(state.scope) or 'all'}",
                                  output_summary=f"{obs['member_count']} members; top={obs['top'][:1]}"
                                                 + ("; ROW CAP HIT" if truncated else ""))
            else:   # descend: reuse the rows already fetched by the probe, no new query
                obs = probes[key]
                self.engine.record_step(0, obs, action)
            new_state = self.engine.transition(state, action, obs)
            added = [f for f in new_state.findings if f["id"] not in {x['id'] for x in state.findings}]
            if kind == "probe":
                pr = new_state.probed[dim]
                summary_text = (f"probed {dim}: top {pr['top_member']} holds {pr['top_share']:.0%} "
                                f"({'concentrated' if pr['concentrated'] else 'diffuse, ruled out'})")
            else:
                summary_text = (f"descend into {dim}: finding(s) {[(f['id'], f['member'], f['share_of_total']) for f in added]}; "
                                f"scope now {scope_text(new_state.scope) or 'all'}")
            self.tracer.event(
                self.name, "markov_transition", engine="Markovian", input_summary=f"step {new_state.step}",
                output_summary=summary_text, prompt_chars=self.engine.prompt_chars[-1],
                carryover_level=self.engine.cascade_levels[-1],
                carryover_tokens=self.engine.stats()["carryover_tokens_per_step"][-1])
            state = new_state
        self.tracer.event(self.name, "investigation_complete", engine="Markovian", input_summary=f"{state.step} step(s)",
                          output_summary=f"stopped: {state.stopped}; {len(state.findings)} finding(s); "
                                         f"max prompt {max(self.engine.prompt_chars, default=0)} chars",
                          target_agent="EvaluatorAgent")
        return state

    # -- mechanism: HOW each finding moved ---------------------------------------------------------
    def explain_mechanisms(self, state: MarkovState, plan: dict) -> int:
        """For each finding, fetch revenue/cost/units in its scope and attach an exact price/volume/unit-cost
        decomposition. Findings that already have one are skipped."""
        done = 0
        for f in state.findings:
            if "mechanism" in f:
                continue
            filters = [*f["scope"], {"dimension": f["dimension"], "member": f["member"]}]
            sql = schema_mod.mechanism_sql(self.schema, plan["base_period"], plan["compare_period"], filters)
            rows, source, truncated = data_access.query(sql, schema_mod.MECHANISM_COLUMNS)
            qid = self.evidence.add(sql, rows, source, kind="mechanism", scope=filters, truncated=truncated)
            f["mechanism"] = analysis.mechanism(rows[0] if rows else {}, plan["metric"])
            f["mechanism_query_id"] = qid
            done += 1
        if done:
            self.tracer.event(self.name, "explain_mechanism", input_summary=f"{done} finding(s)",
                              output_summary="; ".join(
                                  f"{f['id']}: {analysis.dominant_effect(f['mechanism'])} "
                                  f"{(f['mechanism'].get('effects') or {})}" for f in state.findings[:4]))
        return done

    def _decide(self, messages: list[dict], state: MarkovState) -> dict:
        obj, meta = llm.chat_json(messages)
        self.engine.note_usage(meta.get("usage"))
        action = str(obj.get("action", "")).lower()
        reason = str(obj.get("reason", ""))[:160]
        dim = obj.get("dimension")
        options = [d for d, _ in state.descend_options()]
        ok = (
            (action == "probe" and dim in state.open_dimensions)
            or (action == "descend" and dim in options)
            or (action == "finish" and (bool(state.findings) or (not state.open_dimensions and not options)))
        )
        if ok:
            decision = {"action": action, "dimension": dim if action != "finish" else None, "reason": reason}
            self.tracer.event(self.name, "decide_next_step", model=meta["model"], retry_count=meta["retries"],
                              input_summary=f"step {state.step}, open={state.open_dimensions}, descend={options}, "
                                            f"prompt_chars={self.engine.prompt_chars[-1]}",
                              output_summary=f"{action} {decision['dimension'] or ''}: {reason}")
            return decision
        self.engine.fallbacks += 1
        if state.open_dimensions:
            decision = {"action": "probe", "dimension": state.open_dimensions[0],
                        "reason": "fallback to first open dimension"}
        elif options:
            decision = {"action": "descend", "dimension": options[0], "reason": "fallback to best descend option"}
        else:
            decision = {"action": "finish", "dimension": None, "reason": "fallback: nothing left to do"}
        self.tracer.event(self.name, "action_fallback", status="fallback", model=meta["model"],
                          retry_count=meta["retries"], input_summary=f"model replied {short(obj, 120)}",
                          output_summary=f"invalid or premature action; using {decision['action']} {decision['dimension']}")
        return decision

    # -- drafting / revising ------------------------------------------------------------------
    def check_external(self, state: MarkovState, plan: dict) -> bool:
        """Second data source: a public web API (exchange rates) registered in Zetaris as a REST table. Queried through
        the same Zetaris adapter as the sales table. Skipped, and said so, when it is not configured."""
        if schema_mod.external_table_name(self.schema) is None:
            self.tracer.event(self.name, "external_source_skipped", status="skipped",
                              input_summary="second data source", output_summary="ZETARIS_FX_TABLE is not set")
            return False
        sql = schema_mod.external_sql(self.schema, plan["base_period"], plan["compare_period"])
        rows, source, truncated = data_access.query(sql, schema_mod.EXTERNAL_COLUMNS)
        self.external_qid = self.evidence.add(sql, rows, source, kind="external", truncated=truncated)
        self.external_raw = rows[0] if rows else {}
        a = self.external_assessment(state)
        self.tracer.event(self.name, "query_external_source", input_summary=f"{a['series']} via {source}",
                          output_summary=a["statement"], source=source, verdict=a["verdict"])
        return True

    def external_assessment(self, state: MarkovState) -> dict | None:
        if self.external_raw is None:
            return None
        label = schema_mod.external_spec(self.schema)["label"]
        out = analysis.external_assessment(self.external_raw, state.findings, label)
        out["query_id"] = self.external_qid
        return out

    def evidence_payload(self, state: MarkovState) -> dict:
        total = self.evidence.queries[self.evidence.first("total")]["rows"][0]
        base_t, comp_t = analysis.num(total["base_value"]), analysis.num(total["compare_value"])
        return {
            "metric": state.metric, "base_period": state.base_period, "compare_period": state.compare_period,
            "total": {"base": round(base_t, 2), "compare": round(comp_t, 2), "delta": round(comp_t - base_t, 2),
                      "pct_change": None if abs(base_t) < 1e-9 else round((comp_t - base_t) / base_t * 100, 2)},
            "findings": [{"id": f["id"], "level": f["level"], "scope": scope_text(f["scope"]) or "all data",
                          "dimension": f["dimension"], "member": f["member"], "delta": f["delta"],
                          "share_of_scope": f["share_of_scope"], "share_of_total": f["share_of_total"],
                          "lift": f.get("lift"), "mechanism": f.get("mechanism")}
                         for f in state.findings],
            "ruled_out": [{"dimension": r["dimension"], "scope": r["scope"], "why": r["why"]}
                          for r in state.ruled_out],
            "stop_reason": state.stopped,
            "external": self.external_assessment(state),
        }

    def _write(self, user_query: str, state: MarkovState, version: int, extra: str = "") -> dict:
        user = f"QUESTION: {user_query}\nEVIDENCE_JSON:\n{json.dumps(self.evidence_payload(state))}{extra}"
        obj, meta = llm.chat_json([{"role": "system", "content": DRAFT_SYSTEM}, {"role": "user", "content": user}],
                                  required_keys=("summary", "claims"))
        valid = {f["id"] for f in state.findings}
        raw_claims = obj["claims"] if isinstance(obj["claims"], list) else []
        claims = [{"finding_id": c["finding_id"], "statement": str(c["statement"])} for c in raw_claims
                  if isinstance(c, dict) and c.get("finding_id") in valid and c.get("statement")]
        draft = {"version": version, "summary": str(obj["summary"]), "claims": claims,
                 "dropped_claims": len(raw_claims) - len(claims), "model": meta["model"]}
        self._last_meta = meta
        return draft

    def draft(self, user_query: str, state: MarkovState) -> dict:
        draft = self._write(user_query, state, 1)
        self.tracer.event(self.name, "draft_created", model=draft["model"], retry_count=self._last_meta["retries"],
                          target_agent="EvaluatorAgent", input_summary=f"{len(state.findings)} finding(s)",
                          output_summary=draft["summary"])
        return draft

    def revise(self, user_query: str, state: MarkovState, draft: dict, critique: dict) -> dict:
        extended = 0
        if "coverage" in critique["codes"] and self.can_extend(state):
            extended = self._extend_coverage(state)
            self.explain_mechanisms(state, self._plan)
        extra = ("\nPREVIOUS_DRAFT_JSON:\n" + json.dumps({"summary": draft["summary"], "claims": draft["claims"]}) +
                 "\nCRITIQUE_JSON:\n" + json.dumps([i["message"] for i in critique["issues"]]) +
                 "\nRevise the draft to fix every issue. Do not add statements the evidence does not support.")
        revised = self._write(user_query, state, draft["version"] + 1, extra)
        self.tracer.event(self.name, "revise_draft", model=revised["model"], retry_count=self._last_meta["retries"],
                          target_agent="EvaluatorAgent",
                          input_summary=f"addressing {len(critique['issues'])} issue(s); "
                                        f"{extended} extra contributor(s) added",
                          output_summary=revised["summary"])
        return revised

    # -- coverage extension (used when the Evaluator says the drivers explain too little) ------
    def _level0(self, state: MarkovState) -> list[dict]:
        return [f for f in state.findings if f["level"] == 0]

    def _remaining(self, state: MarkovState) -> list[dict]:
        l0 = self._level0(state)
        if not l0:
            return []
        summary = analysis.summarise(self.evidence.queries[l0[0]["query_id"]]["rows"])
        taken = {f["member"] for f in l0}
        return [m for m in summary["aligned"] if m["member"] not in taken]

    def can_extend(self, state: MarkovState) -> bool:
        l0 = self._level0(state)
        cumulative = sum(f["share_of_total"] or 0 for f in l0)
        return bool(l0) and bool(self._remaining(state)) and cumulative < analysis.COVERAGE_TARGET and len(l0) < 5

    def _extend_coverage(self, state: MarkovState) -> int:
        l0 = self._level0(state)
        cumulative = sum(f["share_of_total"] or 0 for f in l0)
        added = 0
        for m in self._remaining(state):
            if cumulative >= analysis.COVERAGE_TARGET or len(l0) + added >= 5:
                break
            state.finding_seq += 1
            share_total = m["delta"] / state.total_delta
            state.findings.append({
                "id": f"f{state.finding_seq}", "level": 0, "dimension": l0[0]["dimension"], "member": m["member"],
                "scope": [], "delta": round(m["delta"], 2), "base": round(m["base"], 2),
                "compare": round(m["compare"], 2), "share_of_scope": round(m["share"], 5),
                "lift": None if m.get("lift") is None else round(m["lift"], 2),
                "share_of_total": round(share_total, 5), "query_id": l0[0]["query_id"]})
            cumulative += share_total
            added += 1
        return added


class EvaluatorAgent:
    name = "EvaluatorAgent"

    def __init__(self, tracer: Tracer):
        self.tracer = tracer

    # -- deterministic checks ---------------------------------------------------------------------
    @staticmethod
    def _row_for(q: dict, member: str) -> dict | None:
        for r in q["rows"]:
            label = "(none)" if r.get("member") is None else str(r["member"])
            if label == member:
                return r
        return None

    def _check_findings(self, state: MarkovState, evidence: Evidence, total_delta: float, issues: list) -> bool:
        ok = True
        for f in state.findings:
            q = evidence.queries.get(f["query_id"])
            row = self._row_for(q, f["member"]) if q else None
            if row is None:
                ok = False
                issues.append({"code": "integrity", "message": f"Finding {f['id']} has no matching row in {f['query_id']}."})
                continue
            delta = analysis.num(row["compare_value"]) - analysis.num(row["base_value"])
            if abs(delta - f["delta"]) > 0.011:
                ok = False
                issues.append({"code": "integrity", "message": f"Finding {f['id']} delta {f['delta']} != recomputed {delta:.2f}."})
            if abs(total_delta) > analysis.EPS and f["share_of_total"] is not None \
                    and abs(delta / total_delta - f["share_of_total"]) > 0.002:
                ok = False
                issues.append({"code": "integrity", "message": f"Finding {f['id']} share of total does not recompute."})
        return ok

    def _check_sums(self, state: MarkovState, evidence: Evidence, total_delta: float, issues: list) -> bool:
        """Every breakdown must reconcile: unscoped ones to the independent total query, scoped ones to the
        finding they drill into. This is what catches silently truncated or partial results."""
        ok = True
        for qid, q in evidence.queries.items():
            if q["kind"] != "breakdown":
                continue
            s = analysis.summarise(q["rows"])["scope_delta"]
            if not q["scope"]:
                expected, label = total_delta, "the independent total"
            else:
                last, parent = q["scope"][-1], q["scope"][:-1]
                match = next((f for f in state.findings if f["dimension"] == last["dimension"]
                              and f["member"] == last["member"] and f["scope"] == parent), None)
                if match is None:
                    continue
                expected, label = match["delta"], f"finding {match['id']}"
            tol = max(SUM_TOLERANCE, 1e-6 * abs(expected))
            if abs(s - expected) > tol:
                ok = False
                issues.append({"code": "sum_mismatch", "message":
                               f"{qid} ({q['dimension']}) sums to {s:.2f} but {label} is {expected:.2f}."})
        return ok

    @staticmethod
    def _check_mechanisms(state: MarkovState, evidence: Evidence, metric: str, issues: list) -> bool:
        """Recompute each finding's price/volume/unit-cost split from the stored mechanism rows, and require the
        effects to add up to the finding's own delta (they are an identity, so any gap is a bug)."""
        ok = True
        for f in state.findings:
            mech, qid = f.get("mechanism"), f.get("mechanism_query_id")
            q = evidence.queries.get(qid) if qid else None
            if mech is None or q is None or not q["rows"]:
                ok = False
                issues.append({"code": "mechanism_missing", "message": f"Finding {f['id']} has no mechanism evidence."})
                continue
            again = analysis.mechanism(q["rows"][0], metric)
            if again.get("effects") != mech.get("effects"):
                ok = False
                issues.append({"code": "mechanism_mismatch", "message": f"Finding {f['id']} mechanism does not recompute."})
            effects = mech.get("effects") or {}
            if effects and abs(sum(effects.values()) - f["delta"]) > max(0.05 * len(effects), 1e-6 * abs(f["delta"])):
                ok = False
                issues.append({"code": "mechanism_mismatch", "message":
                               f"Finding {f['id']} effects sum to {sum(effects.values()):.2f}, delta is {f['delta']}."})
        return ok

    def _check_external(self, executor: ExecutorAgent, evidence: Evidence, issues: list) -> bool | None:
        """Re-run the external-source query and compare. None when the second source was not used."""
        if executor.external_qid is None:
            return None
        q = evidence.queries[executor.external_qid]
        rows, _, _ = data_access.query(q["sql"], schema_mod.EXTERNAL_COLUMNS)
        fresh, stored = (rows[0] if rows else {}), (executor.external_raw or {})
        for key in ("base_value", "compare_value"):
            a, b = fresh.get(key), stored.get(key)
            if (a is None) != (b is None) or (a is not None and abs(analysis.num(a) - analysis.num(b)) > 1e-6):
                issues.append({"code": "external_mismatch",
                               "message": f"The external source now returns {key}={a}, but {b} was used."})
                return False
        return True

    def review(self, user_query: str, state: MarkovState, draft: dict, evidence: Evidence, executor: ExecutorAgent) -> dict:
        issues: list[dict] = []
        total = evidence.queries[evidence.first("total")]["rows"][0]
        total_delta = analysis.num(total["compare_value"]) - analysis.num(total["base_value"])

        # 1. Independent numeric re-check of every finding against the stored query rows.
        integrity_ok = self._check_findings(state, evidence, total_delta, issues)

        # 2. Sums reconcile; mechanism splits add up to each delta; nothing truncated.
        sums_ok = self._check_sums(state, evidence, total_delta, issues)
        mech_ok = self._check_mechanisms(state, evidence, state.metric, issues)
        external_ok = self._check_external(executor, evidence, issues)
        capped = [qid for qid, q in evidence.queries.items() if q["truncated"]]
        if capped:
            issues.append({"code": "truncated", "message": f"Row cap hit in {capped}; results may be incomplete."})

        # 3. Every finding must be cited by at least one claim.
        cited = {c["finding_id"] for c in draft["claims"]}
        missing = [f["id"] for f in state.findings if f["id"] not in cited]
        if missing:
            issues.append({"code": "uncited_finding", "message": f"No claim cites finding(s) {missing}."})
        if not draft["summary"].strip():
            issues.append({"code": "empty_summary", "message": "The summary is empty."})

        # 4. Every number in the text must be a rounding of an evidence number; directions must match.
        payload = executor.evidence_payload(state)
        text = " ".join([draft["summary"]] + [c["statement"] for c in draft["claims"]])
        loose = analysis.ungrounded_numbers(text, payload)
        if loose:
            issues.append({"code": "ungrounded_number", "message":
                           f"Numbers not found in the evidence: {sorted(set(loose))[:6]}. Restate using evidence numbers only."})
        direction = analysis.direction_issues(draft["claims"], payload["findings"])
        issues += [{"code": "direction", "message": m} for m in direction]

        # 5. Coverage of the total change by the level-0 drivers.
        l0 = [f for f in state.findings if f["level"] == 0]
        coverage = sum(f["delta"] for f in l0) / total_delta if l0 and abs(total_delta) > analysis.EPS else 0.0
        coverage = round(coverage, 3)
        if l0 and coverage < MIN_COVERAGE and executor.can_extend(state):
            issues.append({"code": "coverage", "message": f"Cited drivers explain only {coverage:.0%} of the total "
                                                          f"change; add the next-largest contributors."})

        # 6. Model critique of unsupported statements.
        user = (f"QUESTION: {user_query}\nEVIDENCE_JSON:\n{json.dumps(payload)}\n"
                f"DRAFT_JSON:\n{json.dumps({'summary': draft['summary'], 'claims': draft['claims']})}")
        obj, meta = llm.chat_json([{"role": "system", "content": CRITIC_SYSTEM}, {"role": "user", "content": user}],
                                  required_keys=("requires_revision", "issues"))
        critic_issues = [str(i) for i in obj["issues"]] if isinstance(obj["issues"], list) else []
        if obj["requires_revision"] and critic_issues:
            issues += [{"code": "unsupported_claim", "message": i} for i in critic_issues[:5]]

        requires = bool(issues)
        review = {
            "requires_revision": requires, "issues": issues, "codes": [i["code"] for i in issues],
            "coverage": coverage, "model": meta["model"],
            "checks": {"numbers_recomputed": integrity_ok, "sums_reconcile": sums_ok, "mechanism_adds_up": mech_ok, "external_recomputed": external_ok, "not_truncated": not capped,
                       "numbers_grounded": not loose, "directions_consistent": not direction,
                       "all_findings_cited": not missing, "coverage": coverage,
                       "model_critic_flags": len(critic_issues)},
        }
        self.tracer.event(
            self.name, "critique_issued" if requires else "approve_draft", model=meta["model"],
            retry_count=meta["retries"], target_agent="ExecutorAgent" if requires else "PlannerAgent",
            input_summary=f"draft v{draft['version']}",
            output_summary=("; ".join(i["message"] for i in issues) if requires else "all checks passed"),
            checks=review["checks"])
        return review
