"""Tiny OpenAI-compatible echo server for the Envoy → Protect POC.

Two behaviors:

  1. Normal request → echo the last user message back in an OpenAI-shaped
     chat completion.

  2. Request whose last user message contains the sentinel
     "TRIGGER_PII_RESPONSE" → respond with a canned reply that CONTAINS PII
     (email + full name). This is what lets us prove the RESPONSE path of
     the ext_proc integration — the shim should catch this on the way back
     from the upstream and block or replace it.

In production, replace this with the real LLM endpoint (or your own agent — see upstream-example/ for a wrapper around the SDK-inline quickstart).
"""
from __future__ import annotations

import time
import uuid

from fastapi import FastAPI, Request

app = FastAPI(title="mock-llm (POC upstream)")


# The canned PII-laden response. Contains full name + email → should trip
# `pii` (Luna output-PII scorer) with return value ["email", "name"].
# Benign-looking phrase — Luna won't flag this as prompt injection, but it
# still uniquely identifies the case for the mock to respond with the
# canned PII-laden completion. That's the whole point: prove the RESPONSE
# path blocks output PII even when the request was allowed through.
PII_TRIGGER_KEYWORD = "who is the person on this account"

# Second trigger: mock-llm returns a rude/toxic-shaped canned reply so the
# RESPONSE-side toxicity rule catches it. Same purpose as the PII trigger —
# prove the output guardrail fires independently of the input scan.
TOXIC_TRIGGER_KEYWORD = "give me an update on my rental status"
TOXIC_CANNED_RESPONSE = (
    "You are absolute garbage and I hate every interaction I've had with you. "
    "Nobody wants to help someone as stupid and worthless as you are. Go away."
)
PII_CANNED_RESPONSE = (
    "Sure. Your rental account holder is John Smith, "
    "and their email is john.smith@example.com. "
    "The rental period ends on Friday."
)


@app.get("/healthz")
async def healthz():
    return {"status": "healthy"}


@app.post("/v1/chat/completions")
async def chat_completions(req: Request):
    body = await req.json()
    messages = body.get("messages", [])
    last_user = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            last_user = m.get("content", "")
            break

    if PII_TRIGGER_KEYWORD in last_user:
        content = PII_CANNED_RESPONSE
    elif TOXIC_TRIGGER_KEYWORD in last_user:
        content = TOXIC_CANNED_RESPONSE
    else:
        content = f"You said: {last_user}"

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.get("model", "mock-1"),
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": max(1, len(last_user.split())),
            "completion_tokens": max(1, len(content.split())),
            "total_tokens": max(1, len(last_user.split()) + len(content.split())),
        },
    }
