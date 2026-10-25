"""Entry point.

    python run.py                                start the API server (POST /run on port 8000)
    python run.py --input input_examples/example_1.json    run one request and print the JSON response

Routes
    GET  /                     web UI (single page; calls POST /run)
    POST /run                  the agent: {"request_id", "user_query", "context"} -> explanation + collaboration + engines
    GET  /health               liveness, data source and reasoning mode
    GET  /memory               H-MEM memories (without embeddings) and a ledger integrity check
    POST /memory/feedback      {"memory_id", "signal": helpful|not_helpful|wrong}
    POST /memory/sleep         {"apply": false} preview (default) or {"apply": true} execute with backup
    GET  /engine/markovian     cumulative Markovian run stats from persisted history

Plain ASGI on uvicorn: no web framework dependency.
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

from app import config

config.load_dotenv()

from app import llm  # noqa: E402
from app.errors import AppError  # noqa: E402
from app.memory import InvestigationMemory  # noqa: E402
from app.pipeline import history_path, run_request  # noqa: E402
from app.engines.markovian import RunHistory  # noqa: E402

_memory: InvestigationMemory | None = None


def get_memory() -> InvestigationMemory:
    global _memory
    if _memory is None:
        _memory = InvestigationMemory()
    return _memory


def _err(status: int, kind: str, message: str) -> tuple[int, dict]:
    return status, {"status": "error", "error": {"type": kind, "message": message, "stage": "request"}}


def handle(method: str, path: str, body: bytes) -> tuple[int, dict]:
    """Pure request handler (also used directly by the tests)."""
    try:
        if method == "GET" and path == "/health":
            try:
                mode = llm.mode()
            except AppError as exc:
                mode = f"misconfigured: {exc.message}"
            return 200, {"status": "ok", "sample_mode": config.sample_mode(), "reasoning_mode": mode}
        if method == "GET" and path == "/memory":
            mem = get_memory()
            if mem.hmem is None:
                return 200, {"enabled": False, "memories": []}
            return 200, {"enabled": True, "memories": [m.public() for m in mem.hmem.all()],
                         "ledger": mem.hmem.verify_ledger()}
        if method == "GET" and path == "/engine/markovian":
            return 200, RunHistory(history_path()).cumulative()
        if method == "POST" and path in ("/run", "/memory/feedback", "/memory/sleep"):
            try:
                payload = json.loads(body or b"{}")
            except ValueError:
                return _err(400, "invalid_request", "Body must be valid JSON.")
            if path == "/run":
                response, status = run_request(payload, memory=get_memory())
                return status, response
            mem = get_memory()
            if mem.hmem is None:
                return _err(409, "memory_disabled", "MEMORY_ENABLED=false")
            if path == "/memory/feedback":
                try:
                    m = mem.hmem.feedback(payload["memory_id"], payload["signal"], actor="user")
                except KeyError:
                    return _err(404, "not_found", "Unknown memory_id.")
                except (ValueError, TypeError) as exc:
                    return _err(422, "invalid_request", str(exc))
                return 200, {"status": "ok", "memory": m.public()}
            if payload.get("apply") is True:
                return 200, {"status": "ok", "executed": mem.hmem.sleep_execute(actor="user")}
            return 200, {"status": "ok", "preview": mem.hmem.sleep_preview()}
        return _err(404, "not_found", f"No route for {method} {path}.")
    except Exception:  # noqa: BLE001 - never leak a traceback to the client
        return 500, {"status": "error", "error": {"type": "internal_error", "message": "Unexpected error.",
                                                  "stage": "unknown", "details": {}}}


async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    if scope["type"] != "http":
        return
    body = b""
    more = True
    while more:
        message = await receive()
        body += message.get("body", b"")
        more = message.get("more_body", False)
    if scope["method"] == "GET" and scope["path"] in ("/", "/ui"):
        page = (Path(__file__).parent / "app" / "ui.html").read_bytes()
        await send({"type": "http.response.start", "status": 200, "headers": [
            (b"content-type", b"text/html; charset=utf-8"), (b"content-length", str(len(page)).encode())]})
        await send({"type": "http.response.body", "body": page})
        return
    status, payload = await asyncio.to_thread(handle, scope["method"], scope["path"], body)
    data = json.dumps(payload, default=str).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(data)).encode())]})
    await send({"type": "http.response.body", "body": data})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", help="run a single request from a JSON file and print the response")
    parser.add_argument("--output", help="also write the response JSON (without the log lines) to this file")
    args = parser.parse_args()
    if args.input:
        with open(args.input) as fh:
            response, status = run_request(json.load(fh), memory=get_memory())
        text = json.dumps(response, indent=2)
        if args.output:
            with open(args.output, "w") as fh:
                fh.write(text + "\n")
        print(text)
        return 0 if status == 200 else 1
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(config.get("PORT", "8000")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
