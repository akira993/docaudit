# docaudit 1.0.0 configuration

Configuration is the UTF-8 JSON file `.claude/docaudit.json`.  It contains
`"docauditSchema": "1.0"`; key order is not significant. Unknown keys are rejected at every level. Paths
and glob values are repository-relative and must not contain a `..` path
segment (a parent-directory reference) or begin with an absolute path.

| key | type | required | default |
|---|---|---:|---|
| `docauditSchema` | `"1.0"` | yes | — |
| `enabledLayers` | layer ID array | yes | — |
| `corpus.docGlobs` | glob array | yes | — |
| `corpus.excludeDocGlobs` | glob array | no | `[]` |
| `corpus.respectGitignore` | boolean | no | `true` |
| `corpus.auditReportsInCorpus` | boolean | no | `false` |
| `changes.diffGlobs` | glob array | yes | — |
| `changes.regressionRecheck` | boolean | no | `false` |
| `impact.map` | `{source: glob, docs: path[]}` array | yes | — |
| `impact.maxImpactedDocs` | integer at least 1 | yes | — |
| `impact.heuristics` | `{minIdentifierLength: int, excludeBasenames: string[], saturationWarnRatio: number, excludeDocPathTokens: boolean}`; exclusion names are compared case-insensitively | no | omitted |
| `impact.ssotSources` | `{source: path, docs: path[]}` array | no | `[]` |
| `report.path` | string template | yes | — |
| `worktree.tolerateChangesIn` | path string array | no | `omitted` |
| `documentChecks.frontMatterFields` | string array | no | `[]` |
| `documentChecks.frontMatterOverrides` | `{glob: glob, fields: string[]}` array | no | `[]` |
| `documentChecks.indexFiles` | path array | no | `[]` |
| `documentChecks.layerGlobs` | `{check ID: glob[]}` object | no | `{}` |
| `projectChecks` | `{id: string, argv: string[], timeoutSec: int >= 1, cwd?: path}` array | no | `[]` |

`enabledLayers` is the only declaration of project capability.  The availability
of external tools is discovered by the engine rather than configured here.

### Worktree changes made by other tools

The optional `worktree` object accepts only `tolerateChangesIn`, an array of
repository-relative paths such as `cache/` or `logs/run.log`. A trailing `/`
names a directory and everything below it; without `/`, the value names one
exact file. Globs (`*`, `?`, `[`), absolute paths, `..`, `.`, `./`, `//`, and
empty path components are invalid. The engine does not normalize these values.
An absent `worktree` object or an empty list keeps the usual worktree rule.

Before opening a run, the engine checks that each listed path is ignored by
Git, contains no tracked file or corpus document, and neither contains
nor lies inside the configuration file, state root, or report location. A file value that names a directory containing the configuration file,
state root, or a report is also invalid. An
invalid value or failed restriction gives
`config-invalid:worktree.tolerateChangesIn`; a non-object `worktree` gives
`config-invalid:worktree`, and an unknown member gives
`config-invalid:worktree.<member>`. A Git or corpus calculation failure keeps
its own reason (for example, `git-failed`). These checks also run when a run that stopped before its configuration was
sealed is resumed. `docauditSchema` stays `"1.0"`; engines at
1.2.1 and earlier reject the `worktree` key as `config-invalid:worktree`.

A changed entry is tolerated at the gate only when it lies at or below a
listed path, is absent from both the sealed corpus and source snapshot, and
is still ignored by Git. A file added with `git add -f` during the run, or a
path whose ignore rule disappears, is refused. Failure to ask Git also
refuses the change. A newly created parent directory of a listed path is
outside that path and is refused. To avoid a refusal for generated files,
either list their ignored location in `worktree.tolerateChangesIn` or move
their output outside the working tree. Listing a location changes only the
gate's decision; it does not permit the engine to write there.

For `corpus.docGlobs`, like `.git/`, files under the repository's top-level `.mdq/` are neither documents nor changed paths from 1.0.1 onward.

## Old-key disposition

| old key | disposition | 1.0.0 key | reason |
|---|---|---|---|
| `anchorPath` | do not migrate | — | state root is fixed |
| `diffGlobs` | migrate | `changes.diffGlobs` | project fact |
| `docGlobs` | migrate | `corpus.docGlobs` | project fact |
| `excludeDocGlobs` | migrate | `corpus.excludeDocGlobs` | explicit exclusion |
| `respectGitignore` | migrate | `corpus.respectGitignore` | corpus policy |
| `impactMap` | migrate | `impact.map` | project fact |
| `maxImpactedDocs` | migrate | `impact.maxImpactedDocs` | project fact |
| `reportPath` | migrate | `report.path` | project fact |
| `indexFiles` | migrate | `documentChecks.indexFiles` | project fact |
| `frontMatterFields` | migrate | `documentChecks.frontMatterFields` | project fact |
| `frontMatterOverrides` | migrate | `documentChecks.frontMatterOverrides` | project fact |
| `heuristics` | migrate | `impact.heuristics` | project fact; migration supplements earlier-release defaults |
| `layerGlobs` | migrate | `documentChecks.layerGlobs` | `migrate` maps old `format` to `front-matter` plus `links`, and old `semantic` to `orphan` |
| `regressionRecheck` | migrate | `changes.regressionRecheck` | project fact |
| `ssotSources` | migrate | `impact.ssotSources` | project fact |
| `auditReportsInCorpus` | migrate | `corpus.auditReportsInCorpus` | project fact |
| `harness` | do not migrate | — | installation detail |
| `docAuditCommands` | do not migrate | — | command mapping is not imported |
| `reviewCommands` | do not migrate | — | security adapter is built in |
| `codexReview` | do not migrate | — | backend is capability-detected |
| `boundaryCommand` | do not migrate | — | harness detail |
| `auditScope` | do not migrate | — | import record is dropped |
| `expectedAbsentPaths` | do not migrate | — | consumer-specific key |
| `webExtract` | do not migrate | — | capability-detected |
| `symbolGraph` | do not migrate | — | capability-detected |
| `semanticSearch` | do not migrate | — | capability-detected |
| `indexing` | do not migrate | — | capability-detected |
| `contextMode` | do not migrate | — | capability-detected |

## Migration

`migrate` reads exactly three legacy inputs: required
.claude/doc-audit.json, optional .claude/state/docaudit-history.json, and
optional .claude/state/docaudit-last-run.json. The config and last-run limits
are 1 MiB and the history limit is 64 MiB. Input SHA-256 values cover the exact
bytes read. Legacy anchors, run directories, and reports are not read or
migrated.

Before creating the state root or taking the mutex, the converter validates all
inputs and prepares every output line. The old config must be an object and
every key not beginning with `_` must appear in Old-key disposition. The old
history must contain an `entries` array. Each entry has nonempty string `runid`
and `ts`, string `path` and `verdict`, and optional string `contentSha`,
`changeSetSha`, `contractVersion`, and `backend`. The old last-run has string
`runid`, `ts`, and `verdict`. One invalid input rejects the entire preflight
without writing or creating the state root. Each prepared history line must be
at most 1 MiB. The validated, canonical converted config, including its trailing
newline, must also be at most 1 MiB; excess is
`migration-config-invalid:size`.

The fifteen rows marked migrate above are mapped to their 1.0.0 keys.
`docauditSchema` is fixed to `"1.0"`, `enabledLayers` is fixed to `L-SCOPE`,
`L-DOC`, and `L-PROJECT`, and `projectChecks` is empty. The rows in Old-key
disposition use these value-shape conversions where the legacy and 1.0.0
schemas differ:

| legacy value | 1.0.0 value | dropped data or rejection |
|---|---|---|
| `impactMap[i] = {changed, impacts, note?, source?}` | `impact.map[i] = {"source": changed, "docs": impacts}` | Presence of `note` or `source` adds `impactMap.note` or `impactMap.source` once. A non-string `changed` or non-string-array `impacts` is `migration-config-invalid:impactMap[i]`. |
| `regressionRecheck = {enabled: boolean}` or a boolean | `changes.regressionRecheck = enabled` or the original boolean | A non-boolean `enabled`, or a value that is neither an object nor boolean, is `migration-config-invalid:regressionRecheck`. |
| `frontMatterOverrides[i] = {globs: string[], fields: string[]}` | One `{glob, fields}` object per glob, in input order | An empty `globs` drops the entry and adds `frontMatterOverrides[i]`. |
| `layerGlobs[check] = {exclude: string[]}` or a string array | The `exclude` array or original array is passed to the check-ID mapping | Object keys other than `exclude` are ignored. |
| `ssotSources[i] = {name, value?, liveSource, docsThatCite}` | `{source: liveSource, docs: docsThatCite}` after removing a trailing `:<digits>` from each doc and deduplicating in first-seen order | A non-string `liveSource` or one beginning with `http://` or `https://` drops the entry and adds `ssotSources.<name>` when a nonempty string name is available, otherwise `ssotSources.<index>`. |
| `heuristics` | If absent, write the legacy defaults. If an object, retain its keys and values, fill missing `minIdentifierLength`, `saturationWarnRatio`, and `excludeDocPathTokens`, and append legacy default exclusion names not already present case-insensitively. A non-object or invalid subkey remains unchanged for normal validation. | — |

