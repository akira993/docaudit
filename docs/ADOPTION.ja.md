# プロジェクトへの docaudit 導入ガイド

English: [ADOPTION.md](ADOPTION.md)

このガイドは、リポジトリを「監査なし」から「変更のたびに文書との整合を確認する」状態まで導きます。docaudit 1.1.0 が [README](../README.md) の手順で install 済みであることを前提とし、設定の仕様は [CONFIG-1.0.0.md](CONFIG-1.0.0.md)（英語）、コピーして使えるプロンプトは [PROMPTS.ja.md](PROMPTS.ja.md) にあります。skill の指示 file（`SKILL.md`）は日本語です。

このガイドのコマンドは skills-dir install の engine path `~/.claude/skills/docaudit/skills/audit/engine` を使います。marketplace 経由で install した場合は、README の install 節にある engine path に読み替えてください。

## 1. 5 分で始める

1. plugin を一度だけ install し（README の「Install」）、新しい Claude Code session を開く。
2. リポジトリに `.claude/docaudit.json` を作り（第 4 節）、commit する。
3. `/docaudit:audit --full` を実行する。公開された report を読み、指摘された文書を直す。
4. run が `CONSISTENT` で終わるまで `/docaudit:audit --full` を繰り返す。その run が最初の anchor を書く。`NEEDS_FIX` が文書判定だけで止まる場合は `/docaudit:audit --full --accept-baseline` で baseline を受理し、以後は incremental にできる。切れたローカルリンク、失敗した project check、その他の文書判定以外の blocking は受理されないため、report の所見を先に直す。
5. 以後は変更のたびに `/docaudit:audit` を実行する。変更の影響を受ける文書だけが検証される。

1.0.1 へ更新する前に、open な run を `resume <runId> --abandon` で閉じてください。閉じずに 1.0.1 で再開した run は、旧 scope に `.mdq/` 配下の path が含まれていれば `REFUSED seal-drift`、`.mdq/` が旧い作業木スナップショットだけに含まれていれば `REFUSED worktree-modified` になります。いずれも再実行で回復します。

## 2. 考え方

docaudit は Markdown 文書を、それが説明しているコードや設定と突き合わせ、合わなくなった文書を報告します。文書を編集することはありません。すべての run は報告専用で、リポジトリ内に書くのは report と `.claude/state/docaudit/` 配下の run 状態だけです。

次の 5 つの考え方で、残りはすべて説明できます。

- **corpus（対象文書）。** `corpus.docGlobs` が監査対象の文書を選びます。git が無視する file は、`corpus.respectGitignore` を false にしない限り対象外です。
- **changes（変更）。** `changes.diffGlobs` が、変更されたら監査を起動する file を選びます。incremental な run は、同じ profile の最後に受理された run と作業木を比べます。
- **impact map（影響対応表）。** `impact.map` が、変更されたソースとそれを説明する文書を結び付けます。変更された file・対応表・唯一情報源の項目・（設定した場合は）file 名ヒューリスティクスが影響を受ける文書の集合を決め、`impact.maxImpactedDocs` がその上限になります。
- **層と profile。** run は層（layer）を実行します。`L-SCOPE` は影響集合の計算、`L-DOC` は影響を受けた各文書の検証、`L-PROJECT` は組み込みの文書チェックと `projectChecks` の実行、`L-ENRICH`・`L-SECURITY`・`L-ADVERSARIAL`・`L-CLAIM` は追加のレビュー層です。profile（`focused`・`standard`・`extended`）がどの層を走らせるかを選び、設定の `enabledLayers` がリポジトリとして許可する層を宣言します。
- **verdict と anchor。** 完了した run は `CONSISTENT` か `NEEDS_FIX` で終わります。`CONSISTENT` の run はその profile の anchor を前進させ、次の incremental な run はその anchor からの変更を測ります。verdict に到達できない run は理由付きの `undecided`、封印された run の契約が gate の整合性検査に失敗した run は `REFUSED` で終わります。

文書の検証は backend が行います。Codex CLI が engine の利用可能性検査に合格していればそれを使い、そうでなければ Claude Code の中では skill が workflow を通じて Claude Code の agent に検証を渡し、終わったら engine を再開します。

## 3. 前提条件

| 要件 | 補足 |
|---|---|
| macOS または Linux | `projectChecks` は macOS でのみ動きます（`sandbox-exec` を使うため）。 |
| Claude Code と git | skill は Claude Code の中で動き、git が差分とスナップショットを提供します。 |
| Python 3.12 以上 | engine は標準ライブラリだけを使います。 |
| Codex CLI（任意） | `codex` が `PATH` にあり、`codex --version` と `codex exec --help` が動き、Codex home の `auth.json` が読めるときに使われます。`extended` profile で verdict を得るには事実上必須です（第 8 節）。 |
| Node.js（任意） | リポジトリ自身のテストを走らせるときだけ必要です。 |

