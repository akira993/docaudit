# Adopting docaudit in a project

日本語版: [ADOPTION.ja.md](ADOPTION.ja.md)

This guide takes a repository from "no audit" to "every change is checked against its documentation". It assumes docaudit 1.6.0 is installed as described in the [README](../README.md); the configuration reference is [CONFIG-1.0.0.md](CONFIG-1.0.0.md), and copy-paste prompts are in [PROMPTS.md](PROMPTS.md). The skill's instruction file (`SKILL.md`) is written in Japanese.

Commands in this guide use the engine path of the skills-dir install, `~/.claude/skills/docaudit/skills/audit/engine`. If you installed through the marketplace, substitute the engine path given in the README's install section.

## 1. The five-minute path

1. Install the plugin once (README, "Install") and start a new Claude Code session.
2. In the repository, create `.claude/docaudit.json` (section 4) and commit it.
3. Run `/docaudit:audit --full`. Read the report it publishes and fix the documents it flags.
4. Repeat `/docaudit:audit --full` until the run ends `CONSISTENT`. That run writes the first anchor. If `NEEDS_FIX` is blocked only by document judgements, `/docaudit:audit --full --accept-baseline` accepts the baseline and enables later incremental runs. It cannot accept broken local links, failed project checks, or any other non-document blocking finding; fix those findings in the report first.
5. From then on run `/docaudit:audit` after changes. Documents impacted by changes are judged; when `changes.regressionRecheck` is enabled, documents whose latest judgement for the profile was `FAIL` are also judged. Enabled built-in `L-PROJECT` checks inspect corpus documents without limiting themselves to the impacted set.

Before upgrading to 1.0.1, close every open run with `resume <runId> --abandon`. If an unclosed run is resumed in 1.0.1, it ends `REFUSED seal-drift` when its old scope includes a path under `.mdq/`, or `REFUSED worktree-modified` when `.mdq/` appears only in its old working-tree snapshot. In either case, run again to recover.

Before updating to this version, close every run that an earlier version left open (a `.claude/state/docaudit/run-open.json` exists with `state` other than `closed`) with the earlier version's engine: resume it to the end, or run `resume <runId> --abandon`. For a sealed `manifest`, the gate's backend check returns `REFUSED backend-mismatch` if its recorded backend and model differ from those resolved using this version's defaults.

## 2. Mental model

docaudit compares Markdown documents with the code and configuration they describe and reports the documents that no longer match. It never edits a document: every run is report-only, and the only files it writes inside the repository are the report and the run state under `.claude/state/docaudit/`.

Five ideas explain everything else:

- **Corpus.** `corpus.docGlobs` selects the documents that can be audited. Files ignored by git are skipped unless `corpus.respectGitignore` is false.
- **Changes.** `changes.diffGlobs` selects the files whose changes trigger an audit. An incremental run compares the working tree with the last accepted run of the same profile.
- **Impact map.** `impact.map` links changed sources to the documents that describe them. The changed files, the map, the single-source-of-truth entries, (when configured) the file-name heuristics, and (when enabled) `changes.regressionRecheck` for the profile's latest FAIL documents decide which documents are impacted; `impact.maxImpactedDocs` caps that set.
- **Layers and profiles.** A run executes layers. `L-SCOPE` computes the impacted set, `L-DOC` verifies each impacted document, `L-PROJECT` runs the built-in document checks and your `projectChecks`; `L-ENRICH`, `L-SECURITY`, `L-ADVERSARIAL`, and `L-CLAIM` add review layers. A profile (`focused`, `standard`, `extended`) selects which layers run; `enabledLayers` in the configuration declares which layers the repository allows.
- **Verdict and anchor.** A verdict is `CONSISTENT` or `NEEDS_FIX`. A `CONSISTENT` run advances the profile's anchor, and a full `NEEDS_FIX` run accepted with `--accept-baseline` advances it the same way; the next incremental run measures changes from that anchor either way. A run that cannot reach a verdict ends `undecided` with a reason, and a run whose sealed run contract fails the gate's integrity checks ends `REFUSED`.

Document verification is performed by a backend. Three exist: the Codex CLI, the agy CLI, and Claude Code agents. A non-interactive start takes the first usable one in the order Codex, agy, Claude Code agents; when the skill starts the audit and two or more are usable, it asks which to use (section 7). The choice is sealed into the run, and a resume uses the same backend. With Claude Code agents, the skill hands the verification to them through a workflow and resumes the engine when they finish.