Within `layerGlobs`, `format` maps its extracted array to both `front-matter`
and `links`, `semantic` maps to `orphan`, and `existence` keeps its name. Arrays
mapping to the same check ID are concatenated and deduplicated in first-seen
order. Other check IDs are dropped and recorded as `layerGlobs.<id>`. Any
present do-not-migrate keys are also recorded in sorted `droppedKeys`, together
with the value-shape drops in the table; annotation keys beginning with `_` are
ignored and not recorded. The mapped object is passed through the normal config
validator so defaults are filled before canonical JSON serialization. Exactly
those UTF-8 bytes plus one trailing newline are both hashed and written.

Each old history entry produces one history event in original array order:

```json
{"seq":1,"kind":"legacy","runId":"<old-run-id>","ts":"<old-ts>","data":{"source":"history","sourceHash":"<sha256>","legacyIndex":0,"path":"<path>","verdict":"<verdict>","contentSha":null,"changeSetSha":null,"contractVersion":null,"backend":null,"legacyProfile":"unknown"}}
```

Missing optional entry fields become `null`; verdicts are not rewritten. An old
last-run produces one more `legacy` event with `source: "last-run"`, its input
hash, `legacyIndex: 0`, verdict, and `legacyProfile: "unknown"`:

```json
{"seq":2,"kind":"legacy","runId":"<old-run-id>","ts":"<old-ts>","data":{"source":"last-run","sourceHash":"<sha256>","legacyIndex":0,"verdict":"<verdict>","legacyProfile":"unknown"}}
```

Successful conversion ends with exactly one completion marker whose `runId` is
`"migration"` and whose engine-generated UTC `ts` is a string:

```json
{"seq":3,"ts":"<utc-ts>","kind":"migration","runId":"migration","data":{"phase":"completed","inputs":{"<legacy-path>":"<sha256>"},"outputs":{"config":"<sha256>","legacyLines":0},"counts":{"historyEntries":0,"lastRun":0,"droppedKeys":[]},"engineVersion":"<version>"}}
```

If a completed marker's `inputs` exactly match the current input hashes,
conversion returns `unchanged` without writing. A completed marker with other
inputs rejects with `migration-input-changed`. Without a completed marker,
resume counts existing `legacy` events by `(source, sourceHash)` and appends
only the remaining deterministic rows; their contents are not compared. An
existing target config is accepted only when its byte hash is the prepared
config hash; an unreadable target, symlink, or different hash is
`migration-target-exists`.

If an earlier release wrote a target before its completion marker, a later
conversion can reject it as `migration-target-exists` because the prepared
bytes differ; move the target to another name and run the conversion again. If
the completion marker and inputs match, conversion still returns `unchanged`
without rewriting the earlier target.

Heuristic-only impacted documents count toward `impact.maxImpactedDocs`, and a
run stops with `impact-limit` when the limit is exceeded, unlike the earlier
harness, which discarded excess heuristic matches and continued. A configuration
migrated by an earlier release can lack these legacy heuristic defaults; because
matching legacy inputs return `unchanged` and do not rewrite an existing file,
reconcile `impact.heuristics` manually: if absent, set it to this object; if
present, retain its keys and values, add from this object only the keys it does not already have, and
append to `excludeBasenames`, in this object's order, each name not already in
the existing list case-insensitively:

```json
{"minIdentifierLength":5,"excludeBasenames":["readme.md","index.md","changelog.md","license","license.md","__init__.py","makefile","main.md","test.md","skill","skill.md"],"saturationWarnRatio":0.5,"excludeDocPathTokens":false}
```

Full conversion repeats all input reads, hashes, and validation while holding
the state mutex. Any change from preflight is `migration-input-changed`. It
then rejects an active `running` or `awaiting-external-backend` run, appends
remaining `legacy` rows, publishes the config exclusively if absent, and
finally appends the completion marker. The legacy files remain byte-for-byte
unchanged.
Rejections before the mutex write nothing; after mutex acquisition the state
root and lock file can remain. A completed-match check precedes the active-run
check, so an `unchanged` conversion is write-free even while a run is open.

`--dry-run` performs only read-only preflight and completion checks. Unless
the status is `unchanged`, it checks the target config: identical prepared
bytes are convertible; a different or unreadable target is
`migration-target-exists`. It creates no
state root, takes no mutex, and writes nothing. It reports `convertible`,
`not-convertible`, or `unchanged` with `config`, `counts`, `inputs`,
`anchor: {"migrated":false,"reason":"policy"}`, and `runOpen`. An active run
sets `runOpen` true but does not reject dry-run. Anchors are never migrated, so
all profiles begin cold. Imported `legacy` events are audit evidence only and
cannot participate in flip identity matching.

## State files

The state root is `.claude/state/docaudit/`.  C-RUN owns `mutex`, `lease`,
`lease.json`, `run-open.json`, and `runs/<runId>/journal.jsonl`. C-HISTORY owns
`anchors/<profile>.json` and `history.jsonl`. An anchor records the accepted run's
ID and time, contract version, HEAD (or `null`), snapshot, sorted documents,
and snapshot digest. Anchors written from 1.5.0 also record the accepted run's
`resolvedBackendModel` and `engineVersion`; earlier anchors lack these fields.
Full and incremental runs read the profile anchor for the report baseline;
a full run with an unreadable anchor continues without a baseline, while
later recording follows the existing rules. A history line has `seq`, `ts`,
`kind`, `runId`, and object `data`. Known kinds are `outcome`, `judgement`,
`flip`, `anchor`, `legacy`, and `migration`; unknown kinds are retained and
ignored.

An outcome event has optional `acceptBaseline: true` only when `--accept-baseline`
was applied to a full `NEEDS_FIX` run whose blocking items are all `L-DOC`
judgements and whose report was published. The anchor file and its `anchor`
history event have optional `acceptedBaseline: true` only when that acceptance
wrote the anchor.

The `migration` kind uses `runId: "migration"`; its single `completed` event is
the one-time conversion marker described above. It is documentation-level
enumeration, not a reader whitelist.

Candidate anchor files and accepted anchor files have a 64 MiB read/write limit.

History is append-only and read one line at a time. A malformed line, envelope,
or sequence makes it `history-corrupt` and prevents reads and writes. Recovery
is outside the engine: preserve valid lines through the final valid line under a
different name, then begin a new history file.

## Report templates and publication

`report.path` ends in `.md` and contains exactly one `<YYYY-MM-DD>` and at most one
`[_NN]` across the full path. If the date is in the basename, the text
before it must be nonempty; if the date is in a directory, the entire basename
must be nonempty. Invalid values are `config-invalid:report.path`. The date
comes from the run ID; collisions receive `_02`, `_03`, and so on through
`_1000`. Publication never replaces an existing file and returns a receipt
with relative path, SHA-256, and publication time. If every report name is
occupied, the engine returns status 4 with `path-exhausted` and that run's
`runId`, leaving the run open. Move an older report to free a name, then
resume that run.

Reports have fixed front matter fields `title`, `description`, `category`, plus
`created`, `updated`, `version`, and `docaudit`, with sections Run, 対象,
所見, 判定, Anchor, 計測, and 証跡. When `auditReportsInCorpus` is true a
report may become a self target next run. Engine-fixed front matter does not
automatically satisfy `documentChecks.frontMatterFields`. The Run section
places baseline immediately after `- anchor:`. Full and
incremental runs derive it from the profile anchor. Without one it prints
`- baseline: なし`; with one, `- baseline: <runId>（判定役 <M>、engine <V>）`.
Older anchors use `runs/<runId>/manifest.json` for missing values; unreadable
values are `不明` and do not count as differences. Only changed known values add
`- 基準との違い: <parts>。内容が同じ文書でも判定が変わることがあります。`
For example: `- baseline: 20261014T105421Z-c82d9b3f（判定役 codex:gpt-6-luna、engine 1.3.1）`
`- 基準との違い: 判定役 codex:gpt-6-luna → codex:gpt-6.1-sol、engine 1.3.1 → 1.5.0。内容が同じ文書でも判定が変わることがあります。`
Runs sealed by engines before 1.5.0 have no baseline key and omit these lines
when resumed.

