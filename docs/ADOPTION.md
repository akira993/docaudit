# Adopting docaudit in a project

日本語版: [ADOPTION.ja.md](ADOPTION.ja.md)

This guide takes a repository from "no audit" to "every change is checked against its documentation". It assumes docaudit 2.1.0 is installed as described in the [README](../README.md); the configuration reference is [CONFIG-1.0.0.md](CONFIG-1.0.0.md), and copy-paste prompts are in [PROMPTS.md](PROMPTS.md). The three skills' instruction files (`SKILL.md`) are written in Japanese.

Commands in this guide use the engine path of the skills-dir install, `~/.claude/skills/docaudit/skills/audit/engine`. If you installed through the marketplace, substitute the engine path given in the README's install section.

## 1. The five-minute path

1. Install the plugin once (README, "Install") and start a new Claude Code session.
2. In the repository, create `.claude/docaudit.json` (section 4) and commit it.
3. If you want to fix built-in check findings before the audit, run `/docaudit:preflight --full` and approve the fixes you want. Add `--deep` to include a review against the audit verifier's criteria. Run `/docaudit:audit --full`. Read the report it publishes and fix the documents it flags.
4. Repeat `/docaudit:audit --full` until the run ends `CONSISTENT`. That run writes the first anchor. If `NEEDS_FIX` is blocked only by document judgements, `/docaudit:audit --full --accept-baseline` accepts the baseline and enables later incremental runs. It cannot accept broken local links, failed project checks, or any other non-document blocking finding; fix those findings in the report first.
5. From then on run `/docaudit:audit` after changes. Documents impacted by changes are judged; when `changes.regressionRecheck` is enabled, documents whose latest judgement for the profile was `FAIL` are also judged. Enabled built-in `L-PROJECT` checks inspect corpus documents without limiting themselves to the impacted set.

The 1.0.1 upgrade required closing open runs with `resume <runId> --abandon` because it excluded `.mdq/`. An unclosed run resumed in 1.0.1 ended `REFUSED seal-drift` when its old scope included a path under `.mdq/`, or `REFUSED worktree-modified` when `.mdq/` appeared only in its old working-tree snapshot. Starting a new audit recovered either case.

Before upgrading to 2.0.0, use the earlier engine to finish every open run (a `.claude/state/docaudit/run-open.json` with `state` other than `closed`) or close it with `resume <runId> --abandon`. The worktree snapshot format for Git-ignored regular files changed and the top-level `.mdq/` exclusion was removed. After upgrading, a resumed run ends `REFUSED worktree-modified` if the engine detects a difference between the old and new worktree records as an untolerated change. Codex and agy runs with an empty `worktree.tolerateChangesIn` save no `tree.before.json`. If the engine cannot recover the earlier record, `worktreeDiff` is empty even when the worktree digest changes. A Claude Workflow run opened in 1.x and resumed in 2.0.0 ends `REFUSED request-drift` if a previously accepted judgement fails the new proof checks. If documents remain unresolved after the third request, it ends `undecided external-incomplete`. Neither outcome advances the anchor. The default verifier models are the same as in 1.9.0. An older saved scope keeps its original document candidates. Start a new audit to include eligible `.mdq/` documents.

### Updates and notifications

Use `/docaudit:update` to review release notes, commits, and the diff summary. Use `/docaudit:update rollback` to review the previous version. Approve either operation in the Claude Code permission prompt.

The `PreToolUse` hook also loads from `~/.claude/skills/docaudit/`. It asks for permission when a Bash command contains the docaudit engine path (`skills/audit/engine`) and the text `update`. Detection ignores quoting, case, and equivalent path components written with `//`, `/./`, or `..`. This pauses unrequested updates and rollbacks at the permission prompt. A search for `update` inside the engine directory or a commit message quoting a full update command also triggers the prompt.

The hook skips the prompt only when the entire command matches this form. Start with `python3`, `python`, or an absolute Python path like `/usr/bin/python3`. The only Python flags allowed are the separate single-letter flags `-B`, `-I`, `-E`, `-s`, `-S`, and `-u`. Next comes the engine path and `update`, with optional `--source <value>` and `--install-dir <value>`. Finish with `plan`, `plan --to <version>`, `check`, `check --hook`, `notify on`, `notify off`, or `rollback --dry-run`. For example, adding `-W ignore` or appending `2>&1`, `| tail`, or `; echo` triggers a single permission prompt. A non-interactive `claude -p` run blocks commands that trigger the prompt because no user can answer it.

The hook checks the literal command text, so a model can bypass it. For example, it cannot recognize `python3 "$E" update apply` when `E` is set by an earlier command or in the environment, a bare relative engine path after changing to `skills/audit`, `python3 -m skills.audit.engine`, or an engine path or `update` assembled at runtime. You can still run the engine directly from a terminal.

From a shell, run the engine with:

- `update check` to fetch the latest `vX.Y.Z` tag and compare it with the installed version.
- `update notify on|off` to control the opt-in SessionStart hook. It fetches tags for the source at most once every 24 hours, including after a failed fetch, and notifies you when a newer version is known.
- `update plan [--to vX.Y.Z]` to review a release, or `update apply --to vX.Y.Z` to install it.
- `update rollback [--dry-run]` to restore the previous version or check whether that rollback is available.

All subcommands accept `--source` and `--install-dir`. Check and plan read the installed manifest without applying the replacement prerequisites. Apply and rollback replace skills-dir installs and refuse marketplace installs and development checkouts. Several commands used for replacement and verification have a 60-second timeout per invocation. If each of those commands finishes close to its limit, the process takes several minutes in total. Notify, apply, and rollback wait up to 15 seconds for the update lock. Check takes the lock without waiting. The session-start hook does the same when notifications are enabled. Plan and a rollback with no update state directory take no update lock.

Before replacement, apply compares the staged files with the Git tree at the tag. Both apply and a non-dry-run rollback verify the candidate version and plugin name, require staging and installation to be on the same filesystem, and run `claude plugin validate` if the CLI is available. After replacement they verify the installed version, and, for the standard skills-dir install when Claude is available, check the plugin list and look for the audit skill in the output of `claude plugin details`. That command has no `--json` option. If a format change prevents the engine from finding the audit skill on the `Skills` line, verification fails and the engine attempts to restore the previous version.

