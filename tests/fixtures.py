"""Small synthetic repositories used by engine tests."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from skills.audit.engine import procs
from skills.audit.engine.profiles import PROFILE_TABLE


ALL_LAYERS = (
    "L-SCOPE",
    "L-DOC",
    "L-PROJECT",
    "L-ENRICH",
    "L-SECURITY",
    "L-ADVERSARIAL",
    "L-CLAIM",
)


def profile_table_fixture(*, max_calls=None, novel=False):
    rows = [dict(row, enabledLayers=tuple(row["enabledLayers"])) for row in PROFILE_TABLE]
    if max_calls is not None:
        rows[-1]["maxModelCalls"] = max_calls
    if novel:
        rows.append({
            "name": "novel",
            "enabledLayers": ("L-SCOPE", "L-DOC", "L-PROJECT", "L-ENRICH"),
            "backendDirective": "auto", "default": False,
            "targetDuration": None, "maxModelCalls": None,
        })
    return tuple(rows)


def init_repo(root: Path, *, layers=("L-SCOPE", "L-DOC", "L-PROJECT")) -> dict:
    """Create a minimal committed project without borrowing real project data."""
    procs.run_subprocess(
        ["git", "init"], cwd=root, check=True, stdout=subprocess.DEVNULL
    )
    (root / ".claude").mkdir()
    (root / "docs").mkdir()
    (root / "src").mkdir()
    (root / "docs" / "a.md").write_text("# A\n", encoding="utf-8")
    (root / "docs" / "b.md").write_text("# B\n", encoding="utf-8")
    (root / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    config = {
        "docauditSchema": "1.0",
        "enabledLayers": list(layers),
        "corpus": {
            "docGlobs": ["docs/**"],
            "excludeDocGlobs": [],
            "respectGitignore": True,
            "auditReportsInCorpus": False,
        },
        "changes": {"diffGlobs": ["src/**"], "regressionRecheck": False},
        "impact": {
            "map": [{"source": "src/**", "docs": ["docs/a.md"]}],
            "maxImpactedDocs": 20,
            "ssotSources": [],
        },
        "report": {"path": "reports/audit_<YYYY-MM-DD>[_NN].md"},
        "documentChecks": {},
        "projectChecks": [],
    }
    (root / ".claude" / "docaudit.json").write_text(
        json.dumps(config, sort_keys=True), encoding="utf-8"
    )
    procs.run_subprocess(["git", "add", "."], cwd=root, check=True)
    procs.run_subprocess(
        [
            "git",
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture.invalid",
            "commit",
            "-m",
            "fixture",
        ],
        cwd=root,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    return config


def legacy_repo(root: Path, *, entries: int, with_last_run: bool) -> dict:
    """Create a synthetic pre-1.0 repository without a new-format config."""
    procs.run_subprocess(
        ["git", "init"], cwd=root, check=True, stdout=subprocess.DEVNULL
    )
    (root / ".claude" / "state").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "src").mkdir()
    (root / "docs" / "guide.md").write_text("# Guide\n", encoding="utf-8")
    (root / "src" / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    config = {
        "anchorPath": ".claude/state/legacy-anchor.json",
        "diffGlobs": ["src/**"],
        "docGlobs": ["docs/**"],
        "excludeDocGlobs": ["docs/generated/**"],
        "respectGitignore": True,
        "impactMap": [{
            "changed": "src/**",
            "impacts": ["docs/guide.md"],
            "note": "synthetic note",
            "source": "docs/legacy-map.md",
        }],
        "maxImpactedDocs": 25,
        "reportPath": "reports/audit_<YYYY-MM-DD>[_NN].md",
        "indexFiles": ["docs/guide.md"],
        "frontMatterFields": ["title"],
        "frontMatterOverrides": [{
            "globs": ["docs/**", "guides/**"],
            "fields": ["title"],
        }],
        "heuristics": {
            "minIdentifierLength": 3,
            "excludeBasenames": ["generated"],
        },
        "layerGlobs": {
            "format": {"exclude": ["docs/**"]},
            "semantic": {"exclude": ["docs/guide.md"]},
            "existence": {"exclude": ["docs/**"]},
            "boundary": {"exclude": ["src/**"]},
        },
        "regressionRecheck": {"enabled": True},
        "ssotSources": [
            {
                "name": "guide",
                "value": "synthetic",
                "liveSource": "docs/source.md",
                "docsThatCite": ["docs/guide.md:12", "docs/guide.md", "docs/other.md:7"],
            },
            {
                "name": "remote",
                "liveSource": "https:" + "//fixture.invalid/source",
                "docsThatCite": ["docs/guide.md"],
            },
        ],
        "auditReportsInCorpus": False,
        "harness": "fixture",
        "docAuditCommands": {},
        "reviewCommands": {},
        "codexReview": {},
        "boundaryCommand": [],
        "auditScope": {},
        "expectedAbsentPaths": [],
        "webExtract": {},
        "symbolGraph": {},
        "semanticSearch": {},
        "indexing": {},
        "contextMode": "fixture",
        "_note": "synthetic annotation",
    }
    config_path = root / ".claude" / "doc-audit.json"
    config_path.write_text(json.dumps(config, sort_keys=True), encoding="utf-8")

    history_entries = []
    run_ids = [f"20260101T00000{index}Z-{index + 1:08x}" for index in range(3)]
    verdicts = ("CONSISTENT", "NEEDS_FIX", "REFUSED")
    for index in range(entries):
        entry = {
            "runid": run_ids[index % len(run_ids)],
            "path": f"docs/item-{index:02d}.md",
            "contentSha": f"content-{index:02d}",
            "changeSetSha": f"change-{index:02d}",
            "contractVersion": "0.fixture",
            "ts": f"2026-01-01T00:00:{index:02d}.000000+00:00",
            "verdict": verdicts[index % len(verdicts)],
        }
        if index % 2 == 0:
            entry["backend"] = "fixture-backend"
        history_entries.append(entry)
    if history_entries:
        (root / ".claude" / "state" / "docaudit-history.json").write_text(
            json.dumps({"entries": history_entries}, sort_keys=True), encoding="utf-8"
        )

    last_run = None
    if with_last_run:
        last_run = {
            "runid": run_ids[-1],
            "ts": "2026-01-01T00:01:00.000000+00:00",
            "verdict": "NEEDS_FIX",
        }
        (root / ".claude" / "state" / "docaudit-last-run.json").write_text(
            json.dumps(last_run, sort_keys=True), encoding="utf-8"
        )
    return {"config": config, "entries": history_entries, "lastRun": last_run}


def fake_codex_tools(root: Path):
    """Create a shell-based Codex fixture, PATH symlink, and private home."""
    binary_root = root / "fixture-tools"; path_dir = binary_root / "bin"; home = binary_root / "codex-home"
    path_dir.mkdir(parents=True); home.mkdir(); (home / "auth.json").write_text("fixture-auth", encoding="utf-8")
    real = binary_root / "codex-real"
    real.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then echo 'codex fixture'; exit 0; fi\n"
        "if [ \"$1\" = \"exec\" ] && [ \"$2\" = \"--help\" ]; then exit 0; fi\n"
        "out=''\nwhile [ $# -gt 0 ]; do if [ \"$1\" = '-o' ]; then out=$2; shift 2; else shift; fi; done\n"
        "[ -f \"$CODEX_HOME/auth.json\" ] || exit 1\n"
        "prompt=$(/bin/cat)\n"
        "role=$(printf '%s\\n' \"$prompt\" | /usr/bin/sed -n '1s/^docaudit-role: //p')\n"
        "runid=$(printf '%s' \"$prompt\" | /usr/bin/sed -n 's/.*JSON: {\"runId\":\"\\([^\"]*\\)\",\"path\":\"[^\"]*\"}.*/\\1/p')\n"
        "path=$(printf '%s' \"$prompt\" | /usr/bin/sed -n 's/.*JSON: {\"runId\":\"[^\"]*\",\"path\":\"\\([^\"]*\\)\"}.*/\\1/p')\n"
        "[ \"$role\" = adversarial ] && path=$(printf '%s\\n' \"$prompt\" | /usr/bin/sed -n 's/^Target document: //p')\n"
        "[ \"$role\" = claim ] && path=$(printf '%s\\n' \"$prompt\" | /usr/bin/sed -n 's/^file: //p')\n"
        "case \"$prompt\" in *contentHash*) exit 9;; esac\n"
        "mode=$(/bin/cat \"$CODEX_HOME/mode\" 2>/dev/null || printf ok)\n"
        "if [ \"$role\" = adversarial ]; then\n"
        "  if [ \"$mode\" = optional ] || [ \"$mode\" = unverified ]; then\n"
        "    case \"$path\" in docs/a.md) printf '{\"findings\":[{\"severity\":\"critical\",\"title\":\"critical mismatch\",\"file\":\"docs/a.md\"},{\"severity\":\"medium\",\"title\":\"medium note\",\"file\":\"docs/a.md\"}]}' > \"$out\";; docs/b.md) printf '{\"findings\":[{\"severity\":\"high\",\"title\":\"high mismatch\",\"file\":\"docs/b.md\"}]}' > \"$out\";; esac\n"
        "  elif [ \"$mode\" = metric ] && [ \"$path\" = docs/a.md ]; then printf '{\"findings\":[{\"severity\":\"critical\",\"title\":\"metric mismatch\",\"file\":\"docs/a.md\"}]}' > \"$out\"\n"
        "  else printf '{\"findings\":[]}' > \"$out\"; fi; exit 0\n"
        "fi\n"
        "if [ \"$role\" = security ]; then printf '{\"findings\":[]}' > \"$out\"; exit 0; fi\n"
        "if [ \"$role\" = claim ]; then\n"
        "  finding=$(printf '%s\\n' \"$prompt\" | /usr/bin/sed -n 's/^findingId: //p')\n"
        "  if [ \"$mode\" = unverified ] || { [ \"$mode\" = metric ] && [ \"${out##*/}\" = out-1.json ]; }; then printf '{\"findingId\":\"%s\",\"state\":\"unverified\",\"evidenceFile\":null,\"evidenceLine\":null,\"rationale\":\"not yet\"}' \"$finding\" > \"$out\"\n"
        "  elif [ \"$path\" = docs/b.md ]; then printf '{\"findingId\":\"%s\",\"state\":\"rejected\",\"evidenceFile\":\"docs/b.md\",\"evidenceLine\":1,\"rationale\":\"not supported\"}' \"$finding\" > \"$out\"\n"
        "  else printf '{\"findingId\":\"%s\",\"state\":\"confirmed\",\"evidenceFile\":\"docs/a.md\",\"evidenceLine\":1,\"rationale\":\"supported\"}' \"$finding\" > \"$out\"; fi; exit 0\n"
        "fi\n"
        "if [ \"$mode\" = metric ] && [ \"$role\" = judge ] && [ \"$path\" = docs/a.md ] && [ \"${out##*/}\" = out-1.json ]; then exit 9; fi\n"
        "case \"$mode:$path\" in\n"
        " hang:*|mixedhang:docs/b.md) trap '' TERM; echo $$ >> \"$CODEX_HOME/hang-parent.pid\"; /bin/sh -c 'trap \"\" TERM; echo $$ >> \"$1\"; /bin/sleep 60' sh \"$CODEX_HOME/hang-child.pid\" & /bin/sleep 60;;\n"
        " mixedhang:docs/a.md) /bin/sleep 1;;\n"
        " fifo:docs/a.md) /bin/mv \"$out\" \"$out.old\"; /usr/bin/mkfifo \"$out\"; exit 0;;\n"
        " large:docs/a.md) /usr/bin/head -c 2097153 /dev/zero > \"$out\"; exit 0;;\n"
        " invalid:docs/a.md) printf '{' > \"$out\"; exit 0;;\n"
        " identity:docs/a.md) path='docs/other.md';;\n"
        " type:docs/a.md) printf '{\"runId\":\"%s\",\"path\":\"%s\",\"verdict\":\"PASS\",\"rationale\":\"docs/a.md:1 ok\",\"evidence\":\"bad\"}' \"$runid\" \"$path\" > \"$out\"; exit 0;;\n"
        "esac\n"
        "verdict=PASS; rationale='docs/a.md:1 ok'; [ \"$mode\" = fail ] && verdict=FAIL && rationale='docs/a.md:1 verdict: CONSISTENT'\n"
        "printf '{\"runId\":\"%s\",\"path\":\"%s\",\"verdict\":\"%s\",\"rationale\":\"%s\",\"evidence\":[\"docs/a.md:1\"]}' \"$runid\" \"$path\" \"$verdict\" \"$rationale\" > \"$out\"\n",
        encoding="utf-8",
    )
    real.chmod(0o700); (path_dir / "codex").symlink_to(real)
    return real, path_dir, home


def term_tree_script(root: Path, *, parent_exits: bool):
    """Create a TERM-handling parent with a TERM-ignoring descendant."""
    path = root / ("parent-exits.sh" if parent_exits else "hang.sh")
    parent_trap = "trap 'exit 0' TERM" if parent_exits else "trap '' TERM"
    path.write_text(
        "#!/bin/sh\n" + parent_trap + "\n"
        "sh -c 'trap \"\" TERM; echo $$ > \"$1\"; while :; do sleep 1; done' sh \"$2\" &\n"
        "echo $$ > \"$1\"\nwhile :; do sleep 1; done\n",
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path


def fake_mdq(root: Path, *, mode: str = "healthy"):
    """Create a deterministic mdq stand-in and return ``(binary, PATH dir)``."""
    tool_root = root / "fixture-mdq"
    path_dir = tool_root / "bin"
    path_dir.mkdir(parents=True)
    binary = path_dir / "mdq"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys, time\n"
        f"MODE = {mode!r}\n"
        "args = sys.argv[1:]\n"
        "command = args[0] if args else ''\n"
        "def option(name, default=None):\n"
        "    try: return args[args.index(name) + 1]\n"
        "    except (ValueError, IndexError): return default\n"
        "def database():\n"
        "    path = pathlib.Path(option('--db'))\n"
        "    if not path.is_absolute(): path = pathlib.Path.cwd() / path\n"
        "    return path.resolve()\n"
        "if command in ('index', 'stats', 'list', 'search') and option('--lang') != 'ja-jp':\n"
        "    raise SystemExit(3)\n"
        "if command == 'index':\n"
        "    if MODE == 'timeout': time.sleep(60)\n"
        "    root = pathlib.Path(option('--root', '.')).resolve()\n"
        "    files = []\n"
        "    for path in sorted(root.rglob('*.md')):\n"
        "        if '.mdq' in path.parts: continue\n"
        "        rel = path.relative_to(root).as_posix()\n"
        "        text = path.read_text(encoding='utf-8')\n"
        "        heading = next((line.lstrip('#').strip() for line in text.splitlines() if line.startswith('#')), 'Document')\n"
        "        files.append({'path': rel, 'content': text, 'heading_path': [heading], 'chunk_id': rel + ':1'})\n"
        "    db = database(); db.parent.mkdir(parents=True, exist_ok=True)\n"
        "    db.write_text(json.dumps({'files': files, 'chunks': 0 if MODE == 'chunks-zero' else len(files)}, sort_keys=True), encoding='utf-8')\n"
        "    usage = pathlib.Path.cwd() / '.mdq' / 'usage.jsonl'\n"
        "    usage.parent.mkdir(parents=True, exist_ok=True)\n"
        "    usage.write_text(json.dumps({'command': 'index'}) + '\\n', encoding='utf-8')\n"
        "    if MODE == 'mutate-mirror' and files:\n"
        "        (root / files[0]['path']).write_text('# Mirror changed\\n', encoding='utf-8')\n"
        "elif command == 'stats':\n"
        "    value = json.loads(database().read_text(encoding='utf-8'))\n"
        "    if MODE == 'nonutf8-stats': sys.stdout.buffer.write(b'\\xff'); raise SystemExit(0)\n"
        "    print(json.dumps({'files': len(value['files']), 'chunks': value['chunks']}))\n"
        "elif command == 'list':\n"
        "    value = json.loads(database().read_text(encoding='utf-8'))\n"
        "    for item in value['files']: print(json.dumps({'path': item['path'], 'heading_path': item['heading_path'], 'lines': [1, 1]}))\n"
        "elif command == 'search':\n"
        "    value = json.loads(database().read_text(encoding='utf-8'))\n"
        "    query = option('--q', '')\n"
        "    selected = option('--paths')\n"
        "    for item in value['files']:\n"
        "        if selected and item['path'] != selected: continue\n"
        "        haystack = item['content'] + ' ' + ' '.join(item['heading_path'])\n"
        "        if query.lower() in haystack.lower():\n"
        "            print(json.dumps({'path': item['path'], 'chunk_id': item['chunk_id'], 'heading_path': item['heading_path']})); break\n"
        "elif command == 'get':\n"
        "    value = json.loads(database().read_text(encoding='utf-8'))\n"
        "    chunk = option('--chunk-id')\n"
        "    item = next((row for row in value['files'] if row['chunk_id'] == chunk), None)\n"
        "    if item is None: raise SystemExit(1)\n"
        "    print(json.dumps(item))\n"
        "else:\n"
        "    raise SystemExit(2)\n",
        encoding="utf-8",
    )
    binary.chmod(0o700)
    return binary, path_dir


def simulate_external(repo: Path, run_id: str, *, behaviour: str = "normal"):
    """Write one Workflow generation as a synthetic external agent would."""
    requests = repo / ".claude" / "state" / "docaudit" / "runs" / run_id / "requests"
    candidates = [
        path for path in requests.glob("request-*.json")
        if path.stem.removeprefix("request-").isdigit()
    ]
    candidates.sort(key=lambda path: int(path.stem.removeprefix("request-")))
    if not candidates:
        raise FileNotFoundError("workflow request not issued")
    request_path = candidates[-1]
    request = json.loads(request_path.read_text(encoding="utf-8"))
    documents = list(request["documents"])
    completed = list(documents)
    if behaviour == "one-missing" and completed:
        completed = completed[:-1]
    if behaviour == "done-missing":
        return request

    written_ids = []
    for document in completed:
        value = {
            "runId": request["runId"],
            "requestSeq": request["requestSeq"],
            "attempt": request["attempt"],
            "docId": document["docId"],
            "path": document["path"],
            "verdict": "PASS",
            "rationale": f"{document['path']}:1 fixture pass",
            "evidence": [f"{document['path']}:1"],
            "retrievalUsed": request["retrieval"]["method"],
        }
        if behaviour == "stale-request":
            value["requestSeq"] -= 1
            value["attempt"] -= 1
        elif behaviour == "not-requested":
            value["docId"] = "f" * 16
        target = repo / document["judgementPath"]
        target.parent.mkdir(parents=True, exist_ok=True)
        if behaviour == "invalid-json":
            target.write_text("{", encoding="utf-8")
        else:
            target.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        written_ids.append(document["docId"])

    done_documents = written_ids
    if behaviour == "not-requested":
        done_documents = ["f" * 16, *written_ids]
    done = {
        "runId": request["runId"],
        "requestSeq": request["requestSeq"],
        "attempt": request["attempt"],
        "documents": done_documents,
        "invocations": {"reader": 1, "verifiers": len(documents), "closer": 1},
    }
    if behaviour == "done-invalid":
        done["attempt"] += 1
    done_path = repo / request["donePath"]
    done_path.parent.mkdir(parents=True, exist_ok=True)
    done_path.write_text(json.dumps(done, sort_keys=True), encoding="utf-8")
    if behaviour == "stray-write":
        (repo / "unexpected-output.txt").write_text("external write", encoding="utf-8")
    return request