リポジトリ側に必要なのは設定 file だけです。tree 内のどこにある Markdown 文書でも監査でき、report の置き場所と state ディレクトリは最初の run で作られます。

## 4. 設定を書く

`.claude/docaudit.json` を作ります。README の最小例から始め、次の 4 点を調整します。

1. `corpus.docGlobs`: 監査する文書。生成物や取り込んだ文書は `corpus.excludeDocGlobs` で除外します。
2. `changes.diffGlobs`: 変更が監査を起動する file。通常はソース・設定・文書のディレクトリをまとめて指定します。
3. `impact.map`: ソース領域ごとに最低 1 項目（第 5 節）。
4. `report.path`: basename が空でない接頭辞に続けて `<YYYY-MM-DD>` をちょうど 1 つ持ち、任意で `[_NN]` が続く Markdown の path。例: `reports/audit_<YYYY-MM-DD>[_NN].md`。ディレクトリは最初の report 公開時に作られます。report は上書きされず、同じ日の 2 回目は `_02` が付きます。

`extended` profile を使う予定がなければ `enabledLayers` は `["L-SCOPE", "L-DOC", "L-PROJECT"]` のままにし（第 8 節）、`documentChecks.frontMatterFields` と `documentChecks.indexFiles` は文書がその規約に従うようになるまで空にしておきます。未知のキーは拒否されるので、綴り間違いは無視されずに `config-invalid:<detail>` として検証に失敗します。

設定は commit してください。設定はリポジトリの契約の一部で、engine はそのバイト列を毎回の run に封印します。run が開いている間に file を変えると、その run は `REFUSED config-drift` になります。

## 5. 良い impact map を作る

`impact.map` は `{"source": <glob>, "docs": [<path>, ...]}` の並びです。変更された file が `source` に一致すると、列挙された文書すべてが影響対象になります。役に立つ対応表にする規則は 2 つです。

- **ディレクトリではなく責務で対応付ける。** デプロイ手順を説明する文書は、置き場所を問わず、デプロイ script とそれが引用する設定 file から対応付けます。
- **広い 1 項目より、小さい複数項目。** すべての文書を列挙した `src/**` 1 項目だけだと、どんな変更でも corpus 全体を監査することになり、run は `impact.maxImpactedDocs` に達して `undecided impact-limit` で終わります。

`impact.ssotSources` は、ある値の唯一の情報源になる file（版 file、port 一覧など）について対応表と同じように働きます。情報源の変更は、それを引用する文書に常に影響します。

`impact.heuristics` は任意で、キーが無ければ動きません。設定すると `L-SCOPE` は変更された各 file の名前を拡張子あり・なしの 2 形で取り出し、文書本文がそのどれかを素の部分文字列として含むときにその文書を影響対象にします。`minIdentifierLength` は短い名前を落とし、`excludeBasenames` は `index.ts` や `README` のようにどこにでも一致する名前を落とし、`excludeDocPathTokens` は変更された文書自身の名前を無視し、`saturationWarnRatio` はこの照合だけで影響対象になった文書が corpus 全体に占める割合がその比率に達したときに警告します。まずはヒューリスティクスなしで始め、対応表だけでは変更 file の名前を挙げている文書を取りこぼすときに追加してください。

`impact.maxImpactedDocs` は、1 回の incremental な run で検証してよい最大の文書数にします。full の run はこの上限を受けません。

## 6. 旧設定を移行する

リポジトリに legacy な `.claude/doc-audit.json` が残っている場合、`migrate` サブコマンドが変換します。

```sh
python3 ~/.claude/skills/docaudit/skills/audit/engine migrate --dry-run --repo-root .   # 変換内容を表示。終了値 1 = 変換不可
python3 ~/.claude/skills/docaudit/skills/audit/engine migrate --repo-root .             # .claude/docaudit.json を書く
```

dry run は `convertible`・`not-convertible`・`unchanged` のいずれかの結果と、変換後の設定、`counts`（legacy 履歴の件数・last-run 記録の有無・破棄されたキーの一覧）、読んだ入力とそのハッシュ、`anchor`（移行されない）、`runOpen` を表示します。プロジェクトの事実（文書 glob・差分 glob・impact map・report path・front matter と index の設定・ヒューリスティクス・唯一情報源の項目）は 1.0.0 のキーに対応付けられます。旧 install の詳細・コマンド対応・任意ツールを記述していたキーは破棄されます。1.0.0 はツールの利用可能性を設定ではなく検出で決めるためです。対応の全表は CONFIG-1.0.0.md の「Old-key disposition」にあります。

