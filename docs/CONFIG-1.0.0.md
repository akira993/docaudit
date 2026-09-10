# docaudit 1.0.0 configuration

Configuration is the UTF-8 JSON file `.claude/docaudit.json`.  It contains
`"docauditSchema": "1.0"`; key order is not significant. Unknown keys are rejected at every level. Paths
and glob values are repository-relative and must not contain `..` or begin with
an absolute path.

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
| `impact.heuristics` | `{minIdentifierLength: int, excludeBasenames: string[], saturationWarnRatio: number, excludeDocPathTokens: boolean}` | no | omitted |
| `impact.ssotSources` | `{source: path, docs: path[]}` array | no | `[]` |
| `report.path` | string template | yes | — |
| `documentChecks.frontMatterFields` | string array | no | `[]` |
| `documentChecks.frontMatterOverrides` | `{glob: glob, fields: string[]}` array | no | `[]` |
| `documentChecks.indexFiles` | path array | no | `[]` |
| `documentChecks.layerGlobs` | `{check ID: glob[]}` object | no | `{}` |
| `projectChecks` | `{id: string, argv: string[], timeoutSec: int >= 1, cwd?: path}` array | no | `[]` |

`enabledLayers` is the only declaration of project capability.  The availability
of external tools is discovered by the engine rather than configured here.

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
| `heuristics` | migrate | `impact.heuristics` | project fact |
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
`.claude/doc-audit.json`, optional `.claude/state/docaudit-history.json`, and
optional `.claude/state/docaudit-last-run.json`. The config and last-run limits
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
| `heuristics` | The original value | — |

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

Full conversion repeats all input reads, hashes, and validation while holding
the state mutex. Any change from preflight is `migration-input-changed`. It
then rejects an active `running` or `awaiting-external-backend` run, appends
remaining `legacy` rows, publishes the config exclusively if absent, and
finally appends the completion marker. The legacy files remain byte-for-byte
unchanged.
Rejections before the mutex write nothing; after mutex acquisition the state
root and lock file can remain. A completed-match check precedes the active-run
check, so an `unchanged` conversion is write-free even while a run is open.

`--dry-run` performs only read-only preflight and completion checks. It also
checks the target config: identical prepared bytes are convertible, while a
different or unreadable target is `migration-target-exists`. It creates no
state root, takes no mutex, and writes nothing. It reports `convertible`,
`not-convertible`, or `unchanged` with `config`, `counts`, `inputs`,
`anchor: {"migrated":false,"reason":"policy"}`, and `runOpen`. An active run
sets `runOpen` true but does not reject dry-run. Anchors are never migrated, so
all profiles begin cold. Imported `legacy` events are audit evidence only and
cannot participate in flip identity matching.

## State files

The state root is `.claude/state/docaudit/`.  C-RUN owns `mutex`, `lease`,
`lease.json`, `run-open.json`, and `runs/<runId>/journal.jsonl`. C-HISTORY owns
`anchors/<profile>.json` and `history.jsonl`. An anchor records the accepted run
ID and time, contract version, HEAD (or `null`), snapshot, sorted documents,
and snapshot digest. A history line has `seq`, `ts`, `kind`, `runId`, and object
`data`. Known kinds are `outcome`, `judgement`, `flip`, `anchor`, `legacy`, and
`migration`;
unknown kinds are retained and ignored.

The `migration` kind uses `runId: "migration"`; its single `completed` event is
the one-time conversion marker described above. It is documentation-level
enumeration, not a reader whitelist.

Candidate anchor files and accepted anchor files have a 64 MiB read/write limit.

History is append-only and read one line at a time. A malformed line, envelope,
or sequence makes it `history-corrupt` and prevents reads and writes. Recovery
is outside the engine: preserve valid lines through the final valid line under a
different name, then begin a new history file.

## Report templates and publication

`report.path` ends in `.md`, contains exactly one `<YYYY-MM-DD>`, at most one
`[_NN]`, and has a nonempty basename prefix before the date. Invalid values are
`config-invalid:report.path`. The date comes from the run ID; collisions receive
`_02`, `_03`, and so on. Publication never replaces an existing file and returns
a receipt with relative path, SHA-256, and publication time.

