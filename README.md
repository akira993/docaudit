# docaudit

docaudit audits a repository's Markdown documentation against the code and configuration it describes, and tells you which documents no longer match. It runs as a Claude Code plugin skill (`/docaudit:audit`) or as a plain Python command, and it is report-only: it never edits your documents.

New to docaudit? Read [docs/ADOPTION.md](docs/ADOPTION.md) (日本語: [docs/ADOPTION.ja.md](docs/ADOPTION.ja.md)) for the full adoption guide, and [docs/PROMPTS.md](docs/PROMPTS.md) (日本語: [docs/PROMPTS.ja.md](docs/PROMPTS.ja.md)) for copy-paste prompts.

A completed run ends in one verdict, `CONSISTENT` or `NEEDS_FIX`, backed by a per-document judgement whose evidence strings (the Codex backend is asked to cite `file:line` in its rationale; Claude Code agents are asked for repository-relative evidence) are kept in the run's records, and publishes a Markdown report into your repository. A run that cannot reach a verdict ends as `undecided` with a machine-readable `reason` (for example when no verification backend is available), and a run whose sealed evidence or working tree fails the gate's integrity checks (for example, the evidence ledger was tampered with, the working tree changed during the audit, or claim records are inconsistent) ends as `REFUSED`. Runs are change-driven: after the first full audit, later runs look only at the documents impacted by what changed.

## Requirements

- macOS or Linux.
- Claude Code (the plugin host) and git.
- Python 3.12 or newer. The engine uses only the standard library.
- Optional: the Codex CLI. When the engine's availability check passes (`codex` on `PATH`, a working `codex --version` and `codex exec --help`, and a readable `auth.json` in the Codex home) it sends document verification to Codex; otherwise, inside Claude Code, the skill runs the verification through Claude Code agents.
- Node.js is needed only to run the repository's own test suite.

## Install

The plugin is installed as a "skills-dir" plugin: the tracked files of the release tag are placed under `~/.claude/skills/docaudit/`. If that directory already exists it is moved aside first, so that no files from another version remain.

```sh
SRC=$(mktemp -d) && TAR=$(mktemp) \
  && git clone https://github.com/akira993/docaudit "$SRC" \
  && { [ ! -e ~/.claude/skills/docaudit ] \
       || { [ ! -e ~/.claude/skills/docaudit.before-1.0.1 ] && mv ~/.claude/skills/docaudit ~/.claude/skills/docaudit.before-1.0.1; }; } \
  && [ ! -e ~/.claude/skills/docaudit ] \
  && git -C "$SRC" archive --format=tar -o "$TAR" v1.0.1 \
  && mkdir -p ~/.claude/skills/docaudit \
  && tar -x -f "$TAR" -C ~/.claude/skills/docaudit \
  && python3 ~/.claude/skills/docaudit/skills/audit/engine --version   # prints 1.0.1
```

The chain stops at the first step that fails, and an existing install is never overwritten in place. If it stops after the backup was made, `~/.claude/skills/docaudit` holds at most an unverified copy: remove that directory and move `~/.claude/skills/docaudit.before-1.0.1` back. If the backup name already exists from an earlier attempt, rename that older backup first.

Start a new Claude Code session (or run `/reload-plugins`); `claude plugin list` then shows `docaudit@skills-dir` at version 1.0.1, and the skill is available as `/docaudit:audit`.

Alternatively, install through the plugin marketplace that this repository declares. Use one install method, not both: two enabled plugins with the same name have not been verified, so if `~/.claude/skills/docaudit` already exists, move it away first.

```sh
claude plugin marketplace add akira993/docaudit@v1.0.1 \
  && claude plugin install docaudit@akira-plugins \
  && python3 ~/.claude/plugins/cache/akira-plugins/docaudit/1.0.1/skills/audit/engine --version   # prints 1.0.1
```

Start a new Claude Code session (or run `/reload-plugins`); `claude plugin list` then shows `docaudit@akira-plugins` at version 1.0.1. If you installed this way, use that engine path wherever the commands below say `~/.claude/skills/docaudit/skills/audit/engine`. The path assumes the default configuration directory; if the last command fails, locate `skills/audit/engine` under your plugin cache and use that path instead.

## Configure the repository

Create `.claude/docaudit.json` in the repository you want to audit. This minimal configuration audits everything under `docs/`, treats changes under `src/` as the trigger, and maps those changes to the documents they may affect:

```json
{
  "docauditSchema": "1.0",
  "enabledLayers": ["L-SCOPE", "L-DOC", "L-PROJECT"],
  "corpus": {"docGlobs": ["docs/**"], "excludeDocGlobs": [], "respectGitignore": true, "auditReportsInCorpus": false},
  "changes": {"diffGlobs": ["src/**"], "regressionRecheck": false},
  "impact": {"map": [{"source": "src/**", "docs": ["docs/a.md", "docs/b.md", "docs/c.md"]}], "maxImpactedDocs": 20, "ssotSources": []},
  "report": {"path": "reports/audit_<YYYY-MM-DD>[_NN].md"},
  "documentChecks": {"frontMatterFields": [], "indexFiles": []},
  "projectChecks": []
}
```

