#!/usr/bin/env python3
"""Compare, propose, or accept versioned golden label outputs."""

import argparse
import json
from pathlib import Path

from image23mf.quality.goldens import (
    accept_golden_update,
    compare_golden,
    load_golden_payload,
    propose_golden_update,
)


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description=__doc__)
    subcommands = command.add_subparsers(dest="command", required=True)

    compare = subcommands.add_parser("compare", help="compare an actual payload without writing")
    compare.add_argument("baseline", type=Path)
    compare.add_argument("actual", type=Path)

    propose = subcommands.add_parser("propose", help="stage a review bundle; baseline is untouched")
    propose.add_argument("baseline", type=Path)
    propose.add_argument("actual", type=Path)
    propose.add_argument("review", type=Path)

    accept = subcommands.add_parser("accept", help="accept the exact reviewed proposal")
    accept.add_argument("baseline", type=Path)
    accept.add_argument("review", type=Path)
    accept.add_argument("--sha256", required=True)
    return command


def main() -> int:
    arguments = parser().parse_args()
    if arguments.command == "compare":
        _, actual = load_golden_payload(arguments.actual)
        result = compare_golden(arguments.baseline, actual)
        print(json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True))
        return 0 if result.passed else 1
    if arguments.command == "propose":
        actual_manifest, actual = load_golden_payload(arguments.actual)
        review = propose_golden_update(
            arguments.baseline,
            actual,
            arguments.review,
            tolerance=actual_manifest.tolerance,
        )
        print(json.dumps(review.model_dump(mode="json"), indent=2, sort_keys=True))
        return 0
    manifest = accept_golden_update(
        arguments.baseline,
        arguments.review,
        proposal_sha256=arguments.sha256,
    )
    print(f"Accepted {manifest.case_id} at {arguments.baseline}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
