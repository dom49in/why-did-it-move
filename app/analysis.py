"""Contribution analysis, the evidence ledger and deterministic draft checks. Pure functions over query rows."""
from __future__ import annotations

import re

EPS = 1e-9
CONCENTRATION_THRESHOLD = 0.40   # top same-direction member must hold >= 40% of the scope's change
LIFT_THRESHOLD = 1.5             # ...and move >= 1.5x faster than its size alone predicts (share of change / share of base)
MIN_DRIVER_LIFT = 1.0            # later contributors must at least move in line with their size
COVERAGE_TARGET = 0.80           # level-0 contributors are added until they hold >= 80% of the change
MAX_CONTRIBUTORS = 3


def num(value) -> float:
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def summarise(rows: list[dict]) -> dict:
    members = []
    for r in rows:
        label = "(none)" if r.get("member") is None else str(r["member"])
        base, comp = num(r.get("base_value")), num(r.get("compare_value"))
        members.append({"member": label, "base": base, "compare": comp, "delta": comp - base})
    scope_delta = sum(m["delta"] for m in members)
    base_total = sum(m["base"] for m in members)
    for m in members:
        m["share"] = m["delta"] / scope_delta if abs(scope_delta) > EPS else None
        # lift = share of the change / share of the base: how much faster than its size alone would predict
        base_share = m["base"] / base_total if base_total > EPS and m["base"] > 0 else None
        m["base_share"] = base_share
        m["lift"] = (m["share"] / base_share) if (m["share"] is not None and base_share) else None
    members.sort(key=lambda m: -abs(m["delta"]))
    sign = 1.0 if scope_delta >= 0 else -1.0
    aligned = [m for m in members if m["delta"] * sign > EPS]
    return {
        "members": members,
        "aligned": aligned,
        "scope_delta": scope_delta,
        "base_total": sum(m["base"] for m in members),
        "compare_total": sum(m["compare"] for m in members),
    }


def contributors(summary: dict, max_n: int = MAX_CONTRIBUTORS, target: float = COVERAGE_TARGET) -> list[dict]:
    out, cumulative = [], 0.0
    for m in summary["aligned"]:
        if out and m.get("lift") is not None and m["lift"] < MIN_DRIVER_LIFT:
            continue        # moved less than its size predicts: not a driver (the coverage check may still ask for it)
        out.append(m)
        cumulative += m["share"] or 0.0
        if cumulative >= target or len(out) >= max_n:
            break
    return out


def _round_member(m: dict) -> dict:
    return {"member": m["member"], "base": round(m["base"], 2), "compare": round(m["compare"], 2),
            "delta": round(m["delta"], 2), "share": None if m["share"] is None else round(m["share"], 5),
            "lift": None if m.get("lift") is None else round(m["lift"], 2)}


def observation(dimension: str, scope: list[dict], summary: dict, query_id: str, raw_chars: int) -> dict:
    aligned = summary["aligned"]
    why = ""
    concentrated = False
    if abs(summary["scope_delta"]) <= EPS:
        why = "no net change in scope"
    elif not aligned:
        why = "no member moves in the direction of the net change"
    elif aligned[0]["share"] >= CONCENTRATION_THRESHOLD and (aligned[0].get("lift") is None
                                                              or aligned[0]["lift"] >= LIFT_THRESHOLD):
        concentrated = True
    elif aligned[0]["share"] >= CONCENTRATION_THRESHOLD:
        why = (f"proportional: '{aligned[0]['member']}' holds {aligned[0]['share']:.0%} of the change but its size "
               f"alone predicts that (lift {aligned[0]['lift']:.2f})")
    else:
        why = (f"diffuse: largest same-direction member '{aligned[0]['member']}' "
               f"holds {aligned[0]['share']:.0%} of the scope change")
    return {
        "type": "breakdown",
        "dimension": dimension,
        "scope": [dict(s) for s in scope],
        "scope_delta": round(summary["scope_delta"], 2),
        "member_count": len(summary["members"]),
        "top": [_round_member(m) for m in summary["members"][:5]],
        "concentrated": concentrated,
        "contributors": [_round_member(m) for m in contributors(summary)] if concentrated else [],
        "why": why,
        "query_id": query_id,
        "raw_chars": raw_chars,
    }


