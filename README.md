# galileo-log-puller + protect example

Two small standalone scripts that use the official `galileo` Python SDK — no raw HTTP calls.

1. **`pull_galileo_logs.py`** — pull traces from a Galileo log stream into a CSV and JSONL on your machine, so you can wrangle the results in a DataFrame, compare across runs, or feed them into your own reporting.
2. **`protect_quickstart_example.py`** — minimal example showing how to wrap [Galileo Protect](https://docs.galileo.ai/) around a quickstart-style OpenAI agent so prompt-injection attempts are blocked before they reach the LLM.

---

## pull_galileo_logs.py

## What it pulls

For every trace in the log stream you point it at, you get:

- `input` — the full payload sent into the agent
- `output` — the agent's final response
- `spans` — the trace tree (nested)
- `metrics.*` — every Galileo metric, flattened to its own column (e.g. `metrics.pii`, `metrics.kb_article_hallucinated`, `metrics.cost`, `metrics.num_input_tokens`, etc.)
- All standard trace fields (`trace_id`, `created_at`, `tags`, `status_code`, `session_id`, user metadata, …)

## Install

```bash
pip install --upgrade galileo pandas
```

Python 3.10+.

## Configure

The script reads the same two environment variables your agent already uses:

```bash
export GALILEO_API_KEY="<your API key>"
export GALILEO_CONSOLE_URL="https://console.<your-cluster>.galileocloud.io"
```

## Run

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

## Output

In the directory you specified:

- `galileo_traces.csv` — one row per trace, DataFrame-ready, every metric in its own `metrics.<name>` column
- `galileo_traces.json` — same data as JSONL (one trace per line), useful if you'd rather work in JSON than tabular form

The script also prints a summary of which metric columns were populated and how many traces each one covered — easy way to confirm your scorers actually ran on the test cases you sent.

---

## protect_quickstart_example.py

Shows the quickstart pattern plus a runtime guardrail. Sends a benign prompt and a prompt-injection attempt through the same code path. The benign one reaches the LLM; the injection one is blocked by Galileo Protect before any LLM call is made.

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

You'll see two scenarios printed back-to-back: scenario 1 returns a normal LLM reply, scenario 2 returns `[BLOCKED BY GALILEO PROTECT]` and skips the LLM entirely.

### What it's doing

```python
PROMPT_INJECTION_RULESET = Ruleset(
    description="Block prompt injection attempts on user input",
    action="OVERRIDE",
    rules=[Rule(metric="prompt_injection_luna", operator="eq", target_value=True)],
)

protect_response = invoke_protect(
    payload=Payload(input=user_input, output=""),
    prioritized_rulesets=[PROMPT_INJECTION_RULESET],
    project_name=os.environ["GALILEO_PROJECT"],
)

if protect_response.status == ExecutionStatus.triggered:
    return "[BLOCKED]"   # skip the LLM
```

To add more checks — input PII, toxicity, sexism, SQL injection — add more `Rule` entries to the `Ruleset` or define additional `Ruleset`s. The full list of available scorer metrics is in the Galileo SDK (`GalileoScorers`).

---

## License

MIT