Before publication, each finding-line summary is redacted token by token: a token
containing a forbidden outside-repository path fragment becomes `<path>`, and an
email-address match becomes `<email>`. Placeholders are emitted in code spans so
they remain visible in Markdown. When replacements occur, the finding
section adds exactly one `- redacted: N` line. Engine-managed fields, including
headers, paths, reasons, and refused checks, are not redacted; an unsafe value
there still fails the whole report closed. A repository-relative path that itself
matches this safety rule (for example an email-shaped filename) therefore still
fails closed; rename that file to publish a report.

Non-null document judgements are recorded in history independently of a report
receipt only when the gate is not REFUSED, the ledger verifies, their identity is
bound to the sealed scope (impacted path, snapshot content hash, verdict and
summary shape), and their path passes report safety. `judgementsSkipped` on the
outcome records the number not recorded and is absent when zero. Their summaries
and evidence use the same redaction as reports; the evidence ledger and agent
judgement files remain original. Judgements from undecided runs participate in
flip and focused-profile regression selection, while REFUSED runs record none.

## Rejection reasons

Configuration may report `config-missing`, `config-needs-migration`, or
`config-invalid:<detail>`.  File operations may report `absolute-path`,
`parent-segment`, `symlink-component`, `not-directory`, `outside-repo`,
`not-regular`, `too-large`, `not-utf8`, `nul-in-path`, or
`io-unsupported-platform`.  Runs may report `run-in-progress`,
`run-awaiting-external`, `run-interrupted-resume-required`,
`resume-in-progress`, `run-closed`, `journal-corrupt`, `run-not-found`, or
`mutex-timeout`.

Migration may report `migration-source-missing`,
`migration-config-invalid:<key|json|size>`,
`migration-history-invalid:<index|json>`, `migration-last-run-invalid`,
`migration-line-too-large`, `migration-input-changed`,
`migration-target-exists`, or `migration-run-open`. Normal config validation
continues to report `config-invalid:<detail>`, and malformed new history reports
`history-corrupt`.

## Journal envelope

Each valid journal line is a JSON object:

```json
{"seq":1,"ts":"2026-01-01T00:00:00Z","kind":"opened","data":{}}
```

`seq` starts at one and is consecutive; `ts` is a nonempty string (the engine
always writes ISO-8601); `kind` is a nonempty string; and `data`, when present,
is an object. Unknown kinds are retained and ignored by readers.

C-RUN writes these kinds directly for a run's lifecycle transitions (open,
resume, transition, close, and abandon): `opened`, `resumed`, `awaiting`,
`interrupted`, `abandoned`, and `closed`. See "Journal state machine" below
for the full catalogue of kinds the engine writes.

## Engine run directory

Each run is stored below `.claude/state/docaudit/runs/<runId>/`. The engine
owns these files:

| file | purpose |
|---|---|
| `journal.jsonl` | ordered state-machine events |
| `config.snapshot.json` | interpreted, sealed configuration facts |
| `scope.json` | corpus, snapshot, changes, and impacted documents |
| `plan.json` | selected profile row and ordered enabled layers |
| `capability.json` | the single capability probe result for the run |
| `manifest.json` | exclusively published immutable run contract |
| `evidence.jsonl` | append-only typed adapter evidence |
| `verdict.json` | exclusively published gate result |
| `anchor-candidate.json` | candidate state written after a report publishes regardless of verdict; an accepted `NEEDS_FIX` candidate includes `acceptedBaseline` |
| `report.rendered.md` | validated report bytes before publication |
| `metrics.json` | final duration and model-call measurements |
| `retrieval.json` | document-index locations and language for this run |
| `tree.before.json` | persisted pre-dispatch worktree snapshot for Workflow runs and for Codex/agy runs with nonempty `worktree.tolerateChangesIn` |

The capability probe is performed once before seal. Its contract version is
`capability/1.2` and its seventeen fields are `available`, `reason`, `cliVersion`,
`executableHash`, `homeOrigin`, `homePathHash`, `authPresent`, `authReadable`,
`probeContractVersion`, `workflowAvailable`, `workflowReason`, and the six agy
fields `agyAvailable`, `agyReason`, `agyCliVersion`, `agyExecutableHash`,
`agyHomeOrigin` (`env` when `AGY_HOME` is set, otherwise null), and
`agyModelAvailable`. The Codex fields describe Codex and the agy fields describe
agy; they are filled independently (see "agy document adapter"). The engine adds
two more keys to `capability.json` that are not probe fields: `backendSelection`,
an object `{backend, fast}` holding the run's `--backend` value (`agy`, `codex`,
`claude`, or null) and whether `--fast` was given, and `agyRepoBlock`, the reason
the audited repository makes agy unusable, or null. A `capability.json` whose
`probeContractVersion` is `capability/1.1`, as the published 1.1.5 writes it,
has none of the agy fields, `backendSelection`, or `agyRepoBlock`. The engine
does not read the contract version; it reads the missing keys as agy unavailable
with no selection. The home paths and authentication bytes are never recorded.
Resume reads `capability.json` when the journal records `capability-detected`;
otherwise it probes capability again. Gate reads `capability.json` without a
new probe. Workflow runs additionally own deterministic request, completion,
judgement, and receipt files below `requests/` in the same run directory.

## Journal state machine

The normal order is `opened`, `run-options`, `config-sealed`, `scoped`,
`planned`, `capability-detected`, `manifest-intent`, `sealed`, one or more
`layer-started`/`layer-done` pairs, `verdict-intent`, `gated`, `rendered`,
`write-begin`, `write-end`, `reported`, `recorded`, and `closed`.
`report-failed` replaces `reported` when publication fails.
Auxiliary kinds are `resumed`, `interrupted`, `tmp-recovered`, `awaiting`,
`request-issued`, `request-received`, `abandoned`, `evidence-truncated`,
`model-call-limit`, and `model-call-reserved`. The request events define
the generation state of a Workflow request but do not complete an engine layer.
A `layer-done` event records `layerId`, `evidenceSeq`, and the evidence SHA-256.
`rendered` records the report SHA-256; `write-begin` records the report path plus
the temporary file's device and inode; `reported` records the publication
receipt. `run-options` records `mode`, `requestedProfile`, `acceptBaseline`, and
`backendSelection` (`{backend, fast}`, as in `capability.json`); a resume that
has to probe again reads the selection from it.

A stage is complete only when its completion event has been appended. The
completion events are `config-sealed`, `scoped`, `planned`,
`capability-detected`, `sealed`, `layer-done`, `gated`, `rendered`, `reported`,
`report-failed`, `recorded`, and `closed`. Intent, start, recovery, interruption,
resume, and awaiting events do not complete a stage. Resume starts after the
last completion event and never invokes an adapter for an already completed
layer.

Before seal, each completed file event includes the file SHA-256. A file with
no completion event is removed and that stage is repeated. For exclusively
published manifest and verdict files, a matching most-recent intent completes
the missing event; a mismatch is `seal-drift` or `verdict-conflict`. An intent
without its file repeats the stage. A complete but mismatched evidence line is
`evidence-tampered`; an incomplete final line is ignored as
`evidence-truncated` and its layer is repeated.

A restored scope that names a path under a tool directory such as `.mdq/` (a run opened by 1.0.0) is a semantic mismatch.

Report publication records `rendered` before `write-begin`, then publishes,
records `write-end`, and finally records `reported`. If the final report already
matches the rendered SHA-256, resume reconstructs a receipt with
`recovered: true`. Different bytes are `report-conflict`.

## Manifest contract

`manifest.json` has exactly the run facts that gate must recheck:

`runId`, `contractVersion`, `engineVersion`, `startedAt`, `mode`, `profileName`,
`profileSelectionSource`, `profileTableHash`, `registryHash`, `enabledLayers`,
`planHash`, `backendDirective`, `resolvedBackendModel`,
`capabilityResultHash`, `anchorProfile`, `configHash`, `configSnapshotHash`,
`scopeHash`, `configSnapshotFileHash`, `scopeFileHash`, `planFileHash`,
`capabilityFileHash`, `changeSetHash`, `corpusDigest`, `reportPath`,
`allowedWritePaths`, `treeDigestBefore`, `maxModelCalls`, `retrieval`, and
`manifestHash`.

