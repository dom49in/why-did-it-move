"""Offline rules: a deterministic stand-in for the model, used ONLY when SAMPLE_MODE=true and Nightshift
credentials are absent (see app/llm.py: mode()).

This is not a model and is never logged as one: the model field in every trace line is "offline-rules".
Each role below reads the same prompt a model would receive and applies explicit rules to it:

  planner       parse the metric and months out of the question; order hypotheses (recalled H-MEM drivers first)
  step decider  pick the first OPEN dimension from the carryover block; finish when none is left or the
                level-0 drivers already cover >= 90% of the change
  drafter       write the summary and one claim per finding, using only numbers present in the evidence
  critic        flag causal assertions the data cannot support

Nothing here stores or returns a prewritten answer: every string is built from the prompt it is given.
"""
from __future__ import annotations

import json
import re

from .errors import LLMError

_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4, "may": 5,
    "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9, "sept": 9, "sep": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
_MONTH_RE = re.compile(
    r"\b(january|february|march|april|june|july|august|september|october|november|december|"
    r"jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec)\b(?:[\s,]+(\d{4}))?"
    r"|(?:(?<=\bin )|(?<=\bfrom )|(?<=\bto )|(?<=\bvs )|(?<=\bversus )|(?<=\band )|(?<=\bthan ))\b(may)\b(?:[\s,]+(\d{4}))?"
    r"|\b(\d{4})-(0[1-9]|1[0-2])\b", re.I)

from .schema import METRIC_ALIASES  # noqa: E402  (shared word lists: keep the planner and H-MEM in agreement)
_CAUSAL = re.compile(r"\b(because|due to|caused by|as a result of|thanks to|owing to)\b", re.I)
_DATA_CAUSES = re.compile(r"\b(price increase|pricing|promotion|campaign|marketing|weather|competitor|strike|supply)\b",
                          re.I)


def _label(text: str, label: str):
    """Return the JSON value that follows `LABEL:` in a prompt, or None."""
    m = re.search(rf"{label}:\s*", text)
    if not m:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(text[m.end():])
        return value
    except ValueError:
        return None


def _line(text: str, label: str) -> str | None:
    m = re.search(rf"^{label}:\s*(.*)$", text, re.M)
    return m.group(1).strip() if m else None


def respond(messages: list[dict]) -> dict:
    system = next((m["content"] for m in messages if m["role"] == "system"), "")
    user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    role = (_line(system, "ROLE") or "").strip()
    handler = {"planner": _plan, "markovian-step-decider": _step, "executor-drafter": _draft,
               "evaluator-critic": _critique}.get(role)
    if handler is None:
        raise LLMError(f"Offline rules have no handler for role {role!r}.", stage="llm")
    return handler(user)


# -- planner -------------------------------------------------------------------------------------------
def _resolve_year(month: int, year: int | None, max_year: int, max_month: int) -> int:
    if year:
        return year
    return max_year if month <= max_month else max_year - 1


def _periods(question: str, data_range: str) -> tuple[str, str]:
    lo, hi = data_range.split(" to ")
    max_year, max_month = int(hi[:4]), int(hi[5:7])
    found: list[tuple[int, int]] = []
    for m in _MONTH_RE.finditer(question):
        if m.group(5):
            found.append((int(m.group(5)), int(m.group(6))))
            continue
        name, year = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        month = _MONTHS[name.lower()]
        found.append((_resolve_year(month, int(year) if year else None, max_year, max_month), month))
    # de-duplicate, keep order of mention
    seen, months = set(), []
    for ym in found:
        if ym not in seen:
            seen.add(ym)
            months.append(ym)

    def fmt(ym):
        return f"{ym[0]:04d}-{ym[1]:02d}"

    def prev(ym):
        return (ym[0] - 1, 12) if ym[1] == 1 else (ym[0], ym[1] - 1)

    if len(months) >= 2:
        a, b = months[0], months[1]
        if re.search(r"\bfrom\b", question, re.I) and re.search(r"\bto\b", question, re.I):
            return fmt(a), fmt(b)            # "from July to August": base=July, compare=August
        base, comp = sorted([a, b])         # otherwise: the later month is the one being explained
        return fmt(base), fmt(comp)
    if len(months) == 1:
        return fmt(prev(months[0])), fmt(months[0])
    latest = (max_year, max_month)
    return fmt(prev(latest)), fmt(latest)


