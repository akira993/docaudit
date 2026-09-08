# docaudit prompt examples

日本語版: [PROMPTS.ja.md](PROMPTS.ja.md)

Copy-paste prompts for running docaudit inside Claude Code. Each one names the outcome you should expect, so you can tell a normal result from a problem. The skill is `/docaudit:audit`; its options are `--full` and `--profile focused|standard|extended`. Everything the audit reports is read-only: docaudit never edits a document, so fixing what it finds is a separate step that you ask for explicitly.

Engine commands below use the skills-dir path `~/.claude/skills/docaudit/skills/audit/engine`; with a marketplace install, substitute the engine path from the README's install section.

## 1. First audit of a repository

Use after the configuration exists (see [ADOPTION.md](ADOPTION.md), section 4).

```
Run /docaudit:audit --full in this repository. When it finishes, open the published report and list every document it marked as failing with its summary; then, for each of them, show the evidence strings of its judgement from the run's evidence ledger under .claude/state/docaudit/runs/<runId>/ (for runs verified through Claude Code agents they are also in requests/<seq>/judgements/). Do not edit any document.
```

Expected: `NEEDS_FIX` or `CONSISTENT`. On the first run of a corpus `NEEDS_FIX` is normal. `undecided backend-unavailable` means capability detection found no backend at all, which happens outside Claude Code without a working Codex CLI.

## 2. Fix what the first audit found, then repeat

```
The last docaudit report is at reports/<file>.md. For each failing document, show me the mismatch and the code it contradicts, then propose the smallest documentation edit that makes the document correct. Apply the edits I approve, and nothing else. Then run /docaudit:audit --full again.
```

Expected: `CONSISTENT` once every document passes and no blocking finding remains (a broken local link or a failed project check also keeps the run at `NEEDS_FIX`). The `CONSISTENT` run writes the profile's anchor, and later runs can be incremental.

## 3. Regular incremental audit after a change

```
I changed the files in the last commit. Run /docaudit:audit and tell me which documents were impacted, the verdict, and the report path.
```

Expected: `CONSISTENT` when the documents still match, `NEEDS_FIX` with the failing documents otherwise. `undecided anchor-missing` means this profile has no anchor yet: run prompt 1 with `--full` first. `undecided impact-limit` means the change impacted more documents than `impact.maxImpactedDocs` allows: run with `--full`, or narrow the impact map.

## 4. Quick check with the focused profile

```
Run /docaudit:audit --profile focused --full and summarize only the document verification results. Skip the project checks for now.
```

Expected: the `focused` profile runs scope and document verification only. It keeps its own anchor, so its first run must also be `--full`.

## 5. Pre-release sweep with the extended profile

Requires the Codex CLI to pass the availability check and `enabledLayers` to list all seven layers.

```
Run /docaudit:audit --profile extended --full. Report the verdict, then list the adversarial and security findings by severity, and for each confirmed claim show the evidence file and line.
```

Expected: `CONSISTENT` or `NEEDS_FIX` with the confirmed claims marked as blocking. If the engine refuses with `capability-missing:<layer>`, add that layer to `enabledLayers`. If the run ends `undecided workflow-adapter-unavailable`, the verification went through Claude Code agents instead of Codex, which cannot run the review layers.

## 6. Migrate a legacy configuration

```
This repository has a legacy .claude/doc-audit.json. Run the docaudit migrate subcommand with --dry-run (python3 ~/.claude/skills/docaudit/skills/audit/engine migrate --dry-run --repo-root .), show me the converted configuration, the counts, and the dropped keys, and stop. Do not write anything yet.
```

Then, after reviewing:

```
Run the same migrate command without --dry-run, show me the resulting .claude/docaudit.json, and then run /docaudit:audit --full.
```

Expected: the dry run exits 0 with `convertible`, or with `unchanged` when a completed migration with the same legacy inputs is already recorded; exit 1 means `not-convertible` and the result's `reason` says why. The full migration writes the new file. Run again with the same legacy inputs after a completed migration, it reports `unchanged` and writes nothing, even while a run is open; otherwise it refuses to run while a run is open (`migration-run-open`), refuses to replace an existing `.claude/docaudit.json` whose bytes differ from the conversion (`migration-target-exists`), and rejects changed legacy inputs after a completed migration (`migration-input-changed`).

## 7. Understand an undecided or REFUSED result

```
The last /docaudit:audit ended with outcome <undecided|REFUSED> and reason <reason>. Explain what that reason means for docaudit 1.0.0 using docs/CONFIG-1.0.0.md from the plugin directory, tell me whether the anchor moved, and tell me what to run next. Do not change any file.
```

Expected: an explanation and a next step. Common cases: `anchor-missing` (run `--full`), `worktree-modified` (something wrote into the repository during the audit; run again without other tools active), `config-drift` (the configuration changed during the run; run again), `run-awaiting-external` or `run-interrupted-resume-required` (an earlier run is still open; see prompt 8).

## 8. Resume or abandon a run left open

The skill does not recover a run left over from an earlier session, so do it explicitly.

```
docaudit refused to start because an earlier run is still open; its result line gave the reason <run-in-progress|run-awaiting-external|run-interrupted-resume-required>. Read .claude/state/docaudit/run-open.json and tell me the run ID. Decide by the reason, not by the state in that file: if the reason is run-in-progress, tell me to wait, because another process holds the run. If it is run-awaiting-external or run-interrupted-resume-required, resume it with: python3 ~/.claude/skills/docaudit/skills/audit/engine resume <runId> --repo-root . If the result says nextAction invoke-workflow, run the docaudit verification workflow for that request and resume again. If resuming keeps failing, abandon the run with the same command plus --abandon and tell me why.
```

Expected: the run reaches `closed`, and the next audit can start. Abandoning records the run as closed without a verdict (`undecided abandoned`); the anchor does not move.

## 9. Scheduled or non-interactive runs

Outside Claude Code the engine needs the Codex backend. From a shell or a scheduler:

```sh
python3 ~/.claude/skills/docaudit/skills/audit/engine audit --profile standard --repo-root /path/to/repo
```

Read the last line of the output as JSON: `outcome` and `reportPath`. Exit status 0 covers `CONSISTENT`, `NEEDS_FIX`, `undecided`, and `REFUSED`; use `outcome` to decide, not the exit status. Exit status 3 means the engine refused to open a run and `reason` says why.

To run the skill itself non-interactively, use Claude Code in print mode from the repository:

```sh
claude -p '/docaudit:audit' --permission-mode acceptEdits --allowedTools 'Bash,Read,Grep,Glob,Write,Workflow,Skill,Agent'
```

Expected: the same outcomes as an interactive run. The session must be allowed to run the workflow and its agents, which is what the tool list above grants.
