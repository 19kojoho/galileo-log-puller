"""protect-shim — Envoy ext_proc → Galileo Invoke Protect bridge.

Overview
--------
Envoy's `ext_proc` HTTP filter is a bidirectional gRPC stream. Envoy sends
ProcessingRequest messages (request_headers, request_body, response_headers,
response_body). The processor replies with ProcessingResponse messages that
either say "keep going" (CommonResponse) or "stop here" (ImmediateResponse
with an HTTP status + body).

This service implements that stream and, on each buffered body message,
calls Galileo Invoke Protect to get a policy verdict:

    - TRIGGERED  → return ImmediateResponse with HTTP 403 and a JSON body
                   describing which rule fired. Envoy stops here and never
                   forwards the request to the upstream (or, on the response
                   path, replaces the upstream body before the client sees it).

    - PASS       → return CommonResponse with CONTINUE. Envoy proceeds.

BUFFERED mode
-------------
This shim assumes envoy.yaml uses `request_body_mode: BUFFERED` and
`response_body_mode: BUFFERED`. Envoy holds the full body until it can
deliver one `RequestBody` / `ResponseBody` message to the processor.
Streaming mode is a v2 concern — noted in docs/README.md.

Design constraint from the ask
------------------------------
Boring and readable. Vivek reads this in one sitting and trusts it.
No frameworks beyond grpcio + httpx + the galileo SDK.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Optional, Tuple

# Generated Envoy proto bindings (see Dockerfile stage 1 for generation).
# We prepend to sys.path so the tree at /app/generated is importable as
# top-level `envoy.*` packages.
sys.path.insert(0, "/app/generated")

import grpc
from envoy.service.ext_proc.v3 import external_processor_pb2 as ep_pb2
from envoy.service.ext_proc.v3 import external_processor_pb2_grpc as ep_grpc
from envoy.type.v3 import http_status_pb2

# Galileo SDK — call site for invoke_protect. Uses the console URL
# + API key from the env below; the schemas live in galileo_core.
from galileo import invoke_protect, GalileoLogger
from galileo_core.schemas.protect.payload import Payload
from galileo_core.schemas.protect.rule import Rule
from galileo_core.schemas.protect.ruleset import Ruleset


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GALILEO_CONSOLE_URL = os.environ["GALILEO_CONSOLE_URL"]
GALILEO_API_KEY = os.environ["GALILEO_API_KEY"]
GALILEO_PROJECT = os.environ["GALILEO_PROJECT"]
GALILEO_STAGE = os.environ["GALILEO_STAGE"]
GALILEO_LOG_STREAM = os.environ.get("GALILEO_LOG_STREAM", "production")

# Fail-closed by default (regulated workloads).
FAIL_OPEN = os.environ.get("FAIL_OPEN", "false").lower() in ("1", "true", "yes")

# Total time budget per Protect call. Must be < ext_proc message_timeout in
# envoy.yaml so the shim can return a clean response before Envoy gives up.
PROTECT_TIMEOUT_S = float(os.environ.get("PROTECT_TIMEOUT_S", "8"))

SHIM_LISTEN_ADDR = os.environ.get("SHIM_LISTEN_ADDR", "0.0.0.0:50051")

# Make the Galileo SDK pick up our creds from env.
os.environ.setdefault("GALILEO_CONSOLE_URL", GALILEO_CONSOLE_URL)
os.environ.setdefault("GALILEO_API_KEY", GALILEO_API_KEY)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("protect-shim")


# ---------------------------------------------------------------------------
# Rule sets — verified against a live Luna-enabled Galileo cluster
# ---------------------------------------------------------------------------
# Naming trap #1: the SDK preset scorer *catalog name* is not always what
# invoke_protect looks up. For safety scorers the *bare* name is Luna;
# adding `_gpt` selects the LLM-as-judge variant. See docs/README.md for
# the full mapping.
#
# Naming trap #2: a Ruleset with N rules is TRIGGERED only when ALL N rules
# trigger (AND semantics). To get ANY-rule semantics, ship ONE rule per
# ruleset — every ruleset is independent, and if any one is TRIGGERED,
# invoke_protect's top-level ExecutionStatus flips to triggered.

INPUT_RULESETS = [
    Ruleset(rules=[Rule(metric="input_pii",        operator="not_empty", target_value=None)],
            description="input-pii"),
    Ruleset(rules=[Rule(metric="input_toxicity",   operator="gte",       target_value=0.5)],
            description="input-toxicity"),
    Ruleset(rules=[Rule(metric="prompt_injection", operator="gte",       target_value=0.5)],
            description="prompt-injection"),
]

OUTPUT_RULESETS = [
    Ruleset(rules=[Rule(metric="pii",      operator="not_empty", target_value=None)],
            description="output-pii"),
    Ruleset(rules=[Rule(metric="toxicity", operator="gte",       target_value=0.5)],
            description="output-toxicity"),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@dataclass
class Verdict:
    """Normalized result of a Protect call."""

    triggered: bool
    latency_ms: float
    reason: str
    fired_rule: Optional[str] = None
    call_error: Optional[str] = None
    # Held so the Galileo trace can carry the full Payload + Response into
    # add_protect_span — SDK unpacks scorer values + rule results automatically.
    raw_payload: Optional[Payload] = None
    raw_response: Optional[object] = None


def _extract_user_prompt(body_bytes: bytes) -> str:
    """Pull the last user message out of an OpenAI-style chat payload."""
    try:
        payload = json.loads(body_bytes or b"{}")
    except json.JSONDecodeError:
        return ""
    messages = payload.get("messages", [])
    for m in reversed(messages):
        if m.get("role") == "user":
            return m.get("content", "") or ""
    return ""


def _extract_assistant_message(body_bytes: bytes) -> str:
    """Pull the first assistant choice out of an OpenAI-style completion."""
    try:
        payload = json.loads(body_bytes or b"{}")
    except json.JSONDecodeError:
        return ""
    choices = payload.get("choices", [])
    if not choices:
        return ""
    return choices[0].get("message", {}).get("content", "") or ""


async def _call_protect(*, input_text: str, output_text: str, rulesets) -> Verdict:
    """Run one invoke_protect call and normalize the result into a Verdict.

    The SDK is synchronous. Runs in a worker thread with a hard timeout so
    a slow / hung upstream cannot stall the ext_proc stream past Envoy's
    message_timeout.

    `rulesets` is a list of Ruleset instances — each holds one Rule so any
    single triggering rule flips the top-level ExecutionStatus.
    """
    payload = Payload(input=input_text, output=output_text)

    t0 = time.perf_counter()
    try:
        response = await asyncio.wait_for(
            asyncio.to_thread(
                invoke_protect,
                payload=payload,
                prioritized_rulesets=rulesets,
                project_name=GALILEO_PROJECT,
                stage_name=GALILEO_STAGE,
            ),
            timeout=PROTECT_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        latency = (time.perf_counter() - t0) * 1000
        log.warning("Protect call timed out after %.0fms", latency)
        return Verdict(
            triggered=not FAIL_OPEN,
            latency_ms=latency,
            reason="protect_timeout",
            call_error="timeout",
        )
    except Exception as exc:
        latency = (time.perf_counter() - t0) * 1000
        log.warning("Protect call errored (%s) after %.0fms", exc, latency)
        return Verdict(
            triggered=not FAIL_OPEN,
            latency_ms=latency,
            reason="protect_error",
            call_error=str(exc)[:200],
        )
    latency = (time.perf_counter() - t0) * 1000

    # invoke_protect returns an ExecutionStatus of "triggered" / "not_triggered".
    status = getattr(response.status, "value", str(response.status)).lower()
    triggered = status == "triggered"

    # Find which rule tripped (first triggered ruleset_result → first
    # triggered rule_result). This is what the block body will surface.
    fired = None
    if triggered:
        for rs in getattr(response, "ruleset_results", []) or []:
            rs_dict = rs if isinstance(rs, dict) else rs.__dict__
            if str(rs_dict.get("status", "")).upper() != "TRIGGERED":
                continue
            for rr in rs_dict.get("rule_results", []) or []:
                rr_dict = rr if isinstance(rr, dict) else rr.__dict__
                if str(rr_dict.get("status", "")).upper() == "TRIGGERED":
                    fired = rr_dict.get("metric")
                    break
            if fired:
                break

    return Verdict(
        triggered=triggered,
        latency_ms=latency,
        reason=(response.text or "")[:200] if hasattr(response, "text") else "",
        fired_rule=fired,
        raw_payload=payload,
        raw_response=response,
    )


def _block_response(
    *, stage: str, verdict: Verdict, request_id: str,
) -> ep_pb2.ProcessingResponse:
    """Build the ImmediateResponse Envoy returns to the caller.

    CRITICAL: the client-facing body MUST NOT contain the offending content
    (prompt echo, LLM output, scorer values, or verdict.reason — the raw
    text of the offending payload). Full detail lives in the Galileo trace
    only (see add_protect_span, which carries the raw Payload + Response).

    The client body identifies ONLY:
      - blocked (true)
      - stage (request | response)
      - fired_rule (the metric_name that tripped)
      - request_id (uuid the app team can quote to security)

    All timing and correlation fields go into headers, not the body — so
    the client isn't expected to parse extra JSON to correlate a block.
    """
    body = json.dumps({
        "blocked": True,
        "stage": stage,
        "fired_rule": verdict.fired_rule,
        "request_id": request_id,
    }).encode()

    return ep_pb2.ProcessingResponse(
        immediate_response=ep_pb2.ImmediateResponse(
            status=http_status_pb2.HttpStatus(code=403),
            body=body,
            headers=ep_pb2.HeaderMutation(
                set_headers=[
                    _hdr("content-type", "application/json"),
                    _hdr("x-guardrail-decision", "block"),
                    _hdr("x-guardrail-stage", stage),
                    _hdr("x-guardrail-latency-ms", f"{verdict.latency_ms:.1f}"),
                    _hdr("x-guardrail-request-id", request_id),
                ]
            ),
        )
    )


def _continue_with_latency_header(
    *, on_request: bool, latency_ms: float, request_id: str,
) -> ep_pb2.ProcessingResponse:
    """CONTINUE + attach the guardrail latency + request_id headers.

    Setting x-guardrail-request-id on ALLOW as well as BLOCK means the app
    team always has a correlation id to quote to security when auditing —
    not just on blocks.
    """
    mutation = ep_pb2.HeaderMutation(
        set_headers=[
            _hdr("x-guardrail-decision", "allow"),
            _hdr("x-guardrail-latency-ms", f"{latency_ms:.1f}"),
            _hdr("x-guardrail-request-id", request_id),
        ]
    )
    body_response = ep_pb2.BodyResponse(
        response=ep_pb2.CommonResponse(header_mutation=mutation)
    )
    if on_request:
        return ep_pb2.ProcessingResponse(request_body=body_response)
    return ep_pb2.ProcessingResponse(response_body=body_response)


def _hdr(key: str, value: str):
    """envoy.config.core.v3.HeaderValueOption helper."""
    from envoy.config.core.v3 import base_pb2  # local import keeps top-of-file tidy
    return base_pb2.HeaderValueOption(
        header=base_pb2.HeaderValue(key=key, raw_value=value.encode()),
    )


# ---------------------------------------------------------------------------
# Trace logging — one Galileo trace per Envoy stream
# ---------------------------------------------------------------------------
# We wrap the SDK in tiny "safe" helpers below so a trace-store outage
# never breaks Envoy's guardrail path. The shim's job is to enforce policy;
# trace logging is enrichment, and enrichment failures must be non-fatal.

def _new_tracer_or_none() -> Optional[GalileoLogger]:
    try:
        return GalileoLogger(project=GALILEO_PROJECT, log_stream=GALILEO_LOG_STREAM)
    except Exception as exc:
        log.warning("GalileoLogger init failed (%s) — traces will not be emitted", exc)
        return None


def _safely_start_trace(
    tracer: Optional[GalileoLogger], *, prompt: str, request_id: str,
) -> None:
    if tracer is None:
        return
    try:
        # request_id lands as both a tag (queryable in the console UI) and
        # inside metadata (visible in span detail). Security can locate the
        # trace for any client-facing request_id in one search.
        tracer.start_trace(
            input=prompt,
            name="envoy_guardrail",
            tags=[f"request_id:{request_id}"],
            metadata={"request_id": request_id},
        )
    except Exception as exc:
        log.warning("start_trace failed: %s", exc)


def _safely_add_protect_span(
    tracer: Optional[GalileoLogger], *, verdict: Verdict, stage: str,
    request_id: str,
) -> None:
    """Attach a Protect span with the raw Payload + Response.

    The SDK's `add_protect_span` unpacks the Response's ruleset_results,
    rule_results, and metric_results itself — so the console shows scorer
    values inline per ruleset. No manual field flattening needed here.
    """
    if tracer is None:
        return
    if verdict.raw_payload is None or verdict.raw_response is None:
        # The Protect call errored or timed out — we still want the failure
        # visible in the trace but there's no Payload/Response to hand over.
        # Skip for now (metadata gets logged in the shim's stdout via log.info).
        return
    try:
        tracer.add_protect_span(
            payload=verdict.raw_payload,
            response=verdict.raw_response,
            metadata={
                "stage": stage,
                "triggered": str(verdict.triggered),
                "fired_rule": verdict.fired_rule or "",
                "guardrail_latency_ms": f"{verdict.latency_ms:.1f}",
                "request_id": request_id,
            },
            status_code=(403 if verdict.triggered else 200),
        )
    except Exception as exc:
        log.warning("add_protect_span (%s) failed: %s", stage, exc)


def _safely_conclude_and_flush(
    tracer: Optional[GalileoLogger], *, output: str, status_code: int,
) -> None:
    if tracer is None:
        return
    try:
        tracer.conclude(output=output, status_code=status_code)
    except Exception as exc:
        log.warning("trace conclude failed: %s", exc)
    _safely_flush_if_active(tracer)


def _safely_flush_if_active(tracer: Optional[GalileoLogger]) -> None:
    if tracer is None:
        return
    try:
        if tracer.has_active_trace():
            # An open trace we didn't conclude means abnormal stream end.
            tracer.conclude(output="", status_code=499)
        tracer.flush()
    except Exception as exc:
        log.warning("trace flush failed: %s", exc)


# ---------------------------------------------------------------------------
# gRPC service
# ---------------------------------------------------------------------------

class Guardrail(ep_grpc.ExternalProcessorServicer):
    """The ext_proc processing loop.

    One instance per stream. Envoy opens a stream per HTTP request and
    sends up to four messages: request_headers, request_body,
    response_headers, response_body. We reply to each in turn.
    """

    async def Process(self, request_iterator, context):
        # One trace per Envoy stream (= one HTTP request). Started when we
        # see the request_body message; concluded at whichever terminal
        # event fires (request-block, response-block, or clean allow).
        #
        # Every operation on the logger is wrapped in try/except so a
        # trace-store outage cannot break Envoy's guardrail path.
        tracer = None
        prompt = ""
        # One correlation id per HTTP request. Surfaces to the client via
        # x-guardrail-request-id header and (on block) inside the response
        # body. Also tagged on the trace so security can jump straight from
        # a client-facing id to the trace showing the full suppressed
        # content and scorer values.
        request_id = uuid.uuid4().hex
        try:
            async for req in request_iterator:
                kind = req.WhichOneof("request")

                if kind == "request_headers":
                    yield ep_pb2.ProcessingResponse(
                        request_headers=ep_pb2.HeadersResponse()
                    )

                elif kind == "request_body":
                    body = req.request_body.body
                    prompt = _extract_user_prompt(body)

                    # Open a trace for this HTTP request.
                    tracer = _new_tracer_or_none()
                    _safely_start_trace(tracer, prompt=prompt, request_id=request_id)

                    verdict = await _call_protect(
                        input_text=prompt, output_text="", rulesets=INPUT_RULESETS,
                    )
                    log.info(
                        "request_body: triggered=%s rule=%s latency=%.0fms prompt=%r",
                        verdict.triggered, verdict.fired_rule, verdict.latency_ms,
                        prompt[:60],
                    )

                    _safely_add_protect_span(
                        tracer, verdict=verdict, stage="request",
                        request_id=request_id,
                    )

                    if verdict.triggered:
                        _safely_conclude_and_flush(
                            tracer,
                            output=json.dumps({
                                "blocked": True, "stage": "request",
                                "fired_rule": verdict.fired_rule,
                            }),
                            status_code=403,
                        )
                        yield _block_response(
                            stage="request", verdict=verdict,
                            request_id=request_id,
                        )
                        return

                    yield _continue_with_latency_header(
                        on_request=True, latency_ms=verdict.latency_ms,
                        request_id=request_id,
                    )

                elif kind == "response_headers":
                    yield ep_pb2.ProcessingResponse(
                        response_headers=ep_pb2.HeadersResponse()
                    )

                elif kind == "response_body":
                    body = req.response_body.body
                    completion = _extract_assistant_message(body)
                    verdict = await _call_protect(
                        input_text="", output_text=completion, rulesets=OUTPUT_RULESETS,
                    )
                    log.info(
                        "response_body: triggered=%s rule=%s latency=%.0fms completion=%r",
                        verdict.triggered, verdict.fired_rule, verdict.latency_ms,
                        completion[:60],
                    )

                    _safely_add_protect_span(
                        tracer, verdict=verdict, stage="response",
                        request_id=request_id,
                    )

                    if verdict.triggered:
                        _safely_conclude_and_flush(
                            tracer,
                            output=json.dumps({
                                "blocked": True, "stage": "response",
                                "fired_rule": verdict.fired_rule,
                            }),
                            status_code=403,
                        )
                        yield _block_response(
                            stage="response", verdict=verdict,
                            request_id=request_id,
                        )
                        return

                    # Clean end-to-end. Conclude the trace with the actual
                    # completion the client is about to receive.
                    _safely_conclude_and_flush(
                        tracer, output=completion, status_code=200,
                    )

                    yield _continue_with_latency_header(
                        on_request=False, latency_ms=verdict.latency_ms,
                        request_id=request_id,
                    )

                else:
                    log.debug("ignoring unknown ext_proc message kind: %s", kind)
        finally:
            # Belt-and-braces: if the stream ended without a terminal event,
            # still flush what we have so the partial trace surfaces in the
            # console rather than leaking silently.
            _safely_flush_if_active(tracer)


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

async def serve():
    server = grpc.aio.server()
    ep_grpc.add_ExternalProcessorServicer_to_server(Guardrail(), server)
    server.add_insecure_port(SHIM_LISTEN_ADDR)
    await server.start()
    log.info(
        "protect-shim listening on %s (project=%s stage=%s fail_open=%s timeout=%.1fs)",
        SHIM_LISTEN_ADDR, GALILEO_PROJECT, GALILEO_STAGE, FAIL_OPEN, PROTECT_TIMEOUT_S,
    )
    await server.wait_for_termination()


if __name__ == "__main__":
    asyncio.run(serve())