変換後の設定は常に `L-SCOPE`・`L-DOC`・`L-PROJECT` を有効にし、`projectChecks` は空です。legacy な履歴 file があれば、その項目は新しい `history.jsonl` に `legacy` 行として写されます。legacy な anchor は移行されないので、各 profile の最初の run は `--full` でなければなりません。移行が完了すると履歴に完了印が残ります。同じ legacy 入力で `migrate` をもう一度実行すると、run が開いていても `unchanged` を報告して何も書きません。legacy 入力が変わっていれば `migration-input-changed` で拒否されます。それ以外の本番の migrate は run が開いている間は拒否され（`migration-run-open`）、変換結果とバイト列が異なる既存の `.claude/docaudit.json` を置き換えることも拒否します（`migration-target-exists`）。

新しい file を確認して commit し、不要になったら legacy file を取り除いてください。engine が読むのは `.claude/docaudit.json` だけで、legacy file しかないリポジトリは `config-needs-migration` で終わります。

## 7. 監査を実行する

Claude Code の中で、リポジトリにて:

```
/docaudit:audit --full                # corpus 全体。profile に anchor ができるまで必須
/docaudit:audit                       # incremental: anchor 以降の変更の影響を受けた文書だけ
/docaudit:audit --profile focused     # profile の選択。既定は standard
```

skill は engine を実行し、engine が検証を Claude Code の agent に渡したとき（`nextAction: invoke-workflow`）は検証 workflow を起動して engine を再開します。これを最大 3 回繰り返し、超えたら run を放棄します。最後に outcome・理由・report の path を表示します。

Claude Code の外では同じ run をコマンドとして実行します。agent が使えないため Codex backend が必要です。

```sh
python3 ~/.claude/skills/docaudit/skills/audit/engine audit --full --profile standard --repo-root /path/to/repo
```

### anchor のライフサイクル

- anchor は profile ごとに `.claude/state/docaudit/anchors/` に保持されます。
- profile の最初の anchor は、その profile の full の run が `CONSISTENT` で終わったときに書かれます。それまで、その profile の incremental な run は `undecided anchor-missing` で終わるので、`--full` を使い続けてください。
- `--accept-baseline` 付き full の `NEEDS_FIX` も、blocking がすべて `L-DOC` judgement で report 公開に成功した場合は最初の anchor を書く。既知 FAIL 文書は、`impact.map`・`ssotSources`・heuristic により影響対象に入らず、`changes.regressionRecheck` も無効な場合にだけ受理後の再判定対象外となる。`changes.regressionRecheck: true` を使うと profile の最新 FAIL 文書を毎回再判定できるが、その件数は `impact.maxImpactedDocs` に数えられ、超えると run 全体が `impact-limit` で止まり、PASS になるまで集合は縮まない。文書判定以外の blocking は受理されない。`standard` では切れたリンクと project check、`extended` では加えて security／adversarial／claim の blocking 所見を直す。
- 以後、その profile の run が `CONSISTENT` で終わるたびに anchor が前進します。`NEEDS_FIX`・`undecided`・`REFUSED` の run は anchor を動かさないので、次の incremental な run は同じ変更に新しい変更を加えて再び測ります。
- profile を切り替えると新しいライフサイクルが始まります。`focused` の anchor は `standard` の run には使われません。

### run は同時に 1 つ

engine は mutex と lease を保持し、前の run が開いたままの間は新しい監査を拒否します。拒否の理由が状態を示し、状態ごとに対処が違います。

- `run-in-progress`: 別の engine process が今まさに監査を実行中です。終わるのを待ってください。lease を保持している間、再開は `resume-in-progress`、放棄は `run-in-progress` で拒否されます。
- `run-awaiting-external`: run が検証 workflow に渡されたまま再開されていません。`resume <runId>` で再開します。結果行が closed になったか再び引き渡し（`nextAction: invoke-workflow`）になったかを示すので、後者なら workflow を実行してもう一度再開します。
- `run-interrupted-resume-required`: engine process が run の途中で終了しました。`resume <runId>` で再開するか、`resume <runId> --abandon` で verdict なしに閉じます。

skill は前の session から残った run を回復しません。手作業か、PROMPTS.ja.md 第 8 節のプロンプトで行ってください。

