# docaudit

docaudit audits a repository's Markdown documentation against the code and configuration it describes, and tells you which documents no longer match. It runs as a Claude Code plugin skill (`/docaudit:audit`) or as a plain Python command, and it is report-only: it never edits your documents.

New to docaudit? Read [docs/ADOPTION.md](docs/ADOPTION.md) (日本語: [docs/ADOPTION.ja.md](docs/ADOPTION.ja.md)) for the full adoption guide, and [docs/PROMPTS.md](docs/PROMPTS.md) (日本語: [docs/PROMPTS.ja.md](docs/PROMPTS.ja.md)) for copy-paste prompts.

A run ends in one outcome: the verdicts `CONSISTENT` or `NEEDS_FIX`, or, when it cannot reach one, `undecided` or `REFUSED`. See [docs/ADOPTION.md](docs/ADOPTION.md) for what each outcome means and how to act on it. A run that ends `CONSISTENT` or `NEEDS_FIX` publishes a Markdown report into your repository, with the evidence kept in the run's records. In a run that ends `CONSISTENT` or `NEEDS_FIX`, every impacted document has a judgement; when none are impacted, there are no document judgements. Runs are change-driven: after a full audit writes an anchor for the profile, later runs judge impacted documents and, when `changes.regressionRecheck` is enabled, documents whose latest judgement for the profile was `FAIL` (where enabled, built-in `L-PROJECT` checks inspect corpus documents without limiting themselves to the impacted set). A `CONSISTENT` run writes an anchor for its profile; a `NEEDS_FIX` run also does so when accepted with `--full --accept-baseline` and all blocking findings are L-DOC judgements. Without an anchor for that profile, an incremental run ends `undecided anchor-missing`.

## Requirements

- macOS or Linux.
- git. Claude Code is needed only to run docaudit as its plugin skill; outside Claude Code the engine runs as a command and needs the Codex CLI or the agy CLI (see below).
- Python 3.12 or newer. The engine uses only the standard library.
- Optional: the Codex CLI. When the engine's availability check passes (`codex` on `PATH`, a working `codex --version` and `codex exec --help`, and an `auth.json` in the Codex home that is a readable regular file, not a symlink or other non-regular file) it can send document verification to Codex, which runs the model `gpt-6.1-sol` at reasoning effort `medium`. If the account cannot use that model and documents are impacted, model calls fail without document judgements and the run ends `undecided backend-failed`; when no documents are impacted, no model call is made.
- Optional: the agy CLI. It is used only with a dedicated, signed-in home directory named by `AGY_HOME` and a tool-guard hook file that [docs/ADOPTION.md](docs/ADOPTION.md) (section 7) gives verbatim. That section also lists every condition under which agy is not used.
- Backends: document verification runs on Codex, agy, or Claude Code agents (only inside Claude Code). When the skill starts an audit and two or more are usable it asks which to use. Without a choice (a plain command, or a question that cannot be asked) the engine takes the first usable one in the order Codex, agy, Claude Code agents, so an environment with agy but without Codex uses agy where it used Claude Code agents before; `--backend claude` keeps Claude Code agents. `--backend agy|codex|claude` sets the choice explicitly.
- Node.js is needed only to run the repository's own test suite.

## Install

Before updating from an earlier version, close every run that version left open (a `.claude/state/docaudit/run-open.json` exists with `state` other than `closed`) with the earlier version's engine, by resuming it to the end or with `resume <runId> --abandon`; for a sealed `manifest`, the gate's backend check returns `REFUSED backend-mismatch` if its recorded backend and model differ from those resolved using this version's defaults.

The plugin is installed as a "skills-dir" plugin: the tracked files of the release tag are placed under `~/.claude/skills/docaudit/`. If that directory already exists it is moved aside first, to `~/.claude/docaudit.before-1.5.0`, so that no files from another version remain. The backup must not stay under `~/.claude/skills/`: Claude Code loads every directory there as a plugin, and the backup carries the same plugin name, so it would be loaded instead of the new install. For the same reason the chain starts by checking that no `docaudit.*` entry exists under `~/.claude/skills/` (for example a backup made by the install procedure of an earlier release) and stops, before creating or changing anything, if one does or if that check cannot be completed; move every such entry out of `~/.claude/skills/` and run the chain again.

