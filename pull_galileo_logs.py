"""Pull traces from a Galileo log stream and dump them to CSV + JSON.

Use case: you ran your test cases through the agent and want the results
out of the Galileo console — including the metric scores — as something
you can wrangle into a DataFrame, diff against expected, or report on.

Prerequisites
-------------
1. Install the SDK:
       pip install --upgrade "galileo>=1.0.0" pandas
2. Export your credentials (the same ones the agent uses):
       export GALILEO_API_KEY="<your key>"
       export GALILEO_CONSOLE_URL="https://console.<your-cluster>.galileocloud.io"

Run
---
    python pull_galileo_logs.py \
        --project "<your project name>" \
        --log-stream "<your log stream name>" \
        --limit 500

Outputs (in the current dir):
    galileo_traces.csv     - one row per trace with all metric columns
    galileo_traces.json    - same data as JSONL (one trace per line)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

try:
    import pandas as pd
except ImportError:
    print("pandas not installed. Run: pip install pandas", file=sys.stderr)
    raise

# Public API surface — verified in galileo-python source at:
#   src/galileo/__future__/__init__.py:13  → LogStream
#   src/galileo/__future__/__init__.py:20  → RecordType
#   src/galileo/__future__/log_stream.py:733 → export_records()
from galileo.__future__ import LogStream, RecordType


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--project",
        required=True,
        help="Galileo project name (the one your agent writes traces to)",
    )
    parser.add_argument(
        "--log-stream",
        required=True,
        help="Log stream name within the project (e.g. 'production')",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=500,
        help="Max number of traces to pull (default: 500)",
    )
    parser.add_argument(
        "--out-dir",
        default=".",
        help="Directory to write galileo_traces.csv + .json into (default: cwd)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if not os.getenv("GALILEO_API_KEY"):
        print("ERROR: GALILEO_API_KEY env var is not set.", file=sys.stderr)
        return 2
    if not os.getenv("GALILEO_CONSOLE_URL"):
        print("ERROR: GALILEO_CONSOLE_URL env var is not set.", file=sys.stderr)
        return 2

    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Looking up log stream '{args.log_stream}' in project '{args.project}'...")
    log_stream = LogStream.get(name=args.log_stream, project_name=args.project)
    if log_stream is None:
        print(
            f"ERROR: Log stream '{args.log_stream}' not found in project '{args.project}'.",
            file=sys.stderr,
        )
        return 1

    print(f"Found log stream id={log_stream.id}. Pulling up to {args.limit} traces...")

    records: list[dict] = []
    for i, record in enumerate(
        log_stream.export_records(
            record_type=RecordType.TRACE,
            sort=log_stream.trace_columns["created_at"].descending(),
        )
    ):
        records.append(record)
        if len(records) >= args.limit:
            break
        if (i + 1) % 50 == 0:
            print(f"  …pulled {i + 1} so far")

    if not records:
        print("No traces returned. Confirm the project + log stream are correct.")
        return 0

    csv_path = out_dir / "galileo_traces.csv"
    jsonl_path = out_dir / "galileo_traces.json"

    # Flatten the nested `metrics` dict to top-level `metrics.<name>` columns
    # so the CSV is DataFrame-ready and each metric is its own column.
    flat: list[dict] = []
    for r in records:
        out = dict(r)
        metrics = out.pop("metrics", None) or {}
        for k, v in metrics.items():
            out[f"metrics.{k}"] = v
        flat.append(out)

    df = pd.json_normalize(flat)
    df.to_csv(csv_path, index=False)

    with jsonl_path.open("w") as f:
        for r in records:
            f.write(json.dumps(r, default=str) + "\n")

    print(f"\nPulled {len(records)} traces.")
    print(f"  CSV : {csv_path}")
    print(f"  JSON: {jsonl_path}")

    metric_cols = [c for c in df.columns if c.startswith("metrics.")]
    if metric_cols:
        print(f"\nMetric columns ({len(metric_cols)}):")
        for c in sorted(metric_cols):
            non_null = df[c].notna().sum()
            print(f"  {c:<60s} ({non_null}/{len(df)} populated)")
    else:
        print(
            "\nNo metric columns found. Either no metrics ran yet, "
            "or metrics are still computing — check the console."
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
