"""Markovian engine (Python implementation of the Meterless Markovian spec, engines/markovian/AGENTS.md).

A long investigation is run as a sequence of bounded steps ("chunks"). Each step's prompt contains only
the fixed instructions, the bounded carryover block and the latest observation. Earlier raw observations
are never replayed, so prompt size stays flat as the chain grows.

What follows the spec (AGENTS.md section in brackets):
  - ChunkConfig with the spec's bounds, defaults and load-time validation; overlapTokens != 0 is rejected
    rather than silently ignored [2.1, 14]
  - the canonical COMPLETED / REMAINING / DECISIONS / CONTEXT carryover block [3]
  - the carryover compression cascade, in strict order explicit -> llm -> heuristic -> truncation [4.3]
  - ChunkInfo / MarkovianRun records with a mandatory per-run ChunkConfig snapshot [2.2, 2.3]
  - run efficiency and the symbolic cost model, formula for formula [6.3, docs/efficiency-model.md]
  - cumulative stats from persisted run history [2.4, 4.7]
  - token numbers labelled "estimated" (ceil(chars/4)) or "measured" (provider usage) [14]

Where this implementation deliberately differs (the problem is a structured investigation, not free text):
  - The state is authored by code from validated findings, not by a model emitting [STATE_CHECKPOINT]
    markers, so cascade level 1 is called "explicit" (spec name: "marker"). Its acceptance rule is the same
    (>= 24 chars and within the carryover budget).
  - Cascade level 3 first tries progressively smaller renderings of the same state, then the spec's
    key-phrase regex.
  - Mode prompts, streaming, the reflection pass, [OVERLAP], pause markers and the Engine-tab UI are not
    implemented; the reflection role is played by the EvaluatorAgent in app/agents.py.

The TypeScript conformance runner cannot import this module, so conformance is NOT claimed. Instead,
tests/test_markovian.py checks run_efficiency / model_totals / resolve_config against vectors produced by
the real TypeScript reference (tests/fixtures/markovian_reference_vectors.json).
"""
from __future__ import annotations

import copy
import json
import math
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

# ---------------------------------------------------------------------------------------------------------
# Config (spec 2.1)
# ---------------------------------------------------------------------------------------------------------
FRAMING_TOKENS = 400
OUTPUT_BUDGET = 1200
BOUNDS = {
    "chunk_size": (1000, 128000),
    "max_chunks": (1, 32),
    "carryover_tokens": (128, 32768),
    "overlap_tokens": (0, 4096),
}
MAX_OBSERVATION_CHARS = 1500
MAX_FILTERS = 2           # deepest drill-down: at most two scope filters
MAX_FINDINGS = 8
MAX_RULED_OUT = 8


class ConfigValidationError(ValueError):
    """Typed error for an invalid ChunkConfig. Never silently truncates."""


@dataclass(frozen=True)
class ChunkConfig:
    chunk_size: int = 3000        # target generation budget per chunk (tokens)
    max_chunks: int = 12          # chain depth cap
    carryover_tokens: int = 256   # carryover state budget (tokens); real blocks run ~150-250
    overlap_tokens: int = 0       # [OVERLAP] window; non-zero is rejected (not implemented)

    def as_dict(self) -> dict:
        return asdict(self)


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / 4)


