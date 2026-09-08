import json
from pathlib import Path


def version() -> str:
    path = Path(__file__).parents[3] / ".claude-plugin" / "plugin.json"
    return json.loads(path.read_text(encoding="utf-8"))["version"]
