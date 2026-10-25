"""H-MEM (Python subset of the Meterless H-MEM spec, engines/hmem/AGENTS.md).

Implemented, following the spec's numbers exactly (section in brackets):
  - memory model with mandatory `source` and `provenance`, three layers, entity/related/supersede links [2]
  - direct-capture mining of verified run results (the spec's no-model fallback path) [4.3]
  - hybrid retrieval: raw = .35 semantic + .20 keyword + .10 tag + .10 domain + .15 entity + .05 layer
    + .05 recency - .20 superseded; score = clamp01(raw) x confidence; threshold .35; top 5 [6.1]
  - domain-grouped reinjection block plus a trace (retrievalReason, per-memory score, strategy) [6.2]
  - feedback deltas: helpful +.05 (+1 access), not_helpful -.03, wrong -.20 (+ `review` tag) [9.2]
  - append-only trust ledger using the 17 canonical action types (hash-chained: an extra, for tamper evidence) [9.1]
  - supersede links: the loser is kept and penalised in ranking, never deleted [10.2, 6.1]
  - sleep: preview-first, guardrails, backup before execute, consolidate + archive, restore [8]

NOT implemented: LLM-based mining, dreaming, conflict detection, sleep synthesis (merge), real embeddings.
The "embedding" is a hashed bag-of-words vector, which the spec allows ("lightweight embedding"); it is
close to lexical matching, so semantic similarity is weaker than with a learned embedder.

The TypeScript conformance runner cannot import this module, so conformance is NOT claimed. tests/test_hmem.py
checks the formulas, deltas, thresholds and never-happens behaviours that the spec fully determines.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import threading
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

LAYERS = ("short_term", "working", "long_term")
LAYER_WEIGHT = {"long_term": 1.0, "working": 0.8, "short_term": 0.6}
TYPES = ("personal", "factual", "preference", "general")
LEDGER_ACTIONS = (
    "create", "read", "update", "delete", "feedback", "promote", "demote", "merge", "conflict_detected",
    "conflict_resolved", "sleep_consolidate", "sleep_archive", "sleep_synthesize", "dream_proposed",
    "dream_approved", "dream_rejected", "restore",
)
WEIGHTS = {"semantic": 0.35, "keyword": 0.20, "tag": 0.10, "domain": 0.10, "entity": 0.15,
           "layer": 0.05, "recency": 0.05, "superseded": 0.20}
RETRIEVAL_THRESHOLD = 0.35
RETRIEVAL_TOP_N = 5
RECENCY_TAU_DAYS = 14.0
FEEDBACK_DELTAS = {"helpful": 0.05, "not_helpful": -0.03, "wrong": -0.20}
CONSOLIDATE_AFTER_DAYS = 7
ARCHIVE_AFTER_DAYS = 30
ARCHIVE_MAX_ACCESS = 2
SYNTHESIS_SIMILARITY = 0.82
ENTITY_CAP = 10
RELATED_CAP = 5
EMBED_DIM = 128
MIN_CONTENT_CHARS = 8

_STOP = frozenset("a an and are as at be by did do does for from how in is it its of on or than that the this to "
                  "was were what when where which who why with vs versus".split())
_WORD = re.compile(r"[a-z0-9][a-z0-9_.\-]*")


def tokenize(text: str) -> list[str]:
    out = []
    for raw in _WORD.findall(text.lower()):
        for part in re.split(r"[_.\-]+", raw):
            if len(part) >= 2 and part not in _STOP:
                out.append(part)
    return out


def embed(text: str) -> list[float]:
    """Hashed bag-of-words vector (L2-normalised). Deterministic, dependency-free."""
    vec = [0.0] * EMBED_DIM
    for tok, n in Counter(tokenize(text)).items():
        h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
        vec[h % EMBED_DIM] += (1.0 if (h >> 64) & 1 else -1.0) * (1.0 + math.log(n))
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec] if norm else vec


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))  # both L2-normalised


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


@dataclass
class Memory:
    id: str
    content: str
    type: str
    layer: str
    timestamp: str
    last_accessed: str
    access_count: int
    tags: list
    embedding: list
    confidence: float
    source: str
    provenance: dict
    domain: str = "general"
    namespace: str = "general"
    entities: list = field(default_factory=list)
    related_to: list = field(default_factory=list)
    supersedes: str | None = None
    superseded_by: str | None = None
    derived_from: list = field(default_factory=list)
    facts: dict = field(default_factory=dict)   # structured payload (what was mined), not part of the spec model

    def to_dict(self) -> dict:
        return asdict(self)

    def public(self) -> dict:
        d = self.to_dict()
        d.pop("embedding")
        return d

    @staticmethod
    def from_dict(d: dict) -> "Memory":
        return Memory(**d)


class HMem:
    """Store + mining + retrieval + trust ledger + sleep services behind one object."""

    def __init__(self, directory: Path | str, clock: Callable[[], datetime] | None = None):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.store_path = self.dir / "memories.json"
        self.ledger_path = self.dir / "ledger.jsonl"
        self.backup_dir = self.dir / "backups"
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._memories: dict[str, Memory] = {}
        self._last_hash = "GENESIS"
        self._seq = 0
        self._load()

    # -- persistence --------------------------------------------------------------------------
    def now(self) -> datetime:
        return self._clock()

    def _load(self) -> None:
        if self.store_path.exists():
            data = json.loads(self.store_path.read_text())
            self._memories = {m["id"]: Memory.from_dict(m) for m in data.get("memories", [])}
        if self.ledger_path.exists():
            for line in self.ledger_path.read_text().splitlines():
                if line.strip():
                    entry = json.loads(line)
                    self._last_hash, self._seq = entry["hash"], entry["seq"]

    def _save(self) -> None:
        tmp = self.store_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": 1, "memories": [m.to_dict() for m in self._memories.values()]}))
        os.replace(tmp, self.store_path)

    # -- trust ledger (9.1) -----------------------------------------------------------------------
    def _log(self, memory_id: str, action: str, actor: str, previous: dict | None = None,
             new: dict | None = None, details: dict | None = None) -> dict:
        if action not in LEDGER_ACTIONS:
            raise ValueError(f"unknown ledger action {action!r}")
        self._seq += 1
        entry = {"seq": self._seq, "memoryId": memory_id, "action": action, "actor": actor,
                 "timestamp": _iso(self.now()), "previousState": previous, "newState": new,
                 "details": details or {}, "prevHash": self._last_hash}
        entry["hash"] = hashlib.sha256(json.dumps(entry, sort_keys=True, default=str).encode()).hexdigest()
        with self.ledger_path.open("a") as fh:     # append-only: never rewritten or pruned here
            fh.write(json.dumps(entry, default=str) + "\n")
        self._last_hash = entry["hash"]
        return entry

    def ledger(self, memory_id: str | None = None) -> list[dict]:
        if not self.ledger_path.exists():
            return []
        rows = [json.loads(l) for l in self.ledger_path.read_text().splitlines() if l.strip()]
        return [r for r in rows if memory_id is None or r["memoryId"] == memory_id]

    def verify_ledger(self) -> dict:
        prev = "GENESIS"
        for row in self.ledger():
            body = {k: v for k, v in row.items() if k != "hash"}
            digest = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
            if row["prevHash"] != prev or digest != row["hash"]:
                return {"ok": False, "broken_at_seq": row["seq"]}
            prev = row["hash"]
        return {"ok": True, "entries": self._seq}

    # -- store + mining (3, 4.3, 5) ---------------------------------------------------------------
    def get(self, memory_id: str) -> Memory | None:
        return self._memories.get(memory_id)

    def all(self) -> list[Memory]:
        return list(self._memories.values())

    def add(self, content: str, *, source: str, type: str = "factual", layer: str = "working",
            confidence: float = 0.8, tags: list[str] | None = None, domain: str = "general",
            namespace: str | None = None, entities: list[str] | None = None, origin: str = "plan_completion",
            label: str = "", facts: dict | None = None, replace_key: str | None = None,
            actor: str = "agent") -> Memory:
        if not isinstance(source, str) or not source.strip():
            raise ValueError("source is mandatory: a memory with no source is rejected by construction")
        if not isinstance(content, str) or len(content.strip()) < MIN_CONTENT_CHARS:
            raise ValueError(f"content must be a string of at least {MIN_CONTENT_CHARS} characters")
        if type not in TYPES:
            raise ValueError(f"type must be one of {TYPES}")
        if layer not in LAYERS:
            raise ValueError(f"layer must be one of {LAYERS}")
        now = _iso(self.now())
        ents = []
        for e in entities or []:
            if str(e).lower() not in (x.lower() for x in ents):
                ents.append(str(e))
        with self._lock:
            mem = Memory(
                id=f"mem-{uuid.uuid4().hex[:10]}", content=content.strip(), type=type, layer=layer,
                timestamp=now, last_accessed=now, access_count=0,
                tags=sorted({t.lower() for t in (tags or [])}), embedding=embed(content),
                confidence=clamp01(confidence), source=source.strip(),
                provenance={"origin": origin, "learnedAt": now, "label": label or source},
                domain=domain, namespace=namespace or domain, entities=ents[:ENTITY_CAP],
                facts=dict(facts or {}))
            if replace_key:
                mem.facts["key"] = replace_key
                for old in self._memories.values():
                    if old.facts.get("key") == replace_key and old.superseded_by is None:
                        mem.supersedes = old.id
                        break
            self._memories[mem.id] = mem
            self._link_related(mem)
            if mem.supersedes:
                old = self._memories[mem.supersedes]
                before = {"supersededBy": old.superseded_by}
                old.superseded_by = mem.id
                self._log(old.id, "update", actor, before, {"supersededBy": mem.id},
                          {"reason": "same key re-learned; kept and penalised in ranking"})
            self._log(mem.id, "create", actor, None, {"layer": mem.layer, "confidence": mem.confidence},
                      {"source": mem.source, "origin": origin})
            self._save()
            return mem

    def _link_related(self, mem: Memory) -> None:
        mine = {e.lower() for e in mem.entities}
        for other in self._memories.values():
            if other.id == mem.id or other.domain != mem.domain or not mine & {e.lower() for e in other.entities}:
                continue
            if len(mem.related_to) < RELATED_CAP and other.id not in mem.related_to:
                mem.related_to.append(other.id)
            if len(other.related_to) < RELATED_CAP and mem.id not in other.related_to:
                other.related_to.append(mem.id)

    # -- retrieval + reinjection (6) ----------------------------------------------------------------
    def score(self, mem: Memory, query: str, q_emb: list[float], q_tokens: list[str], q_entities: list[str],
              domain_aliases: dict | None = None) -> dict:
        top8 = [t for t, _ in Counter(q_tokens).most_common(8)]
        q_set, m_set = set(top8), set(tokenize(mem.content))
        keyword = len(q_set & m_set) / len(q_set | m_set) if (q_set | m_set) else 0.0
        all_q = set(q_tokens)
        tag = (len({t for t in mem.tags if t in all_q or any(p in all_q for p in tokenize(t))}) / len(mem.tags)
               if mem.tags else 0.0)
        aliases = (domain_aliases or {}).get(mem.domain)
        if aliases:      # caller knows words that signal this domain: any one of them is enough
            domain = 1.0 if all_q & set(aliases) else 0.0
        else:            # otherwise the domain name itself must appear (every word of it)
            dom_tokens = set(tokenize(mem.domain))
            domain = 1.0 if dom_tokens and dom_tokens <= all_q else 0.0
        mem_ents = [e.lower() for e in mem.entities]
        q_ents = {e.lower() for e in q_entities}
        entity = len(q_ents & set(mem_ents)) / len(mem_ents) if mem_ents else 0.0
        days = max((self.now() - _parse(mem.last_accessed)).total_seconds() / 86400.0, 0.0)
        comp = {
            "semantic": max(0.0, cosine(q_emb, mem.embedding)),
            "keyword": keyword, "tag": tag, "domain": domain, "entity": entity,
            "layer": LAYER_WEIGHT[mem.layer],
            "recency": math.exp(-days / RECENCY_TAU_DAYS),
            "superseded": 1.0 if mem.superseded_by else 0.0,
        }
        raw = (WEIGHTS["semantic"] * comp["semantic"] + WEIGHTS["keyword"] * comp["keyword"]
               + WEIGHTS["tag"] * comp["tag"] + WEIGHTS["domain"] * comp["domain"]
               + WEIGHTS["entity"] * comp["entity"] + WEIGHTS["layer"] * comp["layer"]
               + WEIGHTS["recency"] * comp["recency"] - WEIGHTS["superseded"] * comp["superseded"])
        return {"raw": raw, "score": clamp01(raw) * mem.confidence, "components": comp}

    def retrieve(self, query: str, *, entities: list[str] | None = None, top_n: int = RETRIEVAL_TOP_N,
                 threshold: float = RETRIEVAL_THRESHOLD, actor: str = "agent",
                 domain_aliases: dict | None = None) -> dict:
        q_tokens = tokenize(query)
        q_emb = embed(query)
        # entities mentioned in the query: any known memory entity that appears in the query text
        lowered = query.lower()
        known = {e for m in self._memories.values() for e in m.entities if e.lower() in lowered}
        q_entities = list({*(entities or []), *known})
        with self._lock:
            scored = []
            for mem in self._memories.values():
                s = self.score(mem, query, q_emb, q_tokens, q_entities, domain_aliases)
                scored.append((s["score"], mem, s))
            scored.sort(key=lambda x: -x[0])
            hits = [(sc, m, s) for sc, m, s in scored if sc >= threshold][:top_n]
            now = _iso(self.now())
            for sc, mem, s in hits:
                mem.last_accessed = now
                self._log(mem.id, "read", actor, None, None, {"score": round(sc, 4), "query": query[:120]})
            if hits:
                self._save()
        groups: dict[str, list[str]] = {}
        for sc, mem, _ in hits:
            ent = f" [{', '.join(mem.entities)}]" if mem.entities else ""
            groups.setdefault(mem.domain, []).append(f"- ({mem.layer}) {mem.content}{ent}")
        context = "\n".join(f"[{d}]\n" + "\n".join(lines) for d, lines in groups.items())
        trace = {
            "retrievalReason": (f"{len(hits)} of {len(scored)} memories scored >= {threshold} "
                                f"(hybrid formula, confidence-weighted)") if hits else
                               f"no memory scored >= {threshold} out of {len(scored)}",
            "strategy": "none" if not hits else ("minimal" if len(hits) <= 2 else "comprehensive"),
            "threshold": threshold, "scored": len(scored),
            "returned": [{"id": m.id, "score": round(sc, 4)} for sc, m, _ in hits],
        }
        return {
            "memories": [{**m.public(), "score": round(sc, 4),
                          "components": {k: round(v, 4) for k, v in s["components"].items()}} for sc, m, s in hits],
            "context": context, "trace": trace,
        }

    # -- feedback (9.2) -------------------------------------------------------------------------------
    def feedback(self, memory_id: str, signal: str, actor: str = "agent", note: str = "") -> Memory:
        if signal not in FEEDBACK_DELTAS:
            raise ValueError(f"signal must be one of {sorted(FEEDBACK_DELTAS)}")
        with self._lock:
            mem = self._memories.get(memory_id)
            if mem is None:
                raise KeyError(memory_id)
            before = {"confidence": mem.confidence, "accessCount": mem.access_count, "layer": mem.layer}
            mem.confidence = clamp01(mem.confidence + FEEDBACK_DELTAS[signal])
            if signal == "helpful":
                mem.access_count += 1
            if signal == "wrong" and "review" not in mem.tags:
                mem.tags = sorted({*mem.tags, "review"})
            after = {"confidence": mem.confidence, "accessCount": mem.access_count, "layer": mem.layer}
            self._log(mem.id, "feedback", actor, before, after, {"signal": signal, "note": note[:200]})
            # our promotion rule (the spec names `promote` but sets no threshold): proven, reused knowledge
            if signal == "helpful" and mem.layer == "working" and mem.access_count >= 3 and mem.confidence >= 0.85:
                mem.layer = "long_term"
                self._log(mem.id, "promote", actor, {"layer": "working"}, {"layer": "long_term"},
                          {"rule": "access_count>=3 and confidence>=0.85"})
            self._save()
            return mem

    # -- sleep (8) ----------------------------------------------------------------------------------------
    def _referenced_ids(self) -> set[str]:
        refs: set[str] = set()
        for m in self._memories.values():
            refs.update(m.related_to)
            refs.update(m.derived_from)
            if m.supersedes:
                refs.add(m.supersedes)
        return refs

    def sleep_preview(self) -> dict:
        now, refs = self.now(), self._referenced_ids()
        consolidate, archive, synth = [], [], []
        for m in self._memories.values():
            idle = (now - _parse(m.last_accessed)).days
            age = (now - _parse(m.timestamp)).days
            if m.layer == "short_term" and idle >= CONSOLIDATE_AFTER_DAYS:
                consolidate.append(m.id)
            guarded = bool(m.supersedes and not m.superseded_by) or bool(m.derived_from) or m.id in refs
            if age >= ARCHIVE_AFTER_DAYS and m.access_count <= ARCHIVE_MAX_ACCESS and not guarded:
                archive.append(m.id)
        mems = self.all()
        for i, a in enumerate(mems):
            for b in mems[i + 1:]:
                if cosine(a.embedding, b.embedding) >= SYNTHESIS_SIMILARITY:
                    synth.append([a.id, b.id])
        return {"toConsolidate": consolidate, "toArchive": archive, "toSynthesize": synth,
                "note": "synthesis is previewed only; executing it is not implemented"}

    def sleep_execute(self, actor: str = "sleep") -> dict:
        with self._lock:
            plan = self.sleep_preview()
            backup_id = f"backup-{self.now().strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:4]}"
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            if self.store_path.exists():
                shutil.copy(self.store_path, self.backup_dir / f"{backup_id}.json")
            for mid in plan["toConsolidate"]:
                m = self._memories[mid]
                self._log(mid, "sleep_consolidate", actor, {"layer": m.layer}, {"layer": "long_term"})
                m.layer = "long_term"
            for mid in plan["toArchive"]:
                m = self._memories.pop(mid)
                self._log(mid, "sleep_archive", actor, {"layer": m.layer, "accessCount": m.access_count}, None,
                          {"backup": backup_id})
            self._save()
            return {"backup_id": backup_id, "consolidated": len(plan["toConsolidate"]),
                    "archived": len(plan["toArchive"]), "synthesized": 0, "plan": plan}

    def restore(self, backup_id: str, actor: str = "user") -> int:
        path = self.backup_dir / f"{backup_id}.json"
        if not path.exists():
            raise KeyError(backup_id)
        with self._lock:
            data = json.loads(path.read_text())
            self._memories = {m["id"]: Memory.from_dict(m) for m in data["memories"]}
            self._log("*", "restore", actor, None, {"memories": len(self._memories)}, {"backup": backup_id})
            self._save()
            return len(self._memories)