## 8. profile と層

| profile | 層 | backend |
|---|---|---|
| `focused` | `L-SCOPE`・`L-DOC` | Codex が使えればそれ、なければ Claude Code の agent |
| `standard`（既定） | focused + `L-PROJECT` | 同上 |
| `extended` | standard + `L-ENRICH`・`L-SECURITY`・`L-ADVERSARIAL`・`L-CLAIM` | 選択は同じだが、verdict を得るには Codex が必要 |

`extended` は `enabledLayers` が 7 層すべてを列挙しているときだけ選べます。そうでなければ engine は `capability-missing:<layer>` で run を開くことを拒否します（終了値 3）。Claude Code の agent 経由では security・adversarial・claim の層が incomplete（`workflow-adapter-unavailable`）となり run は `undecided` で終わるので、`extended` は Codex CLI が利用可能性検査に合格する環境で使ってください。

`extended` では、adversarial 層が影響を受けた文書ごとに根拠付きの矛盾を求め、security 層が文書化された手順・設定・秘密情報の扱い・権限を run ごとに 1 回レビューし、claim 層が adversarial の `FAIL` をひとつずつリポジトリと照合します。この 4 層の所見のうち verdict を `NEEDS_FIX` にできるのは confirmed な claim だけで、adversarial と security の所見は情報提供です。文書の `FAIL` はどの profile でも verdict を `NEEDS_FIX` にし、project check の `FAIL` は `L-PROJECT` を実行する profile（`standard`・`extended`）で `NEEDS_FIX` にします。

## 9. プロジェクトチェック

`L-PROJECT` は監査対象の文書に対して読み取り専用のチェックを常に 4 つ実行します。必須の front matter 項目（WARN）、ローカルな Markdown リンク（リンク先がなければ blocking な FAIL）、何も指していない path 風のバッククォート token（WARN）、他の文書や index file からリンクされていない文書（WARN）です。`documentChecks.layerGlobs` は一致する文書をチェックから外します。たとえば生成された index を orphan チェックから除外できます。

`projectChecks` は自分のコマンドを追加します。各項目はチェック ID・`argv`・timeout・任意の作業ディレクトリを持ちます。コマンドは macOS 上で、private な一時ディレクトリ以外への書込みを禁じる `sandbox-exec` の profile の中で実行され、`findings` 配列を持つ JSON object を出力しなければなりません。各所見は `id`・`summary`・`severity`（`INFO`・`WARN`・`FAIL`）と任意の `path` を持ちます。`FAIL` の所見は blocking で、すべての文書が合格していても run は `NEEDS_FIX` で終わります。0 以外の終了値・timeout・不正な出力はそれ自体が blocking な `FAIL` 所見になります。Linux では `projectChecks` を空にしてください。空でないと run は `undecided sandbox-unavailable` で終わります。

## 10. 結果を読む

- **結果行。** engine 出力の最終行は 1 つの JSON object です。閉じた run では `nextAction`（`done` か `abort`）・`runId`・`outcome`、あれば `reason` と `reportPath`。workflow への引き渡しでは outcome の代わりに `nextAction: invoke-workflow` と `requestSeq`・`requestPath` が付きます。終了値 0 は engine が正常に終わったことを意味し、`undecided`・`REFUSED`・引き渡しを含みます。3 は run を開くことを拒否したこと（設定・profile・run 状態の問題。`reason` に名前が出ます）、4 は開いた run が続行できなかったことです。
- **report。** `report.path` に公開され、固定の front matter と、run・対象文書・所見・判定・anchor・計測・証跡の節を持ちます。見出しと定型句の大半は日本語です。所見ごとに 1 行が並びます。文書は verdict と食い違いの 1 行要約、それ以外の所見（project check・link・claim など）は severity と要約です。judgement の裏付けとなる証拠の文字列（Codex backend には rationale に `file:line` を引用するよう、Claude Code の agent にはリポジトリ相対の証拠を示すよう求めます）は、run の証拠台帳と履歴の `judgement` 行に、Claude Code の agent で検証した run ではさらに `runs/<runId>/requests/<seq>/judgements/` に保持されます。所見の安全でない要約と履歴の `judgement` 行は report と同じ規則で伏字にし、証拠台帳と `requests/<seq>/judgements/` は原文のままです。検証が始まる前に `undecided` で終わった run は report を公開しません。
- **state ディレクトリ。** `.claude/state/docaudit/history.jsonl` は run の outcome と judgement を 1 行ずつ記録し（ほかに `flip`・`anchor`・`legacy`・`migration` の行）、`anchors/<profile>.json` が現在の anchor、`runs/<runId>/` が封印された manifest・adapter 結果と judgement を含む証拠台帳・`verdict.json` を保持します。state ディレクトリを commit する（履歴を version 管理下で監査可能にする）か無視する（clone ごとのローカル状態にする）かは一度決めてください。engine はどちらでも動きます。

