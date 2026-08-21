#!/usr/bin/env python3
"""Validate one materialized published dictionary against the program contract."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from global_executables.validation import validate_dictionary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--schema-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--integrity-only",
        action="store_true",
        help="check published data integrity without applying this revision's schemas",
    )
    args = parser.parse_args()
    result = validate_dictionary(
        args.root.resolve(),
        args.schema_root.resolve(),
        validate_schema=not args.integrity_only,
    )
    print(json.dumps({"status": "valid", **result}, sort_keys=True))


if __name__ == "__main__":
    main()
