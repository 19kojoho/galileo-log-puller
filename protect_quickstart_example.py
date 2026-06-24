"""Galileo Protect example using the Luna input-PII scorer.

This is the pattern Olu asked about on the call: before the LLM is called,
the user input goes through Galileo Protect. Protect runs the Luna SLM
scorer and returns a verdict synchronously. Based on the verdict, the
application decides what to do — block, warn, log, or proceed.

This example uses `input_pii` (Luna-backed). It's a small in-cluster SLM
so the check returns in <1 second. Add more rulesets (toxicity_luna,
sexist_luna, prompt_injection_luna, etc.) the same way once they're
enabled on your cluster.

Prerequisites
-------------
    pip install --upgrade galileo openai

Env vars:
    GALILEO_API_KEY, GALILEO_PROJECT, GALILEO_LOG_STREAM,
    GALILEO_CONSOLE_URL, OPENAI_API_KEY

A Protect "stage" must exist in your project before you call invoke_protect.
You can create one in the Galileo UI, or programmatically:

    from galileo.stages import create_protect_stage
    from galileo_core.schemas.protect.stage import StageType
    create_protect_stage(
        project_name="<your project>",
        name="<your stage name>",
        stage_type=StageType.local,
    )

Run
---
    python protect_quickstart_example.py
"""
from __future__ import annotations

import os
import sys

from galileo import galileo_context, invoke_protect, log
from galileo.openai import openai
from galileo_core.schemas.protect.execution_status import ExecutionStatus
from galileo_core.schemas.protect.payload import Payload
from galileo_core.schemas.protect.response import Response
from galileo_core.schemas.protect.rule import Rule
from galileo_core.schemas.protect.ruleset import Ruleset


# `input_pii` (Luna SLM) returns a list of PII types detected: ["email"], ["name"],
# ["ssn"], etc. To block on ANY type, we use one ruleset per type — `contains`
# triggers when the detected list contains that type. Rulesets are evaluated
# in order; the first one that triggers wins. This gives us both blanket coverage
# and per-type visibility ("which PII type fired?").
PII_TYPES_TO_BLOCK = ["email", "name", "phone", "ssn", "address", "credit_card", "ip_address"]

RULESETS = [
    Ruleset(
        description=f"Block input containing {pii_type}",
        rules=[Rule(metric="input_pii", operator="contains", target_value=pii_type)],
    )
    for pii_type in PII_TYPES_TO_BLOCK
]


@log(name="protect_input", span_type="tool")
def evaluate_input(user_input: str, stage_name: str, project_name: str) -> Response:
    """Run the user input through every ruleset. Returns the first one that
    triggers (in priority order). If none trigger, returns the final response.

    Wrapped with @log so the Protect call shows up as a `protect_input` span
    inside the agent trace tree — that's what makes the guardrail visible in
    the Galileo UI alongside the LLM call."""
    return invoke_protect(
        payload=Payload(input=user_input, output=""),
        prioritized_rulesets=RULESETS,
        project_name=project_name,
        stage_name=stage_name,
    )


@log(name="agent_turn", span_type="agent")
def chat_with_guardrail(user_input: str) -> str:
    """Pattern Olu asked about: check the input before calling the LLM,
    then decide based on the verdict.

    Wrapped with @log so the whole turn shows up as one agent-level trace
    in the Galileo log stream — input, verdict, LLM child span (if called),
    and final output, all on one tree. The invoke_protect call itself still
    lands in the Protect/Stages section separately.
    """

    project_name = os.environ["GALILEO_PROJECT"]
    stage_name = os.getenv("GALILEO_PROTECT_STAGE", "poc-protect-stage")

    # 1. Synchronous check — Protect runs the Luna scorer and tells us
    #    whether the input violates any of our rulesets.
    verdict = evaluate_input(user_input, stage_name, project_name)

    exec_ms = (verdict.trace_metadata.execution_time or 0) * 1000
    print(f"  Protect verdict : {verdict.status.value:15s}  ({exec_ms:.0f} ms)")

    # 2. The app decides what to do based on the verdict.
    if verdict.status == ExecutionStatus.triggered:
        # OLU CAN PUT HIS LOGIC HERE — block, warn, log, email, etc.
        # For this demo: block and return a safe response.
        print(f"  Action          : BLOCK — input violates policy, skipping LLM call")
        return "[BLOCKED] Your input was flagged as containing sensitive data."

    # 3. Not triggered — safe to call the LLM.
    # The galileo-wrapped openai client auto-creates a child LLM span
    # under this @log-decorated parent.
    print(f"  Action          : PASS — sending to LLM")
    client = openai.OpenAI()
    reply = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": user_input},
        ],
    )
    return reply.choices[0].message.content or ""


def main() -> int:
    for var in ("GALILEO_API_KEY", "GALILEO_PROJECT", "GALILEO_LOG_STREAM", "OPENAI_API_KEY"):
        if not os.getenv(var):
            print(f"ERROR: {var} is not set.", file=sys.stderr)
            return 2

    galileo_context.init(
        project=os.environ["GALILEO_PROJECT"],
        log_stream=os.environ["GALILEO_LOG_STREAM"],
    )

    scenarios = [
        ("benign", "What's the best way to write a Python function for parsing CSV?"),
        ("contains PII", "Hi my name is John Doe and my email is john.doe@example.com — can you help reset my password?"),
    ]

    for label, user_input in scenarios:
        print("=" * 78)
        print(f"SCENARIO — {label}")
        print("=" * 78)
        print(f"  Input           : {user_input}")
        reply = chat_with_guardrail(user_input)
        print(f"  Reply           : {reply[:300]}")
        # Flush after each scenario so each agent_turn lands as its own
        # top-level trace in the log stream (rather than getting siblinged
        # under one mega-trace).
        galileo_context.flush()
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