```sh
{ [ ! -e ~/.claude/skills ] || [ "$(find ~/.claude/skills/ -maxdepth 1 -name 'docaudit.*' -print -quit && echo ok)" = ok ]; } \
  && SRC=$(mktemp -d) && TAR=$(mktemp) \
  && git clone https://github.com/akira993/docaudit "$SRC" \
  && { [ ! -e ~/.claude/skills/docaudit ] \
       || { [ ! -e ~/.claude/docaudit.before-1.5.0 ] && mv ~/.claude/skills/docaudit ~/.claude/docaudit.before-1.5.0; }; } \
  && [ ! -e ~/.claude/skills/docaudit ] \
  && git -C "$SRC" archive --format=tar -o "$TAR" v1.5.0 \
  && mkdir -p ~/.claude/skills/docaudit \
  && tar -x -f "$TAR" -C ~/.claude/skills/docaudit \
  && python3 ~/.claude/skills/docaudit/skills/audit/engine --version
```

The last command prints `1.5.0`. The chain stops at the first step that fails, and an existing install is never overwritten in place. If it stops after the backup was made, `~/.claude/skills/docaudit` is either absent or holds an unverified copy: if it exists, rename it to a name that does not exist yet (for example `~/.claude/docaudit.unverified-1.5.0`) or delete it; then move `~/.claude/docaudit.before-1.5.0` back to `~/.claude/skills/docaudit`. If the backup name already exists from an earlier attempt, rename that older backup first.

Start a new Claude Code session (or run `/reload-plugins`); `claude plugin list` then shows `docaudit@skills-dir` at version 1.5.0, and the skill is available as `/docaudit:audit`.

Alternatively, install through the plugin marketplace that this repository declares. Use one install method, not both: two enabled plugins with the same name have not been verified, so if `~/.claude/skills/docaudit` already exists, move it out of `~/.claude/skills/` first.

```sh
claude plugin marketplace add akira993/docaudit@v1.5.0 \
  && claude plugin install docaudit@akira-plugins \
  && python3 ~/.claude/plugins/cache/akira-plugins/docaudit/1.5.0/skills/audit/engine --version
```

The last command prints `1.5.0`. Start a new Claude Code session (or run `/reload-plugins`); `claude plugin list` then shows `docaudit@akira-plugins` at version 1.5.0. If you installed this way, use that engine path wherever the commands below say `~/.claude/skills/docaudit/skills/audit/engine`. The path assumes the default configuration directory; if the last command fails, locate `skills/audit/engine` under your plugin cache and use that path instead.

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
- `report.path` is where each run publishes its report; `<YYYY-MM-DD>` and the optional `[_NN]` placeholder keep reports unique.
- The built-in link, existence and orphan checks belong to `L-PROJECT`, so they run under the `standard` and `extended` profiles and not under `focused`; the orphan check is skipped when the corpus has one document. `documentChecks.frontMatterFields` and `documentChecks.indexFiles` add required front-matter fields and index files; the orphan check needs a direct, one-hop link from another corpus document or an index file, does not follow links transitively, and exempts documents listed in indexFiles themselves. Documents that link to each other are therefore not reported as orphans even when no index file reaches them. Enable those two checks once your documents follow the convention, otherwise the first run reports the gaps.
- `projectChecks` runs your own commands as part of the audit, inside the macOS `sandbox-exec` sandbox. On Linux leave it empty: a non-empty `projectChecks` makes the run end `undecided`.
- `enabledLayers` declares the layers the repository allows. The `extended` profile can only be selected when all seven layers are listed (see the profile table below).

Every key, its default and its validation rule is documented in [docs/CONFIG-1.0.0.md](docs/CONFIG-1.0.0.md). If the repository still has a legacy .claude/doc-audit.json, `python3 ~/.claude/skills/docaudit/skills/audit/engine migrate --dry-run --repo-root .` shows how it would be converted (exit status 1 means it cannot be converted), and the same command without `--dry-run` writes the new `.claude/docaudit.json` next to the legacy file, but only when that file does not already exist there.

## Run an audit

In Claude Code, inside the repository:

```
/docaudit:audit --full            # first run of a profile: audit the whole corpus
/docaudit:audit --full --accept-baseline # accept a document-only NEEDS_FIX baseline
/docaudit:audit                   # later runs: judge documents impacted by changes since the last accepted run (where enabled, built-in project checks can inspect corpus documents beyond the impacted set)
/docaudit:audit --profile focused # choose a profile (focused, standard, extended); each profile starts with its own --full run
/docaudit:audit --backend codex   # choose who verifies the documents (agy, codex or claude); --fast is allowed only with codex
```

