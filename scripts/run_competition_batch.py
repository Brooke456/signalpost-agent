#!/usr/bin/env python3
"""Drop-in replacement for the starter kit's evaluator command (same flags).

uv run python scripts/run_competition_batch.py --organisations batch.jsonl --bulk brreg-enheter.csv \
    --output out/envelopes.jsonl --profiles-output out/profiles.jsonl --report out/report.json \
    --run-id daily-001 --expected-count 1000
Unknown starter flags (--modules, --checkpoint-every, --resume) are accepted and ignored.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from signalpost.run import main  # noqa: E402

IGNORED_WITH_VALUE = {"--modules", "--checkpoint-every"}
IGNORED_FLAGS = {"--resume"}


def _filter(argv: list[str]) -> list[str]:
    out, skip = [], False
    for i, a in enumerate(argv):
        if skip:
            skip = False
            continue
        key = a.split("=", 1)[0]
        if key in IGNORED_WITH_VALUE:
            skip = "=" not in a
            continue
        if key in IGNORED_FLAGS:
            continue
        out.append(a)
    return out


if __name__ == "__main__":
    raise SystemExit(main(_filter(sys.argv[1:])))
