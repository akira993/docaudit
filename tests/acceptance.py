import re
from pathlib import Path

REGISTRY = []


def acceptance(test_id: str, targets: int):
    if targets < 1:
        raise ValueError("targets must be positive")

    def decorate(function):
        REGISTRY.append((test_id, f"{function.__module__}.{function.__qualname__}", targets))
        return function

    return decorate


def design_ids():
    # tests/design_ids.txt holds the 37 design acceptance IDs, in the canonical order of the
    # 1.0.0 acceptance criteria.
    listing = Path(__file__).parent / "design_ids.txt"
    lines = listing.read_text(encoding="utf-8").splitlines()
    ids = list(dict.fromkeys(line.strip() for line in lines if line.strip() and not line.startswith("#")))
    if any(not re.fullmatch(r"T-[A-Z]+-\d+", i) for i in ids):
        raise ValueError("tests/design_ids.txt contains a malformed ID")
    if len(ids) != 37:
        raise ValueError(f"expected 37 design IDs, found {len(ids)}")
    return ids


def route_ids():
    from .acceptance_plan import PHASES
    return list(dict.fromkeys(i for phase in PHASES.values() for i in phase["route"]))
