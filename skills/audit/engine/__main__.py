"""Package entry point that also supports ``python skills/audit/engine``."""

from pathlib import Path
import sys


_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from skills.audit.engine.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
