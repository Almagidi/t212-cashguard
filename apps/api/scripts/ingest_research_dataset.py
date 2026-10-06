"""Publish a local Polygon-compatible JSON file as an immutable research dataset."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path

from app.backtest.dataset_cache import DatasetRequest, ImmutableDatasetCache


def _code_sha() -> str:
    return (
        os.environ.get("T212_CODE_SHA")
        or subprocess.run(
            ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
        ).stdout.strip()
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--cache-dir", required=True, type=Path)
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--source-label", required=True)
    parser.add_argument("--from-date", required=True, type=date.fromisoformat)
    parser.add_argument("--to-date", required=True, type=date.fromisoformat)
    parser.add_argument("--multiplier", type=int, default=5)
    parser.add_argument("--timespan", choices=("minute", "day"), default="minute")
    parser.add_argument(
        "--adjustment-mode",
        required=True,
        choices=("provider_adjusted", "raw_with_actions", "unadjusted_unknown"),
    )
    args = parser.parse_args(argv)
    source_bytes = args.source.read_bytes()
    rows = json.loads(source_bytes)
    if not isinstance(rows, list):
        raise ValueError("source must contain a JSON list of bar objects")
    request = DatasetRequest(
        provider="local_file",
        source_id=args.source_label,
        source_version="user_supplied_v1",
        source_sha256=hashlib.sha256(source_bytes).hexdigest(),
        symbols=(args.ticker,),
        start=args.from_date,
        end=args.to_date,
        multiplier=args.multiplier,
        timespan=args.timespan,
        venue="XNYS",
        timezone="America/New_York",
        adjustment_mode=args.adjustment_mode,
        corporate_action_policy=f"{args.adjustment_mode}_unqualified",
        membership_source="single_symbol_request",
    )
    handle = ImmutableDatasetCache(args.cache_dir).publish(
        request,
        rows,
        retrieved_at=datetime.now(UTC),
        code_sha=_code_sha(),
    )
    print(
        json.dumps(
            {
                "content_id": handle.content_id,
                "manifest_id": handle.manifest_id,
                "manifest": handle.manifest,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