def _plan(user: str) -> dict:
    question = (_line(user, "QUESTION") or "").strip()
    schema = _label(user, "SCHEMA_JSON") or {}
    data_range = _line(user, "DATA_RANGE") or ""
    memory = _label(user, "PAST_MEMORY_JSON") or []
    metrics = schema.get("metrics", [])
    words = set(re.findall(r"[a-z]+", question.lower()))
    metric = next((name for name, aliases in METRIC_ALIASES.items() if name in metrics and words & set(aliases)), None)
    if metric is None:
        return {"unsupported": True, "reason": f"No supported metric ({', '.join(metrics)}) is mentioned in the question."}
    if " to " not in data_range:
        return {"unsupported": True, "reason": "The data range is unknown."}
    base, comp = _periods(question, data_range)
    dims = list(schema.get("dimensions", {}))
    prior, ruled = [], []
    for item in memory:
        facts = item.get("facts") or {}
        if facts.get("metric") != metric:
            continue
        if facts.get("kind") == "confirmed_driver" and facts.get("dimension") in dims:
            prior.append((facts["dimension"], f"memory {item.get('id')}: confirmed driver for {facts.get('periods')}"))
        elif facts.get("kind") == "ruled_out" and facts.get("dimension") in dims:
            ruled.append(facts["dimension"])
    ordered, seen = [], set()
    for d, why in prior:
        if d not in seen:
            seen.add(d)
            ordered.append({"dimension": d, "rationale": why})
    for d in dims:
        if d not in seen and d not in ruled:
            seen.add(d)
            ordered.append({"dimension": d, "rationale": "schema order (no memory prior)"})
    for d in ruled:
        if d not in seen:
            seen.add(d)
            ordered.append({"dimension": d, "rationale": "memory: ruled out before, test last"})
    return {"metric": metric, "base_period": base, "compare_period": comp, "hypotheses": ordered,
            "success_criteria": f"Attribute at least 80% of the {metric} change to segments backed by queries."}


# -- Markovian step decider ----------------------------------------------------------------------------
def _step(user: str) -> dict:
    """probe every open dimension, then descend into the best concentrated one, then finish."""
    open_m = re.search(r"OPEN to probe:\s*([^;\n]*)", user)
    open_dims = [d.strip() for d in (open_m.group(1) if open_m else "").split(",") if d.strip() and d.strip() != "none"]
    opt_m = re.search(r"DESCEND options:\s*([^;\n]*)", user)
    options = [o.split(" (")[0].strip() for o in (opt_m.group(1) if opt_m else "").split("),") if o.strip() and o.strip() != "none"]
    if open_dims:
        return {"action": "probe", "dimension": open_dims[0],
                "reason": "offline rule: probe every open dimension before descending"}
    if options:
        return {"action": "descend", "dimension": options[0],
                "reason": "offline rule: descend into the most concentrated probed dimension"}
    return {"action": "finish", "dimension": None, "reason": "offline rule: nothing left to probe or descend into"}


# -- drafter -------------------------------------------------------------------------------------------
def _scope(f: dict) -> str:
    return f["scope"] if f.get("scope") and f["scope"] != "all data" else ""


def _n(x) -> str:
    return f"{abs(float(x)):,.2f}"


def _mechanism_text(f: dict) -> str:
    mech = f.get("mechanism") or {}
    eff = mech.get("effects") or {}
    if not eff:
        return ""
    names = {"volume": "volume", "price": "price", "unit_cost": "unit cost"}
    parts = ", ".join(f"{names[k]} effect {_n(v)}" + (" (negative)" if v < 0 else " (positive)") for k, v in eff.items())
    tail = []
    if mech.get("unit_cost_change_pct") is not None:
        tail.append(f"unit cost changed {mech['unit_cost_change_pct']}%")
    if mech.get("avg_price_change_pct") is not None:
        tail.append(f"average price changed {mech['avg_price_change_pct']}%")
    if mech.get("units_change_pct") is not None:
        tail.append(f"units changed {mech['units_change_pct']}%")
    return f" Mechanism: {parts}" + (f" ({'; '.join(tail)})." if tail else ".")