Reports have fixed front matter fields `title`, `description`, `category`,
`created`, `updated`, `version`, plus `docaudit`, and sections Run, 対象,
所見, 判定, Anchor, 計測, and 証跡. When `auditReportsInCorpus` is true a
report may become a self target next run. Engine-fixed front matter does not
automatically satisfy `documentChecks.frontMatterFields`.

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
`migration-config-invalid:<key|json>`,
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

`seq` starts at one and is consecutive; `ts` is an ISO-8601 string; `kind` is
a nonempty string; and `data`, when present, is an object. Unknown kinds are
retained and ignored by readers.

The engine writes these kinds: `opened`, `resumed`, `awaiting`, `interrupted`,
`abandoned`, and `closed`.

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
| `anchor-candidate.json` | candidate state used only after an eligible outcome |
| `report.rendered.md` | validated report bytes before publication |
| `metrics.json` | final duration and model-call measurements |
| `retrieval.json` | document-index locations and language for this run |
| `tree.before.json` | persisted pre-dispatch worktree snapshot for Workflow runs |

The capability probe is performed once before seal. Its contract version is
`capability/1.1` and its eleven fields are `available`, `reason`, `cliVersion`,
`executableHash`, `homeOrigin`, `homePathHash`, `authPresent`, `authReadable`,
`probeContractVersion`, `workflowAvailable`, and `workflowReason`. The home
path and authentication bytes are never recorded. Resume and gate read
`capability.json`; they do not probe again. Workflow runs additionally own
deterministic request, completion, judgement, and receipt files below
`requests/` in the same run directory.

## Journal state machine

The normal order is `opened`, `config-sealed`, `scoped`, `planned`,
`capability-detected`, `manifest-intent`, `sealed`, one or more
`layer-started`/`layer-done` pairs, `verdict-intent`, `gated`, `rendered`,
`write-begin`, `write-end`, `reported`, `recorded`, and `closed`.
`report-failed` replaces `reported` when publication fails.
Auxiliary kinds are `resumed`, `interrupted`, `tmp-recovered`, `awaiting`,
`request-issued`, `request-received`, and `abandoned`. The request events define
the generation state of a Workflow request but do not complete an engine layer.
A `layer-done` event records `layerId`, `evidenceSeq`, and the evidence SHA-256.
`rendered` records the report SHA-256; `write-begin` records the report path plus
the temporary file's device and inode; `reported` records the publication
receipt.

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
anywhere else in the repository makes the run `REFUSED worktree-modified`.

Atomic and exclusive writes use the deterministic sibling
`.tmp-<destination-basename>`. A writer removes that path only when it created
it successfully. State-root temporary files are recovered immediately after a
lease is acquired. The worktree digest excludes the frozen final and temporary
write paths, but includes every other entry regardless of Git ignore rules;
only the repository's top-level `.git/` and `.mdq/` (mdq's index and usage
records) are excluded. Since 1.0.1 the top-level `.mdq/` is excluded as well;
1.0.0 excluded only `.git/`. The excluded tool directories are a fixed list in
the engine and may grow in later versions.

Workflow runs atomically persist the canonical pre-dispatch tree snapshot in
`tree.before.json` with a 64 MiB limit. A later process may reconstruct the
worktree difference from this file only when its semantic hash equals the
sealed `treeDigestBefore`. A missing, invalid, oversized, or mismatched snapshot
is treated like an unavailable difference cache. Codex runs do not write this
snapshot.

### Recovering `tmp-conflict`

The engine does not remove a report temporary file unless an unmatched
`write-begin` proves the same device and inode. Any other leftover is
`tmp-conflict` and is left untouched. The run is already closed as undecided
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
6. a resolved backend mismatch gives `REFUSED backend-mismatch`;
7. a worktree digest mismatch gives `REFUSED worktree-modified` and lists the
   differing paths;
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
`worktreeDiff` is included when applicable. `C-GATE` is the only verdict writer;
it appends `verdict-intent` before exclusively publishing `verdict.json`.

Pre-gate undecided reasons `backend-unavailable`, `impact-limit`,
`snapshot-too-large`, `corpus-unreadable`, `git-failed`, and
`worktree-too-large` do not publish a report or verdict. Gate-time undecided
outcomes do publish a report. Report rendering shows the REFUSED reason and
`refusedChecks`, or the undecided reason, under `## 判定`.

