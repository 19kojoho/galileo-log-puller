"""Envoy-compatible wrapper around the existing chat_with_guardrail function.

The existing `protect_quickstart_example.py` in the repo root is a CLI that
runs `chat_with_guardrail` against a couple of test prompts. Envoy needs an
HTTP endpoint to sit in front of, so this file wraps the SAME function in a
tiny FastAPI service with an OpenAI-compatible /v1/chat/completions route.

Nothing in the existing code is modified. This file only imports from
protect_quickstart_example and adapts the return shape.

Run
---
    pip install -r upstream-example/requirements.txt
    uvicorn upstream-example.protect_quickstart_envoy_example:app --host 0.0.0.0 --port 8000

Then point the envoy-addon docker-compose UPSTREAM_URL at
`http://host.docker.internal:8000` (or run this in-container and use a
service name).

Behavior
--------
- POST /v1/chat/completions       → runs chat_with_guardrail on the last user
                                    message. Returns OpenAI-shape.
- GET  /healthz                   → 200 for docker-compose healthcheck.

Two guardrails will fire on every request:
  1. Envoy's ext_proc → shim → Protect (network-layer, checks BEFORE this
     wrapper is even called)
  2. This wrapper's chat_with_guardrail → invoke_protect (SDK-inline,
     checks INSIDE the wrapper)

Both land as separate traces in the same log stream. Defense in depth.
"""
from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path

# Make the sibling protect_quickstart_example importable when we run from the
# repo root via uvicorn.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from fastapi import FastAPI, Request

# Reuse the existing function verbatim — no re-implementation, no drift.
from protect_quickstart_example import chat_with_guardrail  # noqa: E402


app = FastAPI(title="galileo-log-puller — envoy-addon upstream example")


@app.get("/healthz")
async def healthz():
    return {"status": "healthy"}


@app.post("/v1/chat/completions")
async def chat_completions(req: Request):
    body = await req.json()
    messages = body.get("messages", [])

    # Pull the last user message the same way an OpenAI-compatible route
    # would. This is what chat_with_guardrail expects.
    last_user = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            last_user = m.get("content", "")
            break

    # The existing function does everything: SDK-inline Protect,
    # LLM call (if allowed), formatted reply.
    reply = chat_with_guardrail(last_user)

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.get("model", "protect-quickstart"),
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": reply},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": max(1, len(last_user.split())),
            "completion_tokens": max(1, len(reply.split())),
            "total_tokens": max(1, len(last_user.split()) + len(reply.split())),
        },
    }