When two or more backends are usable and you gave no `--backend`, the skill asks which to use (the first choice, Codex when it is usable, is the recommendation; a question closed without an answer takes it). If the question cannot be asked, the engine's default order applies. `--fast` asks Codex for its priority service tier and is refused with `fast-requires-codex` unless `--backend codex` is also given; an unknown `--backend` value is refused with `backend-invalid`. The choice is sealed into the run, so a resumed run uses the same backend.

Anchors are kept per profile. The first anchor is written after `CONSISTENT`, or after `--full --accept-baseline` ends `NEEDS_FIX` with only L-DOC blocking judgements. Later `CONSISTENT` runs also advance the anchor, and incremental runs measure from it. A known FAIL document is not rechecked after acceptance only when the document itself has not changed, is not impacted by `impact.map`, `ssotSources`, or heuristics, and `changes.regressionRecheck` is disabled. `changes.regressionRecheck: true` rechecks current failed documents; they count toward `impact.maxImpactedDocs`, stop with `impact-limit` when exceeded, and leave that set when their latest judgement is PASS or WARN. Non-document blocking findings are not accepted; find and fix their causes in the report findings first. This path is available in 1.1.0 and later.

Profiles decide which layers run:

| profile | layers | use |
|---|---|---|
| `focused` | scope, document verification | quick check of the impacted documents |
| `standard` (default) | focused + project checks | the everyday audit |
| `extended` | standard + enrichment, security, adversarial and claim review | release sweeps. Requires all seven layers in `enabledLayers` and the Codex CLI: `L-SECURITY`, `L-ADVERSARIAL`, and `L-CLAIM` use Codex for model calls, whichever backend verifies the documents, while `L-ENRICH` makes no model calls. Without a usable Codex, `L-CLAIM` is incomplete (`codex-unavailable`, or `workflow-adapter-unavailable` when the documents are verified through Claude Code agents) and the run ends `undecided` without a verdict; with impacted documents `L-SECURITY` and `L-ADVERSARIAL` are incomplete for the same reason, and with no impacted documents they complete without calls |

Outside Claude Code, with the Codex CLI or a set-up agy available, the same audit runs as a command (add `--backend agy` or `--backend codex` to choose; without it the first usable of Codex and agy is taken):

```sh
python3 ~/.claude/skills/docaudit/skills/audit/engine audit --full --profile standard --repo-root /path/to/repo
```

## What you get

- The result, printed as the last line of output as one JSON object: `nextAction` (`done`, `abort`, `invoke-workflow` while the skill hands verification to Claude Code agents, or `choose-backend` when a backend question is needed before a run is opened), `outcome`, `reason` when there is one, `reportPath` when a report was published, and `choices` (the usable backends) with `choose-backend`.
- The report at `report.path`: the verdict, the audited documents, and one line per finding: each document with its verdict and a one-line summary of the mismatch, and each other finding with its severity and summary, including project checks, links, and claims. Unsafe summary text is redacted and reported as `- redacted: N`; history judgement lines are redacted too, while the evidence ledger and agent judgement files under `runs/<runId>/` remain original. Most of the report's headings and fixed phrases are Japanese. Runs that end `undecided` before verification starts publish no report.
- Run state under `.claude/state/docaudit/`: `history.jsonl` (one line per run and per judgement), and one directory per run that may contain the sealed manifest, evidence ledger, per-document judgements, and final verdict; for example, a run ending `undecided backend-unavailable` has none of those artifacts. Add the state directory to version control or ignore it as you prefer. Inside the repository the audit writes only there and to the report path; scratch files go to a private temporary directory outside the repository, and `migrate` writes the converted configuration.
- Exit status 0 when the engine finished normally (a verdict, an `undecided` or `REFUSED` outcome, a hand-off to Claude Code agents, or the `choose-backend` question), 3 when it refused to open a run or start a resume (the JSON `reason` says why), 4 when an opened run could not proceed or a resumed run could not proceed after resume started. For engine-handled interruptions with status 4, the result includes that run's `runId`; resume or abandon that run.

## Layout

- `skills/audit/` — the audit skill and the engine (`skills/audit/engine`); the skill's instruction file (`SKILL.md`) is written in Japanese; the engine's JSON output is language-neutral
- `agents/`, `workflows/` — the Claude Code agent and workflow used when the documents are verified through Claude Code agents
- `docs/` — the configuration reference, and the adoption guide and prompt examples in English and Japanese
- `tests/` — unit and acceptance tests

Run the tests from a checkout (Node.js required for the workflow-script tests) with:

```sh
python3 -m unittest discover -s tests -t . -q
python3 tests/run_acceptance.py --expect all
```

## License

MIT. See [LICENSE](LICENSE).