## 3. Prerequisites

| requirement | notes |
|---|---|
| macOS or Linux | `projectChecks` run only on macOS (they use `sandbox-exec`). |
| git | git supplies the diff and the snapshot. Claude Code is required for the `/docaudit:audit` skill and the Claude Code agents backend. |
| Python 3.12 or newer | The engine uses only the standard library. |
| Codex CLI (optional) | Used when `codex` is on `PATH`, `codex --version` and `codex exec --help` work, and `auth.json` in the Codex home is a readable regular file (symlinks and other non-regular files are rejected). The Codex account must be able to use the model `gpt-6.1-sol` (section 7). The `L-SECURITY`, `L-ADVERSARIAL`, and `L-CLAIM` layers of the `extended` profile use Codex for model calls (section 8). |
| agy CLI (optional) | Used only with a dedicated, signed-in home directory named by `AGY_HOME` and a tool-guard hook file (section 7, "The agy backend"). |
| Node.js (optional) | Only for running the repository's own test suite. |

A repository needs nothing beyond the configuration file. The Markdown documents that `corpus.docGlobs` selects can be anywhere in the tree; the engine creates the state directory when needed, for example when opening a run or running `migrate`; see section 4's `report.path` for the report directory and file.

## 4. Write the configuration

Create `.claude/docaudit.json`. Start from the minimal example in the README and adjust four things:

1. `corpus.docGlobs`: the documents to audit. Exclude generated or vendored documents with `corpus.excludeDocGlobs`.
2. `changes.diffGlobs`: the files whose changes should trigger an audit. Usually the source, configuration, and documentation directories together.
3. `impact.map`: at least one entry per source area (section 5).
4. `report.path`: a Markdown path ending in `.md` with exactly one `<YYYY-MM-DD>` and at most one `[_NN]` anywhere in the path; the basename must have nonempty text before the date when the date is in the basename, or be nonempty when the date is in a directory. For example, `reports/audit_<YYYY-MM-DD>[_NN].md`. The report directory is created during run preparation after retrieval, even if no report is published; a run refused earlier does not create it. Reports are never overwritten; when a name exists, the next report uses the first available suffix from `_02` through `_1000` at `[_NN]` if present, or immediately after the date otherwise. If all names are taken, the engine stops with status 4, `path-exhausted`, and the open run's `runId`; move an older report to free a name, then resume that run.

Leave `enabledLayers` at `["L-SCOPE", "L-DOC", "L-PROJECT"]` unless you intend to run the `extended` profile (section 8). Keep `documentChecks.frontMatterFields` empty until documents follow that convention; `documentChecks.indexFiles` instead lists orphan-check link sources that need not exist (unreadable files count as empty). Without `--profile`, the default `standard` layers are checked before opening a run; if only `L-PROJECT` is missing, status 3 and `capability-missing:L-PROJECT` refuse it, so specify `--profile focused`. Unknown keys are rejected, so a typo fails validation with `config-invalid:<detail>` instead of being ignored.

Commit the configuration. It is part of the repository's contract, and the engine seals its bytes into every run: changing the file while a run is open makes the run `REFUSED config-drift`.

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
location. At the gate, a change is tolerated only inside that location, outside
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
classification after a restart; the snapshot has a 64 MiB limit and may end
the run as `undecided worktree-too-large`.

## 5. Build a good impact map

`impact.map` is a list of `{"source": <glob>, "docs": [<path>, ...]}` entries. When a changed file matches `source`, only listed documents in the corpus become impacted; listed documents outside the corpus are excluded. Two rules make the map useful:

- **Map by responsibility, not by directory.** A document that explains the deployment procedure should be mapped from the deployment scripts and the configuration files it cites, wherever they live.
- **Prefer several small entries to one broad entry.** A single `src/**` entry listing every corpus document marks those documents as impacted when a changed path matches `src/**`; if the total number of impacted corpus documents exceeds `impact.maxImpactedDocs`, the run ends `undecided impact-limit`.

`impact.ssotSources` works like the map for files that are the single source of truth for a value (a version file, a port table): a change to the source impacts only its listed documents in the corpus; listed documents outside the corpus are excluded.

