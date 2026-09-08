"""The fixed layer registry used by the profile planner."""

from __future__ import annotations

import hashlib
import json


# Declaration order is also the deterministic tie-breaker for planning.
LAYER_REGISTRY = (
    {"id": "L-SCOPE", "dependencies": (), "blockingPolicy": "required"},
    {"id": "L-DOC", "dependencies": ("L-SCOPE",), "blockingPolicy": "blocking"},
    {"id": "L-PROJECT", "dependencies": ("L-SCOPE",), "blockingPolicy": "blocking"},
    {"id": "L-ENRICH", "dependencies": ("L-SCOPE",), "blockingPolicy": "non-blocking"},
    {"id": "L-SECURITY", "dependencies": ("L-SCOPE",), "blockingPolicy": "blocking"},
    {"id": "L-ADVERSARIAL", "dependencies": ("L-SCOPE",), "blockingPolicy": "blocking"},
    {"id": "L-CLAIM", "dependencies": ("L-ADVERSARIAL",), "blockingPolicy": "blocking"},
)

COSELECTION_GROUPS = (("L-ADVERSARIAL", "L-CLAIM"),)


def canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def registry_document(registry=LAYER_REGISTRY):
    """Return the JSON-compatible registry object sealed into a plan."""
    return {
        "layers": [
            {
                "id": row["id"],
                "dependencies": list(row["dependencies"]),
                "blockingPolicy": row["blockingPolicy"],
            }
            for row in registry
        ],
        "coSelectionGroups": [list(group) for group in COSELECTION_GROUPS],
    }


def registry_hash(registry=LAYER_REGISTRY) -> str:
    return hashlib.sha256(canonical_json(registry_document(registry)).encode("utf-8")).hexdigest()