Optional `acceptBaseline` is a boolean; absent means false, and true is valid only with `mode: full`.

`accept-baseline-requires-full` rejects an acceptance request that is not a full run.

The four `*FileHash` values bind the exact bytes of config snapshot, scope,
plan, and capability files. Their corresponding semantic hashes bind canonical
interpreted values. `configHash` binds the exact project configuration bytes.
The engine appends `manifest-intent` with `manifestHash` before exclusive
publication.

## Write guard

All engine write helpers check an in-process guard and record every final path,
temporary-file operation, directory creation, and lock-file creation. An
unlisted write is rejected as `write-not-allowed`.

1. Before opening a run, only the state-root lock and ownership files, history,
   their deterministic temporary paths, any run journal, and the directories
   needed to reach a run directory may be created or changed.
2. After `runId` is known, the guard is bound to that run's fixed files listed
   above, the state-root files, their temporary paths, and the anchor directory.
3. After planning, the exact profile anchor, exact report path and temporary
   path, and only the report's required parent directories are added.
4. Seal freezes the final paths as repo-relative exact matches in
   `allowedWritePaths`. A sealed resume reconstructs the guard only from that
   list. No glob, parent traversal, directory suffix, or absolute path is valid.

The fixed run set includes `retrieval.json` and `tree.before.json`. For a
Workflow run, it also contains, for each request generation 1 through 3, the
exact request, completion, receipt, and per-document judgement paths. The
permitted directories are only `requests/`, each numbered generation
directory, and its `judgements/` directory. Every path is enumerated before
seal; a glob or an open-ended directory permission is never used. A write
outside the engine's fixed write paths is not permitted. Changes made by
other tools outside those paths cause `REFUSED worktree-modified` unless they
meet the listed worktree conditions above.

Atomic and exclusive writes use the deterministic sibling
`.tmp-<destination-basename>`. A writer removes that path only when it created
it successfully. State-root temporary files are recovered immediately after a
lease is acquired. The worktree digest excludes the frozen final and temporary
write paths, but records every other entry regardless of Git ignore rules,
including entries under `worktree.tolerateChangesIn`. Toleration affects the
gate's decision, not the snapshot. The fixed tool-directory exclusions are the
repository's top-level `.git/` and, since 1.0.1, `.mdq/` (mdq's index and usage
records); 1.0.0 excluded only `.git/`. The excluded tool directories are a fixed list in
the engine and may grow in later versions.

Within a listed location, a file that disappears during scanning is omitted
from that snapshot. A file that cannot be read or changes while being read is
recorded as an unstable entry and classified at the gate as a change, even if
both snapshots record it as unstable. Listed entries still count toward the
entry and byte limits for `worktree-too-large`.

Workflow runs atomically persist the canonical pre-dispatch tree snapshot in
`tree.before.json` with a 64 MiB limit. Codex and agy runs do the same when
`worktree.tolerateChangesIn` is nonempty; if that snapshot exceeds the limit,
the run ends before dispatch as `undecided worktree-too-large`. A later process may reconstruct the
worktree difference from this file only when its semantic hash equals the
sealed `treeDigestBefore`. A missing, invalid, oversized, or mismatched snapshot
is treated like an unavailable difference cache. Codex and agy runs with an
empty list do not write this snapshot. If such a run is resumed in another
process after the worktree changes, the gate refuses it with an empty
`worktreeDiff` and no ignored-path guidance; an in-process gate can still list
the paths and give guidance.

### Recovering `tmp-conflict`

The engine checks a leftover report temporary file only when the report is absent.
It removes the file only if an unmatched `write-begin` records its device
and inode; otherwise it reports `tmp-conflict` and leaves the file untouched.
The run is already closed as undecided
`report-publish-failed`, so resume is refused with `run-closed`. Stop audit
processes, inspect the sibling `.tmp-<destination-basename>` outside the engine,
and move it to a separate quarantine location. Confirm that the intended report
path has not been replaced, then start a new `audit`. Do not edit the run journal
or sealed files to force recovery.

## Evidence ledger and deterministic gate

Each evidence line contains `seq`, `ts`, `producerId`, `layerId`, `kind`,
`sha256`, and `data`. The maximum encoded line is 4 MiB; a larger adapter result
is replaced by a small incomplete result with reason `evidence-too-large`.
Unknown kinds are retained and hash-checked, but are excluded from layer-set and
judgement calculations. Of the records appended for one layer,
`adapter-result` is always last; `model-call` and other supporting records for
that layer precede its `adapter-result`.

Gate applies these checks in order:

1. loss of the process lease gives `REFUSED lock-lost`;
2. changed configuration bytes give `REFUSED config-drift`;
3. manifest, latest manifest intent, sealed-file bytes, or semantic plan
   mismatch gives `REFUSED seal-drift`;
4. an evidence record hash mismatch gives `REFUSED evidence-tampered` and has
   a report and `verdict.json`;
5. layer-set mismatch gives `REFUSED layer-missing` or `layer-unexpected`, and
   an adapter identity mismatch gives `REFUSED producer-mismatch`;
6. a resolved backend mismatch gives `REFUSED backend-mismatch`; the gate
   resolves the backend again from the sealed `capability.json`, which carries
   `backendSelection` and `agyRepoBlock`, and compares it with the manifest's
   `resolvedBackendModel`, so a selected backend resolves as it did at seal time;
7. worktree changes outside the listed, Git-ignored, unsealed paths give
   `REFUSED worktree-modified`; `worktreeDiff` lists only those paths. A
   digest mismatch without a recoverable path difference still refuses with
   an empty list. If the scan exceeds an entry or byte limit, the outcome is
   `undecided worktree-too-large`;
8. an out-of-scope, duplicate, stale-content, or invalid document judgement
   gives `REFUSED judgement-mismatch`;
9. an incomplete adapter gives outcome `undecided` with its reason, while a
   complete document adapter missing a judgement gives
   `REFUSED judgement-missing`;
10. a FAIL judgement or blocking FAIL finding gives `NEEDS_FIX`; otherwise the
    verdict is `CONSISTENT`. Findings from non-blocking layers do not affect the
    fold.

The verdict object is either
`{"verdict":"CONSISTENT|NEEDS_FIX|REFUSED","reason":"...","counts":{},"blocking":[],"refusedChecks":[],"gateHash":"..."}`
or
`{"outcome":"undecided","reason":"...","counts":{},"blocking":[],"refusedChecks":[],"gateHash":"..."}`.
`worktreeDiff` is included when applicable. When at least one path is tolerated,
`toleratedWorktreeChanges` lists those paths in sorted order, including in a
mixed refusal. A rule-7 `REFUSED worktree-modified` also has
`worktreeDiffIgnoredOnly: true` when every refused path is Git-ignored at the
gate and outside the sealed corpus and snapshot, even with no tolerance setting.
This flag is absent if Git classification fails or the path difference is
unavailable. `C-GATE` is the only verdict writer;
it appends `verdict-intent` before exclusively publishing `verdict.json`.

Pre-gate undecided reasons `backend-unavailable`, `anchor-missing`, `impact-limit`,
`snapshot-too-large`, `corpus-unreadable`, `git-failed`, and
`worktree-too-large` do not publish a report or verdict. Gate-time undecided
outcomes do publish a report. Report rendering shows the REFUSED reason and
`refusedChecks`, or the undecided reason, under `## 判定`. When paths were
tolerated, it adds `- 大目に見た作業木の変化: <count> 件` and, if any paths are safe to
display, `- 大目に見た path（先頭 <k> 件）: <path1>, ...` for up to the first five.
For `worktreeDiffIgnoredOnly: true`, it adds a guidance line with both
remedies: list the ignored location in `worktree.tolerateChangesIn` or move the
output outside the working tree.

The gate refusal reason catalogue is `lock-lost`, `config-drift`, `seal-drift`,
`evidence-tampered`, `layer-missing`, `layer-unexpected`,
`producer-mismatch`, `backend-mismatch`, `worktree-modified`,
`judgement-mismatch`, `judgement-missing`, `adversarial-blocking`,
`claim-inconsistent`, `claim-unknown`, `claim-duplicate`, `claim-missing`,
and `claim-unadjudicated`. A recovery-detected pre-gate
`seal-drift`, `verdict-conflict`, `request-drift`, or orphan evidence followed
by a complete record (`evidence-tampered`), including one found on resume, is
recorded in history as a REFUSED outcome and closed without publishing a report
or writing a new `verdict.json`. A post-gate `report-conflict` is recorded as undecided
with `report-failed {reason: "report-conflict"}` and does not publish or
overwrite a report. A `tmp-conflict` is recorded as undecided
`report-publish-failed` with `tmp-conflict` as its detail. Unsafe engine-managed rendering values or other failed exclusive publication are also recorded as undecided `report-publish-failed`.

