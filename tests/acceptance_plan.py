ORDER = ["foundation", "scope-report-history", "engine-evidence-gate", "backend", "optional", "migration"]
PHASES = {
    "foundation": {"design": ["T-SAFE-1", "T-SAFE-2", "T-PROFILE-1", "T-PROFILE-2", "T-PROFILE-5", "T-STATE-1"], "route": ["R-CONFIG-1"]},
    "scope-report-history": {"design": ["T-SCOPE-1", "T-BACKEND-3", "T-HISTORY-1", "T-HISTORY-2", "T-REPORT-2"], "route": []},
    "engine-evidence-gate": {"design": ["T-CORE-1", "T-PROFILE-3", "T-PROFILE-4", "T-PROFILE-6", "T-STATE-2", "T-SAFE-6", "T-EVIDENCE-1", "T-REPORT-1", "T-METRIC-2"], "route": []},
    "backend": {"design": ["T-CORE-2", "T-BACKEND-1", "T-BACKEND-2", "T-CHECK-1", "T-CHECK-2", "T-SAFE-3", "T-SAFE-4", "T-SAFE-5", "T-SCOPE-2"], "route": ["R-CAP-1", "R-WF-1", "R-WF-2", "R-WF-3", "R-WF-4", "R-WF-5", "R-DOC-1"]},
    "optional": {"design": ["T-OPTIONAL-1", "T-OPTIONAL-2", "T-OPTIONAL-3", "T-BUDGET-1", "T-PROFILE-7", "T-METRIC-1"], "route": ["R-PROFILE-1"]},
    "migration": {"design": ["T-MIGRATE-1", "T-MIGRATE-2"], "route": ["R-MIG-1"]},
}