The gate refusal reason catalogue is `lock-lost`, `config-drift`, `seal-drift`,
`evidence-tampered`, `layer-missing`, `layer-unexpected`,
`producer-mismatch`, `backend-mismatch`, `worktree-modified`,
`judgement-mismatch`, and `judgement-missing`. A recovery-detected pre-gate
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

`reason` and `reportPath` are omitted when not applicable. Exit status 0 means
the engine finished normally: the run reached `closed`, or it handed the run
to the Workflow backend while the run remains open (see below). Status 3 means refusal before opening a run; its
`nextAction` is `abort`, `runId` and `outcome` are null, and `reason` identifies
the refusal. Status 4 is reserved for a run that was opened but cannot proceed
or record an outcome, such as an operating-system error or rejected write; it
does not reach `closed` and prints an `abort` result. Recovery-detected
integrity failures are recorded in history and closed with status 0. Resume
also reconciles a recorded outcome and any missing anchor step before closing
it.

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

## Codex document adapter

For `codex:<model>`, L-DOC checks impacted documents in path order with at most
three attempts per document and four concurrent calls. Constants are a 2 MiB
output limit, 600 second document timeout, five second termination grace, and
`medium` reasoning effort. The default model is `gpt-5.6-terra`. Each call uses
`codex exec -s read-only --ephemeral --ignore-user-config --ignore-rules -C
<repo-root> -m <model> -c model_reasoning_effort=medium --output-schema
<schema-file> -o <output-file> -`. The prompt is supplied from a private file;
it names the relative target, provenance, mode, up to 100 changed paths, and an
identity containing only run ID and relative path. It instructs the model to
judge the document content against the current repository state, citing repository-relative evidence only; a resource outside the repository is outside the audit scope, not missing. The prompt contains neither the content hash, document bytes, nor private absolute paths.

The output object has exactly `runId`, `path`, `verdict`, `rationale`, and
`evidence`. Verdict is `PASS`, `WARN`, or `FAIL`; evidence is a string array.
File kind, size, JSON, keys, field types, and identity are checked in that order.
An exhausted document remains as a judgement with null verdict and summary,
the sealed content hash and backend model, and `failure {reason, attempts}`.
Such a row is still identity-checked, makes an incomplete adapter undecided,
does not participate in gate folding, and is not copied to judgement or flip
history. A successful row may additionally carry its string-array `evidence`.

Every attempt appends one `model-call` evidence record before the layer's final
`adapter-result`. It contains `callSeq`, relative `path`, `attempt`, `model`,
`exit`, `timedOut`, `durationMs`, `outputBytes`, `valid`, and `reason`.
Codex records explicitly set `confirmed: true` because the engine observes the
process completion.
`metrics.modelCalls` counts these records: `total` and `attempts` are the actual
record count, with counts by layer and backend model plus impacted count and
outcome. `confirmedTotal` counts records whose `confirmed` value is true; a
legacy Codex record with no `confirmed` field is also counted as confirmed.
Complete model-call records survive recovery and retries. Only a final orphan
`adapter-result` is removed; an orphan followed by another complete record is
recovery-time `evidence-tampered`. Codex failures never switch a sealed run to
Workflow.

## Workflow document adapter

All paths passed to an external agent, stored in a request, or stored in a
receipt are repository-relative and include the state-root prefix. In this
section, `R` means `.claude/state/docaudit/runs/<runId>`. A document ID is the
first 16 hexadecimal characters of the SHA-256 of its repository-relative
path.

The engine writes `R/requests/request-<k>.json` before waiting. It has exactly
these fields:

```json
{"runId":"...","requestSeq":1,"attempt":1,"model":"...","documents":[{"docId":"...","path":"docs/example.md","provenance":["mapped"],"contentHash":"...","judgementPath":".claude/state/docaudit/runs/<runId>/requests/1/judgements/<docId>.json"}],"retrieval":{"method":"index|grep","indexDb":null,"indexCwd":null,"indexLang":null,"fallback":"grep"},"donePath":".claude/state/docaudit/runs/<runId>/requests/request-1.done","changed":[],"mode":"full|incremental","createdAt":"..."}
```

