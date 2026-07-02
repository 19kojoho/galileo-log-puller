# upstream-example — wrap `chat_with_guardrail` for Envoy

`protect_quickstart_envoy_example.py` is a ~50-line FastAPI service that imports the existing `chat_with_guardrail` function from `protect_quickstart_example.py` in the repo root and exposes it as an OpenAI-compatible HTTP endpoint. Nothing in the existing quickstart is modified — this file just adapts the shape.

## Run it

From the repo root:

```bash
# once
pip install -r envoy-addon/upstream-example/requirements.txt

# every time
export GALILEO_API_KEY=<yours>
export GALILEO_PROJECT=<yours>
export GALILEO_LOG_STREAM=<yours>
export OPENAI_API_KEY=<yours>
export GALILEO_PROTECT_STAGE=poc-protect-stage      # or whatever you named yours

uvicorn envoy-addon.upstream-example.protect_quickstart_envoy_example:app \
    --host 0.0.0.0 --port 8000
```

Verify directly (bypassing Envoy) — this should work exactly like the CLI:

```bash
curl -sS http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"protect-quickstart","messages":[{"role":"user","content":"What is a Python decorator?"}]}'
```

## Point Envoy at it

The addon's `docker-compose.yml` binds Docker's `host.docker.internal` to your host's `localhost`. Set in `envoy-addon/.env`:

```
UPSTREAM_URL=http://host.docker.internal:8000
```

Then in `envoy-addon/envoy/envoy.yaml`, the `mock_llm` cluster's `address:` and `port_value:` need to match. Change:

```yaml
socket_address:
  address: mock-llm      # ← change to host.docker.internal
  port_value: 8000       # ← keep 8000
```

Restart Envoy:

```bash
docker compose -f envoy-addon/docker-compose.yml restart envoy
```

Now `http://localhost:8080/v1/chat/completions` runs through Envoy → shim → Protect → back through Envoy → your wrapper → SDK-inline Protect → OpenAI. Two Protect calls per request, two separate traces, both browsable.

## What this doesn't try to do

- Doesn't try to look like a full agent framework — it's the smallest possible wrapper so you can see the flow end-to-end
- Doesn't remove or modify the SDK-inline Protect in `chat_with_guardrail` — that stays as-is
- Doesn't add its own OpenAI-schema validation — the wrapper just extracts the last user message and hands it to your existing function