## CLI result and exit status

The final stdout line is one JSON object. A closed or aborted operation uses:

```json
{"nextAction":"done|abort","runId":"...","outcome":"...","reason":"...","reportPath":"..."}
```

`reason` and `reportPath` are omitted when not applicable. Exit status 0 means the engine finished normally:
the run reached `closed`, it handed the run to the Workflow backend while the
run remains open, or it asked for a backend choice without opening a run (see
below). Status 3 means refusal before opening a run or before starting a resume;
its `nextAction` is `abort`, `runId` and `outcome` are null, and `reason`
identifies the refusal. Argument refusals include `backend-invalid` for a
`--backend` value other than `agy`, `codex`, or `claude`, `fast-requires-codex`
for `--fast` without `--backend codex`, and `accept-baseline-requires-full`
for `--accept-baseline` without `--full`. An unsupported platform can return
`io-unsupported-platform`. Resume refusals before the transition to running
include `run-not-found`, `run-closed`, `resume-in-progress`, and `mutex-timeout`;
a timeout while opening a run also returns 3 with a null `runId`. For
engine-handled interruptions after a run opens or a resume starts, status 4
means the run cannot proceed or record an outcome, including a later
`mutex-timeout` or report-name exhaustion (`path-exhausted`). The `abort`
result carries that run's `runId`, and the run stays open for resume or abandon;
it does not reach `closed`. Operating-system errors and rejected writes can
also interrupt an opened run. Recovery-detected integrity failures are
recorded in history and closed with status 0. Resume also reconciles a
recorded outcome and any missing anchor step before closing it.

When resume sees a recorded `NEEDS_FIX` outcome with a report receipt, it
revalidates the manifest and compares the acceptance derived from the manifest,
`verdict.json`, and receipt with outcome `acceptBaseline`. A mismatch, or an
unreadable or invalid manifest or `verdict.json`, stops with `seal-drift` and
does not change the anchor. `REFUSED`, undecided, and `CONSISTENT` outcomes
close as before.

For `migrate --dry-run`, status 0 means `convertible` or `unchanged`, and status
1 means `not-convertible`; both use `nextAction: "done"`. For full `migrate`,
status 0 means `migrated` or `unchanged`. `migration-source-missing` and
`migration-run-open` use status 3; every other migration rejection uses status
4. Full migration rejections use `nextAction: "abort"`. Only migrate results
may additionally expose `config`, `counts`, `inputs`, `anchor`, and `runOpen`.
These migration statuses are separate from the run lifecycle meanings above.

A Workflow handoff exits successfully while the run remains open and uses:

```json
{"nextAction":"invoke-workflow","runId":"...","requestSeq":1,"requestPath":".claude/state/docaudit/runs/.../requests/request-1.json"}
```

The run-open state is `awaiting-external-backend`, and `awaiting` records the
request sequence. Entering that state releases the process lease, so no engine
process remains attached after the handoff. Resume acquires the lease,
transitions through `running`, and either continues the layer or returns to the
awaiting state.

With `--ask-backend`, no `--backend`, and at least two usable backends, the
engine opens no run, exits with status 0, and prints the backend question:

```json
{"nextAction":"choose-backend","runId":null,"outcome":null,"choices":[{"backend":"codex","fast":false},{"backend":"codex","fast":true},{"backend":"agy","fast":false},{"backend":"claude","fast":false}]}
```

`choices` lists only the usable backends, in the order Codex, Codex with
Fast, agy, Claude Code agents (`claude`). A backend is usable under the same
conditions as in the resolution described next, including `agyRepoBlock` for
agy. Without `--backend`
and `--ask-backend`, or with only one usable backend, the engine takes the
first usable backend in the order Codex (`codex:gpt-6.1-sol`), agy
(`agy:gemini-3.8-flash-medium`), Claude Code agents
(`workflow:claude-sonnet-5-5`). With
`--backend`, only the named backend is considered, and one that is not usable
gives `undecided backend-unavailable`.

## Codex document adapter

For `codex:<model>`, L-DOC checks impacted documents in path order with at most
three attempts per document and four concurrent calls. Constants are a 2 MiB
output limit, 900 second document timeout, five second termination grace, and
`medium` reasoning effort. The default model is `gpt-6.1-sol`; a Codex account
that cannot use it fails every attempt, so the layer is incomplete with
`backend-failed` and there is no automatic fallback to another backend. Each
call uses `codex exec -s read-only --ephemeral --ignore-user-config
--ignore-rules -C <repo-root> -m <model> -c model_reasoning_effort=medium
[-c service_tier=priority] --output-schema <schema-file> -o <output-file> -`;
`-c service_tier=priority` is present only when the run's
`backendSelection.fast` is true, for every Codex call of the run. The prompt is supplied from a private file;
it names the relative target, provenance, mode, up to 100 changed paths, and an
identity containing only run ID and relative path. It instructs the model to
judge the document content against the current repository state, citing repository-relative evidence only; a resource outside the repository is outside the audit scope, not missing. Before reporting a repository path as missing, the model searches recursively, including hidden directories, and reports a path found elsewhere as a wrong path. A copy found only inside an ignored nested checkout (a directory with its own .git file or directory, such as a worktree under .worktrees/) is not treated as the file existing elsewhere or as the wrong-path location. The prompt contains neither the content hash, document bytes, nor private absolute paths.

The output object has exactly `runId`, `path`, `verdict`, `rationale`, and
`evidence`. Verdict is `PASS`, `WARN`, or `FAIL`; evidence is a string array.
File kind, size, JSON, keys, field types, and identity are checked in that order.
An exhausted document remains as a judgement with null verdict and summary,
the sealed content hash and backend model, and `failure {reason, attempts}`.
Such a row is still identity-checked, makes an incomplete adapter undecided,
does not participate in gate folding, and is not copied to judgement or flip
history. A successful row may additionally carry its string-array `evidence`.

Every attempt appends one `model-call` evidence record before the layer's final `adapter-result`.
It contains `callSeq`, relative `path`, `attempt`, `model`, `exit`, `timedOut`,
`durationMs`, `outputBytes`, `valid`, and `reason`; for a Codex call `model` is
`codex:<model>`. Failed Codex and agy calls (`reason` not null) may add
`stderrTail` from the CLI error output; successful calls, missing or empty
output, and an empty processed tail omit the key. The last 2048 bytes are read.
If output is longer, bytes through the first newline are dropped; without a
newline, bytes through the first space or tab are dropped; without either,
the key is omitted. Invalid UTF-8 is replaced, ANSI CSI sequences are removed,
and controls other than newline and tab become spaces. A whitespace-separated
token containing a forbidden outside-repository path fragment becomes
`<path>`, an email address becomes `<email>`, and an ASCII run of 32 or more
letters, digits, `_+/=-` becomes `<secret>`. After trimming, at most the last
2048 characters are
retained. Codex records always set `confirmed: true`, including calls that
failed to launch with reason `launch-failed`.
`metrics.modelCalls` counts these records: `total` and `attempts` are the actual
record count, with counts by layer and backend model plus impacted count and
outcome. `confirmedTotal` counts records whose `confirmed` value is true; a
legacy Codex record with no `confirmed` field is also counted as confirmed.
Complete model-call records survive recovery and retries. Only a final orphan
`adapter-result` is removed; an orphan followed by another complete record is
recovery-time `evidence-tampered`. Codex failures never switch a sealed run to
Workflow.

## agy document adapter

For `agy:<model>`, where the model is `gemini-3.8-flash-medium`, L-DOC checks
impacted documents in path order with at most three attempts per document and
two concurrent calls. Each call uses `agy --input-format text --output-format
json --print-timeout 15m --json-schema <schema-file> --model
gemini-3.8-flash-medium --sandbox`, with the prompt on standard input and the
repository root as the working directory. The engine stops waiting for a call
after 960 seconds. The child environment holds only `PATH`, `LANG`, `LC_ALL`,
`TMPDIR`, `HOME` (set to the value of `AGY_HOME`), and
`AGY_CLI_DISABLE_AUTO_UPDATE=true`. `--dangerously-skip-permissions` and `--mode`
are never passed.

