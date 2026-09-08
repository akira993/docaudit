"""Fixed, versioned run-profile data.

Profile names deliberately live in this module only.  Consumers select rows as
data and must not branch on a name.
"""

from __future__ import annotations

import hashlib
import json


PROFILE_TABLE = (
    {
        "name": "focused",
        "enabledLayers": ("L-SCOPE", "L-DOC"),
        "backendDirective": "auto",
        "default": False,
        "targetDuration": None,
        "maxModelCalls": None,
    },
    {
        "name": "standard",
        "enabledLayers": ("L-SCOPE", "L-DOC", "L-PROJECT"),
        "backendDirective": "auto",
        "default": True,
        "targetDuration": None,
        "maxModelCalls": None,
    },
    {
        "name": "extended",
        "enabledLayers": (
            "L-SCOPE",
            "L-DOC",
            "L-PROJECT",
            "L-ENRICH",
            "L-SECURITY",
            "L-ADVERSARIAL",
            "L-CLAIM",
        ),
        "backendDirective": "auto",
        "default": False,
        "targetDuration": None,
        "maxModelCalls": None,
    },
)


def canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def table_document(table=PROFILE_TABLE):
    return [
        {
            "name": row["name"],
            "enabledLayers": list(row["enabledLayers"]),
            "backendDirective": row["backendDirective"],
            "default": row["default"],
            "targetDuration": row["targetDuration"],
            "maxModelCalls": row["maxModelCalls"],
        }
        for row in table
    ]


def profile_table_hash(table=PROFILE_TABLE) -> str:
    return hashlib.sha256(canonical_json(table_document(table)).encode("utf-8")).hexdigest()


def profile_names(table=PROFILE_TABLE):
    return tuple(row["name"] for row in table)
