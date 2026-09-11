# Adopting docaudit in a project

日本語版: [ADOPTION.ja.md](ADOPTION.ja.md)

This guide takes a repository from "no audit" to "every change is checked against its documentation". It assumes docaudit 1.0.3 is installed as described in the [README](../README.md); the configuration reference is [CONFIG-1.0.0.md](CONFIG-1.0.0.md), and copy-paste prompts are in [PROMPTS.md](PROMPTS.md). The skill's instruction file (`SKILL.md`) is written in Japanese.

Commands in this guide use the engine path of the skills-dir install, `~/.claude/skills/docaudit/skills/audit/engine`. If you installed through the marketplace, substitute the engine path given in the README's install section.

## 1. The five-minute path

1. Install the plugin once (README, "Install") and start a new Claude Code session.
2. In the repository, create `.claude/docaudit.json` (section 4) and commit it.
3. Run `/docaudit:audit --full`. Read the report it publishes and fix the documents it flags.
4. Repeat `/docaudit:audit --full` until the run ends `CONSISTENT`. That run writes the first anchor.
5. From then on run `/docaudit:audit` after changes. Only the documents impacted by what changed are verified.

Before upgrading to 1.0.1, close every open run with `resume <runId> --abandon`. If an unclosed run is resumed in 1.0.1, it ends `REFUSED seal-drift` when its old scope includes a path under `.mdq/`, or `REFUSED worktree-modified` when `.mdq/` appears only in its old working-tree snapshot. In either case, run again to recover.

## 2. Mental model

docaudit compares Markdown documents with the code and configuration they describe and reports the documents that no longer match. It never edits a document: every run is report-only, and the only files it writes inside the repository are the report and the run state under `.claude/state/docaudit/`.

Five ideas explain everything else:

- **Corpus.** `corpus.docGlobs` selects the documents that can be audited. Files ignored by git are skipped unless `corpus.respectGitignore` is false.
- **Changes.** `changes.diffGlobs` selects the files whose changes trigger an audit. An incremental run compares the working tree with the last accepted run of the same profile.
- **Impact map.** `impact.map` links changed sources to the documents that describe them. The changed files, the map, the single-source-of-truth entries, and (when configured) the file-name heuristics decide which documents are impacted; `impact.maxImpactedDocs` caps that set.
- **Layers and profiles.** A run executes layers. `L-SCOPE` computes the impacted set, `L-DOC` verifies each impacted document, `L-PROJECT` runs the built-in document checks and your `projectChecks`; `L-ENRICH`, `L-SECURITY`, `L-ADVERSARIAL`, and `L-CLAIM` add review layers. A profile (`focused`, `standard`, `extended`) selects which layers run; `enabledLayers` in the configuration declares which layers the repository allows.
- **Verdict and anchor.** A completed run ends `CONSISTENT` or `NEEDS_FIX`. A `CONSISTENT` run advances the profile's anchor, and the next incremental run measures changes from that anchor. A run that cannot reach a verdict ends `undecided` with a reason, and a run whose sealed run contract fails the gate's integrity checks ends `REFUSED`.

Document verification is performed by a backend. When the Codex CLI passes the engine's availability check it is used; otherwise, inside Claude Code, the skill hands the verification to Claude Code agents through a workflow and resumes the engine when they finish.

## 3. Prerequisites

| requirement | notes |
|---|---|
| macOS or Linux | `projectChecks` run only on macOS (they use `sandbox-exec`). |
| Claude Code and git | The skill runs inside Claude Code; git supplies the diff and the snapshot. |
| Python 3.12 or newer | The engine uses only the standard library. |
| Codex CLI (optional) | Used when `codex` is on `PATH`, `codex --version` and `codex exec --help` work, and `auth.json` in the Codex home is readable. Required in practice for the `extended` profile (section 8). |
| Node.js (optional) | Only for running the repository's own test suite. |

A repository needs nothing beyond the configuration file. Markdown documents anywhere in the tree can be audited; the report path and the state directory are created on the first run.

## 4. Write the configuration

Create `.claude/docaudit.json`. Start from the minimal example in the README and adjust four things:

1. `corpus.docGlobs`: the documents to audit. Exclude generated or vendored documents with `corpus.excludeDocGlobs`.
2. `changes.diffGlobs`: the files whose changes should trigger an audit. Usually the source, configuration, and documentation directories together.
3. `impact.map`: at least one entry per source area (section 5).
4. `report.path`: a Markdown path whose basename has a nonempty prefix before exactly one `<YYYY-MM-DD>`, optionally followed by `[_NN]`, for example `reports/audit_<YYYY-MM-DD>[_NN].md`. The directory is created when the first report is published. Reports are never overwritten; a second run on the same day gets `_02`.