agy is usable only when the capability probe finds it usable and the repository
has no block. The probe checks, in this order, and records the first reason that
applies in `agyReason`: `agy-home-unset` (no `AGY_HOME`), `agy-home-missing`
(`AGY_HOME` is not an existing directory), `agy-not-installed` (no executable
`agy` on `PATH`), then, in `<AGY_HOME>/.gemini/antigravity-cli/settings.json`,
`agy-settings-unreadable` (unreadable, not a JSON object, or a
`permissions` value that is neither `null` nor an object),
`agy-permissions-present` (a non-empty
`permissions.allow`), and `agy-settings-hooks-present` (a `hooks` key); a
missing `settings.json` is accepted. Next come `agy-tool-hook-missing` (no
`<AGY_HOME>/.gemini/config/hooks.json`) and `agy-tool-hook-mismatch` (the file is
unreadable, is not JSON, or differs as a JSON value from the tool-guard document
below; key order and whitespace are ignored), then `agy-login-missing` (no
readable `<AGY_HOME>/.gemini/antigravity-cli/antigravity-oauth-token`). Only then
is agy started: `agy-tmp-unavailable` (no private temporary directory),
`agy-version-failed` (`agy --version` failed or exceeded 20 seconds),
`agy-models-timeout` and `agy-models-failed` (`agy models` exceeded 30 seconds, or failed or was unreadable),
and `agy-model-missing` (the model is absent as a whitespace-separated word from the first 1 MiB of `agy models` output).
The repository check is separate and is recorded in `agyRepoBlock`:
`agy-repo-settings-present` when the repository has a `.gemini/antigravity-cli`
entry of any kind, otherwise `agy-workspace-customizations` when it has a
top-level `.agents` entry. Plugins installed in the agy home are not inspected.

The tool-guard document is exactly:

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

Its matcher accepts every tool name except `find_by_name`, `finish`,
`grep_search`, `list_dir`, and `view_file`, so tools that agy adds later are
denied too. A tool denied by the hook does not run, is not reported in the JSON
output, and is not a failure.

The prompt is the Codex judge prompt with three sentence-level replacements,
because agy refuses every shell command and a refusal fails the whole judgement:
the instruction to use read-only commands becomes an instruction to run no
shell or terminal command and to use only file-reading, directory-listing and
file-search tools; the `find` example becomes a reference to those tools; and the
`git ls-files` and `git check-ignore` steps become a statement that git tracking
cannot be checked, with the nested-checkout rule decided by the presence of a
`.git` file or directory alone. Each replaced sentence must occur exactly once in
the prompt, and after the replacements no `find `, `git ls-files`,
`git check-ignore`, or imperative `run ` may remain; otherwise the attempt fails
with `agy-prompt-invalid` and agy is not started.

The output is one JSON object with `status`, `denied_actions`, and
`structured_output`. The attempt fails with the first reason that applies:
`agy-home-unset` or `agy-home-missing` (checked again when the call starts),
`agy-prompt-invalid`, `launch-failed`, `cancelled`, `timeout` (960 seconds),
`exit-<n>`, `output-too-large` (over 2 MiB), `output-not-json` (not a JSON
object), `agy-status` (`status` is not `SUCCESS`), `agy-denied` (a non-empty
`denied_actions`, even with an answer), `structured-output-missing` (no object
in `structured_output`; the 15 minute print timeout ends here), and then the
judgement schema checks `output-type-mismatch`, `output-schema-mismatch`, and
`identity-mismatch`. The output object, identity, failure rows, `model-call`
records, and layer reasons are those of the Codex document adapter, with these
differences: the judgement's `backendModel` and each record's `model` are
`agy:gemini-3.8-flash-medium`, and an exhausted document's `failure` is
`{"reason": <last attempt's reason>, "attempts": <attempts>}`. When every
document fails with `tmp-unavailable` the layer reason is `tmp-unavailable`;
otherwise a failed document gives `backend-failed`.

## Workflow document adapter

Except for `retrieval.indexDb` and `retrieval.indexCwd`, which are absolute for
`index` and null for `grep`, every path passed to an external agent, stored in
a request, or stored in a receipt is repository-relative. The `requestPath`
handed to the Workflow, and the request's `judgementPath` and `donePath`,
include the state-root prefix; document `path`, `changed` entries, and the
receipt's `accepted[].path` do not. With no impacted documents the adapter
writes neither request nor receipt. In this section, `R` means
`.claude/state/docaudit/runs/<runId>`. A document ID is the first 16
hexadecimal characters of the SHA-256 of its repository-relative path.

The engine writes `R/requests/request-<k>.json` before waiting. It has exactly
these fields:

```json
{"runId":"...","requestSeq":1,"attempt":1,"model":"...","documents":[{"docId":"...","path":"docs/example.md","provenance":["mapped"],"contentHash":"...","judgementPath":".claude/state/docaudit/runs/<runId>/requests/1/judgements/<docId>.json"}],"retrieval":{"method":"index|grep","indexDb":null,"indexCwd":null,"indexLang":null,"fallback":"grep"},"donePath":".claude/state/docaudit/runs/<runId>/requests/request-1.done","changed":[],"mode":"full|incremental","createdAt":"..."}
```

`indexDb` and `indexCwd` are absolute paths when method is `index`; otherwise
they are null. `indexLang` is `ja-jp` for an index and null for grep. `changed`
contains at most 100 repository-relative paths. After writing the request
file, the engine appends `model-call-reserved` to the journal, then appends
`request-issued` with `requestSeq` and the file SHA-256. If the reservation
exceeds the limit, it appends `model-call-limit` and stops: the request file
remains, but there is no `request-issued` entry.

The Workflow script itself does not read or write files. It receives only
`runId` and the repository-relative `requestPath`, and first checks that the
path begins with `R/requests/`. A reader agent reads the request and returns it
under a schema; a mismatched run ID or empty document array stops the Workflow.
One verifier agent per document writes its assigned judgement and returns the
same structured value. After the parallel verifiers finish, a closer agent
writes the completion record from their non-null returns.

Each verifier writes its assigned judgement path. A judgement is at most
1 MiB and has exactly the required keys `runId`, `requestSeq`, `attempt`,
`docId`, `path`, `verdict`, `rationale`, and `evidence`; the only optional key is
`retrievalUsed`. `verdict` is `PASS`, `WARN`, or `FAIL`, `evidence` is a string
array, and `retrievalUsed`, when present, is `index` or `grep`. Validation order
is fixed: regular-file kind and size; UTF-8 JSON object; exact required and
optional key set; field types; then equality of run ID, generation, attempt,
document ID, and path with the assigned request row. A document ID outside the
request is rejected as `not-requested`; an earlier generation or attempt is
rejected as `stale-request`. A valid judgement becomes a result containing
`path`, `verdict`, `summary` from `rationale`, `contentHash`, the sealed
`workflow:<model>` backend, and `evidence`.

After all verifiers return, the closer writes
`R/requests/request-<k>.done`:

```json
{"runId":"...","requestSeq":1,"attempt":1,"documents":["<docId>"],"invocations":{"reader":1,"verifiers":1,"closer":1}}
```

The run ID, generation, attempt, and document IDs must match the request, and
each invocation value must be a nonnegative integer. Invocation values are not
cross-checked against engine counts. An invalid completion record is
`done-invalid`: every requested document is treated as missing and no claimed
invocation values are recorded.

After validation, the engine atomically writes
`R/requests/request-<k>.receipt.json`:

```json
{"runId":"...","requestSeq":1,"accepted":[{"docId":"...","path":"docs/example.md","sha256":"..."}],"rejected":[{"docId":"...","reason":"..."}],"missing":["<docId>"],"invocations":{"reader":1,"verifiers":1,"closer":1}}
```

It then appends `request-received` with the request sequence, receipt SHA-256,
accepted IDs, and missing IDs. Repeating recovery produces byte-identical
receipt content.

The current generation is the greatest `requestSeq` in a `request-issued`
event, not the greatest file found on disk. Resume follows these rules:

1. An issued generation with neither `request-received` nor a completion file
   remains waiting with reason `external-not-finished`.
2. When the completion file exists, model-call records are reconciled first,
   followed by the receipt and `request-received`.
3. A received generation with no missing documents completes the layer.
4. If documents are missing and the generation is below 3, the next request is
   issued once for exactly the union of missing and rejected documents.
