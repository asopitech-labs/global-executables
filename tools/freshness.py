#!/usr/bin/env python3
"""Run one bounded partial freshness scan."""
import sys

from global_executables.cli import main


if __name__ == "__main__":
    sys.argv.insert(1, "freshness")
    main()
