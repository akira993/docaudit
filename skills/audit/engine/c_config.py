"""Loading and sealing the 1.0 project configuration."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass

from .c_io import IoRejected, read_bytes
from .layers import LAYER_REGISTRY, canonical_json


class ConfigRejected(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class SealedConfig:
    bytes_sha256: str
    normalized_json: str
    capability: frozenset[str]
    facts: dict


_TOP = {"docauditSchema", "enabledLayers", "corpus", "changes", "impact", "report", "documentChecks", "projectChecks"}
_CORPUS = {"docGlobs", "excludeDocGlobs", "respectGitignore", "auditReportsInCorpus"}
_CHANGES = {"diffGlobs", "regressionRecheck"}
_IMPACT = {"map", "maxImpactedDocs", "heuristics", "ssotSources"}
_REPORT = {"path"}
_CHECKS = {"frontMatterFields", "frontMatterOverrides", "indexFiles", "layerGlobs"}
_BUILTIN_CHECKS = {"front-matter", "links", "existence", "orphan"}


def _bad(detail: str):
    raise ConfigRejected("config-invalid:" + detail)


def _object(value, label):
    if not isinstance(value, dict):
        _bad(label)
    return value


def _keys(value, allowed, label):
    value = _object(value, label)
    unknown = set(value) - allowed
    if unknown:
        _bad(label + "." + sorted(unknown)[0])
    return value


def _path(value, label):
    if not isinstance(value, str) or not value:
        _bad(label)
    if "\x00" in value or value.startswith(("/", "\\")) or (len(value) >= 2 and value[1] == ":") or ".." in value.split("/"):
        _bad(label)


def _strings(value, label, paths=False):
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        _bad(label)
    if paths:
        for item in value:
            _path(item, label)


def _bool(value, label):
    if not isinstance(value, bool):
        _bad(label)


def _report_path(value):
    _path(value, "report.path")
    if not value.endswith(".md") or value.count("<YYYY-MM-DD>") != 1 or value.count("[_NN]") > 1:
        _bad("report.path")
    base = os.path.basename(value)
    before = base.split("<YYYY-MM-DD>", 1)[0]
    if not before:
        _bad("report.path")


def _validate(data):
    if not isinstance(data, dict):
        _bad("root")
    unknown = set(data) - _TOP
    if unknown:
        _bad(sorted(unknown)[0])
    for required in ("docauditSchema", "enabledLayers", "corpus", "changes", "impact", "report"):
        if required not in data:
            _bad("missing-" + required)
    if data["docauditSchema"] != "1.0":
        _bad("docauditSchema")
    _strings(data["enabledLayers"], "enabledLayers")
    known = {row["id"] for row in LAYER_REGISTRY}
    if any(item not in known for item in data["enabledLayers"]):
        _bad("enabledLayers")
    corpus = _keys(data["corpus"], _CORPUS, "corpus")
    if "docGlobs" not in corpus:
        _bad("missing-corpus.docGlobs")
    _strings(corpus["docGlobs"], "corpus.docGlobs", True)
    corpus.setdefault("excludeDocGlobs", [])
    corpus.setdefault("respectGitignore", True)
    corpus.setdefault("auditReportsInCorpus", False)
    _strings(corpus["excludeDocGlobs"], "corpus.excludeDocGlobs", True)
    _bool(corpus["respectGitignore"], "corpus.respectGitignore")
    _bool(corpus["auditReportsInCorpus"], "corpus.auditReportsInCorpus")
    changes = _keys(data["changes"], _CHANGES, "changes")
    if "diffGlobs" not in changes:
        _bad("missing-changes.diffGlobs")
    _strings(changes["diffGlobs"], "changes.diffGlobs", True)
    changes.setdefault("regressionRecheck", False)
    _bool(changes["regressionRecheck"], "changes.regressionRecheck")
    impact = _keys(data["impact"], _IMPACT, "impact")
    for required in ("map", "maxImpactedDocs"):
        if required not in impact:
            _bad("missing-impact." + required)
    if not isinstance(impact["map"], list): _bad("impact.map")
    for item in impact["map"]:
        item = _keys(item, {"source", "docs"}, "impact.map")
        if set(item) != {"source", "docs"}: _bad("impact.map")
        _path(item["source"], "impact.map.source"); _strings(item["docs"], "impact.map.docs", True)
    if isinstance(impact["maxImpactedDocs"], bool) or not isinstance(impact["maxImpactedDocs"], int) or impact["maxImpactedDocs"] < 1: _bad("impact.maxImpactedDocs")
    if "heuristics" in impact:
        heuristics = _keys(impact["heuristics"], {"minIdentifierLength", "excludeBasenames", "saturationWarnRatio", "excludeDocPathTokens"}, "impact.heuristics")
        for key, value in heuristics.items():
            if key == "minIdentifierLength" and (isinstance(value, bool) or not isinstance(value, int)): _bad("impact.heuristics." + key)
            if key == "excludeBasenames": _strings(value, "impact.heuristics." + key)
            if key == "saturationWarnRatio" and (isinstance(value, bool) or not isinstance(value, (int, float))): _bad("impact.heuristics." + key)
            if key == "excludeDocPathTokens": _bool(value, "impact.heuristics." + key)
    impact.setdefault("ssotSources", [])
    if not isinstance(impact["ssotSources"], list): _bad("impact.ssotSources")
    for item in impact["ssotSources"]:
        item = _keys(item, {"source", "docs"}, "impact.ssotSources")
        if set(item) != {"source", "docs"}: _bad("impact.ssotSources")
        _path(item["source"], "impact.ssotSources.source"); _strings(item["docs"], "impact.ssotSources.docs", True)
    report = _keys(data["report"], _REPORT, "report")
    if set(report) != {"path"}: _bad("report.path")
    _report_path(report["path"])
    checks = _keys(data.setdefault("documentChecks", {}), _CHECKS, "documentChecks")
    checks.setdefault("frontMatterFields", []); checks.setdefault("frontMatterOverrides", []); checks.setdefault("indexFiles", []); checks.setdefault("layerGlobs", {})
    _strings(checks["frontMatterFields"], "documentChecks.frontMatterFields")
    if not isinstance(checks["frontMatterOverrides"], list): _bad("documentChecks.frontMatterOverrides")
    for item in checks["frontMatterOverrides"]:
        item = _keys(item, {"glob", "fields"}, "documentChecks.frontMatterOverrides")
        if set(item) != {"glob", "fields"}: _bad("documentChecks.frontMatterOverrides")
        _path(item["glob"], "documentChecks.frontMatterOverrides.glob"); _strings(item["fields"], "documentChecks.frontMatterOverrides.fields")
    _strings(checks["indexFiles"], "documentChecks.indexFiles", True)
    if not isinstance(checks["layerGlobs"], dict): _bad("documentChecks.layerGlobs")
    for key, value in checks["layerGlobs"].items():
        if key not in _BUILTIN_CHECKS: _bad("documentChecks.layerGlobs." + str(key))
        _strings(value, "documentChecks.layerGlobs." + key, True)
    projects = data.setdefault("projectChecks", [])
    if not isinstance(projects, list): _bad("projectChecks")
    for item in projects:
        item = _keys(item, {"id", "argv", "timeoutSec", "cwd"}, "projectChecks")
        if not {"id", "argv", "timeoutSec"} <= set(item): _bad("projectChecks")
        if not isinstance(item["id"], str): _bad("projectChecks.id")
        _strings(item["argv"], "projectChecks.argv")
        if isinstance(item["timeoutSec"], bool) or not isinstance(item["timeoutSec"], int) or item["timeoutSec"] < 1: _bad("projectChecks.timeoutSec")
        if "cwd" in item: _path(item["cwd"], "projectChecks.cwd")


def load_config(repo) -> SealedConfig:
    try:
        raw = read_bytes(repo, ".claude/docaudit.json")
    except FileNotFoundError as exc:
        try:
            read_bytes(repo, ".claude/doc-audit.json")
        except FileNotFoundError:
            raise ConfigRejected("config-missing") from exc
        raise ConfigRejected("config-needs-migration") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigRejected("config-invalid:json") from exc
    _validate(data)
    facts = {key: data[key] for key in ("corpus", "changes", "impact", "report", "documentChecks", "projectChecks")}
    return SealedConfig(hashlib.sha256(raw).hexdigest(), canonical_json(data), frozenset(data["enabledLayers"]), facts)