On a replacement failure or handled interruption, the updater attempts to restore the previous version. Update state is under `${CLAUDE_CONFIG_DIR:-~/.claude}/docaudit-update/`. See the [configuration reference](CONFIG-1.0.0.md#update-state) for the files there. If automatic restoration did not finish, find the matching `started` entry in `history.jsonl`. If `installDir` is occupied, first move that directory to an unused path outside `skills/`. Then move `asideDir` to `installDir`. Confirm the restored version with the engine's `--version`.

## 2. Mental model

docaudit compares Markdown documents with the code and configuration they describe and reports the documents that no longer match. An audit is report-only and writes only the report and run state under `.claude/state/docaudit/` inside the repository. The separate `/docaudit:preflight` skill edits only approved document fixes, and `/docaudit:update` manages the plugin install and settings.

Five ideas explain everything else:

- **Corpus.** `corpus.docGlobs` selects the documents that can be audited. Files ignored by git are skipped unless `corpus.respectGitignore` is false.
- **Changes.** `changes.diffGlobs` selects the files whose changes trigger an audit. An incremental run compares the working tree with the last accepted run of the same profile.
- **Impact map.** `impact.map` links changed sources to the documents that describe them. The changed files, the map, the single-source-of-truth entries, (when `impact.heuristics` is a nonempty object) the file-name heuristics, and (when enabled) `changes.regressionRecheck` for the profile's latest FAIL documents decide which documents are impacted; `impact.maxImpactedDocs` caps that set.
- **Layers and profiles.** A run executes layers. `L-SCOPE` computes the impacted set, `L-DOC` verifies each impacted document, `L-PROJECT` runs the built-in document checks and your `projectChecks`; `L-ENRICH`, `L-SECURITY`, `L-ADVERSARIAL`, and `L-CLAIM` add review layers. A profile (`focused`, `standard`, `extended`) selects which layers run; `enabledLayers` in the configuration declares which layers the repository allows.
- **Verdict and anchor.** A verdict is `CONSISTENT` or `NEEDS_FIX`. A `CONSISTENT` run advances the profile's anchor, and a full `NEEDS_FIX` run accepted with `--accept-baseline` advances it the same way; the next incremental run measures changes from that anchor either way. A run that cannot reach a verdict ends `undecided` with a reason, and a run whose sealed run contract fails the gate's integrity checks ends `REFUSED`.

Document verification is performed by a backend. Three exist: the Codex CLI, the agy CLI, and Claude Code agents. A non-interactive start takes the first usable one in the order Codex, agy, Claude Code agents; when the skill starts the audit and two or more are usable, it asks which to use (section 7). The choice is sealed into the run, and a resume uses the same backend. With Claude Code agents, the skill hands the verification to them through a workflow and resumes the engine when they finish.

## 3. Prerequisites

| requirement | notes |
|---|---|
| macOS | The write guard for `audit`, `resume` without `--abandon`, and `migrate` when it writes the converted configuration uses `fcntl.F_GETPATH`; without it those commands stop with `io-unsupported-platform` (status 3 for `audit` and `resume`, 4 for `migrate` that reaches writes, all with `runId: null`). Linux is expected to stop for this reason; `scope`, `precheck`, `migrate --dry-run`, and `resume --abandon` do not use the guard and do not stop for this reason (simulated on macOS by removing `fcntl.F_GETPATH`; not run on a Linux machine). `projectChecks` use macOS `sandbox-exec`. With `--ask-backend`, no `--backend`, and at least two usable backends, `audit` returns the `choose-backend` question first with status 0. |
| git | git supplies the diff and the snapshot. Claude Code is required for the `/docaudit:audit`, `/docaudit:preflight`, and `/docaudit:update` skills and the Claude Code agents backend. |
| Python 3.12 or newer | The engine uses only the standard library. |
| Codex CLI (optional) | Used when `codex` is on `PATH`, `codex --version` and `codex exec --help` work, and `auth.json` in the Codex home is a readable regular file. Non-regular files are rejected, for example symlinks. The Codex account must be able to use the model `gpt-6.1-sol` (section 7). The `L-SECURITY`, `L-ADVERSARIAL`, and `L-CLAIM` layers of the `extended` profile use Codex for model calls (section 8). |
| agy CLI (optional) | Used only with a dedicated, signed-in home directory named by `AGY_HOME` and a tool-guard hook file (section 7, "The agy backend"). |
| Node.js (optional) | Only for running the repository's own test suite. |

A repository needs nothing beyond the configuration file. The Markdown documents that `corpus.docGlobs` selects can be anywhere in the tree; the engine creates the state directory when needed, for example when opening a run or running `migrate`; see section 4's `report.path` for the report directory and file.

## 4. Write the configuration

Create `.claude/docaudit.json`. Start from the minimal example in the README and adjust four things:

1. `corpus.docGlobs`: the documents to audit. Exclude generated or vendored documents with `corpus.excludeDocGlobs`.
2. `changes.diffGlobs`: the files whose changes should trigger an audit. A starting set is the source, configuration, and documentation directories together.
3. `impact.map`: at least one entry per source area (section 5).
4. `report.path`: a Markdown path ending in `.md` with exactly one `<YYYY-MM-DD>` and at most one `[_NN]` anywhere in the path; the basename must have nonempty text before the date when the date is in the basename, or be nonempty when the date is in a directory. For example, `reports/audit_<YYYY-MM-DD>[_NN].md`. The report directory is created during run preparation after retrieval, even if no report is published; a run refused earlier does not create it. Reports are never overwritten; when a name exists, the next report uses the first available suffix from `_02` through `_1000` at `[_NN]` if present, or immediately after the date otherwise. If all names are taken, the engine stops with status 4, `path-exhausted`, and the open run's `runId`; move an older report to free a name, then resume that run.

Leave `enabledLayers` at `["L-SCOPE", "L-DOC", "L-PROJECT"]` unless you intend to run the `extended` profile (section 8). Keep `documentChecks.frontMatterFields` empty until documents follow that convention; `documentChecks.indexFiles` instead lists orphan-check link sources that need not exist (unreadable files count as empty). Without an explicit profile (positional or `--profile`), the default `standard` layers are checked before opening a run; if only `L-PROJECT` is missing, status 3 and `capability-missing:L-PROJECT` refuse it, so specify `--profile focused`. Unknown keys are rejected, so a typo fails validation with `config-invalid:<detail>` instead of being ignored.

Commit the configuration. It is part of the repository's contract, and the engine seals its bytes into every run: changing the file while a run is open makes the run `REFUSED config-drift`. This refusal also occurs when the manifest has no `configHash` and the configuration file is readable.

If another tool generates files during an audit, you can optionally list their
Git-ignored location under `worktree.tolerateChangesIn`: a trailing `/` names a
directory, while a path without it names one exact file. Use repository-relative
paths such as `cache/` or `logs/run.log`; globs, absolute paths, empty or `.`
components, `..`, `./`, and `//` are invalid. The location must contain no
tracked file, corpus document, configuration, state, or report location and must not lie inside any of those locations. The
engine checks this before opening the run. A bad value gives
`config-invalid:worktree.tolerateChangesIn`, while a non-object `worktree` gives
`config-invalid:worktree`; a Git or corpus calculation failure keeps its own
reason. Engines at 1.2.1 and earlier reject the `worktree` key as
`config-invalid:worktree`. The schema value remains `"1.0"`.

The tree scan still records Git-ignored entries, including those in a listed
location. The scan records Git-ignored regular files by metadata without
reading their contents and counts them toward the 200,000-entry limit, but
not the 4 GiB content limit. It still hashes the contents of regular files in
listed locations, tracked files, non-ignored untracked files, and all regular
files when Git is unavailable. For an ignored file recorded by metadata, rewriting the
same bytes changes the worktree record if a recorded timestamp changes. The gate
then refuses the run with `worktree-modified`. At the gate, a change is tolerated
only inside that location, outside
the sealed corpus and source snapshot, and still ignored by Git. Adding a file
with `git add -f`, removing its ignore rule, or creating a parent directory of
the listed location can therefore cause refusal. The verdict lists tolerated
paths in `toleratedWorktreeChanges`. Only a rule-7 refusal with listed differing
paths can set `worktreeDiffIgnoredOnly` to true: the gate must confirm that every
refused path is ignored by Git and outside the sealed corpus and snapshot. If Git
classification fails or path differences cannot be recovered, the flag and
its report guidance are absent. When present, the report suggests two remedies:
list the location in
`worktree.tolerateChangesIn` or move the output outside the working tree.
Codex and agy runs with a nonempty list save `tree.before.json` for path-level
classification after a restart. If the serialized snapshot exceeds 64 MiB,
the run ends `undecided worktree-too-large`.

## 5. Build a good impact map

`impact.map` is a list of `{"source": <glob>, "docs": [<path>, ...]}` entries. When a changed file matches `source`, only listed documents in the corpus become impacted; listed documents outside the corpus are excluded. Two rules make the map useful:

- **Map by responsibility, not by directory.** A document that explains the deployment procedure should be mapped from the deployment scripts and the configuration files it cites, wherever they live.
- **Prefer several small entries to one broad entry.** A single `src/**` entry listing every corpus document marks those documents as impacted when a changed path matches `src/**`; if the total number of impacted corpus documents exceeds `impact.maxImpactedDocs`, the run ends `undecided impact-limit`.

`impact.ssotSources` works like the map for files that are the single source of truth for a value, for example a version file or a port table. A change to the source impacts only its listed documents in the corpus; listed documents outside the corpus are excluded.

`impact.heuristics` is optional and off when the key is absent or its value is an empty object `{}`. With a nonempty object, `L-SCOPE` takes the file name of every changed file, with and without its extension, and marks a document as impacted when its text contains one of those names as a plain substring. `minIdentifierLength` drops short names, `excludeBasenames` drops names such as `index.ts` or `README` that would match everywhere (exclusion names are case-insensitive), `excludeDocPathTokens` ignores the names of changed documents themselves, and `saturationWarnRatio` warns when the documents impacted only by this matching reach that share of the whole corpus. Start without heuristics; add them when the map alone misses documents that name the changed files.

Set `impact.maxImpactedDocs` to the largest set you are willing to verify in one incremental run. A full run is not capped by it.

## 6. Migrate a legacy configuration

If the repository still has a legacy .claude/doc-audit.json, the `migrate` subcommand converts it:

```sh
python3 ~/.claude/skills/docaudit/skills/audit/engine migrate --dry-run --repo-root .   # shows the conversion; exit 1 = not convertible
python3 ~/.claude/skills/docaudit/skills/audit/engine migrate --repo-root .             # writes .claude/docaudit.json when it is absent
```

The dry run prints a result of `convertible`, `not-convertible`, or `unchanged`, together with the converted configuration, `counts` (legacy history entries, whether a last-run record exists, and the list of dropped keys), the inputs it read with their hashes, `anchor` (never migrated), and `runOpen`. Project facts (document globs, diff globs, the impact map, report path, front-matter and index settings, heuristics, single-source-of-truth entries) are mapped to their 1.0.0 keys; migration writes heuristics with the earlier-release defaults filled in. Keys that described the old installation, command mappings, or optional tools are dropped, because 1.0.0 detects tool availability instead of configuring it. The full disposition table is in CONFIG-1.0.0.md, "Old-key disposition".

The converted configuration always enables `L-SCOPE`, `L-DOC`, and `L-PROJECT` with no `projectChecks`. If a legacy history file exists, its entries are copied into the new `history.jsonl` as `legacy` lines; legacy anchors are not migrated, so the first run of every profile must be `--full`. A completed migration leaves a marker in the history. Running `migrate` again with the same legacy inputs reports `unchanged` and writes nothing, even while a run is open; with changed legacy inputs it is rejected with `migration-input-changed`. Otherwise the full migration refuses to run while a run is open (`migration-run-open`) and refuses to replace an existing `.claude/docaudit.json` whose bytes differ from the conversion (`migration-target-exists`).

Heuristic-only impacted documents count toward `impact.maxImpactedDocs`, so exceeding the limit stops the run with `impact-limit`, unlike the earlier harness, which discarded excess heuristic matches and continued. For example, a migration in 1.1.0 leaves `impact.heuristics` values absent when the legacy input does not specify them. Matching legacy inputs return `unchanged` and leave the existing file alone, so reconcile `impact.heuristics` manually: if absent, set it to this object; if present, retain its keys and values, add from this object only the keys it does not already have, and append to `excludeBasenames`, in this object's order, each name not already in the existing list case-insensitively:

```json
{"minIdentifierLength":5,"excludeBasenames":["readme.md","index.md","changelog.md","license","license.md","__init__.py","makefile","main.md","test.md","skill","skill.md"],"saturationWarnRatio":0.5,"excludeDocPathTokens":false}
```

Review the new file, commit it, and remove the legacy file when you no longer need it. The engine reads only `.claude/docaudit.json`; a repository that still has only the legacy file ends with `config-needs-migration`.

## 7. Run audits

Inside Claude Code, in the repository:

```
/docaudit:audit --full                # whole corpus; required until the profile has an anchor
/docaudit:audit --full --accept-baseline # accept a document-only NEEDS_FIX baseline
/docaudit:audit                       # incremental: documents impacted by changes since the anchor
/docaudit:audit --profile focused     # choose the profile. Omitting it selects focused when the conditions in section 8 are met
/docaudit:audit standard              # same as --profile standard
```

`audit` accepts a positional profile or `--profile`; if both are given they must match, and if neither is given the profile is selected automatically (section 8). `scope` and `precheck` select a profile through `--profile`.

The skill starts the engine with `--ask-backend` (see "Choose the backend" below). It runs the engine, and when the engine hands verification to Claude Code agents (`nextAction: invoke-workflow`) it launches the verification workflow and resumes the engine, up to three times, then abandons the run. On `done` or `abort` it follows `skills/audit/present.md`: conclusion first, reason and report path when available, the report's two count lines, a table of FAIL and WARN documents, and next steps. It identifies documents without a verdict when the report lists them.

Outside Claude Code the same run is a command; it needs the Codex backend or a set-up agy because no Claude Code agents are available (add `--backend agy` or `--backend codex` to choose):

```sh
python3 ~/.claude/skills/docaudit/skills/audit/engine audit --full --profile standard --repo-root /path/to/repo
```

For a read-only preview, run `python3 ~/.claude/skills/docaudit/skills/audit/engine scope --repo-root /path/to/repo`. It accepts `--full`, `--profile focused|standard|extended`, and `--repo-root` with the same defaults as `audit`, and uses the same configuration, anchor, history, and profile selection. It prints one JSON object in the `scope.json` structure, including `changed`, `impacted`, and per-document `provenance` such as `self` and `regression`. It opens no run, takes no lease or mutex, writes no repository, state, or report files or directories, invokes no model or external backend CLI, and performs no worktree integrity check. Success exits 0; calculation or configuration refusal exits 3 with the pre-run refusal JSON and `reason`.

Run `python3 ~/.claude/skills/docaudit/skills/audit/engine precheck --repo-root /path/to/repo` to add built-in project findings to the same preview. It takes the same flags as `scope`, writes no state, and calls no model or external backend. If `projectLayer` is true, `checks` covers the corpus, including documents outside `impacted`, and `projectChecksSkipped` counts the configured checks it did not run. In Claude Code, `/docaudit:preflight` runs `precheck`, shows its findings, and uses the conversation model to propose document fixes. It applies only approved fixes and reruns `precheck`. Add `--deep` to also review the impacted documents against the audit verifier's criteria before approving fixes. Both modes finish without running `audit` or `resume` or writing to Git.

Preflight review and the audit's judgement are separate so that the audit can verify the corrected documents independently of preflight. Running both steps incurs the cost of model review against the audit criteria twice only when you choose `--deep`.

### Choose the backend

Three backends can verify documents: the Codex CLI, the agy CLI, and Claude Code agents (only inside Claude Code).

- `--backend agy|codex|claude` names the backend. `--fast`, allowed only together with `--backend codex`, adds Codex's priority service tier. A `--backend` value other than those three is refused with `backend-invalid`, and `--fast` without `--backend codex` with `fast-requires-codex`; both refusals happen before a run is opened (exit status 3). A named backend that cannot be used ends the run `undecided backend-unavailable`.
- The skill always starts the engine with `--ask-backend`. The engine checks argument inconsistencies (`accept-baseline-requires-full`, `backend-invalid`, or `fast-requires-codex`) before loading the configuration. A refusal during argument checks, configuration loading, validation of `worktree.tolerateChangesIn`, or profile resolution exits with status 3 before the backend question. If you gave no `--backend` and at least two backends are usable, the engine opens no run: it prints `nextAction: choose-backend` with `runId: null` and a `choices` array, and exits with status 0. `choices` lists only the usable backends, in this order: Codex, Codex with `"fast":true`, agy (`{"backend":"agy","fast":false}`), Claude Code agents (`{"backend":"claude","fast":false}`). The skill asks you to pick one (the first is the recommendation) and starts the engine again with `--backend`, plus `--fast` when `fast` is true. If the question is closed without an answer the first choice is used; if it cannot be asked, the skill starts the engine without `--backend`, which gives the default below.
- Without `--backend` and without `--ask-backend` (a plain command, or the last case above) the engine takes the first usable backend in the order Codex, agy, Claude Code agents. An environment with a usable agy but no working Codex therefore changes its main backend from Claude Code agents to agy; pass `--backend claude` to keep Claude Code agents.
- The choice is written into the run (`backendSelection` in the `run-options` record and in `capability.json`), so `resume` uses the same backend and the same Fast setting.

The Codex backend runs the model `gpt-6.1-sol` at reasoning effort `medium`, and Fast adds `-c service_tier=priority`. Document-verification calls are limited to 900 seconds each, with up to three attempts per document and four calls in flight. If the Codex account cannot use `gpt-6.1-sol` and documents are impacted, the calls fail without document judgements and the run ends `undecided backend-failed`; when no documents are impacted, no model call is made. Regardless of which backend verifies the documents, Codex calls for the `L-SECURITY`, `L-ADVERSARIAL`, and `L-CLAIM` layers in the `extended` profile use that model and reasoning effort, with a 900-second limit and up to three attempts per assessment. `L-ADVERSARIAL` assesses each impacted document with up to four calls in flight; `L-SECURITY` assesses all impacted documents together; `L-CLAIM` assesses each `FAIL` finding from `L-ADVERSARIAL` one at a time. docaudit does not switch to another backend by itself; choose one with `--backend`.

The Claude Code agents backend runs the verifier agent `docaudit:doc-impact-verifier` with model `claude-sonnet-5-5` at effort `high`.

### The agy backend

agy verifies documents with the agy CLI and the model `gemini-4-hydrogen-medium`. It is used only when every condition below holds. When one fails, agy is left out of `choices` and of the default order without a message; the reason is recorded in the run's `capability.json` (`agyReason` for the home and the tool, `agyRepoBlock` for the repository).

**Set up a dedicated home.**

1. Create an empty directory and point `AGY_HOME` at it in the environment the engine runs in, for example `mkdir "$HOME/.agy-docaudit"` and `export AGY_HOME="$HOME/.agy-docaudit"`. `AGY_HOME` is required: if it is unset, or is not an existing directory, agy is not used (create the directory first, because agy itself would create a missing home). docaudit starts every agy process with `HOME` replaced by `AGY_HOME` and with only `PATH`, `LANG`, `LC_ALL`, `TMPDIR` and `AGY_CLI_DISABLE_AUTO_UPDATE=true` besides, so your everyday agy login, settings and plugins are not used.
2. Sign in inside that home: start agy with `HOME="$AGY_HOME" agy`, sign in when it asks, and quit. docaudit does not sign in for you; it checks that `.gemini/antigravity-cli/antigravity-oauth-token` exists in `AGY_HOME` and is readable.
3. Install the tool guard. agy's web search is not stopped by permission settings, so docaudit requires a hook that denies every tool except the six that read and search files (`view_file`, `read_file`, `list_dir`, `find_by_name`, `grep_search`, `finish`). agy 1.4.9 and 1.5.10 read files with `read_file`, and older versions used `view_file`, so both are allowed. Run `mkdir -p "$AGY_HOME/.gemini/config"`, then create `.gemini/config/hooks.json` in `AGY_HOME` with exactly this content:

```json
{
  "docaudit-deny-tools": {
    "PreToolUse": [
      {
        "matcher": "^(?:|[^fglrv].*|f(?:|[^i].*|i(?:|[^n].*|n(?:|[^di].*|d(?:|[^_].*|_(?:|[^b].*|b(?:|[^y].*|y(?:|[^_].*|_(?:|[^n].*|n(?:|[^a].*|a(?:|[^m].*|m(?:|[^e].*|e(?:.+)))))))))|i(?:|[^s].*|s(?:|[^h].*|h(?:.+))))))|g(?:|[^r].*|r(?:|[^e].*|e(?:|[^p].*|p(?:|[^_].*|_(?:|[^s].*|s(?:|[^e].*|e(?:|[^a].*|a(?:|[^r].*|r(?:|[^c].*|c(?:|[^h].*|h(?:.+)))))))))))|l(?:|[^i].*|i(?:|[^s].*|s(?:|[^t].*|t(?:|[^_].*|_(?:|[^d].*|d(?:|[^i].*|i(?:|[^r].*|r(?:.+))))))))|r(?:|[^e].*|e(?:|[^a].*|a(?:|[^d].*|d(?:|[^_].*|_(?:|[^f].*|f(?:|[^i].*|i(?:|[^l].*|l(?:|[^e].*|e(?:.+)))))))))|v(?:|[^i].*|i(?:|[^e].*|e(?:|[^w].*|w(?:|[^_].*|_(?:|[^f].*|f(?:|[^i].*|i(?:|[^l].*|l(?:|[^e].*|e(?:.+))))))))))$",
        "hooks": [
          {
            "type": "command",
            "command": "printf '%s' '{\"decision\":\"deny\",\"reason\":\"docaudit: this tool is disabled while judging documents\"}'",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

   The engine reads the file as JSON and compares it with this document: key order and whitespace do not matter, but any changed, missing or extra key or value makes agy unusable.
4. Keep permissions out of the home. `.gemini/antigravity-cli/settings.json` in `AGY_HOME` can be absent. If it exists, it must not have a non-empty `permissions.allow` or a `hooks` key. This includes grants you added there temporarily for another purpose.

**When agy is not used.** The startup check tests the following in this order and records the first that applies as `agyReason`; only the reason is recorded, never the contents of a file. agy itself is started only by the last checks (`agy --version` and `agy models`).

| reason | agy is not used because |
|---|---|
| `agy-home-unset` | `AGY_HOME` is not set or is empty |
| `agy-home-missing` | `AGY_HOME` is not an existing directory |
| `agy-not-installed` | no executable `agy` was found on `PATH` |
| `agy-settings-unreadable` | `settings.json` exists but cannot be read, is not a JSON object, or has `permissions` set to neither `null` nor an object |
| `agy-permissions-present` | `settings.json` has a non-empty `permissions.allow` |
| `agy-settings-hooks-present` | `settings.json` has a `hooks` key |
| `agy-tool-hook-missing` | `.gemini/config/hooks.json` does not exist |
| `agy-tool-hook-mismatch` | `.gemini/config/hooks.json` cannot be read, is not JSON, or differs from the document above |
| `agy-login-missing` | the login token file is missing or not readable |
| `agy-tmp-unavailable` | no private temporary directory could be created |
| `agy-version-failed` | `agy --version` failed or took longer than 20 seconds |
| `agy-models-timeout` | `agy models` took longer than 30 seconds |
| `agy-models-failed` | `agy models` failed or its output could not be read |
| `agy-model-missing` | the first 1 MiB of `agy models` output has no whitespace-separated word equal to `gemini-4-hydrogen-medium` |

The audited repository is checked as well, and it is recorded as `agyRepoBlock`: `agy-repo-settings-present` when the repository has a `.gemini/antigravity-cli` entry (whatever it contains; checked first), and `agy-workspace-customizations` when it has a top-level `.agents` entry. Workspace settings and hooks can run commands from the repository's own content and can allow tools, so agy is not used on such a repository.

**What the agy backend cannot do.**

- agy is run without the ability to run shell commands (the required deny hook stops the command tool while the judgement continues; without that hook, running agy directly makes it auto-deny tools that need permission and the whole reply comes back empty), so the judge prompt for agy leaves out the checks that need git (`git ls-files`, `git check-ignore`). Under agy docaudit cannot tell tracked files from ignored ones; the nested-checkout rule (a copy found only under a directory that has its own `.git` file or directory is another checkout, not evidence) is decided by the presence of that `.git` entry alone.
- Every agy launch writes a conversation and logs into `AGY_HOME`, so the directory grows over time.
- docaudit does not inspect the plugins installed in the agy home; a plugin can carry hooks of its own.
- One call is limited to 15 minutes by agy (`--print-timeout 15m`), and the engine stops waiting after 960 seconds. Two calls run at a time, and each document gets up to three attempts.

A tool that the guard hook denies is not a failure: the tool does not run and agy carries on. A document that fails every attempt gets no judgement and the run ends `undecided backend-failed`. The last attempt's reason is kept in the document's `failure` record, and every attempt's reason in its `model-call` record; if a private temporary directory cannot be created for a document verified through Codex or agy, no attempt starts and no `model-call` record is written. If this happens for every document, the run ends `undecided tmp-unavailable` with no document `failure` records; if it happens for only some documents, each affected document has `failure` reason `tmp-unavailable` and `attempts` 0, and the run ends `undecided backend-failed`:

| reason | meaning |
|---|---|
| `agy-home-unset`, `agy-home-missing` | `AGY_HOME` is unset or not a directory when the judgement starts |
| `agy-prompt-invalid` | docaudit's own rewrite of the judge prompt for agy failed its check, so agy was not started (all three attempts fail the same way) |
| `launch-failed` | agy could not be started |
| `cancelled` | the attempt was stopped because the run was stopping |
| `timeout` | agy did not finish within 960 seconds |
| `exit-<n>` | agy exited with status `n`, for example when it is not signed in |
| `output-too-large` | the output was larger than 2 MiB |
| `output-not-json` | the output was not a JSON object |
| `agy-status` | the reported status was not `SUCCESS` |
| `agy-denied` | agy reported a denied action, even though it returned an answer |
| `structured-output-missing` | there was no structured answer; the 15 minute limit ends here too |
| `output-type-mismatch`, `output-schema-mismatch`, `identity-mismatch` | the answer did not match the judgement schema, or did not echo the run and document it was asked about |
| `document-unread` | agy omitted the read-back field or a requested line, supplied malformed read-back, or returned a line number or text that did not match the document |
| `quote-not-in-document` | agy returned a FAIL without a required nonblank quotation or with a quotation that did not match a line of the document |
| `tmp-unavailable` | no private temporary directory could be created before any attempt, so there is no `model-call` record |

### The anchor lifecycle

- Anchors are kept per profile under `.claude/state/docaudit/anchors/`.
- The first anchor of a profile is written by the first full run of that profile that ends `CONSISTENT`. Until then, an incremental run of that profile ends `undecided anchor-missing`, so keep using `--full`.
- A full `NEEDS_FIX` run with `--accept-baseline` also writes its first anchor when every blocking item is an `L-DOC` judgement and its report was published. A known FAIL document is then not rechecked only when the document itself has not changed, is not impacted by `impact.map`, `ssotSources`, or heuristics, and `changes.regressionRecheck` is disabled. Set `changes.regressionRecheck: true` to recheck the profile's latest FAIL documents each time; they count toward `impact.maxImpactedDocs`, stop the whole run with `impact-limit` when exceeded, and leave that set when their latest judgement is PASS or WARN. Non-document blocking findings are never accepted: `focused` produces none; in `standard` and `extended` they are broken links and failed project checks; `extended` adds only confirmed claims, while security and adversarial findings are non-blocking.
- Every later run of the profile that ends `CONSISTENT` advances the anchor, and a later full `NEEDS_FIX` run accepted with `--accept-baseline` under the same condition as above advances it the same way. Any other `NEEDS_FIX` run, and every `undecided` or `REFUSED` run, leaves the anchor unchanged, so the next incremental run measures the same changes again, plus anything new.
- Switching profiles starts a new lifecycle: a `focused` anchor does not serve a `standard` run.

### One run at a time

The engine holds a mutex and a lease, and a new audit is refused while an earlier run is still open. The refusal names the state, and each state has its own remedy:

- `run-in-progress`: another engine process is running the audit right now. Wait for it to finish; while it holds the lease, resume is refused with `resume-in-progress` and abandon with `run-in-progress`.
- `run-awaiting-external`: the run was handed to the verification workflow and not resumed. Resume it with `resume <runId>`; the result line tells you whether it closed or handed off again (`nextAction: invoke-workflow`), in which case run the workflow and resume once more.
- `run-interrupted-resume-required`: the engine process ended mid-run. Resume it with `resume <runId>`, or abandon it with `resume <runId> --abandon` to close it. If no outcome is recorded, abandonment records `undecided abandoned`; otherwise history retains the recorded outcome. A run left open by an earlier engine after `capability-missing:<layer>` returns status 4 with that reason and its `runId` on resume; the configuration was sealed when it opened, so adding layers cannot repair that resume. Close it with `resume <runId> --abandon`.

The skill does not recover a run left over from an earlier session; do it by hand or with the prompts in PROMPTS.md, section 8.

## 8. Profiles and layers

| profile | layers | backend |
|---|---|---|
| `focused` | `L-SCOPE`, `L-DOC` | the backend chosen or defaulted in section 7 |
| `standard` (default, with the exception below) | focused + `L-PROJECT` | same |
| `extended` | standard + `L-ENRICH`, `L-SECURITY`, `L-ADVERSARIAL`, `L-CLAIM` | same selection for the documents, but `L-SECURITY`, `L-ADVERSARIAL`, and `L-CLAIM` use Codex for model calls, while `L-ENRICH` makes no model calls |

Without an explicit profile (positional or `--profile`), an incremental run proposes `focused` only when all of these hold: at most 10 changed files, at most 15 impacted documents, a total current size of at most 65536 bytes for changed files that were not deleted (not the diff size), the latest `standard` outcome has verdict `CONSISTENT`, and no changed path contains a sensitive term (case-insensitive substring matching: `auth`, `security`, `permission`, `access-control`, `iam`, `crypto`, `billing`, `session`, `token`, `oauth`, `acl`, `rbac`, `secret`, `.env`). It uses `focused` only if that profile has an anchor; otherwise it keeps `standard` with `profileSelectionSource` set to `classifier-demoted` in the plan and manifest. Other cases use `standard`. `focused` does not run `L-PROJECT`.

`extended` can be selected only when `enabledLayers` lists all seven layers; otherwise the engine refuses to open the run with `capability-missing:<layer>` (exit status 3). The `L-SECURITY`, `L-ADVERSARIAL`, and `L-CLAIM` layers run on Codex whichever backend verifies the documents (with Fast, if you chose it), so they need a Codex that passes the availability check. Without one, `L-CLAIM` is incomplete and the run ends `undecided`: with reason `codex-unavailable` when the documents are verified through agy, and `workflow-adapter-unavailable` when they are verified through Claude Code agents. With impacted documents `L-SECURITY` and `L-ADVERSARIAL` are incomplete for the same reason, and with no impacted documents they complete without calls. Use `extended` where the Codex CLI passes the availability check.

In `extended`, the adversarial layer asks for evidence-backed contradictions per impacted document, the security layer reviews documented procedures, configuration, secret handling, and permissions once per run, and the claim layer re-checks every adversarial `FAIL` against the repository. Of the findings from the four layers added by `extended`, including `L-ENRICH`, only a confirmed claim can make the verdict `NEEDS_FIX`; adversarial, security, and enrichment findings are informational. A document `FAIL` makes the verdict `NEEDS_FIX` in every profile, and a project-check `FAIL` does so in the profiles that run `L-PROJECT` (`standard` and `extended`).

## 9. Project checks

`L-PROJECT` applies four kinds of read-only checks over the sealed corpus: required front-matter fields (WARN), local Markdown links (a missing target is a blocking FAIL), path-like backtick tokens that point at nothing (WARN), and documents that no other document or index file links to (WARN). Documents listed in `documentChecks.indexFiles` are themselves exempt from orphan findings. `documentChecks.layerGlobs` removes matching documents from a check, for example to exempt generated indexes from the orphan check. When the sealed corpus has only one document, the orphan check does not run and records `orphan: skipped`.

For `links` and `orphan`, link destinations and titles support CommonMark 0.31.2 forms (balanced parentheses with up to 32 nested pairs, backslash-escaped parentheses, spaces in `<...>` destinations, and titles delimited by double quotes, single quotes, or parentheses excluded from the destination). The parser accepts multiline link text and titles if they contain no blank line. At positions that allow separating whitespace, it accepts at most one line ending. Destinations must stay on one line. Nested square brackets in link text, reference definitions indented by four or more spaces (including inside lists), and reference definitions following list or quote markers are not extracted, and entity and numeric character references in destinations (for example `&amp;`) are kept literal rather than decoded.

`projectChecks` adds your own commands. Each entry names a check ID, an `argv`, a timeout, and optionally a working directory. The command runs on macOS inside a `sandbox-exec` profile that forbids writes outside the private temporary directory (writing to `/dev/null` is the only other permitted write), and must print a JSON object with a `findings` array, each finding having `id`, `summary`, `severity` (`INFO`, `WARN`, or `FAIL`), and optionally `path`. A `FAIL` finding is blocking: the run ends `NEEDS_FIX` even when every document passed. A nonzero exit, a timeout, or invalid output is itself a blocking `FAIL` finding. `standard` and `extended` run these checks; `focused` does not. On a non-Darwin platform, nonempty `projectChecks` would cause `undecided sandbox-unavailable`, but on Linux `audit`, `resume` without `--abandon`, and `migrate` when it writes the converted configuration are expected to stop earlier with `io-unsupported-platform` because the write guard requires `fcntl.F_GETPATH` (status 3 for `audit` and `resume`, 4 for `migrate`, all with `runId: null`; simulated on macOS by removing `fcntl.F_GETPATH`; not run on a Linux machine). Empty `projectChecks` does not remove that limitation for those commands; `scope`, `precheck`, `migrate --dry-run`, and `resume --abandon` do not use the guard and do not stop for this reason.

## 10. Read the results

- **The result line.** The last line of the engine output is one JSON object. A closed run has `nextAction` (`done` or `abort`), `runId`, `outcome`, and when present `reason` and `reportPath`; a hand-off to the workflow has `nextAction: invoke-workflow` with `requestSeq` and `requestPath` instead of an outcome; a backend question has `nextAction: choose-backend`, `runId` null, and `choices`, and no run is opened. Exit status 0 means the engine finished normally, including `undecided`, `REFUSED`, the hand-off, and the backend question; 2 means command-line parsing rejected an unknown command or option, an ambiguous option abbreviation (such as `--f`), an unknown profile name, a mismatch between the positional profile and `--profile`, a missing required argument (for example the run ID for `resume` or a value for an option), an unexpected argument, or a missing command (except with `--version`) and printed `usage:` to standard error instead of a JSON result line; 3 means it refused to open a run or start a resume (configuration, profile, run-state, or argument problem such as `accept-baseline-requires-full`, `backend-invalid`, or `fast-requires-codex`, or unsupported platform `io-unsupported-platform` for `audit` and `resume`, named in `reason`); 4 means an opened run could not proceed or a resumed run could not proceed after resume started, including a later `mutex-timeout` or exhausted report names (`path-exhausted`). For engine-handled interruptions of an opened or resumed run with status 4, the result includes that run's `runId`. Resume or abandon the still-open run. Both commands stop when their required read of `history.jsonl` fails or finds corrupt history. Abandonment that fails for a history reason (`history-corrupt`, `PermissionError`, or `OSError`) exits 4 with `runId: null`. Full `migrate` rejections use status 3 for `migration-source-missing` and `migration-run-open`, and 4 for all other reasons; migration creates no run, and its handled results omit `runId`. Its `runOpen` reports whether an existing open run was detected, not a migration run to resume or abandon. If `fcntl.F_GETPATH` is missing, `migrate` that reaches writes exits 4 with `io-unsupported-platform` and `runId: null` (macOS simulation; Linux itself was not tested). With `--ask-backend`, no `--backend`, and at least two usable backends, `audit` returns the `choose-backend` question first with status 0.
- **The report.** Published at `report.path` with fixed front matter and sections for the run, the audited documents, the findings, the verdict, the anchor, the measurements, and the evidence. Most of its headings and fixed phrases are Japanese. Under `## 判定`, after the verdict, any reason, and any refused checks, it always prints `- 文書の判定の数: PASS n、WARN n、FAIL n、判定なし n` and `- 文書以外の所見の数: n`. It has an entry per finding: each document with its verdict and a summary of the mismatch, and each other finding with its severity and summary. Examples of other findings are project checks, links, and claims. Line breaks in summary text are preserved. The evidence strings behind a judgement (the Codex backend is asked to cite `file:line` in its rationale; Claude Code agents are asked for repository-relative evidence) are kept in the run's evidence ledger, in the `judgement` lines of the history, and, for runs verified through Claude Code agents, in `runs/<runId>/requests/<seq>/judgements/`. Unsafe summary text is redacted and counted as `- redacted: N`; history `judgement` lines use the same redact rule, while the ledger and `requests/<seq>/judgements/` remain original. Runs that end `undecided` before verification starts publish no report. The Run section shows the baseline anchor's backend model and engine version and adds `基準との違い` when known values differ from this run. When the engine verifies an unchanged document again, it makes a fresh judgement without reusing the previous one. If the judgements differ, the report shows the current one.

  The quotation check confirms that nonblank text occurs in the document, with no minimum length. An accurate quotation passes this test even if the verifier misreads it, since the check does not assess whether the quotation supports the claim. Read-back confirms only the requested lines. It cannot confirm that any text was read when no lines qualify or when the engine cannot read the document as UTF-8 during line selection. Examples of read failures are another encoding, even a single malformed byte, or a read refusal. When the engine cannot read the document, it also skips comparing quotations to the document.

- **The state directory.** `.claude/state/docaudit/history.jsonl` records one line per run outcome (plus `flip`, `anchor`, `legacy`, and `migration` lines), and per report-safe `L-DOC` PASS, WARN, or FAIL judgement only when the run is not `REFUSED`, the ledger verifies, and all judgement checks pass; omitted PASS, WARN, and FAIL judgements are counted in the outcome's `judgementsSkipped` when nonzero. `anchors/<profile>.json` holds the current anchor. In `runs/<runId>/`, the engine writes the manifest at sealing, appends adapter results and judgements to the evidence ledger during verification, and writes `verdict.json` when the gate completes. For example, a run ending `undecided backend-unavailable` has none of those artifacts. Decide once whether to commit the state directory (auditable history in version control) or ignore it (local state per clone); the engine works either way.

- **The scope preview.** `scope` returns the scope JSON itself, creates no run records, report, or state directory, and exits 0 on successful calculation or 3 with a refusal JSON containing `reason`.

- **The pre-audit check.** `precheck` returns the selected scope and built-in project findings without creating run records, a report, or a state directory. Status 0 means the preview completed, 3 means refusal with `reason`, and 2 means invalid arguments with usage on standard error.

### Outcomes at a glance

| outcome | meaning | what to do |
|---|---|---|
| `CONSISTENT` | every impacted document matches and no blocking finding | nothing; the anchor advanced |
| `NEEDS_FIX` | at least one document fails, or a blocking finding (a broken link, a failed project check, a confirmed claim) | fix what the report names, run again |
| `NEEDS_FIX` (`--full --accept-baseline` accepted) | failures are document judgements only | the anchor advanced; known FAIL documents are rechecked when they change |
| `undecided anchor-missing` | incremental run without an anchor | run with `--full` and the same `--profile` if that run used one |
| `undecided backend-unavailable` | capability detection found no usable backend: no Codex, no usable agy, and not inside Claude Code; or the backend named with `--backend` cannot be used | install or fix Codex, set up agy (section 7), or run inside Claude Code; if the backend named with `--backend` is unavailable, fix that backend, omit `--backend`, or name another usable backend |
| `undecided backend-failed` | a document verified through Codex or agy failed every attempt (for example agy not signed in, or a Codex account that cannot use `gpt-6.1-sol`), or a Codex assessment in `L-ADVERSARIAL`, `L-SECURITY`, or `L-CLAIM` failed every attempt or could not create a private temporary directory before any attempt | see the reason in the document's `failure` and `model-call` records or in the layer's `model-call` records; a document ending `tmp-unavailable` has no `model-call` record; check temporary directory access; for failed Codex and agy attempts, inspect `stderrTail` in the `model-call` records when present |
| `undecided tmp-unavailable` | every document verified through Codex or agy could not create a private temporary directory, so no attempt started; or `projectChecks` could not create its own private temporary directory | check temporary directory access and rerun; in the first case, documents have no `failure` or `model-call` records |
| `undecided external-incomplete` | a document verified through Claude Code agents remained unresolved after the third request | inspect the request and response records, resolve the external workflow, and rerun |
| `undecided impact-limit` | more impacted documents than `impact.maxImpactedDocs` | run with `--full`, raise `impact.maxImpactedDocs`, or narrow the routes that add documents (`impact.map`, `impact.ssotSources`, `impact.heuristics`, and `changes.regressionRecheck`) |
| `undecided corpus-unreadable` | when the scope calculation rejects a file while checking its attributes for a reason other than a symlink or non-regular file, `reason` includes the path as `corpus-unreadable:<path>`; the same refusal during the corpus check for `worktree.tolerateChangesIn` exits with status 3 before opening a run; a new run could not read a regular worktree file, including one outside the corpus, while computing the digest (except paths allowed by `worktree.tolerateChangesIn` and Git-ignored regular files recorded by metadata) (corpus documents and files matched by `diffGlobs` over 8 MiB are treated as unreadable), or Workflow retrieval preparation, including on resume before this step completed, found a sealed corpus document missing, unreadable, non-regular, replaced by a symlink, or beneath a symlinked parent | restore a readable regular file and non-symlink parents; make the file 8 MiB or smaller; if it is over 8 MiB, exclude it from every set that reads it: from the corpus with `docGlobs` or `excludeDocGlobs` if it is a corpus document, and narrow `changes.diffGlobs` so it no longer matches the file if it matches a change source; then rerun |
| `undecided sandbox-unavailable` | nonempty `projectChecks` used by `standard` or `extended` on a non-Darwin platform, missing `/usr/bin/sandbox-exec`, or a `/usr/bin/true` preflight that cannot start, exits nonzero, or times out after 20 seconds | on macOS, restore `sandbox-exec` or fix the preflight and rerun; on Linux `audit`, `resume` without `--abandon`, and `migrate` when it writes the converted configuration are expected to stop earlier with `io-unsupported-platform` even with empty `projectChecks` (status 3 for `audit` and `resume`, 4 for `migrate`, all with `runId: null`); `scope`, `precheck`, `migrate --dry-run`, and `resume --abandon` do not use the guard and do not stop for this reason (simulated on macOS by removing `fcntl.F_GETPATH`; not run on a Linux machine) |
| `undecided workflow-adapter-unavailable` | `extended` with the documents verified through Claude Code agents and no usable Codex | make Codex usable |
| `undecided codex-unavailable` | `extended` with the documents verified through agy and no usable Codex | make Codex usable |
| `undecided abandoned` | the command's result line after successful abandonment; history records this only if no outcome exists, otherwise retaining the recorded outcome | run again |
| `REFUSED worktree-modified` | an unaccepted working-tree change was found; `worktreeDiff` lists only refused paths | stop the writer and run again, list its ignored location in `worktree.tolerateChangesIn`, or move its output outside the working tree; follow the report guidance when `worktreeDiffIgnoredOnly` is true |
| `REFUSED config-drift` | `.claude/docaudit.json` changed during the run, or the manifest has no `configHash` while the configuration file is readable | run again |
| other `REFUSED` reasons | the sealed run contract failed an integrity check (lease, seal, evidence hashes, layer set, backend, judgements, adversarial and claim consistency) | inspect `runs/<runId>/`; run again |

## 11. Troubleshooting

- **`config-invalid:<detail>` on every run.** The detail names the offending key. Paths must be repository-relative without a `..` path segment (a parent-directory reference, for example `a/../b`); `report.path` must end in `.md` and contain exactly one `<YYYY-MM-DD>` and at most one `[_NN]` anywhere in the path; the basename must have nonempty text before the date when the date is in the basename, or be nonempty when the date is in a directory. For `config-invalid:worktree.tolerateChangesIn`, check the path form and whether Git ignores it and it excludes tracked files, corpus documents, and the engine's configuration, state, and report locations.
- **`config-needs-migration`.** Only the legacy file exists. Run `migrate` (section 6).
- **The first run reports many front-matter or orphan warnings.** They are non-blocking. Fix them over time, or exempt documents with `documentChecks.layerGlobs`.
- **`history-corrupt`.** `history.jsonl` has a malformed line, is not a regular file, is not UTF-8, or contains an overlong line; an anchor file under `anchors/` is unreadable or malformed; or the anchor candidate that a `CONSISTENT` run or a `NEEDS_FIX` run accepted with `--accept-baseline` points at is unreadable or does not match its recorded hash. A corrupt `history.jsonl` stops both reading and appending history. If only an anchor file is corrupt, incremental runs for that profile stop with `history-corrupt`, while `--full` ignores the anchor, reads history, and appends the outcome; only an outcome that advances the anchor (`CONSISTENT` or `NEEDS_FIX` accepted with `--accept-baseline`) then stops during recording with `history-corrupt` and exit status 4. Keep a copy of the whole state directory, then find which file is broken: a malformed history line is fixed by moving the history file aside while preserving its valid lines under another name and starting a new history file; a broken anchor is fixed by moving that profile's anchor file aside, after which the profile needs a new `--full` run. If the failure happened inside a run (exit status 4), that run remains open; repair a corrupt `history.jsonl` first because abandonment also fails while it is corrupt, then resume or abandon the run before the next audit. A corrupt anchor file alone does not prevent abandonment. If `history.jsonl` itself cannot be read because of permissions, it stops with exit status 4 and reason `PermissionError`, not `history-corrupt`; repair permissions, then resume or abandon that run. As an exception to the non-regular-file case, if `history.jsonl` is a symbolic link, the run stops with exit status 4 and reason `OSError`, not `history-corrupt`; replace the link with a regular file, then resume or abandon that run.
- **`mutex-timeout`.** Another engine process is using state for an operation (for example, `migrate`). Wait for it to finish and retry; if `run-open.json` still names an open run, resume or abandon that run. **`run-in-progress`.** Another engine process holds the run. Wait for it, or resume or abandon the run named in `run-open.json` once no process holds it.
- **A plugin skill is missing after install.** Start a new Claude Code session or run `/reload-plugins`. Check the plugin with `claude plugin list`, then check for `/docaudit:audit`, `/docaudit:preflight`, and `/docaudit:update`.
- **Something was written into the repository that you did not expect.** An audit writes only the report and run state, which is under `.claude/state/docaudit/`. Since 2.0.0, top-level `.mdq/` paths are ordinary corpus candidates, snapshot entries, and changed paths. To allow another tool to write there during audits, add `.mdq/` to Git's ignore rules and set `worktree.tolerateChangesIn: [".mdq/"]`, subject to the eligibility rules in section 4. When another tool writes during a run, the engine tolerates only changes under the configured location that Git still ignores and that lie outside the sealed corpus and snapshot. A change outside it, including a newly created parent directory, causes `REFUSED worktree-modified`; the two remedies are to list an eligible location in `worktree.tolerateChangesIn` or move the output outside the working tree. If the scan exceeds its limits, the result is `undecided worktree-too-large`. Reduce the number of entries, or reduce the bytes in files whose contents are hashed if the size limit is the cause.

### Update reasons

| reason | meaning |
|---|---|
| `update-busy` | Another update operation holds the lock. |
| `update-install-manifest` | The installed plugin manifest is unreadable or has an invalid name or version. |
| `update-marketplace-install: …` | The install is in a marketplace cache. The reason includes the marketplace commands to use. |
| `update-development-checkout` | The install is a development checkout with a `.git` entry. |
| `update-install-overlaps-state` | The install and update state directories overlap. |
| `update-already-installed`, `update-target-tag-missing` | The requested tag is already installed, or the target tag is invalid or unavailable. |
| `update-installed-tag-missing` | The installed version's tag is absent. The plan succeeds with up to 20 commits and no diff summary. |
| `update-plugin-name-mismatch`, `staging-plugin-name` | The candidate plugin's name differs, or reading the name failed. |
| `update-filesystem`, `update-path-exists` | Staging, backups, and the install parent are not on one filesystem, or a required destination is already occupied. |
| `update-rollback-aside-missing`, `update-rollback-install-changed` | There is no usable recorded backup, or the installed version changed since the last successful replacement. |
| `update-check-git`, `update-clone`, `update-tags`, `update-tag-commit`, `update-notes`, `update-log`, `update-diffstat` | A Git command failed while fetching or describing the release. |
| `staging-archive`, `staging-tree`, `staging-version`, `staging-plugin-validate` | Extraction or candidate verification failed. |
| `update-aside`, `update-place`, `installed-version`, `installed-plugin-list`, `installed-plugin-details`, `update-post-install` | Moving or verifying the install failed. Check the recorded paths and verification results. |
| `update-history`, `update-restore-failed`, `tmp-unavailable` | Writing history, restoring the previous install, or creating private temporary storage failed. |

For a command timeout, the reason gains a `-timeout` suffix. The restored version verification is recorded as `restored-version` in `checks`. If a handled interruption occurs during replacement and both restoration and history recording succeed, the history entry records the exception name, for example `KeyboardInterrupt`. If restoration fails, the reason in the entry is `update-restore-failed`.

## Workflow retrieval: index retirement

Workflow document retrieval uses targeted text searches and Read, regardless of what is on PATH. It does not build or health-check an external document index; the recorded reason is `mdq-retired`. New Workflow runs always record method `grep`, with `indexDb`, `indexCwd`, and `indexLang` all null; method `index` appears only in records sealed before 1.7.0. Retrieval preparation still checks every sealed corpus path without creating a mirror: a document or parent replaced by a symlink closes the run as `undecided` / `corpus-unreadable`, including on resume before preparation completed. Cleanup of older temporary indexes remains, limited to indexes whose ownership by the repository and run is verified. Before upgrading, finish or abandon old open runs with the old engine.

For Markdown research across project documents, we recommend mdrg (repository coming soon). The engine does not invoke mdrg.
