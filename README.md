# galileo-log-puller

A small Python script to pull traces from a Galileo log stream into a CSV and JSONL on your machine, so you can wrangle the results in a DataFrame, compare across runs, or feed them into your own reporting.

Uses the official `galileo` Python SDK end-to-end — no raw HTTP calls.

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

## License

MIT