Leave `enabledLayers` at `["L-SCOPE", "L-DOC", "L-PROJECT"]` unless you intend to run the `extended` profile (section 8), and keep `documentChecks.frontMatterFields` and `documentChecks.indexFiles` empty until your documents follow those conventions. Unknown keys are rejected, so a typo fails validation with `config-invalid:<detail>` instead of being ignored.

Commit the configuration. It is part of the repository's contract, and the engine seals its bytes into every run: changing the file while a run is open makes the run `REFUSED config-drift`.

## 5. Build a good impact map

`impact.map` is a list of `{"source": <glob>, "docs": [<path>, ...]}` entries. When a changed file matches `source`, every listed document becomes impacted. Two rules make the map useful:

- **Map by responsibility, not by directory.** A document that explains the deployment procedure should be mapped from the deployment scripts and the configuration files it cites, wherever they live.
- **Prefer several small entries to one broad entry.** A single `src/**` entry that lists every document makes every change audit the whole corpus; the run then hits `impact.maxImpactedDocs` and ends `undecided impact-limit`.

`impact.ssotSources` works like the map for files that are the single source of truth for a value (a version file, a port table): a change to the source always impacts the documents that cite it.

`impact.heuristics` is optional and off unless the key is present. When configured, `L-SCOPE` takes the file name of every changed file, with and without its extension, and marks a document as impacted when its text contains one of those names as a plain substring. `minIdentifierLength` drops short names, `excludeBasenames` drops names such as `index.ts` or `README` that would match everywhere, `excludeDocPathTokens` ignores the names of changed documents themselves, and `saturationWarnRatio` warns when the documents impacted only by this matching reach that share of the whole corpus. Start without heuristics; add them when the map alone misses documents that name the changed files.

Set `impact.maxImpactedDocs` to the largest set you are willing to verify in one incremental run. A full run is not capped by it.

## 6. Migrate a legacy configuration

If the repository still has a legacy `.claude/doc-audit.json`, the `migrate` subcommand converts it:

```sh
python3 ~/.claude/skills/docaudit/skills/audit/engine migrate --dry-run --repo-root .   # shows the conversion; exit 1 = not convertible
python3 ~/.claude/skills/docaudit/skills/audit/engine migrate --repo-root .             # writes .claude/docaudit.json
```

The dry run prints a result of `convertible`, `not-convertible`, or `unchanged`, together with the converted configuration, `counts` (legacy history entries, whether a last-run record exists, and the list of dropped keys), the inputs it read with their hashes, `anchor` (never migrated), and `runOpen`. Project facts (document globs, diff globs, the impact map, report path, front-matter and index settings, heuristics, single-source-of-truth entries) are mapped to their 1.0.0 keys. Keys that described the old installation, command mappings, or optional tools are dropped, because 1.0.0 detects tool availability instead of configuring it. The full disposition table is in CONFIG-1.0.0.md, "Old-key disposition".

The converted configuration always enables `L-SCOPE`, `L-DOC`, and `L-PROJECT` with no `projectChecks`. If a legacy history file exists, its entries are copied into the new `history.jsonl` as `legacy` lines; legacy anchors are not migrated, so the first run of every profile must be `--full`. A completed migration leaves a marker in the history. Running `migrate` again with the same legacy inputs reports `unchanged` and writes nothing, even while a run is open; with changed legacy inputs it is rejected with `migration-input-changed`. Otherwise the full migration refuses to run while a run is open (`migration-run-open`) and refuses to replace an existing `.claude/docaudit.json` whose bytes differ from the conversion (`migration-target-exists`).

Review the new file, commit it, and remove the legacy file when you no longer need it. The engine reads only `.claude/docaudit.json`; a repository that still has only the legacy file ends with `config-needs-migration`.

## 7. Run audits

Inside Claude Code, in the repository:

```
/docaudit:audit --full                # whole corpus; required until the profile has an anchor
/docaudit:audit                       # incremental: documents impacted by changes since the anchor
/docaudit:audit --profile focused     # choose the profile; the default is standard
```

The skill runs the engine, and when the engine hands verification to Claude Code agents (`nextAction: invoke-workflow`) it launches the verification workflow and resumes the engine, up to three times, then abandons the run. At the end it shows the outcome, the reason, and the report path.

Outside Claude Code the same run is a command; it needs the Codex backend because no agents are available:

```sh
python3 ~/.claude/skills/docaudit/skills/audit/engine audit --full --profile standard --repo-root /path/to/repo
```

### The anchor lifecycle

- Anchors are kept per profile under `.claude/state/docaudit/anchors/`.
- The first anchor of a profile is written by the first full run of that profile that ends `CONSISTENT`. Until then, an incremental run of that profile ends `undecided anchor-missing`, so keep using `--full`.
- Every later run of the profile that ends `CONSISTENT` advances the anchor. A `NEEDS_FIX`, `undecided`, or `REFUSED` run leaves it unchanged, so the next incremental run measures the same changes again, plus anything new.
- Switching profiles starts a new lifecycle: a `focused` anchor does not serve a `standard` run.

### One run at a time

The engine holds a mutex and a lease, and a new audit is refused while an earlier run is still open. The refusal names the state, and each state has its own remedy:

- `run-in-progress`: another engine process is running the audit right now. Wait for it to finish; while it holds the lease, resume is refused with `resume-in-progress` and abandon with `run-in-progress`.
- `run-awaiting-external`: the run was handed to the verification workflow and not resumed. Resume it with `resume <runId>`; the result line tells you whether it closed or handed off again (`nextAction: invoke-workflow`), in which case run the workflow and resume once more.
- `run-interrupted-resume-required`: the engine process ended mid-run. Resume it with `resume <runId>`, or abandon it with `resume <runId> --abandon` to close it without a verdict.

The skill does not recover a run left over from an earlier session; do it by hand or with the prompts in PROMPTS.md, section 8.

## 8. Profiles and layers

| profile | layers | backend |
|---|---|---|
| `focused` | `L-SCOPE`, `L-DOC` | Codex when available, otherwise Claude Code agents |
| `standard` (default) | focused + `L-PROJECT` | same |
| `extended` | standard + `L-ENRICH`, `L-SECURITY`, `L-ADVERSARIAL`, `L-CLAIM` | same selection, but a verdict requires Codex |

`extended` can be selected only when `enabledLayers` lists all seven layers; otherwise the engine refuses to open the run with `capability-missing:<layer>` (exit status 3). Through Claude Code agents the security, adversarial, and claim layers are incomplete (`workflow-adapter-unavailable`) and the run ends `undecided`, so use `extended` where the Codex CLI passes the availability check.

In `extended`, the adversarial layer asks for evidence-backed contradictions per impacted document, the security layer reviews documented procedures, configuration, secret handling, and permissions once per run, and the claim layer re-checks every adversarial `FAIL` against the repository. Of the findings from these four layers, only a confirmed claim can make the verdict `NEEDS_FIX`; adversarial and security findings are informational. A document `FAIL` makes the verdict `NEEDS_FIX` in every profile, and a project-check `FAIL` does so in the profiles that run `L-PROJECT` (`standard` and `extended`).

## 9. Project checks

`L-PROJECT` always runs four read-only checks over the audited documents: required front-matter fields (WARN), local Markdown links (a missing target is a blocking FAIL), path-like backtick tokens that point at nothing (WARN), and documents that no other document or index file links to (WARN). `documentChecks.layerGlobs` removes matching documents from a check, for example to exempt generated indexes from the orphan check.

`projectChecks` adds your own commands. Each entry names a check ID, an `argv`, a timeout, and optionally a working directory. The command runs on macOS inside a `sandbox-exec` profile that forbids writes outside the private temporary directory, and must print a JSON object with a `findings` array, each finding having `id`, `summary`, `severity` (`INFO`, `WARN`, or `FAIL`), and optionally `path`. A `FAIL` finding is blocking: the run ends `NEEDS_FIX` even when every document passed. A nonzero exit, a timeout, or invalid output is itself a blocking `FAIL` finding. On Linux leave `projectChecks` empty: a non-empty list ends the run `undecided sandbox-unavailable`.

## 10. Read the results