`impact.heuristics` is optional and off unless the key is present. When configured, `L-SCOPE` takes the file name of every changed file, with and without its extension, and marks a document as impacted when its text contains one of those names as a plain substring. `minIdentifierLength` drops short names, `excludeBasenames` drops names such as `index.ts` or `README` that would match everywhere (exclusion names are case-insensitive), `excludeDocPathTokens` ignores the names of changed documents themselves, and `saturationWarnRatio` warns when the documents impacted only by this matching reach that share of the whole corpus. Start without heuristics; add them when the map alone misses documents that name the changed files.

Set `impact.maxImpactedDocs` to the largest set you are willing to verify in one incremental run. A full run is not capped by it.

## 6. Migrate a legacy configuration

If the repository still has a legacy .claude/doc-audit.json, the `migrate` subcommand converts it:

```sh
python3 ~/.claude/skills/docaudit/skills/audit/engine migrate --dry-run --repo-root .   # shows the conversion; exit 1 = not convertible
python3 ~/.claude/skills/docaudit/skills/audit/engine migrate --repo-root .             # writes .claude/docaudit.json when it is absent
```

The dry run prints a result of `convertible`, `not-convertible`, or `unchanged`, together with the converted configuration, `counts` (legacy history entries, whether a last-run record exists, and the list of dropped keys), the inputs it read with their hashes, `anchor` (never migrated), and `runOpen`. Project facts (document globs, diff globs, the impact map, report path, front-matter and index settings, heuristics, single-source-of-truth entries) are mapped to their 1.0.0 keys; migration writes heuristics with the earlier-release defaults filled in. Keys that described the old installation, command mappings, or optional tools are dropped, because 1.0.0 detects tool availability instead of configuring it. The full disposition table is in CONFIG-1.0.0.md, "Old-key disposition".

The converted configuration always enables `L-SCOPE`, `L-DOC`, and `L-PROJECT` with no `projectChecks`. If a legacy history file exists, its entries are copied into the new `history.jsonl` as `legacy` lines; legacy anchors are not migrated, so the first run of every profile must be `--full`. A completed migration leaves a marker in the history. Running `migrate` again with the same legacy inputs reports `unchanged` and writes nothing, even while a run is open; with changed legacy inputs it is rejected with `migration-input-changed`. Otherwise the full migration refuses to run while a run is open (`migration-run-open`) and refuses to replace an existing `.claude/docaudit.json` whose bytes differ from the conversion (`migration-target-exists`).

