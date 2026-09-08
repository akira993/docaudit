"""Explicit runtime dependencies for the audit engine.

Backend discovery and layer execution stay injectable.  The
real implementations are registered by the engine; this module is the one
stable seam shared by sealing and the deterministic gate.
"""

from __future__ import annotations

import datetime as _datetime
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Mapping

from . import procs
from .profiles import PROFILE_TABLE


DEFAULT_CODEX_MODEL = "gpt-5.6-terra"
DEFAULT_WORKFLOW_MODEL = "sonnet"
PROBE_CONTRACT_VERSION = "capability/1.1"


@dataclass(frozen=True)
class CapabilityResult:
    available: bool
    reason: str | None
    cliVersion: str | None
    executableHash: str | None
    homeOrigin: str | None
    homePathHash: str | None
    authPresent: bool | None
    authReadable: bool | None
    probeContractVersion: str | None
    workflowAvailable: bool = False
    workflowReason: str | None = None

    def document(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def workflow_available(self) -> bool:
        return self.workflowAvailable


@dataclass(frozen=True)
class AdapterResult:
    layerId: str
    producerId: str
    status: str
    reason: str | None = None
    judgements: tuple[dict[str, Any], ...] = ()
    findings: tuple[dict[str, Any], ...] = ()
    modelCalls: tuple[dict[str, Any], ...] = ()
    observations: Mapping[str, Any] = field(default_factory=dict)

    def document(self) -> dict[str, Any]:
        value = {
            "layerId": self.layerId,
            "producerId": self.producerId,
            "status": self.status,
            "judgements": [dict(item) for item in self.judgements],
            "findings": [dict(item) for item in self.findings],
            "modelCalls": [dict(item) for item in self.modelCalls],
            "observations": dict(self.observations),
        }
        if self.reason is not None:
            value["reason"] = self.reason
        return value


@dataclass
class EngineDeps:
    clock: Callable[[], Any]
    capability_resolver: Callable[[str, Mapping[str, str]], CapabilityResult]
    layer_adapters: Mapping[str, Callable[[dict[str, Any]], AdapterResult | dict[str, Any]]]
    run_subprocess: Callable[..., Any]
    fault_hooks: Mapping[str, Callable[..., Any]] = field(default_factory=dict)
    profile_table: tuple[Mapping[str, Any], ...] = PROFILE_TABLE


def _now() -> str:
    return (
        _datetime.datetime.now(_datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def unavailable(reason: str = "unavailable") -> CapabilityResult:
    """Return the single safe representation of an unavailable backend."""
    return CapabilityResult(False, reason, None, None, None, None, None, None, None, False, None)


def capability_document(result: CapabilityResult | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(result, CapabilityResult):
        return result.document()
    if isinstance(result, Mapping):
        return dict(result)
    raise TypeError("capability-result")


def adapter_document(result: AdapterResult | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(result, AdapterResult):
        return result.document()
    if isinstance(result, Mapping):
        return dict(result)
    raise TypeError("adapter-result")


def _workflow_available(result: CapabilityResult | Mapping[str, Any]) -> bool:
    """Read the explicit workflow capability field."""
    if isinstance(result, Mapping):
        return result.get("workflowAvailable") is True
    return getattr(result, "workflow_available", False) is True


def resolve_backend(
    directive: str, capability_result: CapabilityResult | Mapping[str, Any]
) -> str | None:
    """Resolve one backend/model from a sealed directive and probe result."""
    facts = capability_document(capability_result)
    codex_available = facts.get("available") is True
    workflow_available = _workflow_available(capability_result)
    if directive.startswith("codex:"):
        return directive if codex_available and len(directive) > len("codex:") else None
    if directive.startswith("workflow:"):
        return directive if workflow_available and len(directive) > len("workflow:") else None
    if directive != "auto":
        return None
    if codex_available:
        return "codex:" + DEFAULT_CODEX_MODEL
    if workflow_available:
        return "workflow:" + DEFAULT_WORKFLOW_MODEL
    return None


def _scope_adapter(ctx: dict[str, Any]) -> AdapterResult:
    scope = ctx["scope"]
    return AdapterResult(
        layerId=ctx["layer_id"],
        producerId=ctx["layer_id"],
        status="complete",
        observations=scope,
    )


def _unavailable_adapter(ctx: dict[str, Any]) -> AdapterResult:
    return AdapterResult(
        layerId=ctx["layer_id"],
        producerId=ctx["layer_id"],
        status="incomplete",
        reason="adapter-unavailable",
    )


def production() -> EngineDeps:
    """Construct production adapters for the implemented layers."""
    from . import c_cap, c_check, c_dispatch, c_optional
    from .layers import LAYER_REGISTRY

    adapters = {row["id"]: _unavailable_adapter for row in LAYER_REGISTRY}
    scope_id = LAYER_REGISTRY[0]["id"]
    adapters[scope_id] = _scope_adapter
    adapters["L-DOC"] = c_dispatch.adapter
    adapters["L-PROJECT"] = c_check.adapter
    adapters["L-ENRICH"] = c_optional.enrich
    adapters["L-SECURITY"] = c_optional.security
    adapters["L-ADVERSARIAL"] = c_optional.adversarial
    adapters["L-CLAIM"] = c_optional.claim
    return EngineDeps(
        clock=_now,
        capability_resolver=c_cap.detect,
        layer_adapters=adapters,
        run_subprocess=procs.run_subprocess,
        fault_hooks={},
        profile_table=PROFILE_TABLE,
    )