def _is_driver(f: dict) -> bool:
    return f.get("lift") is None or f["lift"] >= 1.5


def _draft(user: str) -> dict:
    ev = _label(user, "EVIDENCE_JSON")
    if not isinstance(ev, dict):
        raise LLMError("Offline drafter found no EVIDENCE_JSON.", stage="llm")
    total, findings = ev["total"], ev["findings"]
    metric = ev["metric"].replace("_", " ")
    word = "rose" if total["delta"] > 0 else "fell" if total["delta"] < 0 else "was flat"
    pct = f" ({total['pct_change']}%)" if total.get("pct_change") is not None else ""
    parts = [f"{metric.capitalize()} {word} by {_n(total['delta'])}{pct}, from {_n(total['base'])} in "
             f"{ev['base_period']} to {_n(total['compare'])} in {ev['compare_period']}."]
    level0 = [f for f in findings if f["level"] == 0 and f.get("share_of_total") is not None]
    drivers = [f for f in level0 if _is_driver(f)]
    movers = [f for f in level0 if not _is_driver(f)]
    if drivers:
        names = ", ".join(f"{f['dimension']}={f['member']} ({f['share_of_total']:.0%}, lift {f['lift']}x)"
                          if f.get("lift") is not None else f"{f['dimension']}={f['member']} ({f['share_of_total']:.0%})"
                          for f in drivers)
        parts.append(f"The change is concentrated in {names}.")
    if movers:
        names = ", ".join(f"{f['dimension']}={f['member']} ({f['share_of_total']:.0%})" for f in movers)
        parts.append(f"Other contributors moved roughly in line with their size: {names}.")
    deeper = [f for f in findings if f["level"] > 0]
    if deeper:
        d = deeper[0]
        parts.append(f"Within {d['scope']}, {d['dimension']}={d['member']} holds {d['share_of_scope']:.0%} of that scope's change.")
    lead = next((f for f in drivers if (f.get("mechanism") or {}).get("effects")), None)
    if lead:
        eff = lead["mechanism"]["effects"]
        dom = max(eff, key=lambda k: abs(eff[k]))
        parts.append(f"For {lead['dimension']}={lead['member']} the largest effect is {dom.replace('_', ' ')} ({_n(eff[dom])}).")
    if isinstance(ev.get("external"), dict) and ev["external"].get("statement"):
        parts.append(ev["external"]["statement"])
    if ev.get("ruled_out"):
        dims = sorted({r["dimension"] for r in ev["ruled_out"]})
        parts.append(f"No concentrated driver was found by {', '.join(dims)}.")
    if not findings:
        parts.append("No single segment concentrated the change, so it looks broad-based.")
    claims = []
    for f in findings:
        verb = "rose" if f["delta"] > 0 else "fell"
        scope = f" within {_scope(f)}" if _scope(f) else ""
        share = f"{f['share_of_total']:.0%} of the total change" if f.get("share_of_total") is not None else "an unknown share"
        within = f" and {f['share_of_scope']:.0%} of its scope" if f.get("share_of_scope") is not None and f["level"] > 0 else ""
        size = ""
        if f.get("lift") is not None:
            size = (f" (lift {f['lift']}x: it moved faster than its size predicts)" if _is_driver(f)
                    else f" (lift {f['lift']}x: roughly in line with its size)")
        claims.append({"finding_id": f["id"], "statement":
                       f"{f['dimension']}={f['member']}{scope} {verb} by {_n(f['delta'])}, which is {share}{within}{size}."
                       + _mechanism_text(f)})
    return {"summary": " ".join(parts), "claims": claims}


# -- critic --------------------------------------------------------------------------------------------
def _critique(user: str) -> dict:
    draft = _label(user, "DRAFT_JSON") or {}
    texts = [draft.get("summary", "")] + [c.get("statement", "") for c in draft.get("claims", [])]
    issues = []
    for t in texts:
        if _CAUSAL.search(t) and _DATA_CAUSES.search(t):
            issues.append(f"Asserts a business cause the data cannot show: {t[:120]}")
    return {"requires_revision": bool(issues), "issues": issues}
