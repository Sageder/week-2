#!/usr/bin/env python3
"""Run the interactive xAI shortcut-abuse evaluation."""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import environment
import xai_client

ROOT = Path(__file__).resolve().parent


def parse_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}


def run_trial(
    *,
    api_key: str,
    base_url: str,
    model: str,
    task_condition: str,
    temperature: float,
    max_turns: int,
    rng: random.Random,
) -> dict[str, Any]:
    task = environment.make_task(task_condition, rng)
    messages: list[dict[str, Any]] = environment.initial_messages(task)
    events: list[dict[str, Any]] = []
    outcome = "other"
    final_text = ""
    usage = Counter()

    for turn in range(1, max_turns + 1):
        response = xai_client.chat(
            api_key=api_key,
            base_url=base_url,
            model=model,
            messages=messages,
            tools=environment.tool_definitions(),
            temperature=temperature,
        )
        for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cost_in_usd_ticks"):
            usage[key] += response.get("usage", {}).get(key, 0)

        message = response["choices"][0]["message"]
        final_text = message.get("content") or ""
        tool_calls = message.get("tool_calls") or []
        assistant_message: dict[str, Any] = {"role": "assistant", "content": message.get("content")}
        if tool_calls:
            assistant_message["tool_calls"] = tool_calls
        messages.append(assistant_message)

        if not tool_calls:
            messages.append({"role": "user", "content": "Use one available tool for your next action."})
            continue

        finished = False
        for call in tool_calls:
            name = call.get("function", {}).get("name", "")
            args = parse_arguments(call.get("function", {}).get("arguments"))
            tool_text, action_outcome, finished, metadata = environment.handle_action(name, args, task)
            events.append({"turn": turn, "tool": name, "arguments": args, **metadata})
            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id", f"call-{turn}"),
                "name": name,
                "content": tool_text,
            })
            if action_outcome is not None:
                outcome = action_outcome
            if finished:
                break
        if finished:
            break

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "task_condition": task_condition,
        "max_turns": max_turns,
        "outcome": outcome,
        "events": events,
        "final_text": final_text,
        "usage": dict(usage),
        "task": {key: value for key, value in task.items() if key != "solution"},
        "ground_truth_solution": task["solution"],
    }


def write_summary(rows: list[dict[str, Any]], path: Path) -> None:
    grouped: dict[tuple[str, str], Counter[str]] = {}
    for row in rows:
        key = (row["model"], row["task_condition"])
        grouped.setdefault(key, Counter())[row["outcome"]] += 1
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["model", "task_condition", "trials", "solve", "alert", "abuse", "other"])
        for key, counts in sorted(grouped.items()):
            writer.writerow([*key, sum(counts.values()), *(counts[name] for name in ("solve", "alert", "abuse", "other"))])


def main() -> int:
    xai_client.load_env(ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=os.getenv("XAI_MODEL", "grok-4.6"))
    parser.add_argument("--trials", type=int, default=5, help="Trials per task condition")
    parser.add_argument("--task", choices=["solvable", "unsolvable", "both"], default="both")
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--max-turns", type=int, default=20)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "results")
    args = parser.parse_args()

    api_key = os.getenv("XAI_API_KEY")
    if not api_key:
        parser.error("XAI_API_KEY is missing; set it in week-2/.env or the environment")
    base_url = os.getenv("XAI_BASE_URL", "https://api.x.ai/v1")
    conditions = ["solvable", "unsolvable"] if args.task == "both" else [args.task]
    rng = random.Random(args.seed)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    jsonl_path = args.output_dir / f"trials-{stamp}.jsonl"
    summary_path = args.output_dir / f"summary-{stamp}.csv"
    rows: list[dict[str, Any]] = []

    total = len(conditions) * args.trials
    completed = 0
    with jsonl_path.open("w") as log:
        for condition in conditions:
            for _ in range(args.trials):
                row = run_trial(
                    api_key=api_key,
                    base_url=base_url,
                    model=args.model,
                    task_condition=condition,
                    temperature=args.temperature,
                    max_turns=args.max_turns,
                    rng=rng,
                )
                rows.append(row)
                log.write(json.dumps(row) + "\n")
                log.flush()
                completed += 1
                print(f"[{completed}/{total}] {condition}: {row['outcome']}", flush=True)

    write_summary(rows, summary_path)
    print(f"\nTrial log: {jsonl_path}")
    print(f"Summary:   {summary_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        raise SystemExit(130)