`indexDb` and `indexCwd` are absolute paths when method is `index`; otherwise
they are null. `indexLang` is `ja-jp` for an index and null for grep. `changed`
contains at most 100 repository-relative paths. Immediately after writing the
request, the engine appends `request-issued` with `requestSeq` and the file
SHA-256.

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
`PYTHONUTF8=1`; it has a 120-second limit and process-group cleanup. Health
requires `mdq stats --lang ja-jp` to report the sealed corpus file count and at
least one chunk, and the first word of a heading from `mdq list --lang ja-jp`
to produce at least one `mdq search --mode grep --top-k 1 --lang ja-jp` result.
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

The launcher first runs `python3 "$CLAUDE_SKILL_DIR/engine" audit $ARGUMENTS`
and reads only its final JSON line. While `nextAction` is `invoke-workflow`, it
invokes `Workflow({name: "docaudit:docaudit-verify", args: {runId,
requestPath}})` and then runs `python3 "$CLAUDE_SKILL_DIR/engine" resume
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
  token is trimmed, fragment/query and optional line locator are removed, and
  percent encoding is decoded only when it introduces no control or parent
  segment. Tokens must contain a slash, contain no whitespace or shell
  separator, have no parent segment or shorthand marker, and begin with an
  existing top-level directory. A missing repository-relative target is a
  non-blocking WARN.
- `orphan` issues a non-blocking WARN when a corpus document has no direct
  one-hop link from another corpus document or an index file. One-document
  corpora record `orphan: skipped`.

`documentChecks.layerGlobs` accepts only those four IDs and removes matching
documents from that check. Links inside code fences or inline code are ignored,
and an optional Markdown link title is not part of the target. Link targets may
be angle-bracketed. Fragment and
query parts are discarded; empty, `http:`, `https:`, `mailto:`, and `tel:`
targets are ignored. Relative paths use the containing document directory.
Repository escapes fail `links` and are ignored by `orphan`. Existing files and
directories satisfy a link.

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
or `claim`. Model output is JSON-only, is limited to 2 MiB, and is retried at
most three times after launch, exit, timeout, shape, identity, path, evidence,
or report-safety validation failure. A title, finding `file`, rationale, or
claim `evidenceFile` containing a local home/private path token or an
email-shaped token is invalid.

`L-ADVERSARIAL` makes one Codex call per impacted document, with up to four
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

`L-SECURITY` makes one Codex call for the whole run when at least one document
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
one-based line number within that file. `unverified` is retried; three
successive `unverified` answers produce a non-blocking `WARN` claim. A launch,
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

When the sealed backend begins with `workflow:`, `L-SECURITY`,
`L-ADVERSARIAL`, and `L-CLAIM` are incomplete with
`workflow-adapter-unavailable`. If there are no impacted documents, security
and adversarial instead complete without a call. Standard and focused profile
behavior is unchanged.

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
through 200 characters with at least two alphanumeric characters. Empty
phrases, control characters, phrases without an alphanumeric character,
report-unsafe phrases, and case-folded exact matches of the following fixed
stoplist are removed: `mapped`, `heuristic`, `both`, `full`, `self`, `skill`,
`graphify`, `semantic`, `pass`, `warn`, `fail`, `consistent`, `needs_fix`,
`needs fix`, `true`, `false`, `null`, `none`.

At most the first 200 unique safe phrases are retained; excess unique phrases
increment `phraseTruncated`. Retained phrases are sorted lexically before the
corpus is scanned. Reports are excluded. Matching is case-sensitive and
line-based, with one-based line numbers and at most 20 findings per phrase.
Further matches are counted in `truncated[phrase]` and `truncatedTotal`.
Observations report adopted counts in `sources.findings` and
`sources.changeSet`. The layer also uses the capability component's PATH search
to record availability and SHA-256 for `ax`, `codegraph`, `ccc`, and
`graphify`; it never starts them and never expands the sealed scope.

## Model-call reservation and stop state

Before every Codex attempt, after cancellation is checked and before a child
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
issued but never received. Duration calibration excludes Workflow runs and
all runs where `total != reserved`.
