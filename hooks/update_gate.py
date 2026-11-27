"""Request approval before docaudit updates and rollbacks.

The hook checks commands whose normalized text contains an engine entry path and update.
It misses a path supplied solely by an earlier assignment or the environment,
a bare path after changing to skills/audit, python3 -m skills.audit.engine,
and a path or the word update assembled during execution, such as
skills/audit/e*, eng${x}ine, or $'\\x75pdate'. A literal engine path assigned in
the same command is visible to the check.
Commands containing the engine path and update prompt for approval outside the
listed forms, including plan, check, or notify with extra arguments, directory
searches for update, and commit messages quoting a full update command.
"""

import json
import re
import sys


ENGINE_PATH = "skills/audit/engine"
ENGINE_TARGET = re.compile(re.escape(ENGINE_PATH) + r"(?:/__main__\.py|/\.|/)?(?![\w./-])")
SHELL_QUOTES = str.maketrans("", "", "'\"\\")
PATH_BOUNDARY = re.compile(r"([\s;|&<>()`]+)")
PATH_CHARS = r"\w./~+@:\-"


def _literal_path(suffix: str = "") -> str:
    repeat = "*" if suffix else "+"
    return (
        rf"(?:(?!-)[{PATH_CHARS}]{repeat}{suffix}"
        rf"|'(?!-)[{PATH_CHARS} \t]{repeat}{suffix}'"
        rf'|"(?!-)(?:[{PATH_CHARS} \t]|\$\{{CLAUDE_PLUGIN_ROOT\}}){repeat}{suffix}")'
    )


# An internal newline can start a second command.
SAFE_UPDATE_COMMAND = re.compile(
    r"(?:(?:/[\w.+@-]+)*/)?python3?[ \t]+"
    r"(?:-[BIEsSu][ \t]+)*"
    rf"{_literal_path(re.escape(ENGINE_PATH))}[ \t]+update[ \t]+"
    rf"(?:(?:--source|--install-dir)[ \t]+{_literal_path()}[ \t]+)*"
    r"(?:plan(?:[ \t]+--to[ \t]+v?[0-9]+\.[0-9]+\.[0-9]+)?"
    r"|check(?:[ \t]+--hook)?|notify[ \t]+(?:on|off)|rollback[ \t]+--dry-run)"
)


def _normalize_path(path: str) -> str:
    components = path.split("/")
    stack = []
    folded_parent = False
    last = len(components) - 1
    for index, component in enumerate(components):
        if not component or (component == "." and 0 < index < last):
            continue
        folded_parent = component == ".." and bool(stack) and stack[-1] != ".."
        if folded_parent:
            stack.pop()
        else:
            stack.append(component)

    leading_slash = "/" if path.startswith("/") else ""
    trailing_slash = "/" if stack and (path.endswith("/") or folded_parent) else ""
    return leading_slash + "/".join(stack) + trailing_slash


def _normalize(command: str) -> str:
    return command.replace("\\\n", "").translate(SHELL_QUOTES).lower()


def _fold_paths(command: str) -> str:
    pieces = PATH_BOUNDARY.split(command)
    for index in range(0, len(pieces), 2):
        if "/" in pieces[index]:
            pieces[index] = _normalize_path(pieces[index])
    return "".join(pieces)


def requires_approval(command: str) -> bool:
    try:
        normalized = _normalize(command)
        folded = _fold_paths(normalized)
        return (
            "update" in normalized
            and (ENGINE_TARGET.search(normalized) is not None or ENGINE_TARGET.search(folded) is not None)
            and SAFE_UPDATE_COMMAND.fullmatch(command.strip(" \t\n")) is None
        )
    except Exception:
        return ENGINE_PATH in command.lower()


def main() -> int:
    try:
        command = json.load(sys.stdin)["tool_input"]["command"]
    except Exception:
        return 0
    if isinstance(command, str) and command and requires_approval(command):
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": "Updating or rolling back docaudit requires user approval.",
            },
        }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