class Evidence:
    """Ledger of every query run, with its rows, so claims can be re-verified independently."""

    def __init__(self) -> None:
        self.queries: dict[str, dict] = {}
        self._n = 0

    def add(self, sql: str, rows: list[dict], source: str, dimension: str | None = None,
            scope: list[dict] | None = None, kind: str = "breakdown", truncated: bool = False) -> str:
        qid = f"q{self._n}"
        self._n += 1
        self.queries[qid] = {"sql": sql, "rows": rows, "source": source, "dimension": dimension, "kind": kind,
                             "scope": [dict(s) for s in (scope or [])], "truncated": truncated}
        return qid

    def first(self, kind: str) -> str:
        for qid, q in self.queries.items():
            if q["kind"] == kind:
                return qid
        raise KeyError(f"no {kind} query recorded")

    def public(self) -> list[dict]:
        return [{"query_id": qid, "kind": q["kind"], "sql": q["sql"], "rows_returned": len(q["rows"]),
                 "source": q["source"], "dimension": q["dimension"], "scope": q["scope"],
                 "truncated": q["truncated"]}
                for qid, q in self.queries.items()]


# ---------------------------------------------------------------------------------------------------------
# Deterministic draft checks (run by the EvaluatorAgent regardless of which model wrote the draft)
# ---------------------------------------------------------------------------------------------------------
_DATE = re.compile(r"\b\d{4}-\d{2}(?:-\d{2})?\b|\b(?:19|20)\d{2}\b")
_NUMBER = re.compile(r"(?<![\w.])(\$?)(\d[\d,]*(?:\.\d+)?)\s?(%|k\b|m\b|bn?\b)?", re.I)


def numbers_in_text(text: str) -> list[dict]:
    """Numbers a reader would take as claims. Dates, ids and bare small integers (counts) are ignored."""
    cleaned = _DATE.sub(" ", text)
    out = []
    for m in _NUMBER.finditer(cleaned):
        raw = m.group(2).replace(",", "")
        decimals = len(raw.split(".")[1]) if "." in raw else 0
        suffix = (m.group(3) or "").lower()
        value = float(raw)
        if not suffix and decimals == 0 and value <= 10 and not m.group(1):
            continue
        out.append({"text": m.group(0).strip(), "value": value, "decimals": decimals, "suffix": suffix})
    return out


def _leaf_numbers(obj, acc: set) -> None:
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        acc.add(abs(float(obj)))
    elif isinstance(obj, dict):
        for v in obj.values():
            _leaf_numbers(v, acc)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _leaf_numbers(v, acc)


def ungrounded_numbers(text: str, payload: dict) -> list[str]:
    """Numbers in `text` that are not a rounding of any number in the evidence payload."""
    known: set[float] = set()
    _leaf_numbers(payload, known)
    bad = []
    for n in numbers_in_text(text):
        scale = {"": 1.0, "%": 1.0, "k": 1e3, "m": 1e6, "b": 1e9, "bn": 1e9}[n["suffix"]]
        value, tol = n["value"] * scale, 0.5 * 10 ** (-n["decimals"]) * scale + 1e-9
        ok = False
        for e in known:
            # a share stored as 0.463 may be written "46%" or "46.3%", so compare against e and e*100
            if any(abs(value - c) <= tol for c in (e, e * 100)):
                ok = True
                break
        if not ok:
            bad.append(n["text"])
    return bad


_UP = re.compile(r"\b(rose|risen|rise|increase[sd]?|increasing|grew|grow|grown|growth|higher|gain(?:ed|s)?|up)\b", re.I)
_DOWN = re.compile(r"\b(fell|fallen|fall|decline[sd]?|declining|decrease[sd]?|decreasing|drop(?:ped|s)?|dropped|lower|"
                   r"shrank|shrunk|loss|down)\b", re.I)


def direction_issues(claims: list[dict], findings: list[dict]) -> list[str]:
    """A claim that says a finding went the opposite way to its recorded delta."""
    by_id = {f["id"]: f for f in findings}
    issues = []
    for c in claims:
        f = by_id.get(c["finding_id"])
        if not f or abs(f["delta"]) <= EPS:
            continue
        up, down = bool(_UP.search(c["statement"])), bool(_DOWN.search(c["statement"]))
        if f["delta"] < 0 and up and not down:
            issues.append(f"Claim for {f['id']} says it rose but its delta is {f['delta']}.")
        elif f["delta"] > 0 and down and not up:
            issues.append(f"Claim for {f['id']} says it fell but its delta is +{f['delta']}.")
    return issues


# ---------------------------------------------------------------------------------------------------------
# Mechanism: HOW a segment moved (price / volume / unit cost), exact and checkable
# ---------------------------------------------------------------------------------------------------------
def _pct(new: float, old: float):
    return None if abs(old) < EPS else round((new / old - 1.0) * 100, 2)


