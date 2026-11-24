"""Read-only scope and built-in project findings before an audit."""

from . import c_check, c_scope


def preview(repo_root, *, full=False, profile=None):
    with c_scope.preview_context(repo_root, full=full, profile=profile) as (repo, scope, facts, plan):
        project_layer = "L-PROJECT" in plan.enabledLayers
        checks = []
        if project_layer:
            checks, _ = c_check._builtin({"repo": repo, "scope": scope, "config_facts": facts})
        return {
            "profileName": scope["profileName"],
            "mode": scope["mode"],
            "anchor": scope["anchor"]["runId"] if scope["anchor"] is not None else None,
            "changed": [row["path"] for row in scope["changed"]],
            "impacted": scope["impacted"],
            "projectLayer": project_layer,
            "checks": checks,
            "projectChecksSkipped": len(facts["projectChecks"]) if project_layer else 0,
            "warnings": scope["warnings"],
        }
