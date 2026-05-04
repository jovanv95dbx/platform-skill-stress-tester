#!/usr/bin/env python3
"""Persona relay — stateful chat broker between SA agents and Ollama.

Why: keep the persona system prompt and the running conversation history
OUT of the SA agent's context. The SA never sees the persona definition;
it just posts its next reply and gets the customer's next reply back.

Architecture:
  SA agent   ──HTTP──▶  this relay (port 11435)  ──HTTP──▶  Ollama (port 11434)
                          │ holds messages[] per persona
                          │ uses pre-baked `<name>-persona` Ollama models

Endpoints:
  POST /chat/<persona>         — start or continue a session.
       body: {} OR {"text": "<SA's next reply>"}
       returns: {"customer": "...", "turn": N, "persona": "<name>"}
       First call (no `text`) sends the canonical opener and returns the customer's intro.
       Subsequent calls require `text`.

  GET  /history/<persona>      — debug: returns full messages array.
  POST /reset/<persona>        — clear session.
  GET  /health                 — liveness.

Stdlib only. No deps. Run: python3 persona_relay.py [--port 11435]
"""

import argparse
import datetime as _dt
import json
import os
import sys
import threading
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OLLAMA_URL = "http://localhost:11434/api/chat"
OPENER = (
    "You are chatting with a Databricks Solutions Architect over text. "
    "Introduce yourself and explain what you need in 2-4 sentences. Stay in character."
)

# In-memory session store. Maps persona -> list of {role, content}.
# Per-persona lock to avoid concurrent updates clobbering history.
_sessions: dict[str, list[dict]] = {}
_locks: dict[str, threading.Lock] = {}
_global_lock = threading.Lock()

# Disk-persistence config (set in main()).
_log_dir: str | None = None


def _utcnow() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _append_log(persona: str, turn: int, role: str, content: str) -> None:
    """Append a single chat event to <log_dir>/<persona>-chat.jsonl. No-op if log_dir unset."""
    if not _log_dir:
        return
    try:
        path = os.path.join(_log_dir, f"{persona}-chat.jsonl")
        rec = {"ts": _utcnow(), "persona": persona, "turn": turn, "role": role, "content": content}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001
        # Best-effort logging; don't kill a chat over a disk error.
        sys.stderr.write(f"[relay] log write failed for {persona}: {e}\n")


def _get_lock(persona: str) -> threading.Lock:
    with _global_lock:
        if persona not in _locks:
            _locks[persona] = threading.Lock()
        return _locks[persona]


def _ollama_chat(model: str, messages: list[dict]) -> str:
    payload = json.dumps({
        "model": model,
        "stream": False,
        "messages": messages,
    }).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_URL, data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        body = json.loads(r.read())
    return body["message"]["content"]


class Handler(BaseHTTPRequestHandler):
    # Quiet down access log
    def log_message(self, fmt, *args):  # noqa: ARG002
        return

    def _send_json(self, status: int, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def do_GET(self):
        if self.path == "/health":
            return self._send_json(200, {"ok": True})
        if self.path.startswith("/history/"):
            persona = self.path[len("/history/"):]
            with _get_lock(persona):
                msgs = list(_sessions.get(persona, []))
            return self._send_json(200, {"persona": persona, "messages": msgs})
        return self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path.startswith("/reset/"):
            persona = self.path[len("/reset/"):]
            with _get_lock(persona):
                _sessions.pop(persona, None)
            return self._send_json(200, {"ok": True, "persona": persona})

        if self.path.startswith("/chat/"):
            persona = self.path[len("/chat/"):]
            body = self._read_body()
            text = body.get("text")

            with _get_lock(persona):
                msgs = _sessions.setdefault(persona, [])
                sa_text_for_log: str | None = None

                if not msgs:
                    # First turn — send the opener as user role.
                    msgs.append({"role": "user", "content": OPENER})
                    sa_text_for_log = "<<OPENER>>"
                else:
                    # Subsequent turn — agent must provide its reply.
                    if not text:
                        return self._send_json(400, {
                            "error": "session already started; provide 'text' with your SA reply"
                        })
                    msgs.append({"role": "user", "content": text})
                    sa_text_for_log = text

                model = f"{persona}-persona"
                try:
                    reply = _ollama_chat(model, msgs)
                except urllib.error.HTTPError as e:
                    return self._send_json(502, {
                        "error": "ollama HTTP error",
                        "code": e.code,
                        "detail": e.read().decode("utf-8", "replace"),
                    })
                except Exception as e:  # noqa: BLE001
                    return self._send_json(500, {"error": str(e)})

                msgs.append({"role": "assistant", "content": reply})
                turn_number = sum(1 for m in msgs if m["role"] == "assistant")

                # Persist this turn pair to disk (sa -> customer).
                _append_log(persona, turn_number, "sa", sa_text_for_log or "")
                _append_log(persona, turn_number, "customer", reply)

            return self._send_json(200, {
                "persona": persona,
                "turn": turn_number,
                "customer": reply,
            })

        return self._send_json(404, {"error": "not found"})


def main():
    global _log_dir
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=11435)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--log-dir",
        default=None,
        help="If set, append every chat turn to <log-dir>/<persona>-chat.jsonl",
    )
    args = parser.parse_args()

    if args.log_dir:
        os.makedirs(args.log_dir, exist_ok=True)
        _log_dir = os.path.abspath(args.log_dir)
        print(f"persona-relay logging chat turns to {_log_dir}", flush=True)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"persona-relay listening on http://{args.host}:{args.port}", flush=True)
    print(f"  POST /chat/<persona>     -- start or continue a session", flush=True)
    print(f"  GET  /history/<persona>  -- debug: full history", flush=True)
    print(f"  POST /reset/<persona>    -- clear session", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
