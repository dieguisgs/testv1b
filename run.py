"""Run VWAPSb from a checkout without installing the package."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from vwapsb.cli import main

if __name__ == "__main__":
    raise SystemExit(main())