Heuristic-only impacted documents count toward `impact.maxImpactedDocs`, so exceeding the limit stops the run with `impact-limit`, unlike the earlier harness, which discarded excess heuristic matches and continued. A configuration migrated by an earlier release can lack the earlier defaults; because matching legacy inputs return `unchanged` and do not rewrite an existing file, reconcile `impact.heuristics` manually: if absent, set it to this object; if present, retain its keys and values, add from this object only the keys it does not already have, and append to `excludeBasenames`, in this object's order, each name not already in the existing list case-insensitively:

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
/docaudit:audit --profile focused     # choose the profile; the default is standard
```

The skill starts the engine with `--ask-backend` (see "Choose the backend" below). It runs the engine, and when the engine hands verification to Claude Code agents (`nextAction: invoke-workflow`) it launches the verification workflow and resumes the engine, up to three times, then abandons the run. At the end it shows the outcome, the reason, and the report path.

Outside Claude Code the same run is a command; it needs the Codex backend or a set-up agy because no Claude Code agents are available (add `--backend agy` or `--backend codex` to choose):

```sh
python3 ~/.claude/skills/docaudit/skills/audit/engine audit --full --profile standard --repo-root /path/to/repo
```

For a read-only preview, run `python3 ~/.claude/skills/docaudit/skills/audit/engine scope --repo-root /path/to/repo`. It accepts `--full`, `--profile focused|standard|extended`, and `--repo-root` with the same defaults as `audit`, and uses the same configuration, anchor, history, and profile selection. It prints one JSON object in the `scope.json` structure, including `changed`, `impacted`, and per-document `provenance` such as `self` and `regression`. It opens no run, takes no lease or mutex, writes no repository, state, or report files or directories, invokes no model or external backend CLI, and performs no worktree integrity check. Success exits 0; calculation or configuration refusal exits 3 with the pre-run refusal JSON and `reason`.

### Choose the backend

Three backends can verify documents: the Codex CLI, the agy CLI, and Claude Code agents (only inside Claude Code).

- `--backend agy|codex|claude` names the backend. `--fast`, allowed only together with `--backend codex`, adds Codex's priority service tier. A `--backend` value other than those three is refused with `backend-invalid`, and `--fast` without `--backend codex` with `fast-requires-codex`; both refusals happen before a run is opened (exit status 3). A named backend that cannot be used ends the run `undecided backend-unavailable`.
- The skill always starts the engine with `--ask-backend`. If you gave no `--backend` and at least two backends are usable, the engine opens no run: it prints `nextAction: choose-backend` with `runId: null` and a `choices` array, and exits with status 0. `choices` lists only the usable backends, in this order: Codex, Codex with `"fast":true`, agy (`{"backend":"agy","fast":false}`), Claude Code agents (`{"backend":"claude","fast":false}`). The skill asks you to pick one (the first is the recommendation) and starts the engine again with `--backend`, plus `--fast` when `fast` is true. If the question is closed without an answer the first choice is used; if it cannot be asked, the skill starts the engine without `--backend`, which gives the default below.
- Without `--backend` and without `--ask-backend` (a plain command, or the last case above) the engine takes the first usable backend in the order Codex, agy, Claude Code agents. An environment with a usable agy but no working Codex therefore changes its main backend from Claude Code agents to agy; pass `--backend claude` to keep Claude Code agents.
- The choice is written into the run (`backendSelection` in the `run-options` record and in `capability.json`), so `resume` uses the same backend and the same Fast setting.

The Codex backend runs the model `gpt-6.1-sol` at reasoning effort `medium`, and Fast adds `-c service_tier=priority`. Document-verification calls are limited to 900 seconds each, with up to three attempts per document and four calls in flight. If the Codex account cannot use `gpt-6.1-sol` and documents are impacted, the calls fail without document judgements and the run ends `undecided backend-failed`; when no documents are impacted, no model call is made. Regardless of which backend verifies the documents, Codex calls for the `L-SECURITY`, `L-ADVERSARIAL`, and `L-CLAIM` layers in the `extended` profile use that model and reasoning effort, with a 900-second limit and up to three attempts per assessment. `L-ADVERSARIAL` assesses each impacted document with up to four calls in flight; `L-SECURITY` assesses all impacted documents together; `L-CLAIM` assesses each `FAIL` finding from `L-ADVERSARIAL` one at a time. docaudit does not switch to another backend by itself; choose one with `--backend`.

The Claude Code agents backend runs the verifier agent `docaudit:doc-impact-verifier` with model `claude-sonnet-5-5` at effort `high`.

### The agy backend

agy verifies documents with the agy CLI and the model `gemini-3.8-flash-medium`. It is used only when every condition below holds. When one fails, agy is left out of `choices` and of the default order without a message; the reason is recorded in the run's `capability.json` (`agyReason` for the home and the tool, `agyRepoBlock` for the repository).

**Set up a dedicated home.**

1. Create an empty directory and point `AGY_HOME` at it in the environment the engine runs in, for example `mkdir "$HOME/.agy-docaudit"` and `export AGY_HOME="$HOME/.agy-docaudit"`. `AGY_HOME` is required: if it is unset, or is not an existing directory, agy is not used (create the directory first, because agy itself would create a missing home). docaudit starts every agy process with `HOME` replaced by `AGY_HOME` and with only `PATH`, `LANG`, `LC_ALL`, `TMPDIR` and `AGY_CLI_DISABLE_AUTO_UPDATE=true` besides, so your everyday agy login, settings and plugins are not used.
2. Sign in inside that home: start agy with `HOME="$AGY_HOME" agy`, sign in when it asks, and quit. docaudit does not sign in for you; it checks that `.gemini/antigravity-cli/antigravity-oauth-token` exists in `AGY_HOME` and is readable.
3. Install the tool guard. agy's web search is not stopped by permission settings, so docaudit requires a hook that denies every tool except the five that read and search files (`view_file`, `list_dir`, `find_by_name`, `grep_search`, `finish`). Run `mkdir -p "$AGY_HOME/.gemini/config"`, then create `.gemini/config/hooks.json` in `AGY_HOME` with exactly this content:

```json
{
  "docaudit-deny-tools": {
    "PreToolUse": [
      {
        "matcher": "^(?:|[^fglv].*|f(?:|[^i].*|i(?:|[^n].*|n(?:|[^di].*|d(?:|[^_].*|_(?:|[^b].*|b(?:|[^y].*|y(?:|[^_].*|_(?:|[^n].*|n(?:|[^a].*|a(?:|[^m].*|m(?:|[^e].*|e(?:.+)))))))))|i(?:|[^s].*|s(?:|[^h].*|h(?:.+))))))|g(?:|[^r].*|r(?:|[^e].*|e(?:|[^p].*|p(?:|[^_].*|_(?:|[^s].*|s(?:|[^e].*|e(?:|[^a].*|a(?:|[^r].*|r(?:|[^c].*|c(?:|[^h].*|h(?:.+)))))))))))|l(?:|[^i].*|i(?:|[^s].*|s(?:|[^t].*|t(?:|[^_].*|_(?:|[^d].*|d(?:|[^i].*|i(?:|[^r].*|r(?:.+))))))))|v(?:|[^i].*|i(?:|[^e].*|e(?:|[^w].*|w(?:|[^_].*|_(?:|[^f].*|f(?:|[^i].*|i(?:|[^l].*|l(?:|[^e].*|e(?:.+))))))))))$",
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
4. Keep permissions out of the home. `.gemini/antigravity-cli/settings.json` in `AGY_HOME` may be absent, but if it exists it must not have a non-empty `permissions.allow` or a `hooks` key. This includes grants you added there temporarily for another purpose.

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
| `agy-model-missing` | the first 1 MiB of `agy models` output has no whitespace-separated word equal to `gemini-3.8-flash-medium` |

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
- `run-interrupted-resume-required`: the engine process ended mid-run. Resume it with `resume <runId>`, or abandon it with `resume <runId> --abandon` to close it without a verdict. A run left open by an earlier engine after `capability-missing:<layer>` returns status 4 with that reason and its `runId` on resume; the configuration was sealed when it opened, so adding layers cannot repair that resume. Close it with `resume <runId> --abandon`.

The skill does not recover a run left over from an earlier session; do it by hand or with the prompts in PROMPTS.md, section 8.

## 8. Profiles and layers

| profile | layers | backend |
|---|---|---|
| `focused` | `L-SCOPE`, `L-DOC` | the backend chosen or defaulted in section 7 |
| `standard` (default) | focused + `L-PROJECT` | same |
| `extended` | standard + `L-ENRICH`, `L-SECURITY`, `L-ADVERSARIAL`, `L-CLAIM` | same selection for the documents, but `L-SECURITY`, `L-ADVERSARIAL`, and `L-CLAIM` use Codex for model calls, while `L-ENRICH` makes no model calls |

`extended` can be selected only when `enabledLayers` lists all seven layers; otherwise the engine refuses to open the run with `capability-missing:<layer>` (exit status 3). The `L-SECURITY`, `L-ADVERSARIAL`, and `L-CLAIM` layers run on Codex whichever backend verifies the documents (with Fast, if you chose it), so they need a Codex that passes the availability check. Without one, `L-CLAIM` is incomplete and the run ends `undecided`: with reason `codex-unavailable` when the documents are verified through agy, and `workflow-adapter-unavailable` when they are verified through Claude Code agents. With impacted documents `L-SECURITY` and `L-ADVERSARIAL` are incomplete for the same reason, and with no impacted documents they complete without calls. Use `extended` where the Codex CLI passes the availability check.

In `extended`, the adversarial layer asks for evidence-backed contradictions per impacted document, the security layer reviews documented procedures, configuration, secret handling, and permissions once per run, and the claim layer re-checks every adversarial `FAIL` against the repository. Of the findings from the four layers added by `extended`, including `L-ENRICH`, only a confirmed claim can make the verdict `NEEDS_FIX`; adversarial, security, and enrichment findings are informational. A document `FAIL` makes the verdict `NEEDS_FIX` in every profile, and a project-check `FAIL` does so in the profiles that run `L-PROJECT` (`standard` and `extended`).

## 9. Project checks

`L-PROJECT` applies four kinds of read-only checks over the sealed corpus: required front-matter fields (WARN), local Markdown links (a missing target is a blocking FAIL), path-like backtick tokens that point at nothing (WARN), and documents that no other document or index file links to (WARN). Documents listed in `documentChecks.indexFiles` are themselves exempt from orphan findings. `documentChecks.layerGlobs` removes matching documents from a check, for example to exempt generated indexes from the orphan check. When the sealed corpus has only one document, the orphan check does not run and records `orphan: skipped`.

`projectChecks` adds your own commands. Each entry names a check ID, an `argv`, a timeout, and optionally a working directory. The command runs on macOS inside a `sandbox-exec` profile that forbids writes outside the private temporary directory (writing to `/dev/null` is the only other permitted write), and must print a JSON object with a `findings` array, each finding having `id`, `summary`, `severity` (`INFO`, `WARN`, or `FAIL`), and optionally `path`. A `FAIL` finding is blocking: the run ends `NEEDS_FIX` even when every document passed. A nonzero exit, a timeout, or invalid output is itself a blocking `FAIL` finding. On Linux leave `projectChecks` empty when using `standard` or `extended`: those profiles run these checks, so a non-empty list ends the run `undecided sandbox-unavailable`; `focused` does not run them.

## 10. Read the results

- **The result line.** The last line of the engine output is one JSON object. A closed run has `nextAction` (`done` or `abort`), `runId`, `outcome`, and when present `reason` and `reportPath`; a hand-off to the workflow has `nextAction: invoke-workflow` with `requestSeq` and `requestPath` instead of an outcome; a backend question has `nextAction: choose-backend`, `runId` null, and `choices`, and no run is opened. Exit status 0 means the engine finished normally, including `undecided`, `REFUSED`, the hand-off, and the backend question; 2 means command-line parsing rejected an unknown option, an unknown `--profile` value, or a missing command and printed `usage:` to standard error instead of a JSON result line; 3 means it refused to open a run or start a resume (configuration, profile, run-state, or argument problem such as `accept-baseline-requires-full`, `backend-invalid`, or `fast-requires-codex`, or unsupported platform `io-unsupported-platform`, named in `reason`); 4 means an opened run could not proceed or a resumed run could not proceed after resume started, including a later `mutex-timeout` or exhausted report names (`path-exhausted`). For engine-handled interruptions with status 4, the result includes that run's `runId`; resume or abandon the still-open run.
- **The report.** Published at `report.path` with fixed front matter and sections for the run, the audited documents, the findings, the verdict, the anchor, the measurements, and the evidence. Most of its headings and fixed phrases are Japanese. It has one line per finding: each document with its verdict and a one-line summary of the mismatch, and each other finding with its severity and summary, including project checks, links, and claims; the evidence strings behind a judgement (the Codex backend is asked to cite `file:line` in its rationale; Claude Code agents are asked for repository-relative evidence) are kept in the run's evidence ledger, in the `judgement` lines of the history, and, for runs verified through Claude Code agents, in `runs/<runId>/requests/<seq>/judgements/`. Unsafe summary text is redacted and counted as `- redacted: N`; history `judgement` lines use the same redact rule, while the ledger and `requests/<seq>/judgements/` remain original. Runs that end `undecided` before verification starts publish no report. The Run section shows the baseline anchor's backend model and engine version and adds `基準との違い` when known values differ from this run, since judgement can change even for unchanged documents.
- **The state directory.** `.claude/state/docaudit/history.jsonl` records one line per run outcome and per judgement (plus `flip`, `anchor`, `legacy`, and `migration` lines), `anchors/<profile>.json` holds the current anchor, and `runs/<runId>/` may hold the sealed manifest, the evidence ledger with adapter results and judgements, and `verdict.json`; for example, a run ending `undecided backend-unavailable` has none of those artifacts. Decide once whether to commit the state directory (auditable history in version control) or ignore it (local state per clone); the engine works either way.

- **The scope preview.** `scope` returns the scope JSON itself, creates no run records, report, or state directory, and exits 0 on successful calculation or 3 with a refusal JSON containing `reason`.

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
| `undecided corpus-unreadable` | a new run could not read a regular worktree file, including one outside the corpus, while computing the digest (except paths allowed by `worktree.tolerateChangesIn`) (corpus documents and files matched by `diffGlobs` over 8 MiB are treated as unreadable), or a Claude Code agent run could not copy a corpus document into the verification mirror | make the file readable and 8 MiB or smaller; if it is over 8 MiB, exclude it from reading with `docGlobs`, `excludeDocGlobs`, or `diffGlobs`; for a file outside the corpus in a Git-ignored location that holds no tracked file, you can instead allow that location in `worktree.tolerateChangesIn` (see the conditions above); then rerun |
| `undecided sandbox-unavailable` | nonempty `projectChecks` used by `standard` or `extended` on a non-Darwin platform, missing `/usr/bin/sandbox-exec`, or a `/usr/bin/true` preflight that cannot start, exits nonzero, or times out after 20 seconds | empty `projectChecks` on Linux when using `standard` or `extended` (`focused` does not run them); on macOS, restore `sandbox-exec` or fix the preflight and rerun |
| `undecided workflow-adapter-unavailable` | `extended` with the documents verified through Claude Code agents and no usable Codex | make Codex usable |
| `undecided codex-unavailable` | `extended` with the documents verified through agy and no usable Codex | make Codex usable |
| `undecided abandoned` | the run was abandoned | run again |
| `REFUSED worktree-modified` | an unaccepted working-tree change was found; `worktreeDiff` lists only refused paths | stop the writer and run again, list its ignored location in `worktree.tolerateChangesIn`, or move its output outside the working tree; follow the report guidance when `worktreeDiffIgnoredOnly` is true |
| `REFUSED config-drift` | `.claude/docaudit.json` changed during the run | run again |
| other `REFUSED` reasons | the sealed run contract failed an integrity check (lease, seal, evidence hashes, layer set, backend, judgements, adversarial and claim consistency) | inspect `runs/<runId>/`; run again |

## 11. Troubleshooting

- **`config-invalid:<detail>` on every run.** The detail names the offending key. Paths must be repository-relative without a `..` path segment (a parent-directory reference, for example `a/../b`); `report.path` must end in `.md` and contain exactly one `<YYYY-MM-DD>` and at most one `[_NN]` anywhere in the path; the basename must have nonempty text before the date when the date is in the basename, or be nonempty when the date is in a directory. For `config-invalid:worktree.tolerateChangesIn`, check the path form and whether Git ignores it and it excludes tracked files, corpus documents, and the engine's configuration, state, and report locations.
- **`config-needs-migration`.** Only the legacy file exists. Run `migrate` (section 6).
- **The first run reports many front-matter or orphan warnings.** They are non-blocking. Fix them over time, or exempt documents with `documentChecks.layerGlobs`.
- **`history-corrupt`.** `history.jsonl` has a malformed line, is not a regular file, is not UTF-8, or contains an overlong line; an anchor file under `anchors/` is unreadable or malformed; or the anchor candidate that a `CONSISTENT` run points at is unreadable or does not match its recorded hash. A corrupt `history.jsonl` stops both reading and appending history. If only an anchor file is corrupt, incremental runs for that profile stop with `history-corrupt`, while `--full` ignores the anchor, reads history, and appends the outcome; only an outcome that advances the anchor (`CONSISTENT` or `NEEDS_FIX` accepted with `--accept-baseline`) then stops during recording with `history-corrupt` and exit status 4. Keep a copy of the whole state directory, then find which file is broken: a malformed history line is fixed by moving the history file aside while preserving its valid lines under another name and starting a new history file; a broken anchor is fixed by moving that profile's anchor file aside, after which the profile needs a new `--full` run. If the failure happened inside a run (exit status 4), that run remains open; repair a corrupt `history.jsonl` first because abandonment also fails while it is corrupt, then resume or abandon the run before the next audit. A corrupt anchor file alone does not prevent abandonment. If `history.jsonl` itself cannot be read because of permissions, it stops with exit status 4 and reason `PermissionError`, not `history-corrupt`; repair permissions, then resume or abandon that run. As an exception to the non-regular-file case, if `history.jsonl` is a symbolic link, the run stops with exit status 4 and reason `OSError`, not `history-corrupt`; replace the link with a regular file, then resume or abandon that run.
- **`mutex-timeout`.** Another engine process is using state for an operation (for example, `migrate`). Wait for it to finish and retry; if `run-open.json` still names an open run, resume or abandon that run. **`run-in-progress`.** Another engine process holds the run. Wait for it, or resume or abandon the run named in `run-open.json` once no process holds it.
- **The skill is not listed after install.** Start a new Claude Code session or run `/reload-plugins`, then check `claude plugin list`.
- **Something was written into the repository that you did not expect.** The engine writes only the report and run state; the state is under `.claude/state/docaudit/`. It excludes the top-level `.mdq/` from its tree scan. Other tools may write during a run, but a changed path is tolerated only under the configured, still Git-ignored location and outside the sealed corpus and snapshot. A change outside it, including a newly created parent directory, causes `REFUSED worktree-modified`; the two remedies are to list an eligible location in `worktree.tolerateChangesIn` or move the output outside the working tree. If the tree exceeds its entry or byte limit, the result is instead `undecided worktree-too-large`.