5. Missing documents at generation 3 make the adapter incomplete with reason
   `external-incomplete`. Each unresolved result records failure reason
   `external-missing`, `external-invalid`, or `done-invalid` and `attempts: 3`.

The SHA-256 of every issued request is checked on resume. A mismatch is
`REFUSED request-drift`, recorded in history without publishing a report.
Receipt and ledger recovery are idempotent: an existing model-call identity
`(requestSeq, role, docId)` is never appended twice, so interruption before or
after a receipt cannot roll a generation backward or duplicate calls.

For each generation, the engine records one `reader`, one `verifier` per
requested document, and one `closer` model-call. Every record has
`requestSeq`, `role`, `docId` (null for reader and closer),
`model: workflow:<model>`, `exit: null`, and `durationMs: 0`. A verifier is
`confirmed: true` only when its judgement exists and passes validation;
missing or rejected judgements are unconfirmed and include a reason. `valid`
records the validation result. Reader and closer are always unconfirmed because
the engine cannot observe their actual calls. External invocation claims are
copied only to the receipt and do not affect model-call totals. A two-document
generation therefore records four calls, two confirmed and two unconfirmed.

When the sealed impacted set is empty, the engine creates no request and no
Workflow invocation. L-DOC completes with no judgements and observation
`externalSkipped: no-impacted-documents`; the run has zero model calls and
closes normally.

## Workflow document retrieval

Codex runs seal retrieval method `backend-native`. Before sealing a Workflow
run, the engine finds `mdq` directly from `PATH` under the same executable-file
rules as backend capability detection. If it is absent, retrieval is `grep`
with reason `mdq-not-installed`.

When available, the engine chooses the first writable temporary parent outside
the repository, creates a new mode-0700 directory named
`docaudit-index-<runId>`, and never reuses an existing directory or symbolic
link. It copies only the sealed corpus into a repository-relative mirror.
Source bytes are read through validated file descriptors, mirror files are
created exclusively, and hard links are forbidden. A document changed into a
symbolic link before copying makes the run undecided with reason
`corpus-unreadable` rather than exposing the link target.

With the mirror as its working directory, indexing runs `mdq index --root .
--db ../index.sqlite --lang ja-jp`. Every `mdq` child receives only
`PATH`, `HOME`, `LANG`, `LC_ALL`, and `TMPDIR` when present, plus
`PYTHONUTF8=1`; `mdq index` has a 120-second limit; `mdq stats`, `mdq list`,
and `mdq search` each have a 20-second limit, with process-group cleanup. Health
requires `mdq stats --db <index database> --lang ja-jp` to report the sealed
corpus file count and at least one chunk, and the first word of a heading from
`mdq list --db <index database> --lang ja-jp` to produce at least one `mdq
search --db <index database> --q <word> --mode grep --top-k 1 --lang ja-jp`
result, where `<index database>` is the absolute
`docaudit-index-<runId>/index.sqlite` path (not the mirror-relative
`../index.sqlite` literal that `index` passes).
Index or health failure is fail-open: retrieval becomes `grep` with a specific
reason. The engine's indexing never writes `.mdq/` into the repository.
If an external mdq invocation writes to the repository's top-level `.mdq/`, the gate ignores it.

The manifest stores `retrieval` with `method`, `indexAvailable`,
`indexHealthy`, `reason`, `files`, and `chunks`. Metrics store retrieval method
and health. `retrieval.json` stores `indexDb`, `indexCwd`, and `indexLang`, and
the request copies those values into its retrieval object. All verifier index
commands first change to `indexCwd`. If the database or mirror disappears while
a run is waiting, a verifier falls back to targeted Grep and Read and reports
`retrievalUsed: grep`; any next-generation request is issued with method
`grep`.

Each index directory contains an ownership marker `owner.json` with
`{"repo": sha256(repo root realpath), "runId": "..."}`. After a run is opened,
stale `docaudit-index-*` directories other than the current run are removed on
a best-effort basis only when this marker identifies the current repository.
Directories without a readable marker or owned by another repository are left
untouched. The current directory is also removed, after the same ownership
check, when the run closes, is abandoned, or exits without being able to record
an outcome.

## Workflow launcher

The launcher first runs `python3 -B "$CLAUDE_SKILL_DIR/engine" audit
--ask-backend $ARGUMENTS` and reads only its final JSON line. For
`choose-backend` it asks, with the question tool, which of `choices` to use
(the first is the recommendation) and runs `audit --backend <backend>
$ARGUMENTS`, adding `--fast` when the answer's `fast` is true. If the question
is closed without an answer it uses the backend of the first choice; if the
question tool is unavailable or refused it runs `audit $ARGUMENTS`. While
`nextAction` is `invoke-workflow`, it
invokes `Workflow({name: "docaudit:docaudit-verify", args: {runId,
requestPath}})` and then runs `python3 -B "$CLAUDE_SKILL_DIR/engine" resume
<runId>`. It repeats that pair at most three times. If the limit is exceeded it
runs `resume <runId> --abandon`. For `done` or `abort`, it displays the outcome,
reason, and report path that are present and stops.

## Private temporary storage

Before creating storage, the engine considers `TMPDIR`, `/tmp`, and `/var/tmp`
in order. It resolves each candidate, checks that it is a writable searchable
directory without probing by file creation, and selects the first one outside
the repository and state root. It does not use the process-wide cached default
temporary directory. If none qualifies, the adapter returns
`tmp-unavailable` without starting a child process. Directories are mode 0700;
prompt, schema, and output placeholders use exclusive creation. The
entire directory is removed when its layer returns or raises, so private raw
model output never enters the evidence ledger or tree digest.

## Project checks

L-PROJECT always provides four read-only built-ins over the sealed corpus:

- `front-matter` requires configured simple `key: value` fields in an opening
  `---` block. The first matching override replaces the field list. Missing
  fields are non-blocking WARN findings.
- `links` checks inline and reference Markdown links. Missing or escaping local
  targets are blocking FAIL findings.
- `existence` checks only path-like backtick tokens outside code fences. A
  token is trimmed and cut at its first `#` or `?`; if what remains contains
  a shorthand marker (`*`, `{`, `}`, `...`, `…`) the token is skipped
  silently. The engine then considers up to two bases: the whole remaining
  token and, when it contains a colon, the part before the first colon (an
  optional line locator such as `:12`). A base counts only when it looks
  like a repository path: it must contain a slash, not start with `//`,
  contain no space, tab, `|`, `<` or `>`, have no parent segment, and begin,
  optionally after one leading `/`, with an existing top-level directory.
  Each counted base is tried as written and, when it is percent-encoded and
  decoding introduces no control character or parent segment, also in
  decoded form. A token none of whose bases counts is ignored; a token is a
  non-blocking WARN only when at least one base counted and none of the
  tried forms names an existing repository-relative target.
- `orphan` issues a non-blocking WARN when a corpus document has no direct
  one-hop link from another corpus document or an index file. Documents listed
  in `documentChecks.indexFiles` are exempt. One-document corpora record
  `orphan: skipped`.

`documentChecks.layerGlobs` accepts only those four IDs and removes matching
documents from that check. Links inside code fences or inline code are ignored,
and an optional Markdown link title is not part of the target. Link targets may
be angle-bracketed. Fragment and
query parts are discarded; empty, `http:`, `https:`, `mailto:`, and `tel:`
targets are ignored. Relative paths use the containing document directory.
Repository escapes fail `links` and are ignored by `orphan`. Existing files and
directories satisfy a link, unless a component of the path is a symbolic link or
the resolved path lies outside the repository root; such a link fails `links` as
`broken relative link`.

Configured `projectChecks` run only on Darwin through
`/usr/bin/sandbox-exec`. The engine first runs one sandboxed `/usr/bin/true`
preflight with `sandbox-exec -p <profile-text> /usr/bin/true`; configured
checks reuse the same sealed `-p <profile-text>` value. No profile file is
created in private temporary storage. A missing sandbox or failed preflight returns incomplete
`sandbox-unavailable` and starts no configured command. The profile is
`(version 1)(allow default)(deny file-write*)(allow file-write* (subpath
"<private-temp>"))(allow file-write* (literal "/dev/null"))`. A check receives
only selected process variables plus `TMPDIR`, `DOCAUDIT_TMP`, and
`DOCAUDIT_REPO_ROOT`; its working directory is the repository or its configured
relative directory.

