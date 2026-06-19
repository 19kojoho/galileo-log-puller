# galileo-log-puller + protect example

Small standalone scripts built on the official `galileo` Python SDK — no raw HTTP calls — to help you (1) pull your traces out for offline analysis, and (2) add runtime guardrails to an existing agent.

**What's in this repo**

| File | Use case |
|---|---|
| [`pull_galileo_logs.py`](#pull_galileo_logspy) | Pull every trace from a log stream into CSV + JSONL for analysis or reporting. |
| [`protect_quickstart_example.py`](#protect_quickstart_examplepy) | End-to-end Protect example: benign prompt passes, PII prompt is blocked before the LLM is called. |
| [📋 Drop-in snippet](#-drop-in-add-protect-to-an-existing-agent) | The 10-ish lines to paste into an agent you already have. |

---

## 📋 Drop-in: add Protect to an existing agent

If you already have a Galileo-instrumented agent and just want to add a runtime guardrail, this is all you need.

### Step 1. Create a Protect stage in your project (once)

A "stage" is a named container Protect calls live under. Create one — takes a few seconds:

```python
from galileo.stages import create_protect_stage
from galileo_core.schemas.protect.stage import StageType

create_protect_stage(
    project_name="<your project name>",
    name="poc-protect-stage",
    stage_type=StageType.local,
)
```

You can re-run this; it'll fail noisily if the stage already exists, which is fine — just ignore.

### Step 2. Paste these imports and ruleset at the top of your agent file

```python
import os
from galileo import invoke_protect
from galileo_core.schemas.protect.execution_status import ExecutionStatus
from galileo_core.schemas.protect.payload import Payload
from galileo_core.schemas.protect.rule import Rule
from galileo_core.schemas.protect.ruleset import Ruleset

# What you want to block. Each ruleset = one PII type. If any trigger, block.
# `input_pii` runs the Luna SLM scorer (in-cluster, sub-second).
RULESETS = [
    Ruleset(rules=[Rule(metric="input_pii", operator="contains", target_value=t)])
    for t in ["email", "name", "phone", "ssn", "address", "credit_card"]
]
```

### Step 3. Add 5 lines before your LLM call

Wherever your code currently calls the LLM, insert this just before:

```python
verdict = invoke_protect(
    payload=Payload(input=user_input, output=""),
    prioritized_rulesets=RULESETS,
    project_name=os.environ["GALILEO_PROJECT"],
    stage_name="poc-protect-stage",
)
if verdict.status == ExecutionStatus.triggered:
    return "[BLOCKED] Your input was flagged."   # ← put your handling here
# else: fall through to your existing LLM call
```

That's the whole integration. Replace the `return "[BLOCKED]"` line with whatever your app should do — log, warn, email, fall back to a safer model, etc.

### What you'll see when it runs

- **No block** → `verdict.status == ExecutionStatus.not_triggered`. Your code continues to the LLM call as before.
- **Block** → `verdict.status == ExecutionStatus.triggered`. The LLM is not called, your handling runs.
- Every Protect call also lands in the Galileo console under **Project → Stages → poc-protect-stage** so your security team can audit decisions and edit rules without you redeploying.

### Picking a different scorer

If you want to block on something other than input PII — toxicity, sexism, prompt injection — change the `metric` value and the rule pattern. Examples:

```python
# Block on detected prompt injection
Ruleset(rules=[Rule(metric="prompt_injection_luna", operator="eq", target_value=True)])

# Block on toxicity above a threshold
Ruleset(rules=[Rule(metric="input_toxicity_luna", operator="gt", target_value=0.7)])
```

Note: some Luna scorers may not be loaded on every cluster. Check with your Galileo contact which Luna metrics are enabled before relying on them in production.

---

## `pull_galileo_logs.py`

Pulls every trace from a Galileo log stream into local CSV + JSONL so you can wrangle the results in a DataFrame, compare runs, or feed them into your own reporting.

### What it pulls

For every trace in the log stream you point it at, you get:

- `input` — the full payload sent into the agent
- `output` — the agent's final response
- `spans` — the trace tree (nested)
- `metrics.*` — every Galileo metric, flattened to its own column (e.g. `metrics.pii`, `metrics.kb_article_hallucinated`, `metrics.cost`, `metrics.num_input_tokens`)
- All standard trace fields (`trace_id`, `created_at`, `tags`, `status_code`, `session_id`, user metadata, …)

### Install

```bash
pip install --upgrade galileo pandas
```

Python 3.10+.

### Configure

```bash
export GALILEO_API_KEY="<your API key>"
export GALILEO_CONSOLE_URL="https://console.<your-cluster>.galileocloud.io"
```

### Run

```bash
python pull_galileo_logs.py \
  --project "<your project name>" \
  --log-stream "<your log stream name>" \
  --limit 500
```

Optional flags:

| Flag | Default | Description |
|---|---|---|
| `--limit` | `500` | Max traces to pull |
| `--out-dir` | `.` (current dir) | Where to write the CSV + JSONL |

### Output

- `galileo_traces.csv` — one row per trace, DataFrame-ready, every metric in its own `metrics.<name>` column
- `galileo_traces.json` — same data as JSONL (one trace per line)

The script also prints a summary of which metric columns were populated, so you can confirm your scorers actually ran on the test cases you sent.

---

## `protect_quickstart_example.py`

A complete runnable end-to-end example: takes the Galileo quickstart pattern (Galileo-wrapped OpenAI client + `galileo_context`) and adds a Protect check on the input. Two scenarios run back-to-back so you can see one pass and one get blocked.

### Install

```bash
pip install --upgrade galileo openai
```

### Configure

```bash
export GALILEO_API_KEY="..."
export GALILEO_PROJECT="<your project name>"
export GALILEO_LOG_STREAM="<your log stream name, e.g. production>"
export GALILEO_CONSOLE_URL="https://console.<your-cluster>.galileocloud.io"
export OPENAI_API_KEY="..."
```

### Run

```bash
python protect_quickstart_example.py
```

Output you'll see:

```
SCENARIO — benign
  Input           : What's the best way to write a Python function for parsing CSV?
  Protect verdict : not_triggered    (508 ms)
  Action          : PASS — sending to LLM
  Reply           : When writing a Python function for parsing CSV files...

SCENARIO — contains PII
  Input           : Hi my name is John Doe and my email is john.doe@example.com...
  Protect verdict : triggered        (479 ms)
  Action          : BLOCK — input violates policy, skipping LLM call
  Reply           : [BLOCKED] Your input was flagged as containing sensitive data.
```

Note: this example creates a Protect stage in code on first import. If you'd rather create it ahead of time, use the snippet in [Step 1](#step-1-create-a-protect-stage-in-your-project-once) above.

---

## License

MIT