### outcome 早見表

| outcome | 意味 | 対処 |
|---|---|---|
| `CONSISTENT` | 影響を受けた文書がすべて一致し、blocking な所見もない | なし。anchor が前進した |
| `NEEDS_FIX` | 少なくとも 1 文書が失敗、または blocking な所見（切れたリンク・失敗した project check・confirmed な claim） | report に出たものを直して再実行 |
| `NEEDS_FIX`（`--full --accept-baseline` で受理） | FAIL が文書判定だけ | anchor は前進した。既知 FAIL は文書を直したときに再判定 |
| `undecided anchor-missing` | anchor のない incremental な run | `--full` で実行 |
| `undecided backend-unavailable` | 能力検出で使える backend がなかった（Codex がなく、Claude Code の中でもない） | Codex を install または修復するか、Claude Code の中で実行 |
| `undecided impact-limit` | 影響文書が `impact.maxImpactedDocs` を超えた | 対応表を絞るか上限を上げるか、`--full` で実行 |
| `undecided corpus-unreadable` | 検証用 mirror の準備中に corpus 内の文書を読めなかった（Claude Code の agent backend） | file を読めるようにする（権限、壊れた symlink）して再実行 |
| `undecided sandbox-unavailable` | `sandbox-exec` のない環境で `projectChecks` を設定 | Linux では `projectChecks` を空に |
| `undecided workflow-adapter-unavailable` | Claude Code の agent 経由の `extended` | Codex backend を使う |
| `undecided abandoned` | run が放棄された | 再実行 |
| `REFUSED worktree-modified` | run 中に作業木が変わった | run 中は file を編集・生成しない。再実行 |
| `REFUSED config-drift` | run 中に `.claude/docaudit.json` が変わった | 再実行 |
| その他の `REFUSED` | 封印された run の契約が整合性検査に失敗（lease・封印・証拠ハッシュ・層の集合・backend・judgement） | `runs/<runId>/` を確認して再実行 |

## 11. 困ったとき

- **毎回 `config-invalid:<detail>` になる。** detail が問題のキーを示します。path はリポジトリ相対で `..` を含まず、`report.path` は `.md` で終わり、`<YYYY-MM-DD>` をちょうど 1 つ含み、その前に空でない basename の接頭辞が必要です。
- **`config-needs-migration`。** legacy file しかありません。`migrate` を実行してください（第 6 節）。
- **最初の run で front matter や orphan の警告が大量に出る。** どれも non-blocking です。時間をかけて直すか、`documentChecks.layerGlobs` で文書を除外してください。
- **`history-corrupt`。** `history.jsonl` に壊れた行がある・通常ファイルでない・UTF-8 でない・行が長すぎる、`anchors/` 配下の anchor file が読めない・形式が不正、または `CONSISTENT` の run が指す anchor 候補が読めない・記録されたハッシュと一致しない状態です。直すまで engine は履歴を読みも書きもしません。state ディレクトリ全体の複製を取ってから、どの file が壊れているかを特定します。履歴の行が壊れていれば、有効な行を別名で残しつつ履歴 file を退避して新しい履歴 file を始めます。anchor が壊れていれば、その profile の anchor file を退避し、その profile は新たに `--full` の run が必要になります。run の途中で起きた場合（終了値 4）はその run が開いたままなので、次の監査の前に再開か放棄をします。`history.jsonl` 自体が権限で読めない場合は `history-corrupt` ではなく終了値 4・reason `PermissionError` で止まります。権限を直して再実行してください。
- **`mutex-timeout` または `run-in-progress`。** 別の engine process が run を保持しています。終了を待つか、どの process も保持しなくなってから `run-open.json` にある run を再開または放棄してください。
- **install 後に skill が一覧に出ない。** 新しい Claude Code session を開くか `/reload-plugins` を実行し、`claude plugin list` で確認してください。
- **想定外の file がリポジトリに書かれた。** engine がリポジトリ内に書くのは report と `.claude/state/docaudit/` だけです。repo 直下の `.mdq/`（mdq の索引と利用記録）だけは例外で、1.0.1 以降 gate は無視します。それ以外は監査中に動いた別のツールによるもので、run が `REFUSED worktree-modified` になる原因でもあります。