Check stdout is at most 1 MiB and must be exactly an object with `findings`.
Each finding requires string `id`, `summary`, and severity `INFO`, `WARN`, or
`FAIL`, with an optional validated repository-relative `path`. IDs are prefixed
with the configured check ID and FAIL is blocking. Nonzero exit becomes
`check-failed`, timeout becomes `check-timeout`, and invalid output becomes
`check-invalid`; these are blocking FAIL findings while the adapter remains
complete. Only `sandbox-unavailable` and `tmp-unavailable` make it incomplete.

## Optional layers

The `extended` profile selects four opt-in layers. Unselected adapters are not
started. Every Codex prompt begins with the machine-readable line
`docaudit-role: <role>`, where the role is `judge`, `adversarial`, `security`,
or `claim`. Model output is JSON-only, is limited to 2 MiB, and each model
assessment uses at most three calls in total, with another call after a
launch, exit, timeout, shape, identity, path, evidence, or report-safety
validation failure. A title, finding `file`, rationale, or
claim `evidenceFile` containing a local home/private path token or an
email-shaped token is invalid. Each call is limited to 900 seconds.

`L-ADVERSARIAL` makes one Codex assessment per impacted document, with up to four
documents in flight. Its prompt identifies the repository-relative document,
provenance, and changed-path summary and asks for evidence-backed
contradictions with code, configuration, other documents, section references,
and procedure prerequisites. The exact schema is an object with one
`findings` array; each element has only `severity`, `title`, and `file`.
Severity is one of `critical`, `high`, `medium`, or `low`; `file` must name an
existing regular repository-relative file. Findings are normalized as follows:

- ID: `adv:` plus the first 16 hexadecimal characters of SHA-256 over
  `file + "|" + severity + "|" + normalized-title`. Normalization trims,
  compresses whitespace, and lowercases.
- `critical` and `high` become `FAIL`, `medium` becomes `WARN`, and `low`
  becomes `INFO`.
- `blocking` is always false. Duplicate IDs collapse to one finding and the
  removed count is recorded as `deduplicated`.

`L-SECURITY` makes one Codex assessment for the whole run when at least one document
is impacted. Its prompt lists impacted documents and asks for evidence-backed
problems in documented procedures, configuration, secret handling, and
permissions. It uses the same schema, validation, severity map, and duplicate
rule as the adversarial layer, with an `sec:` ID prefix. Security findings also
always have `blocking: false`; an empty finding array is complete.

`L-CLAIM` reads the first `L-ADVERSARIAL` adapter result in the ledger and
checks every finding whose normalized severity is `FAIL`, one at a time. A
missing source result is incomplete with `claim-input-missing`. Its output has
exactly `findingId`, `state`, `evidenceFile`, `evidenceLine`, and `rationale`.
`state` is `confirmed`, `rejected`, or `unverified`. The first two require an
existing regular repository-relative evidence file of at most 2 MiB and a
one-based line number within that file. `unverified` is retried, up to
three attempts in all; if the answer of the last attempt is `unverified`, the
claim is a non-blocking `WARN`, whatever the earlier attempts returned. A launch,
exit, timeout, or invalid response after all attempts makes the layer
incomplete with `backend-failed`.

A confirmed claim becomes `claim:<findingId>`, `FAIL`, and blocking. A rejected
claim is `INFO` and non-blocking. An unverified claim is `WARN` and
non-blocking. The old state name `refuted` corresponds to `rejected` in 1.0.0.
There is no fourth `not-adjudicable` state. Gate rule 9b derives the required
claim IDs from adversarial `FAIL` severity, requires a one-to-one match, rejects
unknown or duplicate claims, and verifies state-to-blocking consistency. A
missing claim is `claim-missing`; an unverified claim is
`claim-unadjudicated`; inconsistent data is `claim-inconsistent`. Thus only a
confirmed claim can contribute an optional blocking finding.

These three layers run on Codex with the default Codex model whichever backend
the run sealed for documents, and they follow the run's Fast selection like
every other Codex call. They need `available` in the sealed `capability.json`
to be true. When it is not, `L-SECURITY`, `L-ADVERSARIAL`, and `L-CLAIM` are
incomplete with `codex-unavailable`, or with `workflow-adapter-unavailable`
when the sealed backend begins with `workflow:`. If there are no impacted
documents, security and adversarial instead complete without a call. Standard
and focused profile behavior is unchanged.

## Enrichment and sibling phrases

`L-ENRICH` is always complete, non-blocking, and makes no model calls. For an
incremental run it obtains one Git diff through the scope component, using the
anchor HEAD and non-deleted changed paths with rename, external-diff,
text-conversion, quoting, and color effects disabled; source and destination
prefixes are fixed to `a/` and `b/`. A rejected or failed Git operation, Git
timeout, OS error, or UTF-8 decode failure is recorded in `notes` and
contributes no change-set phrases. A full run has no change-set phrase source.
Corpus paths that fail report-safety validation are likewise noted and skipped.

Deleted diff lines contribute quoted phrases and semantic versions matching
`v?N.N.N`. A phrase also present in the same path's whitespace-normalized added
lines is removed. `L-DOC` judgements contribute quoted phrases from `summary`
only when their verdict is `FAIL` or `WARN`. Finding phrases precede change-set
phrases for case-sensitive duplicate removal. Adversarial finding titles are
not an enrichment source because `L-ENRICH` precedes `L-ADVERSARIAL` in
registry order.

Backtick phrases are 2 through 80 characters and contain only Unicode letters
or digits, spaces, or `._/@:#<>=()-`. Double-quoted phrases are normally 6
through 200 characters with at least two Unicode alphanumeric words. The
alternative for a phrase containing a non-ASCII alphanumeric character is 4
through 200 characters with at least two alphanumeric characters. After
surrounding whitespace is stripped, empty phrases, phrases containing a
character in U+0000–U+001F or U+007F, phrases without an alphanumeric
character, report-unsafe phrases, and case-folded exact matches of the
following fixed
stoplist are removed: `mapped`, `heuristic`, `both`, `full`, `self`, `skill`,
`graphify`, `semantic`, `pass`, `warn`, `fail`, `consistent`, `needs_fix`,
`needs fix`, `true`, `false`, `null`, `none`.

At most the first 200 unique safe phrases are retained. Before that limit,
unsafe phrases are discarded without increasing `phraseTruncated`; after 200
are retained, each further unique phrase increments `phraseTruncated` before
its safety is checked. Retained phrases are sorted lexically before the
corpus is scanned. Reports are excluded. Matching is case-sensitive and
line-based, with one-based line numbers and at most 20 findings per phrase.
Further matches are counted in `truncated[phrase]` and `truncatedTotal`.
Observations report adopted counts in `sources.findings` and
`sources.changeSet`. The layer also uses the capability component's PATH search
to record availability and SHA-256 for `ax`, `codegraph`, `ccc`, and
`graphify`; it never starts them and never expands the sealed scope.

## Model-call reservation and stop state

Before every Codex or agy attempt, after cancellation is checked and before a child
process is started, the layer atomically reserves one call. A Workflow request
generation atomically reserves `2 + document-count` immediately before it is
issued: one reader, one verifier per requested document, and one closer. An
external wait does not reserve again. A later generation containing only
missing documents makes a new reservation.

Every successful reservation appends `model-call-reserved` with `layerId`,
`count`, and cumulative `used`. Reservations are never released. `used` is the
sum of those durable events across the run, including failed attempts and work
interrupted before a model-call record. A null `maxModelCalls` disables only
the upper-bound check; reservation events are still written.

If `used + requested` exceeds a non-null limit, the engine appends exactly one
durable `model-call-limit` event containing `layerId`, `requested`, `used`, and
`limit`, then starts no new call. Concurrent later attempts and resumed
processes observe that event and do not append another. The current layer is
recorded incomplete with `model-call-limit`; every remaining enabled layer is
recorded incomplete and skipped without starting its adapter. Resume first
reconciles the ledger, then completes any missing stopped-layer results without
starting adapters. The deterministic gate therefore produces `undecided
model-call-limit`, records history, and never advances the anchor.

`metrics.modelCalls` retains `total`, `attempts`, `byLayer`,
`byBackendModel`, and `confirmedTotal`, and adds `limit`, `reserved`, and
`rejected`. `reserved` is the sum of durable reservation counts even when the
limit is null; `rejected` is zero or one. The invariant is
`total <= reserved`. Equality holds for an uninterrupted Codex run and for
received Workflow generations. It may be strict after interruption between
reservation and model-call recording, or when a Workflow generation was
issued but never received. The final duration is recorded in
`metrics.json`, but the engine performs no duration calibration.