- `corpus.docGlobs` selects the documents to audit; `changes.diffGlobs` selects the files whose changes trigger an audit.
- `impact.map` links changed sources to the documents that describe them; `impact.maxImpactedDocs` caps one incremental run.
- `report.path` is where each run publishes its report; `<YYYY-MM-DD>` and the optional `[_NN]` suffix keep reports unique.
- Link, existence and orphan checks on the documents always run. `documentChecks.frontMatterFields` and `documentChecks.indexFiles` add required front-matter fields and index files that every document should be reachable from; enable those two once your documents follow the convention, otherwise the first run reports the gaps.
- `projectChecks` runs your own commands as part of the audit, inside the macOS `sandbox-exec` sandbox. On Linux leave it empty: a non-empty `projectChecks` makes the run end `undecided`.
- `enabledLayers` declares the layers the repository allows. The `extended` profile can only be selected when all seven layers are listed (see the profile table below).

Every key, its default and its validation rule is documented in [docs/CONFIG-1.0.0.md](docs/CONFIG-1.0.0.md). If the repository still has a legacy `.claude/doc-audit.json`, `python3 ~/.claude/skills/docaudit/skills/audit/engine migrate --dry-run --repo-root .` shows how it would be converted (exit status 1 means it cannot be converted), and the same command without `--dry-run` writes the new `.claude/docaudit.json` next to the legacy file.

## Run an audit

In Claude Code, inside the repository:

```
/docaudit:audit --full            # first run of a profile: audit the whole corpus
/docaudit:audit                   # later runs: only the documents impacted by changes since the last accepted run
/docaudit:audit --profile focused # choose a profile (focused, standard, extended); each profile starts with its own --full run
```

Anchors are kept per profile. The first anchor of a profile is written only when a full run of that profile ends `CONSISTENT`; until then every run of the profile must be started with `--full`, and an incremental run without an anchor ends `undecided` with the reason `anchor-missing`. After that, any run of the profile that ends `CONSISTENT` advances the anchor, and incremental runs measure changes from it.

Profiles decide which layers run:

| profile | layers | use |
|---|---|---|
| `focused` | scope, document verification | quick check of the impacted documents |
| `standard` (default) | focused + project checks | the everyday audit |
| `extended` | standard + enrichment, security, adversarial and claim review | release sweeps. Requires all seven layers in `enabledLayers` and the Codex backend: through Claude Code agents the security, adversarial and claim layers are incomplete and the run ends `undecided` without a verdict |

Outside Claude Code, with the Codex CLI available, the same audit runs as a command:

```sh
python3 ~/.claude/skills/docaudit/skills/audit/engine audit --full --profile standard --repo-root /path/to/repo
```

## What you get

- The result, printed as the last line of output as one JSON object: `nextAction` (`done`, `abort`, or `invoke-workflow` while the skill hands verification to Claude Code agents), `outcome`, `reason` when there is one, and `reportPath` when a report was published.
- The report at `report.path`: the verdict, the audited documents, and one line per finding: each document with its verdict and a one-line summary of the mismatch, and each other finding with its severity and summary, including project checks, links, and claims; the evidence strings behind a judgement are kept in the run's evidence ledger and history. Most of the report's headings and fixed phrases are Japanese. Runs that end `undecided` before verification starts publish no report.
- Run state under `.claude/state/docaudit/`: `history.jsonl` (one line per run and per judgement), and one directory per run holding the sealed manifest, the evidence ledger, the per-document judgements and the final verdict. Add the state directory to version control or ignore it as you prefer. Inside the repository the audit writes only there and to the report path; scratch files go to a private temporary directory outside the repository, and `migrate` writes the converted configuration.
- Exit status 0 when the engine finished normally (a verdict, an `undecided` or `REFUSED` outcome, or a hand-off to Claude Code agents), 3 when it refused to open a run (the JSON `reason` says why), 4 when an opened run could not proceed.

## Layout

- `skills/audit/` — the audit skill and the engine (`skills/audit/engine`); the skill's instruction file (`SKILL.md`) is written in Japanese; the engine's JSON output is language-neutral
- `agents/`, `workflows/` — the Claude Code agent and workflow used when Codex is not available
- `docs/` — the configuration reference, and the adoption guide and prompt examples in English and Japanese
- `tests/` — unit and acceptance tests

Run the tests from a checkout (Node.js required for the workflow-script tests) with:

```sh
python3 -m unittest discover -s tests -t . -q
python3 tests/run_acceptance.py --expect all
```

## License

MIT. See [LICENSE](LICENSE).