def resolve_config(**partial) -> ChunkConfig:
    cfg = ChunkConfig(**{**ChunkConfig().as_dict(), **partial})
    for name, (lo, hi) in BOUNDS.items():
        value = getattr(cfg, name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ConfigValidationError(f"{name} must be a number")
        if value < lo or value > hi:
            raise ConfigValidationError(f"{name}={value} outside [{lo}, {hi}]")
    if cfg.carryover_tokens <= 0:
        raise ConfigValidationError("carryover_tokens must be > 0")
    if cfg.chunk_size < FRAMING_TOKENS + cfg.carryover_tokens + OUTPUT_BUDGET:
        raise ConfigValidationError(
            f"chunk_size={cfg.chunk_size} < framing({FRAMING_TOKENS}) + carryover({cfg.carryover_tokens}) "
            f"+ outputBudget({OUTPUT_BUDGET})")
    if cfg.overlap_tokens != 0:
        raise ConfigValidationError(
            "overlap_tokens != 0 is not implemented here; the spec requires implementing [OVERLAP] or rejecting it")
    return cfg


# ---------------------------------------------------------------------------------------------------------
# Efficiency math (spec 4.1, 6.3 and docs/efficiency-model.md)
# ---------------------------------------------------------------------------------------------------------
def run_efficiency(chunk_tokens: list[int], cfg: ChunkConfig) -> dict:
    """Historical run efficiency, always against the run's OWN config snapshot."""
    total_used = sum(chunk_tokens)
    standard, prefix = 0, 0
    for t in chunk_tokens:
        standard += prefix + t
        prefix += t
    markovian = len(chunk_tokens) * cfg.carryover_tokens + total_used
    saved = max(0, standard - markovian)
    return {
        "total_tokens_used": total_used,
        "standard_cost": standard,
        "markovian_cost": markovian,
        "tokens_saved": saved,
        "efficiency_percent": (saved / standard * 100) if standard > 0 else 0.0,
    }


DOC_DEFAULTS = {"F": 400, "G": 200, "S": 300, "O": 1200, "C": 800}


def model_totals(n: int, k: dict | None = None) -> dict:
    """Symbolic input-token cost model: naive grows ~N^2, Markovian ~N."""
    k = k or DOC_DEFAULTS
    naive = n * (k["F"] + k["G"] + k["S"]) + (k["O"] * n * (n - 1)) / 2
    markov = n * (k["F"] + k["G"] + k["C"] + k["S"])
    savings = naive - markov
    return {"naive_total": naive, "markovian_total": markov, "savings": savings,
            "efficiency": savings / naive}


def projection_curve(cfg: ChunkConfig, steps: int = 10) -> list[dict]:
    return [{"step": s, "standard": (s - 1) * cfg.chunk_size + cfg.chunk_size,
             "markovian": cfg.carryover_tokens + cfg.chunk_size} for s in range(1, steps + 1)]


# ---------------------------------------------------------------------------------------------------------
# Carryover (spec 3, 4.3)
# ---------------------------------------------------------------------------------------------------------
_KEY_PHRASE = re.compile(r"(?:^|\n|\.\s+)((?:Therefore|Key insight|Status|Decision|Decided|Next|Remaining|Completed)[^.\n]*)",
                         re.I)


def canonical_block(completed: str, remaining: str, decisions: str, context: str) -> str:
    return f"COMPLETED: {completed}\nREMAINING: {remaining}\nDECISIONS: {decisions}\nCONTEXT: {context}"


class CarryoverService:
    """Compression cascade in strict order: explicit -> llm -> heuristic -> truncation."""

    def __init__(self, compressor_fn: Callable[[str, str], str] | None = None):
        self.compressor_fn = compressor_fn

    def next(self, explicit: str | None, fallbacks: list[str], previous: str, chunk_text: str,
             cfg: ChunkConfig) -> tuple[str, str]:
        budget = cfg.carryover_tokens
        if explicit is not None:
            sanitized = explicit.strip()
            if len(sanitized) >= 24 and estimate_tokens(sanitized) <= budget:
                return sanitized, "explicit"
        if self.compressor_fn is not None:
            try:
                preview = chunk_text[-2000:]
                prompt = (f"Previous state:\n{previous or '(none)'}\n\nLatest chunk (tail):\n{preview}\n\n"
                          "Compress into 3-5 critical points as a labeled block:\n"
                          "COMPLETED: ...\nREMAINING: ...\nDECISIONS: ...\nCONTEXT: ...")
                compressed = self.compressor_fn(prompt, "You compress reasoning state. Be terse and lossless on decisions.").strip()
                if len(compressed) >= 24 and estimate_tokens(compressed) <= budget:
                    return compressed, "llm"
            except Exception:  # noqa: BLE001 - the cascade exists to survive model/API instability
                pass
        for candidate in fallbacks:
            if len(candidate) >= 24 and estimate_tokens(candidate) <= budget:
                return candidate, "heuristic"
        phrases = [m.group(1).strip() for m in _KEY_PHRASE.finditer(chunk_text)][:5]
        if phrases:
            summary = "; ".join(phrases)
            if estimate_tokens(summary) <= budget:
                return summary, "heuristic"
        words = (chunk_text or previous).split()
        tail = ""
        for w in reversed(words):
            candidate = w + (" " + tail if tail else "")
            if estimate_tokens(candidate) > budget:
                break
            tail = candidate
        return tail, "truncation"


# ---------------------------------------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------------------------------------
def scope_key(scope: list[dict]) -> str:
    return "|".join(f"{s['dimension']}={s['member']}" for s in scope) or "(all)"


def _scope_text(scope: list[dict]) -> str:
    return " AND ".join(f"{s['dimension']}={s['member']}" for s in scope) or "all data"


@dataclass
class MarkovState:
    objective: str
    metric: str
    base_period: str
    compare_period: str
    total_delta: float
    hypotheses: dict
    order: list
    budget_steps: int = 8
    step: int = 0
    scope: list = field(default_factory=list)
    open_dimensions: list = field(default_factory=list)
    ruled_out: list = field(default_factory=list)
    findings: list = field(default_factory=list)
    finding_seq: int = 0
    dropped_findings: int = 0
    dropped_ruled_out: int = 0
    stopped: str = ""
    probed: dict = field(default_factory=dict)        # dimension -> compact result of probing it in the CURRENT scope
    implied_by: dict = field(default_factory=dict)    # child dimension -> parent dimension (e.g. product -> category)

    def descend_options(self) -> list[tuple[str, float]]:
        """Probed dimensions in the current scope whose change is concentrated, best first."""
        opts = [(d, p["top_share"]) for d, p in self.probed.items() if p.get("concentrated")]
        # a parent level (category) always looks more concentrated than its child (product): prefer the specific one
        drop = {self.implied_by[d] for d, _ in opts if d in self.implied_by}
        return sorted([o for o in opts if o[0] not in drop], key=lambda x: -x[1])

    def covered(self) -> float:
        """Share of the total change already attributed by level-0 findings."""
        return sum((f.get("share_of_total") or 0.0) for f in self.findings if f["level"] == 0)


def _render(state: MarkovState, level: int) -> str:
    """Render the state as the canonical block. Higher `level` = smaller rendering."""
    keep_find = [MAX_FINDINGS, 4, 2, 1][level]
    keep_ruled = [MAX_RULED_OUT, 4, 2, 0][level]
    finds = sorted(state.findings, key=lambda f: -abs(f.get("share_of_total") or 0))[:keep_find]
    done = "; ".join(
        f"{f['id']} {_scope_text(f['scope']) + ' > ' if f['scope'] else ''}{f['dimension']}={f['member']} "
        f"delta {f['delta']} ({(f.get('share_of_total') or 0):.0%} of total)" for f in finds) or "no findings yet"
    completed = f"{state.step} step(s) done; covered {state.covered():.0%} of total change; findings: {done}"
    probed = "; ".join(f"{d} top={p['top_member']} {p['top_share']:.0%} ({'concentrated' if p['concentrated'] else 'diffuse'})"
                       for d, p in state.probed.items()) or "none yet"
    options = ", ".join(f"{d} ({sh:.0%})" for d, sh in state.descend_options()) or "none"
    remaining = (f"scope: {_scope_text(state.scope)}; OPEN to probe: {', '.join(state.open_dimensions) or 'none'}; "
                 f"PROBED here: {probed}; DESCEND options: {options}; "
                 f"steps left: {max(state.budget_steps - state.step, 0)}")
    ruled = state.ruled_out[-keep_ruled:] if keep_ruled else []
    decisions = "; ".join(f"ruled out {r['dimension']} in {r['scope']} ({str(r['why'])[:70]})" for r in ruled) or "none"
    objective = state.objective[:200 if level == 0 else 60]
    hyp = "" if level >= 2 else "; hypotheses: " + ", ".join(
        f"{d} ({r[:60] if level == 0 else r[:20]})" for d, r in state.hypotheses.items())
    context = (f"objective: {objective}; metric {state.metric}, {state.base_period} -> {state.compare_period}; "
               f"total delta {round(state.total_delta, 2)}{hyp}")
    return canonical_block(completed, remaining, decisions, context)


def _size(obj) -> int:
    return len(json.dumps(obj, separators=(",", ":"), default=str))


def _bound_observation(observation: dict) -> dict:
    obs = {k: v for k, v in observation.items() if k != "raw_chars"}
    for key in ("top", "contributors"):
        while _size(obs) > MAX_OBSERVATION_CHARS and obs.get(key):
            obs[key] = obs[key][:-1]
    return obs


# ---------------------------------------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------------------------------------
class MarkovianEngine:
    SYSTEM = (
        "ROLE: markovian-step-decider\n"
        "You are the next-action policy of a bounded-state investigation of why a business metric changed. "
        "You see only the CARRYOVER block and the latest observation, never the full history.\n"
        "Reply with ONLY a JSON object: "
        '{"action": "probe" | "descend" | "finish", "dimension": "<see rules>", "reason": "<max 20 words>"}.\n'
        "Rules:\n"
        "- probe: query one dimension from 'OPEN to probe' in the current scope. This does not change the scope.\n"
        "- descend: commit to one dimension listed in 'DESCEND options'; the investigation then continues inside its "
        "top member. Probe every open dimension first, then descend into the option with the highest share.\n"
        "- finish: when there is nothing left to probe and no descend option, or the remaining steps are better saved.\n"
        "Use the hypotheses and what has already been ruled out. Never name a dimension that is not listed."
    )

    def __init__(self, config: ChunkConfig | None = None,
                 compressor_fn: Callable[[str, str], str] | None = None) -> None:
        self.config = config or resolve_config()
        self.carryover = CarryoverService(compressor_fn)
        self.chunks: list[dict] = []          # ChunkInfo records (spec 2.2)
        self.prompt_chars: list[int] = []
        self.cascade_levels: list[str] = []
        self.measured_prompt_tokens: list[int | None] = []
        self.fallbacks = 0
        self._previous_carry = ""
        self._pending: dict | None = None
        self._t0 = time.time()

    # -- state ---------------------------------------------------------------------------------
    def init_state(self, objective: str, metric: str, base_period: str, compare_period: str, total_delta: float,
                   hypotheses: list[dict], all_dimensions: list[str],
                   implied_by: dict | None = None) -> MarkovState:
        order = [h["dimension"] for h in hypotheses if h["dimension"] in all_dimensions]
        order += [d for d in all_dimensions if d not in order]
        state = MarkovState(
            objective=objective, metric=metric, base_period=base_period, compare_period=compare_period,
            total_delta=total_delta,
            hypotheses={h["dimension"]: str(h.get("rationale", "")) for h in hypotheses
                        if h["dimension"] in all_dimensions},
            order=order, budget_steps=self.config.max_chunks, implied_by=dict(implied_by or {}))
        state.open_dimensions = self._open(state)
        return state

    @staticmethod
    def _open(state: MarkovState) -> list[str]:
        """Dimensions still to probe in the current scope. A parent level (category) is implied by its child
        (product) once the child is in scope, so probing it would only restate the scope."""
        used = {s["dimension"] for s in state.scope}
        implied = {state.implied_by[d] for d in used if d in state.implied_by}
        return [d for d in state.order if d not in used and d not in implied and d not in state.probed]

    # -- prompts -------------------------------------------------------------------------------
    def build_carryover(self, state: MarkovState, chunk_text: str = "") -> tuple[str, str]:
        explicit = _render(state, 0)
        fallbacks = [_render(state, lvl) for lvl in (1, 2, 3)]
        text, level = self.carryover.next(explicit, fallbacks, self._previous_carry, chunk_text, self.config)
        self._previous_carry = text
        self.cascade_levels.append(level)
        return text, level

    def build_step_prompt(self, state: MarkovState, observation: dict) -> list[dict]:
        obs_text = json.dumps(_bound_observation(observation), separators=(",", ":"), default=str)
        carry, level = self.build_carryover(state, obs_text)
        user = (f"CARRYOVER (canonical block, <= {self.config.carryover_tokens} tokens):\n{carry}\n"
                f"LATEST_OBSERVATION_JSON:\n{obs_text}")
        self.prompt_chars.append(len(self.SYSTEM) + len(user))
        self._pending = {"carryover": carry, "level": level}
        return [{"role": "system", "content": self.SYSTEM}, {"role": "user", "content": user}]

    def note_usage(self, usage: dict | None) -> None:
        tokens = usage.get("prompt_tokens") if isinstance(usage, dict) else None
        self.measured_prompt_tokens.append(tokens if isinstance(tokens, int) else None)

    def record_step(self, raw_observation_chars: int, bounded_observation: dict, decision: dict) -> dict:
        """Persist this step as a ChunkInfo (spec 2.2). tokens = ceil(chars/4) of everything the step
        produced: the decision text plus the raw query result it triggered (what history replay would carry)."""
        decision_text = json.dumps(decision, separators=(",", ":"), default=str)
        content = decision_text + "\n" + json.dumps(_bound_observation(bounded_observation),
                                                   separators=(",", ":"), default=str)
        chunk = {
            "id": len(self.chunks) + 1,
            "tokens": math.ceil((raw_observation_chars + len(decision_text)) / 4),
            "carryover": (self._pending or {}).get("carryover", ""),
            "content": content,
        }
        self.chunks.append(chunk)
        return chunk

    # -- transition ----------------------------------------------------------------------------
    def transition(self, state: MarkovState, action: dict, observation: dict) -> MarkovState:
        """Pure state update.

        probe   : record what probing `dimension` showed in the current scope (scope unchanged); a diffuse
                  dimension is also logged as ruled out.
        descend : turn the probed dimension's contributors into findings and narrow the scope to its top member.
        finish  : no change except the step counter.
        """
        s = copy.deepcopy(state)
        s.step += 1
        kind, dimension = action.get("action"), action.get("dimension")
        if kind == "probe" and dimension:
            contribs = observation.get("contributors") or []
            top = (observation.get("top") or [{}])[0]
            concentrated = bool(observation.get("concentrated") and contribs)
            s.probed[dimension] = {
                "top_member": (contribs[0] if concentrated else top).get("member", "?"),
                "top_share": float((contribs[0] if concentrated else top).get("share") or 0.0),
                "concentrated": concentrated, "query_id": observation.get("query_id"),
            }
            if not concentrated:
                s.ruled_out.append({"dimension": dimension, "scope": scope_key(s.scope),
                                    "why": observation.get("why") or "diffuse"})
        elif kind == "descend" and dimension:
            contribs = observation.get("contributors") or []
            depth = len(s.scope)
            for c in (contribs if depth == 0 else contribs[:1]):
                s.finding_seq += 1
                share_total = c["delta"] / s.total_delta if abs(s.total_delta) > 1e-9 else None
                s.findings.append({
                    "id": f"f{s.finding_seq}", "level": depth, "dimension": dimension, "member": c["member"],
                    "scope": [dict(x) for x in s.scope], "delta": round(c["delta"], 2),
                    "base": c["base"], "compare": c["compare"], "lift": c.get("lift"),
                    "share_of_scope": c["share"],
                    "share_of_total": None if share_total is None else round(share_total, 5),
                    "query_id": observation["query_id"],
                })
            if contribs:
                s.scope.append({"dimension": dimension, "member": contribs[0]["member"]})
                s.probed = {}
                if len(s.scope) >= MAX_FILTERS:
                    s.stopped = "max_depth"
        if len(s.findings) > MAX_FINDINGS:
            s.findings.sort(key=lambda f: (f["level"], -abs(f.get("share_of_total") or 0)))
            s.dropped_findings += len(s.findings) - MAX_FINDINGS
            s.findings = s.findings[:MAX_FINDINGS]
        if len(s.ruled_out) > MAX_RULED_OUT:
            s.dropped_ruled_out += len(s.ruled_out) - MAX_RULED_OUT
            s.ruled_out = s.ruled_out[-MAX_RULED_OUT:]
        s.open_dimensions = self._open(s)
        return s

    @staticmethod
    def should_stop(state: MarkovState) -> tuple[bool, str]:
        if state.stopped:
            return True, state.stopped
        if state.step >= state.budget_steps:
            return True, "step_budget"
        if not state.open_dimensions and not state.descend_options():
            return True, "exhausted"
        return False, ""

    # -- records and measurement -----------------------------------------------------------------
    @staticmethod
    def status_for(stopped: str) -> str:
        return "max-chunks" if stopped == "step_budget" else "completed"

    def stats(self) -> dict:
        tokens = [c["tokens"] for c in self.chunks]
        eff = run_efficiency(tokens, self.config)
        measured_ok = bool(self.measured_prompt_tokens) and all(t is not None for t in self.measured_prompt_tokens)
        carry_actual = [estimate_tokens(c["carryover"]) for c in self.chunks]
        # same standard cost, but Markovian charged for the carryover each step ACTUALLY carried, not the budget
        actual_cost = sum(carry_actual) + eff["total_tokens_used"]
        actual_saved = max(0, eff["standard_cost"] - actual_cost)
        return {
            "name": "markovian",
            "implementation": "python, spec-aligned (see app/engines/markovian.py); TS conformance not run",
            "chunk_config": self.config.as_dict(),
            "steps": len(self.chunks),
            "prompt_chars_per_step": list(self.prompt_chars),
            "max_prompt_chars": max(self.prompt_chars, default=0),
            "carryover_cascade_levels": list(self.cascade_levels),
            "carryover_tokens_per_step": [estimate_tokens(c["carryover"]) for c in self.chunks],
            "chunk_tokens": tokens,
            "efficiency": eff,
            "efficiency_using_actual_carryover": {
                "markovian_cost": actual_cost, "tokens_saved": actual_saved,
                "efficiency_percent": (actual_saved / eff["standard_cost"] * 100) if eff["standard_cost"] else 0.0},
            "token_label": "estimated",
            "measured_prompt_tokens_per_step": list(self.measured_prompt_tokens) if measured_ok else None,
            "action_fallbacks": self.fallbacks,
            "note": ("efficiency uses spec 6.3: standard cost replays every prior chunk; Markovian cost is "
                     "steps x carryover_tokens + tokens used. It can be 0 for short chains."),
        }

    def run_record(self, prompt: str, status: str, duration_ms: int) -> dict:
        eff = run_efficiency([c["tokens"] for c in self.chunks], self.config)
        return {
            "id": str(uuid.uuid4()),
            "timestamp": int(time.time() * 1000),
            "prompt": prompt[:300],
            "mode": "RESEARCH",
            "chunkConfig": self.config.as_dict(),
            "chunks": [{**c, "carryover": c["carryover"][:800], "content": c["content"][:800]} for c in self.chunks],
            "totalTokensUsed": eff["total_tokens_used"],
            "totalTokensSaved": eff["tokens_saved"],
            "efficiencyPercent": round(eff["efficiency_percent"], 2),
            "durationMs": duration_ms,
            "status": status,
            "tokenLabel": "estimated",
        }


class RunHistory:
    """Persistent run history and cumulative stats (spec 2.4, 4.7). JSONL, one run per line."""

    def __init__(self, path: Path | str):
        self.path = Path(path)

    def record(self, run: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as fh:
            fh.write(json.dumps(run, default=str) + "\n")

    def load(self) -> list[dict]:
        if not self.path.exists():
            return []
        runs = []
        for line in self.path.read_text().splitlines():
            try:
                runs.append(json.loads(line))
            except ValueError:
                continue
        return runs

    def cumulative(self) -> dict:
        runs = self.load()
        done = [r for r in runs if r.get("status") == "completed"]
        used = sum(r.get("totalTokensUsed", 0) for r in runs)
        saved = sum(r.get("totalTokensSaved", 0) for r in runs)
        return {
            "totalRuns": len(runs),
            "completedRuns": len(done),
            "totalTokensUsed": used,
            "totalTokensSaved": saved,
            "averageEfficiency": (sum(r.get("efficiencyPercent", 0) for r in runs) / len(runs)) if runs else 0.0,
            "totalChunksProcessed": sum(len(r.get("chunks", [])) for r in runs),
            "lastRunTimestamp": runs[-1]["timestamp"] if runs else 0,
            "efficiencyHistory": [{"timestamp": r["timestamp"], "efficiency": r.get("efficiencyPercent", 0),
                                   "tokensSaved": r.get("totalTokensSaved", 0)} for r in runs[-50:]],
        }
