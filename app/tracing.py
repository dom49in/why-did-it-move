"""JSONL trace logging. Every agent action, handoff and model call is written to logs/ and stdout."""
from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import config


def short(value, limit: int = 220) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[: limit - 3] + "..."


class Tracer:
    def __init__(self, trace_id: str | None = None, log_dir: Path | str | None = None, echo: bool = True):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        self.trace_id = trace_id or f"trace-{stamp}-{uuid.uuid4().hex[:8]}"
        directory = Path(log_dir or config.get("LOG_DIR", str(config.ROOT / "logs")))
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"{self.trace_id}.jsonl"
        self.echo = echo and (config.get("LOG_STDOUT", "true") or "true").lower() != "false"
        self.events: list[dict] = []
        self.models_used: list[str] = []

    def event(self, agent_name: str, action: str, input_summary="", output_summary="",
              target_agent: str | None = None, model: str | None = None,
              confidence: float | None = None, retry_count: int = 0,
              status: str = "success", **extra) -> dict:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "trace_id": self.trace_id,
            "agent_name": agent_name,
            "action": action,
            "input_summary": short(input_summary),
            "output_summary": short(output_summary),
            "target_agent": target_agent,
            "model": model,
            "confidence": confidence,
            "retry_count": retry_count,
            "status": status,
        }
        record.update(extra)
        if model and model not in self.models_used:
            self.models_used.append(model)
        self.events.append(record)
        line = json.dumps(record, default=str)
        with self.path.open("a") as fh:
            fh.write(line + "\n")
        if self.echo:
            print(line, file=sys.stdout, flush=True)
        return record

    def agents_seen(self) -> list[str]:
        seen: list[str] = []
        for e in self.events:
            if e["agent_name"] not in seen:
                seen.append(e["agent_name"])
        return seen
