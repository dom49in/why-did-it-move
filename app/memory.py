"""Domain glue between the agents and the H-MEM engine (app/engines/hmem.py).

Planner side: recall past confirmed drivers and ruled-out dimensions for a question, as HINTS (never evidence).
Finalise side: score each recalled memory against what this run actually found (feedback), then mine this
run's verified findings into new memories. Every step is written to H-MEM's trust ledger.
"""
from __future__ import annotations

from pathlib import Path

from . import config
from .engines.hmem import HMem
from .schema import METRIC_ALIASES


def default_dir() -> Path:
    base = Path(config.get("MEMORY_DIR", ".memory"))
    return (base if base.is_absolute() else config.ROOT / base) / "hmem"


_MONTH_NAMES = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
                "november", "december"]


def period_label(period: str) -> str:
    """'2026-09' -> 'September 2026', so memories match how people actually ask ("September versus August")."""
    return f"{_MONTH_NAMES[int(period[5:7]) - 1].capitalize()} {period[:4]}"


def _scope_key(scope: list[dict]) -> str:
    return "|".join(f"{s['dimension']}={s['member']}" for s in scope) or "(all)"


class InvestigationMemory:
    def __init__(self, hmem: HMem | None = None, directory: Path | str | None = None):
        self.enabled = (config.get("MEMORY_ENABLED", "true") or "true").lower() != "false"
        self.hmem = hmem if hmem is not None else (HMem(directory or default_dir()) if self.enabled else None)

    @staticmethod
    def threshold() -> float:
        """Spec default is 0.35 (HMem.RETRIEVAL_THRESHOLD). The spec says to tune per domain: here memories are short,
        machine-written and questions rarely name an entity (the 0.15 entity term is then 0), so the best
        relevant memory scores ~0.29. Default 0.25; override with HMEM_THRESHOLD."""
        return config.get_float("HMEM_THRESHOLD", 0.25)

    # -- recall ---------------------------------------------------------------------------------
    def recall(self, query: str) -> dict:
        if not self.enabled or self.hmem is None:
            return {"memories": [], "context": "", "trace": {"retrievalReason": "memory disabled", "returned": []}}
        return self.hmem.retrieve(query, actor="PlannerAgent", threshold=self.threshold(),
                                  domain_aliases=METRIC_ALIASES)

    @staticmethod
    def hints(recall: dict, limit: int = 5) -> list[dict]:
        return [{"id": m["id"], "score": m["score"], "content": m["content"][:240], "facts": m.get("facts", {}),
                 "confidence": m["confidence"]} for m in recall["memories"][:limit]]

    # -- feedback on recalled memories ------------------------------------------------------------
    def feedback_on_outcome(self, recalled: list[dict], metric: str, findings: list[dict], ruled_out: list[dict],
                            analysed: set[tuple[str, str]]) -> list[dict]:
        """helpful when the prior still holds on today's data, not_helpful when the data contradicts it.
        `analysed` is the set of (dimension, scope_key) breakdowns this run actually queried."""
        if not self.enabled or self.hmem is None:
            return []
        found = {(f["dimension"], f["member"], _scope_key(f["scope"])) for f in findings}
        found_dims = {(f["dimension"], _scope_key(f["scope"])) for f in findings}
        ruled = {(r["dimension"], r["scope"]) for r in ruled_out}
        out = []
        for m in recalled:
            facts = m.get("facts") or {}
            if facts.get("metric") != metric:
                continue
            signal = None
            if facts.get("kind") == "confirmed_driver":
                key = (facts["dimension"], facts["member"], _scope_key(facts.get("scope", [])))
                if key in found:
                    signal = "helpful"
                elif (facts["dimension"], _scope_key(facts.get("scope", []))) in analysed:
                    signal = "not_helpful"
            elif facts.get("kind") == "ruled_out":
                key2 = (facts["dimension"], facts.get("scope_key", "(all)"))
                if key2 in ruled:
                    signal = "helpful"
                elif key2 in found_dims:
                    signal = "not_helpful"
            if signal:
                mem = self.hmem.feedback(m["id"], signal, actor="PlannerAgent",
                                         note="prior checked against this run's queries")
                out.append({"memory_id": m["id"], "signal": signal, "confidence": round(mem.confidence, 3)})
        return out

    # -- mining ---------------------------------------------------------------------------------------
    def store_run(self, plan: dict, findings: list[dict], ruled_out: list[dict], unresolved: bool) -> list[str]:
        if not self.enabled or self.hmem is None:
            return []
        metric, base, comp = plan["metric"], plan["base_period"], plan["compare_period"]
        periods = f"{base}->{comp}"
        label = metric.replace("_", " ")
        haircut = 0.8 if unresolved else 1.0
        stored = []
        for f in findings:
            if f.get("share_of_total") is None:
                continue
            scope = f["scope"]
            where = " AND ".join(f"{s['dimension']}={s['member']}" for s in scope)
            where = (where + " AND ") if where else ""
            share = f["share_of_total"]
            eff = (f.get("mechanism") or {}).get("effects") or {}
            dom = max(eff, key=lambda k: abs(eff[k])).replace("_", " ") + " effect" if eff else "n/a"
            content = (f"Why {label} changed in {period_label(comp)} versus {period_label(base)} ({periods}): "
                       f"{where}{f['dimension']}={f['member']} accounted for {share:.0%} of the change "
                       f"(delta {f['delta']:+,.2f}); dominant mechanism: {dom}.")
            mem = self.hmem.add(
                content, source="evaluator_approved_run" if not unresolved else "unresolved_run",
                type="factual", layer="working", confidence=round(haircut * (0.6 + 0.35 * min(abs(share), 1.0)), 2),
                tags=[metric, "driver", f["dimension"], period_label(base), period_label(comp)],
                domain=metric, namespace=f"{metric}/drivers",
                entities=[f["member"], *[s["member"] for s in scope]], origin="plan_completion",
                label=f"driver for {periods}",
                facts={"kind": "confirmed_driver", "metric": metric, "periods": periods, "dimension": f["dimension"],
                       "member": f["member"], "scope": scope, "share_of_total": share, "delta": f["delta"]},
                replace_key=f"driver|{metric}|{periods}|{_scope_key(scope)}|{f['dimension']}={f['member']}",
                actor="PlannerAgent")
            stored.append(mem.id)
        for r in ruled_out:
            content = (f"Why {label} changed in {period_label(comp)} versus {period_label(base)} ({periods}): "
                       f"dimension {r['dimension']} (scope {r['scope']}) was ruled out as a driver "
                       f"({str(r['why'])[:80]}).")
            mem = self.hmem.add(
                content, source="evaluator_approved_run" if not unresolved else "unresolved_run",
                type="factual", layer="working", confidence=round(haircut * 0.7, 2),
                tags=[metric, "ruled_out", r["dimension"], period_label(base), period_label(comp)],
                domain=metric, namespace=f"{metric}/ruled_out",
                entities=[], origin="plan_completion", label=f"ruled out for {periods}",
                facts={"kind": "ruled_out", "metric": metric, "periods": periods, "dimension": r["dimension"],
                       "scope_key": r["scope"]},
                replace_key=f"ruled|{metric}|{periods}|{r['scope']}|{r['dimension']}", actor="PlannerAgent")
            stored.append(mem.id)
        return stored