def mechanism(row: dict, metric: str) -> dict:
    """Exact price-volume-cost decomposition of a segment's change from its revenue, cost and units.

    gross_margin: delta = volume_effect + price_effect + unit_cost_effect   (identity, not an estimate)
        volume_effect    = (u1 - u0) * (p0 - c0)        p = revenue / unit, c = cost / unit
        price_effect     = u1 * (p1 - p0)
        unit_cost_effect = -u1 * (c1 - c0)
    revenue: delta = volume_effect + price_effect with the same definitions (no cost term).
    units:   delta = units change.
    """
    rev0, rev1 = num(row.get("revenue_base")), num(row.get("revenue_compare"))
    cost0, cost1 = num(row.get("cost_base")), num(row.get("cost_compare"))
    u0, u1 = num(row.get("units_base")), num(row.get("units_compare"))
    out = {
        "revenue": {"base": round(rev0, 2), "compare": round(rev1, 2), "delta": round(rev1 - rev0, 2)},
        "cost": {"base": round(cost0, 2), "compare": round(cost1, 2), "delta": round(cost1 - cost0, 2)},
        "units": {"base": round(u0, 2), "compare": round(u1, 2), "delta": round(u1 - u0, 2)},
        "units_change_pct": _pct(u1, u0),
    }
    if u0 > EPS and u1 > EPS:
        p0, p1, c0, c1 = rev0 / u0, rev1 / u1, cost0 / u0, cost1 / u1
        out["avg_price_change_pct"] = _pct(p1, p0)
        out["unit_cost_change_pct"] = _pct(c1, c0)
        if metric == "gross_margin":
            out["effects"] = {"volume": round((u1 - u0) * (p0 - c0), 2), "price": round(u1 * (p1 - p0), 2),
                              "unit_cost": round(-u1 * (c1 - c0), 2)}
        elif metric == "revenue":
            out["effects"] = {"volume": round((u1 - u0) * p0, 2), "price": round(u1 * (p1 - p0), 2)}
        else:
            out["effects"] = {"volume": round(u1 - u0, 2)}
    else:
        out["avg_price_change_pct"] = out["unit_cost_change_pct"] = None
        out["effects"] = {}
    return out


def dominant_effect(mech: dict) -> str | None:
    eff = mech.get("effects") or {}
    return max(eff, key=lambda k: abs(eff[k])) if eff else None


# ---------------------------------------------------------------------------------------------------------
# External context: does a market-wide series (e.g. an exchange rate) explain the change?
# ---------------------------------------------------------------------------------------------------------
EXTERNAL_MATERIAL_PCT = 1.0     # a monthly average move smaller than this is treated as flat
EXTERNAL_CONCENTRATION_LIFT = 2.0


def external_assessment(raw: dict, findings: list[dict], label: str) -> dict:
    """Deterministic test of one hypothesis. A market-wide series moves every segment together, so if the change is
    concentrated in one segment (high lift) the series cannot be the primary explanation. Correlation context only:
    it never asserts a cause. `raw` is the one-row result of schema.external_sql."""
    b, c = raw.get("base_value"), raw.get("compare_value")
    nb, nc = int(num(raw.get("base_n"))), int(num(raw.get("compare_n")))
    out = {"series": label, "observations_base": nb, "observations_compare": nc}
    if b is None or c is None or nb == 0 or nc == 0 or abs(num(b)) < EPS:
        out.update(verdict="no_data", statement=f"No {label} observations were available for both periods, so it "
                                               f"could not be assessed.")
        return out
    b, c = num(b), num(c)
    pct = round((c / b - 1.0) * 100, 2)
    out.update(base_average=round(b, 4), compare_average=round(c, 4), change_pct=pct)
    l0 = [f for f in findings if f.get("level") == 0 and f.get("share_of_total") is not None]
    lead = max(l0, key=lambda f: abs(f["share_of_total"])) if l0 else None
    head = f"The {label} averaged {round(b, 4)} in the base month and {round(c, 4)} in the compare month ({pct:+.2f}%)."
    if abs(pct) < EXTERNAL_MATERIAL_PCT:
        out.update(verdict="flat", statement=head + " That is too small to explain the change.")
    elif lead is not None and lead.get("lift") is not None and lead["lift"] >= EXTERNAL_CONCENTRATION_LIFT:
        out.update(verdict="concentrated", lead_finding=lead["id"], lead_share=lead["share_of_total"],
                   lead_lift=lead["lift"],
                   statement=head + f" A market-wide move touches every segment, but the change is concentrated in "
                                    f"{lead['dimension']}={lead['member']} ({lead['share_of_total']:.0%} of the change, "
                                    f"lift {lead['lift']}x), so it is not the primary explanation.")
    else:
        out.update(verdict="broad",
                   statement=head + " The change is spread across segments, so this is consistent with a broad "
                                    "contributor. This is correlation only and does not prove a cause.")
    return out
