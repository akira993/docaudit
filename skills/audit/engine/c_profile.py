"""Validation and deterministic planning for fixed profile data."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from collections.abc import Mapping, Sequence

from .layers import COSELECTION_GROUPS, LAYER_REGISTRY, registry_hash
from .profiles import PROFILE_TABLE, profile_table_hash


class ProfileRejected(ValueError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class SealedPlan:
    profileName: str
    profileSelectionSource: str
    enabledLayers: tuple[str, ...]
    backendDirective: str
    profileTableHash: str
    registryHash: str
    planHash: str

    # Friendly Python aliases retain the externally sealed camel-case schema.
    @property
    def profile_name(self):
        return self.profileName

    @property
    def enabled_layers(self):
        return self.enabledLayers

    def document(self, include_plan_hash=True):
        value = {
            "profileName": self.profileName,
            "profileSelectionSource": self.profileSelectionSource,
            "enabledLayers": list(self.enabledLayers),
            "backendDirective": self.backendDirective,
            "profileTableHash": self.profileTableHash,
            "registryHash": self.registryHash,
        }
        if include_plan_hash:
            value["planHash"] = self.planHash
        return value


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _rows(registry):
    if not isinstance(registry, Sequence) or isinstance(registry, (str, bytes)):
        raise ProfileRejected("registry-schema")
    result = []
    seen = set()
    for row in registry:
        if not isinstance(row, Mapping):
            raise ProfileRejected("registry-schema")
        identifier = row.get("id")
        dependencies = row.get("dependencies")
        if not isinstance(identifier, str) or not identifier or identifier in seen:
            raise ProfileRejected("registry-schema")
        if not isinstance(dependencies, Sequence) or isinstance(dependencies, (str, bytes)):
            raise ProfileRejected("registry-schema")
        if not all(isinstance(item, str) and item for item in dependencies):
            raise ProfileRejected("registry-schema")
        seen.add(identifier)
        result.append((identifier, tuple(dependencies)))
    if not result:
        raise ProfileRejected("registry-empty")
    ids = {identifier for identifier, _ in result}
    if any(dependency not in ids for _, dependencies in result for dependency in dependencies):
        raise ProfileRejected("registry-dependency")
    return result


def _validate_selection(layers, registry):
    rows = _rows(registry)
    ids = {identifier for identifier, _ in rows}
    selected = set(layers)
    unknown = selected - ids
    if unknown:
        raise ProfileRejected("unknown-layer:" + sorted(unknown)[0])
    dependencies = dict(rows)
    for identifier in selected:
        for dependency in dependencies[identifier]:
            if dependency not in selected:
                raise ProfileRejected("dependency-not-closed:" + identifier + "->" + dependency)
    for group in COSELECTION_GROUPS:
        present = selected.intersection(group)
        if present and present != set(group):
            raise ProfileRejected("coselection-required:" + ",".join(group))
    return rows


def _validate_row(row, registry):
    if not isinstance(row, Mapping):
        raise ProfileRejected("profile-schema")
    required = {"name", "enabledLayers", "backendDirective", "default", "targetDuration", "maxModelCalls"}
    if set(row) != required:
        raise ProfileRejected("profile-schema")
    if not isinstance(row["name"], str) or not row["name"]:
        raise ProfileRejected("profile-schema")
    layers = row["enabledLayers"]
    if not isinstance(layers, Sequence) or isinstance(layers, (str, bytes)) or not layers:
        raise ProfileRejected("profile-schema")
    if not all(isinstance(layer, str) and layer for layer in layers) or len(set(layers)) != len(layers):
        raise ProfileRejected("profile-schema")
    if not isinstance(row["backendDirective"], str) or not row["backendDirective"]:
        raise ProfileRejected("profile-schema")
    if not isinstance(row["default"], bool):
        raise ProfileRejected("profile-schema")
    if row["targetDuration"] is not None and not isinstance(row["targetDuration"], (int, float)):
        raise ProfileRejected("profile-schema")
    if row["maxModelCalls"] is not None and (not isinstance(row["maxModelCalls"], int) or isinstance(row["maxModelCalls"], bool) or row["maxModelCalls"] < 1):
        raise ProfileRejected("profile-schema")
    _validate_selection(tuple(layers), registry)


def validate_table(table, registry=LAYER_REGISTRY):
    """Validate all fixed rows before any run starts."""
    if not isinstance(table, Sequence) or isinstance(table, (str, bytes)) or not table:
        raise ProfileRejected("profile-table-empty")
    names = set()
    defaults = 0
    for row in table:
        _validate_row(row, registry)
        if row["name"] in names:
            raise ProfileRejected("profile-name-duplicate:" + row["name"])
        names.add(row["name"])
        defaults += row["default"]
    if defaults != 1:
        raise ProfileRejected("profile-default-count:" + str(defaults))


def _topological_layers(selected, registry):
    rows = _validate_selection(selected, registry)
    selected_set = set(selected)
    emitted = []
    remaining = {identifier: set(deps) for identifier, deps in rows if identifier in selected_set}
    while remaining:
        ready = [identifier for identifier, _ in rows if identifier in remaining and not remaining[identifier]]
        if not ready:
            raise ProfileRejected("registry-cycle")
        for identifier in ready:
            emitted.append(identifier)
            del remaining[identifier]
            for dependencies in remaining.values():
                dependencies.discard(identifier)
    return tuple(emitted)


def resolve(table, name_or_None, capability, registry=LAYER_REGISTRY) -> SealedPlan:
    """Seal the selected fixed-table row against declared capabilities."""
    validate_table(table, registry)
    if not isinstance(capability, frozenset):
        capability = frozenset(capability)
    selected_row = None
    if name_or_None is None:
        selected_row = next(row for row in table if row["default"])
    else:
        for row in table:
            if row["name"] == name_or_None:
                selected_row = row
                break
    if selected_row is None:
        raise ProfileRejected("profile-not-found:" + str(name_or_None))
    requested = tuple(selected_row["enabledLayers"])
    missing = set(requested) - capability
    if missing:
        raise ProfileRejected("capability-missing:" + sorted(missing)[0])
    enabled = _topological_layers(requested, registry)
    value = {
        "profileName": selected_row["name"],
        "profileSelectionSource": "explicit",
        "enabledLayers": list(enabled),
        "backendDirective": selected_row["backendDirective"],
        "profileTableHash": profile_table_hash(table),
        "registryHash": registry_hash(registry),
    }
    plan_hash = hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()
    return SealedPlan(
        profileName=value["profileName"],
        profileSelectionSource=value["profileSelectionSource"],
        enabledLayers=tuple(value["enabledLayers"]),
        backendDirective=value["backendDirective"],
        profileTableHash=value["profileTableHash"],
        registryHash=value["registryHash"],
        planHash=plan_hash,
    )