- **The result line.** The last line of the engine output is one JSON object. A closed run has `nextAction` (`done` or `abort`), `runId`, `outcome`, and when present `reason` and `reportPath`; a hand-off to the workflow has `nextAction: invoke-workflow` with `requestSeq` and `requestPath` instead of an outcome. Exit status 0 means the engine finished normally, including `undecided`, `REFUSED`, and the hand-off; 3 means it refused to open a run (configuration, profile, or run-state problem, named in `reason`); 4 means an opened run could not proceed.
- **The report.** Published at `report.path` with fixed front matter and sections for the run, the audited documents, the findings, the verdict, the anchor, the measurements, and the evidence. Most of its headings and fixed phrases are Japanese. It has one line per finding: each document with its verdict and a one-line summary of the mismatch, and each other finding with its severity and summary, including project checks, links, and claims; the evidence strings behind a judgement (the Codex backend is asked to cite `file:line` in its rationale; Claude Code agents are asked for repository-relative evidence) are kept in the run's evidence ledger, in the `judgement` lines of the history, and, for runs verified through Claude Code agents, in `runs/<runId>/requests/<seq>/judgements/`. Unsafe summary text is redacted and counted as `- redacted: N`; history `judgement` lines use the same redact rule, while the ledger and `requests/<seq>/judgements/` remain original. Runs that end `undecided` before verification starts publish no report.
- **The state directory.** `.claude/state/docaudit/history.jsonl` records one line per run outcome and per judgement (plus `flip`, `anchor`, `legacy`, and `migration` lines), `anchors/<profile>.json` holds the current anchor, and `runs/<runId>/` holds the sealed manifest, the evidence ledger with the adapter results and judgements, and `verdict.json`. Decide once whether to commit the state directory (auditable history in version control) or ignore it (local state per clone); the engine works either way.

### Outcomes at a glance

| outcome | meaning | what to do |
|---|---|---|
| `CONSISTENT` | every impacted document matches and no blocking finding | nothing; the anchor advanced |
| `NEEDS_FIX` | at least one document fails, or a blocking finding (a broken link, a failed project check, a confirmed claim) | fix what the report names, run again |
| `undecided anchor-missing` | incremental run without an anchor | run with `--full` |
| `undecided backend-unavailable` | capability detection found no usable backend: no Codex, and not inside Claude Code | install or fix Codex, or run inside Claude Code |
| `undecided impact-limit` | more impacted documents than `impact.maxImpactedDocs` | narrow the map or raise the limit, or run `--full` |
| `undecided corpus-unreadable` | a document in the corpus could not be read while the verification mirror was prepared (Claude Code agent backend) | make the file readable (permissions, broken symlink) and run again |
| `undecided sandbox-unavailable` | `projectChecks` configured on a platform without `sandbox-exec` | empty `projectChecks` on Linux |
| `undecided workflow-adapter-unavailable` | `extended` through Claude Code agents | use the Codex backend |
| `undecided abandoned` | the run was abandoned | run again |
| `REFUSED worktree-modified` | the working tree changed during the run | do not edit or generate files while a run is open; run again |
| `REFUSED config-drift` | `.claude/docaudit.json` changed during the run | run again |
| other `REFUSED` reasons | the sealed run contract failed an integrity check (lease, seal, evidence hashes, layer set, backend, judgements) | inspect `runs/<runId>/`; run again |

## 11. Troubleshooting

- **`config-invalid:<detail>` on every run.** The detail names the offending key. Paths must be repository-relative without `..`; `report.path` must end in `.md`, contain exactly one `<YYYY-MM-DD>`, and have a nonempty basename prefix before it.
- **`config-needs-migration`.** Only the legacy file exists. Run `migrate` (section 6).
- **The first run reports many front-matter or orphan warnings.** They are non-blocking. Fix them over time, or exempt documents with `documentChecks.layerGlobs`.
- **`history-corrupt`.** `history.jsonl` has a malformed line, is not a regular file, is not UTF-8, or contains an overlong line; an anchor file under `anchors/` is unreadable or malformed; or the anchor candidate that a `CONSISTENT` run points at is unreadable or does not match its recorded hash. The engine neither reads nor appends history until this is repaired. Keep a copy of the whole state directory, then find which file is broken: a malformed history line is fixed by moving the history file aside while preserving its valid lines under another name and starting a new history file; a broken anchor is fixed by moving that profile's anchor file aside, after which the profile needs a new `--full` run. If the failure happened inside a run (exit status 4), that run is still open: resume or abandon it before the next audit. If `history.jsonl` itself cannot be read because of permissions, it stops with exit status 4 and reason `PermissionError`, not `history-corrupt`; repair permissions and run again.
- **`mutex-timeout` or `run-in-progress`.** Another engine process holds the run. Wait for it, or resume or abandon the run named in `run-open.json` once no process holds it.
- **The skill is not listed after install.** Start a new Claude Code session or run `/reload-plugins`, then check `claude plugin list`.
- **Something was written into the repository that you did not expect.** Inside the repository the engine writes only the report and `.claude/state/docaudit/`. The top-level `.mdq/` (mdq's index and usage record) is the only exception: since 1.0.1 the gate ignores it. Anything else came from another tool that ran during the audit; that is also what makes a run `REFUSED worktree-modified`.
