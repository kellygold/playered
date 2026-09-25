#!/usr/bin/env python3
"""Run the fast representative suite and the explicit production mural budget."""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--git-commit", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    commands = (
        (
            sys.executable,
            str(ROOT / "benchmarks" / "run_baseline.py"),
            "--run-id",
            f"{arguments.run_id}-core",
            "--git-commit",
            arguments.git_commit,
            "--output-dir",
            str(arguments.output_dir / "core"),
            "--verify-against",
            str(ROOT / "benchmarks" / "budgets.json"),
        ),
        (
            sys.executable,
            str(ROOT / "benchmarks" / "run_mural_partition.py"),
            "--run-id",
            f"{arguments.run_id}-mural",
            "--git-commit",
            arguments.git_commit,
            "--output-dir",
            str(arguments.output_dir / "mural"),
            "--verify-against",
            str(ROOT / "benchmarks" / "mural-partition-budgets.json"),
        ),
    )
    failed = False
    for command in commands:
        result = subprocess.run(command, cwd=ROOT, check=False)
        failed = failed or result.returncode != 0
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
