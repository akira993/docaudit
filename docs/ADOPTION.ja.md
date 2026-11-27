# プロジェクトへの docaudit 導入ガイド

English: [ADOPTION.md](ADOPTION.md)

このガイドは、リポジトリを「監査なし」から「変更のたびに文書との整合を確認する」状態まで導きます。docaudit 2.0.0 が [README](../README.md) の手順で install 済みであることを前提とし、設定の仕様は [CONFIG-1.0.0.md](CONFIG-1.0.0.md)（英語）、コピーして使えるプロンプトは [PROMPTS.ja.md](PROMPTS.ja.md) にあります。3 つの skill の指示ファイル（`SKILL.md`）は日本語です。

このガイドのコマンドは skills-dir install の engine path `~/.claude/skills/docaudit/skills/audit/engine` を使います。marketplace 経由で install した場合は、README の install 節にある engine path に読み替えてください。

## 1. 5 分で始める

1. plugin を一度だけ install し（README の「Install」）、新しい Claude Code session を開く。
2. リポジトリに `.claude/docaudit.json` を作り（第 4 節）、commit する。
3. 監査前に組み込み検査の所見をもとに文書を直したいときは、`/docaudit:preflight --full` を実行し、適用する修正案を承認する。監査の判定基準でも文書を読みたいときは `--deep` を加える。`/docaudit:audit --full` を実行する。公開された report を読み、指摘された文書を直す。
4. run が `CONSISTENT` で終わるまで `/docaudit:audit --full` を繰り返す。その run が最初の anchor を書く。`NEEDS_FIX` が文書判定だけで止まる場合は `/docaudit:audit --full --accept-baseline` で baseline を受理し、以後は incremental にできる。切れたローカルリンク、失敗した project check、その他の文書判定以外の blocking は受理されないため、report の所見を先に直す。
5. 以後は変更のたびに `/docaudit:audit` を実行する。変更の影響を受ける文書を判定し、`changes.regressionRecheck` が有効なら、この profile で最新の判定が `FAIL` の文書も判定する。有効な組み込み `L-PROJECT` チェックは、影響文書だけに限らず corpus の文書を調べる。

1.0.1 への更新時は `.mdq/` が除外されたため、開いている run を `resume <runId> --abandon` で閉じる必要がありました。当時、閉じずに再開した run は、旧 scope に `.mdq/` 配下のパスが含まれていれば `REFUSED seal-drift`、旧版の作業木の記録だけに含まれていれば `REFUSED worktree-modified` になりました。どちらの場合も、監査の再実行で回復できました。

2.0.0 に更新する前に、開いている run（`.claude/state/docaudit/run-open.json` の `state` が `closed` 以外）を旧版の engine で最後まで実行するか、`resume <runId> --abandon` で閉じてください。Git が無視する通常ファイルの記録形式が変わり、リポジトリ直下の `.mdq/` が除外されなくなりました。更新後に旧版の run を再開し、新旧の作業木の記録の違いが許容されない変更として検出されると、`REFUSED worktree-modified` で終わります。`worktree.tolerateChangesIn` が空の Codex・agy の run は `tree.before.json` を保存しません。比較前の記録が得られなければ、作業木の digest が変わっていても `worktreeDiff` は空です。1.x で開いた Claude Workflow の run を 2.0.0 で再開し、受理済みの判定が新版の証拠検査を通らなければ、`REFUSED request-drift` で終わります。3 回目の request の後も未解決の文書が残れば、`undecided external-incomplete` で終わります。どちらの場合も anchor は進みません。判定役の既定モデルは 1.9.0 と同じです。旧版が保存した scope は当時の文書候補を引き継ぎます。条件を満たす `.mdq/` の文書を含めるには、新しい監査を始めてください。

### 更新と通知

`/docaudit:update` では、リリースノート、コミット、差分の概要を確認できます。`/docaudit:update rollback` では、戻し先の版を確認できます。更新も巻き戻しも Claude Code の許可画面で承認してください。

`PreToolUse` フックは `~/.claude/skills/docaudit/` にインストールした場合も読み込まれます。Bash コマンドに docaudit の engine のパス（`skills/audit/engine`）と `update` の文字列があると、実行前に許可を求めます。引用符や英字の大文字・小文字の違いは判定に影響しません。パスに `//`、`/./`、`..` があっても、同じ場所を指すなら同じ扱いです。利用者が頼んでいない更新や巻き戻しも許可画面で止まります。engine のフォルダ内を `update` で検索するコマンドや、更新コマンドの全文を引用したコミットメッセージでも許可を求めます。

許可を求めずに通すのは、コマンド全体が次の形に一致するときだけです。先頭は `python3`、`python`、または `/usr/bin/python3` のような Python の絶対パスです。指定できる Python のフラグは `-B`、`-I`、`-E`、`-s`、`-S`、`-u` だけで、一つずつ書きます。その後に engine のパスと `update` を置き、`--source <値>` や `--install-dir <値>` を指定できます。末尾は `plan`、`plan --to <版>`、`check`、`check --hook`、`notify on`、`notify off`、`rollback --dry-run` のいずれかです。たとえば `-W ignore` を指定したり、末尾に `2>&1`、`| tail`、`; echo` を付けたりすると、許可を 1 回求めます。対話できない `claude -p` では、答える利用者がいないため、許可を求めるコマンドは止まります。

フックが調べるのは、コマンドにそのまま書かれている文字列です。モデルはこのフックを回り込めます。たとえば前のコマンドや環境で `E` を設定してから実行する `python3 "$E" update apply`、`skills/audit` に移ってから engine を相対パスで指すコマンド、`python3 -m skills.audit.engine`、engine のパスや `update` を実行時に組み立てるコマンドは見分けません。端末から engine を直接実行する操作は従来どおりです。

シェルからは engine に次の引数を渡します。

- `update check` は取得先の最新の `vX.Y.Z` タグを調べ、インストール済みの版と比べます。
- `update notify on|off` はセッション開始時の通知を有効または無効にします。有効にすると、取得先を確認するのは 24 時間に 1 回までです。取得に失敗した場合も 24 時間は再取得せず、新しい版が分かっている場合に通知します。
- `update plan [--to vX.Y.Z]` で更新内容を確認し、`update apply --to vX.Y.Z` で指定の版に更新します。
- `update rollback [--dry-run]` で前の版に戻します。`--dry-run` を指定すると戻せるかを確認できます。

すべてのサブコマンドで `--source` と `--install-dir` を指定できます。`check` と `plan` はインストール済みのマニフェストを読み、入れ替えの前提条件は検査しません。`apply` と `rollback` は skills-dir にインストールされた版を入れ替えます。marketplace 経由のインストールや開発用の作業場所は拒否します。入れ替えと検証で使う複数のコマンドには、1 回あたり 60 秒の制限があります。それらのコマンドがいずれも上限近くまでかかると、全体の所要時間は数分になります。

`notify`・`apply`・`rollback` は、同時実行を防ぐロックが空くまで最大 15 秒待ちます。`check` は空きを待たずにロックを取ります。セッション開始時のフックも通知が有効な場合は同じです。`plan` と、更新の記録先がまだない場合の `rollback` はロックを取りません。

入れ替える前に、`apply` は準備したファイルをタグが示す Git のツリーと照合します。`apply` と `--dry-run` なしの `rollback` はどちらも候補の版とプラグイン名を確かめ、準備先とインストール先が同じファイルシステムにあることを確かめます。Claude CLI が使えれば `claude plugin validate` も実行します。入れ替え後はインストール済みの版を確認します。既定の skills-dir インストールで Claude CLI が使える場合は、プラグイン一覧を確認し、`claude plugin details` の表示から audit skill を探します。この命令には `--json` がありません。表示形式が変わり、`Skills` の行から audit skill を読み取れなくなると、検証に失敗して元の版への復元を試みます。

入れ替えが失敗した場合や、処理中に検知できる中断が起きた場合は、元の版への復元を試みます。更新の記録先は `${CLAUDE_CONFIG_DIR:-~/.claude}/docaudit-update/` です。[設定リファレンス](CONFIG-1.0.0.md#update-state)にファイルの一覧があります。自動復元が終わらなかった場合は、`history.jsonl` で該当する `started` の行を探してください。`installDir` が埋まっていれば、そのディレクトリを先に `skills/` の外にある空きパスへ移します。次に `asideDir` を `installDir` に移し、engine の `--version` で戻った版を確認してください。

## 2. 考え方

docaudit は Markdown 文書を、それが説明しているコードや設定と突き合わせ、合わなくなった文書を報告します。監査は報告専用で、リポジトリ内に書くのは report と `.claude/state/docaudit/` 配下の run 状態だけです。`/docaudit:preflight` は承認された箇所の文書を直し、`/docaudit:update` はプラグインのインストールと更新設定を扱います。

次の 5 つの考え方で、残りはすべて説明できます。

- **corpus（対象文書）。** `corpus.docGlobs` が監査対象の文書を選びます。git が無視する file は、`corpus.respectGitignore` を false にしない限り対象外です。
- **changes（変更）。** `changes.diffGlobs` が、変更されたら監査を起動する file を選びます。incremental な run は、同じ profile の最後に受理された run と作業木を比べます。
- **impact map（影響対応表）。** `impact.map` が、変更されたソースとそれを説明する文書を結び付けます。変更された file・対応表・唯一情報源の項目・（`impact.heuristics` が空でない object の場合は）file 名ヒューリスティクス・（有効なときは）`changes.regressionRecheck` による profile の最新 FAIL 文書が影響を受ける文書の集合を決め、`impact.maxImpactedDocs` がその上限になります。
- **層と profile。** run は層（layer）を実行します。`L-SCOPE` は影響集合の計算、`L-DOC` は影響を受けた各文書の検証、`L-PROJECT` は組み込みの文書チェックと `projectChecks` の実行、`L-ENRICH`・`L-SECURITY`・`L-ADVERSARIAL`・`L-CLAIM` は追加のレビュー層です。profile（`focused`・`standard`・`extended`）がどの層を走らせるかを選び、設定の `enabledLayers` がリポジトリとして許可する層を宣言します。
- **verdict と anchor。** verdict は `CONSISTENT` か `NEEDS_FIX` のどちらかです。`CONSISTENT` の run はその profile の anchor を前進させ、`--accept-baseline` で受理された full の `NEEDS_FIX` の run も同様に anchor を前進させます。次の incremental な run はどちらの場合も、その anchor からの変更を測ります。verdict に到達できない run は理由付きの `undecided`、封印された run の契約が gate の整合性検査に失敗した run は `REFUSED` で終わります。

文書の検証は backend が行います。backend は 3 つあります: Codex CLI、agy CLI、Claude Code の agent です。非対話の起動では、Codex、agy、Claude Code の agent の順で最初に使えるものを取ります。skill が監査を起動し、使えるものが 2 つ以上あるときは、どれを使うかを聞きます（第 7 節）。選択は run に封印され、再開しても同じ backend が使われます。Claude Code の agent を使うときは、skill が workflow を通じて agent に検証を渡し、終わったら engine を再開します。

## 3. 前提条件

| 要件 | 補足 |
|---|---|
| macOS | `audit`・`resume`（`--abandon` なし）・`migrate`（変換した設定を書き込むとき）の書込みの見張りは `fcntl.F_GETPATH` を使い、無い環境ではこれらの命令が `io-unsupported-platform` で止まります（`audit`・`resume` は終了値 3、書込みに進む `migrate` は 4、いずれも `runId: null`）。Linux ではこの理由で止まる見込みですが、`scope`・`precheck`・`migrate --dry-run`・`resume --abandon` は見張りを使わず、この理由では止まりません（macOS 上で `fcntl.F_GETPATH` を取り除いて模擬、Linux 実機では未実行）。`projectChecks` は macOS の `sandbox-exec` を使います。 `audit --ask-backend` は `--backend` なしで利用可能な backend が 2 つ以上なら、先に終了値 0 で `choose-backend` の質問を返します。 |
| git | git が差分とスナップショットを提供します。Claude Code が必要なのは `/docaudit:audit`・`/docaudit:preflight`・`/docaudit:update` の各 skill と Claude Code agents の backend です。 |
| Python 3.12 以上 | engine は標準ライブラリだけを使います。 |
| Codex CLI（任意） | `codex` が `PATH` にあり、`codex --version` と `codex exec --help` が動き、Codex home の `auth.json` が読める通常 file であるときに使われます。通常 file 以外は拒否します（例は symlink）。Codex のアカウントが model `gpt-6.1-sol` を使えることが必要です（第 7 節）。`extended` profile の `L-SECURITY`・`L-ADVERSARIAL`・`L-CLAIM` の 3 層は model を呼ぶときは Codex を使います（第 8 節）。 |
| agy CLI（任意） | `AGY_HOME` で指定した専用ホームディレクトリにログイン済みで、道具の制限用の hook file があるときだけ使われます（第 7 節「agy backend」）。 |
| Node.js（任意） | リポジトリ自身のテストを走らせるときだけ必要です。 |

リポジトリ側に必要なのは設定 file だけです。`corpus.docGlobs` が選ぶ Markdown 文書は tree 内のどこにあってもよく、state ディレクトリは、必要になったときに engine が自分で作ります（例えば run を開くときや `migrate` を行うとき）。report のディレクトリと file は第 4 節の `report.path` を参照してください。

## 4. 設定を書く

`.claude/docaudit.json` を作ります。README の最小例から始め、次の 4 点を調整します。

1. `corpus.docGlobs`: 監査する文書。生成物や取り込んだ文書は `corpus.excludeDocGlobs` で除外します。
2. `changes.diffGlobs`: 変更が監査を起動する file。最初の設定にはソース・設定・文書のディレクトリをまとめて指定します。
3. `impact.map`: ソース領域ごとに最低 1 項目（第 5 節）。
4. `report.path`: `.md` で終わり、path 全体で `<YYYY-MM-DD>` をちょうど 1 つ、`[_NN]` を高々 1 つ含む Markdown の path。日付が basename 内にある場合はその前に空でない文字列が必要で、日付が directory 側にある場合は basename 全体が空でなければ有効です。例: `reports/audit_<YYYY-MM-DD>[_NN].md`。report のディレクトリは retrieval 後の run 準備中に作られ、report を公開せずに終わっても残ります。それより前に拒否された run では作られません。report は上書きされず、同じ名前があれば `_02` から `_1000` までの最初の空き番号を、`[_NN]` がある位置か、無ければ日付の直後に付けます。すべて埋まると終了値 4・`path-exhausted`・開いた run の `runId` を返し、run は開いたままです。古い report を動かして名前を空け、その run を再開します。

`extended` profile を使う予定がなければ `enabledLayers` は `["L-SCOPE", "L-DOC", "L-PROJECT"]` のままにします（第 8 節）。`documentChecks.frontMatterFields` は文書が規約に従うまで空にしておきます。`documentChecks.indexFiles` は orphan 検査でリンク元と数える file の一覧で、file は存在しなくてもよく、読めない場合は空として扱われます。位置引数と `--profile` のどちらでも profile を指定しないと、既定の `standard` の層を run を開く前に照合し、`L-PROJECT` だけが足りなければ終了値 3・`capability-missing:L-PROJECT` で拒否されるため、`--profile focused` を指定します。未知のキーは拒否されるので、綴り間違いは無視されずに `config-invalid:<detail>` として検証に失敗します。

設定は commit してください。設定はリポジトリの契約の一部で、engine はそのバイト列を毎回の run に封印します。run が開いている間に file を変えると、その run は `REFUSED config-drift` になります。manifest に `configHash` がなく、設定 file が読める場合も同じ拒否になります。

監査中に別のツールが file を生成する場合は、Git が無視する場所を任意で
`worktree.tolerateChangesIn` に列挙できます。末尾が `/` ならそのディレクトリ、
無ければ正確に一致する file を指します。`cache/` や `logs/run.log` のような
リポジトリ相対の path を使います。glob・絶対 path・空や `.` の部分・`..`・
`./`・`//` は使えません。追跡 file、corpus の文書、engine の設定・state・
report の場所を含まないことが必要です。指定する場所は、それらの場所の中にも置けません。engine は run を開く前に調べます。
不正な値は `config-invalid:worktree.tolerateChangesIn`、object でない
`worktree` は `config-invalid:worktree` です。Git や corpus の計算が失敗した
場合は、その理由のまま止まります。1.2.1 以前の engine は `worktree` key を
`config-invalid:worktree` で拒否します。schema の値は `"1.0"` のままです。

作業木を走査するときは、列挙した場所の中も含め、Git が無視する項目も記録します。
Git が無視する通常ファイルは中身を読まず、属性を記録します。これらは 200,000 件の上限に数えますが、4 GiB の上限には数えません。
列挙した場所にあるファイル、追跡済みのファイル、無視されていない未追跡ファイルは中身からハッシュ値を計算します。Git が使えない場合は、すべての通常ファイルで中身からハッシュ値を計算します。
属性を記録する Git の無視ファイルは、同じ中身で書き直しても、記録する時刻が変わると作業木の記録が変わり、`worktree-modified` で拒否されます。
gate で大目に見るのは、列挙した場所の中にあり、封印した corpus と source の
snapshot に含まれず、その時点でも Git が無視する変化だけです。run 中に
`git add -f` した file、無視規則が消えた path、列挙した場所の親ディレクトリを
新しく作った場合は拒否の原因になります。大目に見た path は verdict の
`toleratedWorktreeChanges` に並びます。`worktreeDiffIgnoredOnly` が true に
なるのは、規則 7 で変化した path を列挙して拒否し、その拒否 path がすべて Git に
無視され、封印した corpus と snapshot の外にあると gate が確認できた場合だけです。
Git への確認が失敗した場合や path の差分を復元できない場合は、この印も report の
案内も出ません。印がある場合、report は二つの回避策を案内します。場所を
`worktree.tolerateChangesIn` に書くか、書き出し先を作業木の外へ移します。
一覧が空でない Codex/agy の run は、別の process で再開した後も path ごとに
判定できるよう `tree.before.json` を保存します。保存用の形式に変換した snapshot が
64 MiB を超えると、run は `undecided worktree-too-large` で終わります。

## 5. 良い impact map を作る

`impact.map` は `{"source": <glob>, "docs": [<path>, ...]}` の並びです。変更された file が `source` に一致すると、列挙された文書のうち corpus にあるものだけが影響対象になり、corpus にないものは除かれます。役に立つ対応表にする規則は 2 つです。

- **ディレクトリではなく責務で対応付ける。** デプロイ手順を説明する文書は、置き場所を問わず、デプロイ script とそれが引用する設定 file から対応付けます。
- **広い 1 項目より、小さい複数項目。** corpus 内の全文書を列挙した `src/**` の 1 項目は、変更された path が `src/**` に一致したときにそれらの文書を影響対象にします。影響対象となった corpus 内の文書の総数が `impact.maxImpactedDocs` を超えると、run は `undecided impact-limit` で終わります。

`impact.ssotSources` は、ある値の唯一の情報源になる file について対応表と同じように働きます。情報源の例は版 file や port 一覧です。情報源の変更で影響対象になるのは、列挙された文書のうち corpus にあるものだけです。corpus にないものは除かれます。

`impact.heuristics` は任意で、キーが無いか値が空の object `{}` なら動きません。空でない object を設定すると `L-SCOPE` は変更された各 file の名前を拡張子あり・なしの 2 形で取り出し、文書本文がそのどれかを素の部分文字列として含むときにその文書を影響対象にします。`minIdentifierLength` は短い名前を落とし、`excludeBasenames` は `index.ts` や `README` のようにどこにでも一致する名前を落とします（除外名の照合では大小文字を区別しません）。`excludeDocPathTokens` は変更された文書自身の名前を無視し、`saturationWarnRatio` はこの照合だけで影響対象になった文書が corpus 全体に占める割合がその比率に達したときに警告します。まずはヒューリスティクスなしで始め、対応表だけでは変更 file の名前を挙げている文書を取りこぼすときに追加してください。

`impact.maxImpactedDocs` は、1 回の incremental な run で検証してよい最大の文書数にします。full の run はこの上限を受けません。

## 6. 旧設定を移行する

リポジトリに legacy な .claude/doc-audit.json が残っている場合、`migrate` サブコマンドが変換します。

```sh
python3 ~/.claude/skills/docaudit/skills/audit/engine migrate --dry-run --repo-root .   # 変換内容を表示。終了値 1 = 変換不可
python3 ~/.claude/skills/docaudit/skills/audit/engine migrate --repo-root .             # 対象が無いときだけ .claude/docaudit.json を書く
```

dry run は `convertible`・`not-convertible`・`unchanged` のいずれかの結果と、変換後の設定、`counts`（legacy 履歴の件数・last-run 記録の有無・破棄されたキーの一覧）、読んだ入力とそのハッシュ、`anchor`（移行されない）、`runOpen` を表示します。プロジェクトの事実（文書 glob・差分 glob・impact map・report path・front matter と index の設定・ヒューリスティクス・唯一情報源の項目）は 1.0.0 のキーに対応付けられ、ヒューリスティクスは以前の版の既定値を補って書かれます。旧 install の詳細・コマンド対応・任意ツールを記述していたキーは破棄されます。1.0.0 はツールの利用可能性を設定ではなく検出で決めるためです。対応の全表は CONFIG-1.0.0.md の「Old-key disposition」にあります。

変換後の設定は常に `L-SCOPE`・`L-DOC`・`L-PROJECT` を有効にし、`projectChecks` は空です。legacy な履歴 file があれば、その項目は新しい `history.jsonl` に `legacy` 行として写されます。legacy な anchor は移行されないので、各 profile の最初の run は `--full` でなければなりません。移行が完了すると履歴に完了印が残ります。同じ legacy 入力で `migrate` をもう一度実行すると、run が開いていても `unchanged` を報告して何も書きません。legacy 入力が変わっていれば `migration-input-changed` で拒否されます。それ以外の本番の migrate は run が開いている間は拒否され（`migration-run-open`）、変換結果とバイト列が異なる既存の `.claude/docaudit.json` を置き換えることも拒否します（`migration-target-exists`）。

heuristic だけで影響対象になった文書も `impact.maxImpactedDocs` に数えられるため、上限を超えると run は `impact-limit` で止まります。以前の harness は超過した heuristic の一致を切り捨てて続けていました。たとえば 1.1.0 で移行した設定では、legacy 入力に指定していない `impact.heuristics` の値は補われません。同じ legacy 入力で migrate を再実行すると `unchanged` になり既存 file は書き換えられないため、`impact.heuristics` は手で整合させてください。無ければ次の object を設定し、既にあればその key と値を保ったまま、次の object にある不足 key だけを補い、`excludeBasenames` には次の object の順で既存 list に大小文字を区別せず無い名前だけを末尾に足します。

```json
{"minIdentifierLength":5,"excludeBasenames":["readme.md","index.md","changelog.md","license","license.md","__init__.py","makefile","main.md","test.md","skill","skill.md"],"saturationWarnRatio":0.5,"excludeDocPathTokens":false}
```

新しい file を確認して commit し、不要になったら legacy file を取り除いてください。engine が読むのは `.claude/docaudit.json` だけで、legacy file しかないリポジトリは `config-needs-migration` で終わります。

## 7. 監査を実行する

Claude Code の中で、リポジトリにて:

```
/docaudit:audit --full                # corpus 全体。profile に anchor ができるまで必須
/docaudit:audit --full --accept-baseline # 文書判定だけの NEEDS_FIX を baseline として受理
/docaudit:audit                       # incremental: anchor 以降の変更の影響を受けた文書だけ
/docaudit:audit --profile focused     # profile を指定。省略時は第 8 節の条件を満たすと focused を選択
/docaudit:audit standard              # --profile standard と同じ
```

`audit` は位置引数の profile と `--profile` を受け付けます。両方を指定するときは同じ値が必要で、どちらも指定しなければ profile を自動選択します（第 8 節）。`scope` と `precheck` は `--profile` で profile を指定します。

skill は engine を `--ask-backend` 付きで起動します（下の「backend を選ぶ」）。engine を実行し、engine が検証を Claude Code の agent に渡したとき（`nextAction: invoke-workflow`）は検証 workflow を起動して engine を再開します。これを最大 3 回繰り返し、超えたら run を放棄します。`done` または `abort` の後は `skills/audit/present.md` に従います。まず結論を示し、理由と report のパスがあれば添えます。report があれば 2 行の件数を示し、FAIL と WARN の文書を表にまとめ、次の作業を案内します。report に判定のない文書が載っていれば、その文書も示します。

Claude Code の外では同じ run をコマンドとして実行します。Claude Code の agent が使えないため、Codex backend か設定済みの agy が必要です（`--backend agy` または `--backend codex` で選べます）。

```sh
python3 ~/.claude/skills/docaudit/skills/audit/engine audit --full --profile standard --repo-root /path/to/repo
```

読み取り専用の下見には `python3 ~/.claude/skills/docaudit/skills/audit/engine scope --repo-root /path/to/repo` を実行します。`--full`・`--profile focused|standard|extended`・`--repo-root` を受け付け、既定値と設定・anchor・履歴・profile の選択は `audit` と同じです。`changed`・`impacted`・文書ごとの `provenance`（`self`・`regression` を含む）を、`scope.json` と同じ形の JSON 1 つとして出力します。run を開かず、lease・mutex を取らず、リポジトリ・state・report の file やディレクトリを作成・変更せず、モデルや外部の backend CLI を呼ばず、作業木の整合性を照合しません。計算できれば終了値 0、計算や設定の問題で拒否するときは run 開始前と同じ `reason` 付きの拒否 JSON と終了値 3 を返します。

同じ範囲に組み込み検査の所見を加えるには、`python3 ~/.claude/skills/docaudit/skills/audit/engine precheck --repo-root /path/to/repo` を実行します。引数は `scope` と同じです。状態を書き込まず、モデルや外部の判定役も呼びません。`projectLayer` が true なら、`checks` には `impacted` の外も含む監査対象全体の所見が入り、`projectChecksSkipped` には実行を省いた設定済みの検査の件数が入ります。Claude Code で `/docaudit:preflight` を実行すると、`precheck` の所見を表示し、会話モデルが直し案を作ります。承認された箇所だけを直し、`precheck` を再実行します。`--deep` を加えると、修正の承認前に監査の判定基準でも影響対象の文書を読みます。どちらの手順でも `audit` と `resume` の実行や Git への書き込みは行いません。

先回り修正の読み取りと監査の判定を別々に行うのは、監査が先回り修正と独立に、修正後の文書を確かめるためです。監査の判定基準でモデルが文書を読む費用が先回り修正と監査の両方でかかるのは、`--deep` を選んだときだけです。

### backend を選ぶ

文書を検証できる backend は 3 つです: Codex CLI、agy CLI、Claude Code の agent（Claude Code の中だけ）。

- `--backend agy|codex|claude` で backend を指名します。`--fast` は `--backend codex` と一緒のときだけ指定でき、Codex の priority service tier を使います。この 3 つ以外の `--backend` の値は `backend-invalid`、`--backend codex` なしの `--fast` は `fast-requires-codex` で拒否され、どちらも run を開く前の拒否です（終了値 3）。指名した backend を使えないときは、run が `undecided backend-unavailable` で終わります。
- skill は常に engine を `--ask-backend` 付きで起動します。引数の不整合（`accept-baseline-requires-full`・`backend-invalid`・`fast-requires-codex`）は設定を読む前に検査します。引数、設定の読み込み、`worktree.tolerateChangesIn` の検査、profile の解決のいずれかで拒否すると、backend を尋ねる前に終了値 3 で止まります。`--backend` を指定せず、使える backend が 2 つ以上あるとき、engine は run を開かず、`nextAction: choose-backend`・`runId: null`・`choices` 配列を出力して終了値 0 で終わります。`choices` には使える backend だけが次の順で並びます: Codex、`"fast":true` の Codex、agy（`{"backend":"agy","fast":false}`）、Claude Code の agent（`{"backend":"claude","fast":false}`）。skill は 1 つ選ぶよう尋ね（先頭が推奨）、`--backend`（`fast` が true なら `--fast` も）を付けて engine を起動し直します。答えずに閉じられたら先頭の選択肢を使い、質問できないときは skill が `--backend` なしで engine を起動するので、次の既定になります。
- `--backend` も `--ask-backend` もないとき（素のコマンド、または上の最後の場合）、engine は Codex、agy、Claude Code の agent の順で最初に使える backend を取ります。したがって、agy は使えるが Codex が動かない環境では、主担当が Claude Code の agent から agy に変わります。Claude Code の agent を使い続けるには `--backend claude` を付けます。
- 選択は run に書かれる（`run-options` 記録と `capability.json` の `backendSelection`）ので、`resume` でも同じ backend・同じ Fast 設定になります。

Codex backend は model `gpt-6.1-sol` を reasoning effort `medium` で動かし、Fast では `-c service_tier=priority` が加わります。文書の検証では、1 回の呼び出しは最大 900 秒・文書ごとに最大 3 回の試行・同時に最大 4 回の呼び出しです。Codex のアカウントが `gpt-6.1-sol` を使えず、影響文書がある場合は、呼び出しが失敗して文書の判定はなく、run は `undecided backend-failed` で終わります。影響文書が 0 件なら model は呼び出されません。文書をどの backend で検証していても、`extended` profile の `L-SECURITY`・`L-ADVERSARIAL`・`L-CLAIM` の Codex 呼び出しは、この model と reasoning effort を使います。3 層の評価は 1 回の呼び出しが最大 900 秒で、1 評価につき最大 3 回試します。`L-ADVERSARIAL` は影響文書ごとに 1 評価を同時に最大 4 回・`L-SECURITY` は影響文書全体で 1 評価・`L-CLAIM` は `L-ADVERSARIAL` の `FAIL` 所見ごとに 1 評価を順に行います。docaudit が自分で別の backend に切り替えることはないので、`--backend` で選んでください。

Claude Code の agent backend は判定役の agent `docaudit:doc-impact-verifier` を model `claude-sonnet-5-5`、effort `high` で動かします。

### agy backend

agy は agy CLI と model `gemini-4-hydrogen-medium` で文書を検証します。下の条件がすべて成り立つときだけ使われます。1 つでも欠けると、agy は何も表示されないまま `choices` と既定の順から外れ、理由は run の `capability.json` に記録されます（ホームと道具については `agyReason`、リポジトリについては `agyRepoBlock`）。

**専用ホームを用意する。**

1. 空のディレクトリを作り、engine を動かす環境で `AGY_HOME` にそれを指定します。例: `mkdir "$HOME/.agy-docaudit"` のあと `export AGY_HOME="$HOME/.agy-docaudit"`。`AGY_HOME` は必須です。未設定、または存在するディレクトリでなければ agy は使われません（ホームが無いと agy 自身が作ってしまうので、先にディレクトリを作ってください）。docaudit は agy を起動するたびに `HOME` を `AGY_HOME` に差し替え、それ以外は `PATH`・`LANG`・`LC_ALL`・`TMPDIR`・`AGY_CLI_DISABLE_AUTO_UPDATE=true` だけを渡すので、普段使いの agy のログイン・設定・plugin は使われません。
2. そのホームでログインします。`HOME="$AGY_HOME" agy` で agy を起動し、求められたら sign in して終了します。docaudit は代わりにログインしません。`AGY_HOME` の `.gemini/antigravity-cli/antigravity-oauth-token` があり読めることを確認するだけです。
3. 道具の制限（hook）を置きます。agy の Web 検索は権限設定では止まらないため、docaudit は、file を読む・探す 6 つの道具（`view_file`・`read_file`・`list_dir`・`find_by_name`・`grep_search`・`finish`）以外のすべての道具を拒否する hook を必須にしています。agy 1.4.9 と 1.5.10 は file を `read_file` で読み、それより古い版は `view_file` で読んでいたので、両方を許可しています。先に `mkdir -p "$AGY_HOME/.gemini/config"` を実行し、`AGY_HOME` の `.gemini/config/hooks.json` に、次の内容をそのまま置いてください。

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

   engine はこの file を JSON として読み、この文書と比べます。key の順序や空白は問いませんが、値の変更・欠け・余計な key があると agy は使えません。
4. ホームに許可を置かないでください。`AGY_HOME` の `.gemini/antigravity-cli/settings.json` は任意です。置く場合は、空でない `permissions.allow` も `hooks` という key も持ってはいけません。別の目的で一時的に足した許可も含みます。

**agy が使われない条件。** 起動時確認は次を上から順に調べ、最初に当たったものを `agyReason` に記録します。記録するのは理由だけで、file の中身は記録しません。agy 自体を起動するのは最後の確認（`agy --version` と `agy models`）だけです。

| reason | agy を使わない理由 |
|---|---|
| `agy-home-unset` | `AGY_HOME` が未設定または空 |
| `agy-home-missing` | `AGY_HOME` が存在するディレクトリではない |
| `agy-not-installed` | `PATH` に実行できる `agy` がない |
| `agy-settings-unreadable` | `settings.json` があるが読めない、JSON object でない、または `permissions` が `null` でも object でもない |
| `agy-permissions-present` | `settings.json` に空でない `permissions.allow` がある |
| `agy-settings-hooks-present` | `settings.json` に `hooks` という key がある |
| `agy-tool-hook-missing` | `.gemini/config/hooks.json` が無い |
| `agy-tool-hook-mismatch` | `.gemini/config/hooks.json` が読めない、JSON でない、または上の文書と違う |
| `agy-login-missing` | ログインの token file が無い、または読めない |
| `agy-tmp-unavailable` | private な一時ディレクトリを作れなかった |
| `agy-version-failed` | `agy --version` が失敗した、または 20 秒を超えた |
| `agy-models-timeout` | `agy models` が 30 秒を超えた |
| `agy-models-failed` | `agy models` が失敗した、または出力を読めなかった |
| `agy-model-missing` | `agy models` の出力の先頭 1 MiB を空白で区切った語に `gemini-4-hydrogen-medium` がない |

監査対象のリポジトリも調べ、`agyRepoBlock` に記録します。リポジトリに `.gemini/antigravity-cli` がある場合（中身は問いません。先に調べます）は `agy-repo-settings-present`、最上位に `.agents` がある場合は `agy-workspace-customizations` です。workspace の設定や hook は、リポジトリ自身の内容から command を実行でき、道具を許可することもできるため、そのようなリポジトリでは agy を使いません。

**agy backend にできないこと。**

- agy はシェルのコマンドを実行できない状態で動かします（必須の deny hook により、コマンドの道具は止まりますが判定は続きます。hook が無い環境で agy を直接使うと、権限の要る道具が自動で拒否され、返答全体が空になります）。そのため agy 用の judge 依頼文からは git を使う確認（`git ls-files`・`git check-ignore`）を外しています。agy では docaudit は追跡されている file と除外されている file を区別できません。入れ子の checkout の規則（独自の `.git` file またはディレクトリを持つディレクトリの下でしか見つからない複製は、別の checkout にあるもので、証拠にならない）は、その `.git` の有無だけで判定します。
- agy は起動のたびに `AGY_HOME` へ会話と log を書くので、ディレクトリは時間とともに大きくなります。
- docaudit は agy のホームに入れた plugin を検査しません。plugin は独自の hook を持てます。
- 1 回の呼び出しは agy 側で 15 分（`--print-timeout 15m`）に制限され、engine は 960 秒で待つのをやめます。同時に走るのは 2 本で、文書ごとに最大 3 回試します。

hook が拒否した道具は失敗ではありません。道具は実行されず、agy は続けます。すべての試行に失敗した文書は判定を持たず、run は `undecided backend-failed` で終わります。最後の試行の reason は文書の `failure` 記録に、各試行の reason は `model-call` 記録に残ります。ただし Codex・agy で検証する文書が private な一時ディレクトリを作れない場合、その文書では試行が始まらず、`model-call` 記録もありません。すべての文書で作れなければ run は `undecided tmp-unavailable` で終わり、文書ごとの `failure` 記録はありません。一部の文書だけなら、その文書の `failure` は reason `tmp-unavailable`・`attempts` 0 となり、run は `undecided backend-failed` で終わります。

| reason | 意味 |
|---|---|
| `agy-home-unset`、`agy-home-missing` | 判定の開始時に `AGY_HOME` が未設定またはディレクトリでない |
| `agy-prompt-invalid` | docaudit が agy 用に書き換えた judge 依頼文が自身の検査に通らず、agy を起動しなかった（3 回とも同じ結果になる） |
| `launch-failed` | agy を起動できなかった |
| `cancelled` | run が止まる途中で、その試行を止めた |
| `timeout` | agy が 960 秒以内に終わらなかった |
| `exit-<n>` | agy が終了値 `n` で終わった（例: ログインしていない） |
| `output-too-large` | 出力が 2 MiB を超えた |
| `output-not-json` | 出力が JSON object でなかった |
| `agy-status` | 報告された status が `SUCCESS` でなかった |
| `agy-denied` | 答えが返っても、agy が拒否された操作を報告した |
| `structured-output-missing` | 構造化された答えがなかった。15 分の上限もここに入る |
| `output-type-mismatch`、`output-schema-mismatch`、`identity-mismatch` | 答えが判定の schema に合わない、または尋ねられた run と文書をそのまま返していない |
| `document-unread` | agy の出力に読み戻しの欄や指定の行がない、欄の形式が正しくない、または行番号や本文が文書と一致しない |
| `quote-not-in-document` | agy が FAIL に必要な引用を返さない、引用が空白だけ、または引用が文書のどの行にも含まれない |
| `tmp-unavailable` | 試行前に private な一時ディレクトリを作れなかったため、`model-call` 記録はない |

### anchor のライフサイクル

- anchor は profile ごとに `.claude/state/docaudit/anchors/` に保持されます。
- profile の最初の anchor は、その profile の full の run が `CONSISTENT` で終わったときに書かれます。それまで、その profile の incremental な run は `undecided anchor-missing` で終わるので、`--full` を使い続けてください。
- `--accept-baseline` 付き full の `NEEDS_FIX` も、blocking がすべて `L-DOC` judgement で report 公開に成功した場合は最初の anchor を書く。既知 FAIL 文書は、その文書自身が変更されておらず、`impact.map`・`ssotSources`・heuristic により影響対象に入らず、`changes.regressionRecheck` も無効な場合にだけ受理後の再判定対象外となる。`changes.regressionRecheck: true` を使うと profile の最新 FAIL 文書を毎回再判定できるが、その件数は `impact.maxImpactedDocs` に数えられ、超えると run 全体が `impact-limit` で止まり、最新の判定が PASS または WARN になると集合から外れる。文書判定以外の blocking は受理されない。`focused` では生じず、`standard` と `extended` では切れたリンクと失敗した project check がそれにあたり、`extended` で加わるのは confirmed な claim だけである。security と adversarial の所見は non-blocking である。
- 以後、その profile の run が `CONSISTENT` で終わるたびに anchor が前進し、上と同じ条件で `--accept-baseline` により受理された full の `NEEDS_FIX` の run も同様に前進させます。それ以外の `NEEDS_FIX` の run、および `undecided`・`REFUSED` の run は anchor を動かさないので、次の incremental な run は同じ変更に新しい変更を加えて再び測ります。
- profile を切り替えると新しいライフサイクルが始まります。`focused` の anchor は `standard` の run には使われません。

### run は同時に 1 つ

engine は mutex と lease を保持し、前の run が開いたままの間は新しい監査を拒否します。拒否の理由が状態を示し、状態ごとに対処が違います。

- `run-in-progress`: 別の engine process が今まさに監査を実行中です。終わるのを待ってください。lease を保持している間、再開は `resume-in-progress`、放棄は `run-in-progress` で拒否されます。
- `run-awaiting-external`: run が検証 workflow に渡されたまま再開されていません。`resume <runId>` で再開します。結果行が closed になったか再び引き渡し（`nextAction: invoke-workflow`）になったかを示すので、後者なら workflow を実行してもう一度再開します。
- `run-interrupted-resume-required`: engine process が run の途中で終了しました。`resume <runId>` で再開するか、`resume <runId> --abandon` で閉じます。結果が未記録なら放棄は `undecided abandoned` を記録し、記録済みなら履歴はその結果を保ちます。以前の engine が `capability-missing:<layer>` の拒否後に開いたまま残した run は、再開すると終了値 4・その reason・`runId` で止まります。設定は run を開いたときに封印されているため、層を足しても再開では直らず、`resume <runId> --abandon` で閉じます。

skill は前の session から残った run を回復しません。手作業か、PROMPTS.ja.md 第 8 節のプロンプトで行ってください。

## 8. profile と層

| profile | 層 | backend |
|---|---|---|
| `focused` | `L-SCOPE`・`L-DOC` | 第 7 節で選んだ、または既定になった backend |
| `standard`（既定。下記の例外あり） | focused + `L-PROJECT` | 同上 |
| `extended` | standard + `L-ENRICH`・`L-SECURITY`・`L-ADVERSARIAL`・`L-CLAIM` | 文書の検証は同じ選択だが、`L-SECURITY`・`L-ADVERSARIAL`・`L-CLAIM` の 3 層は model を呼ぶときは Codex を使い、`L-ENRICH` はモデルを呼ばない |

位置引数と `--profile` のどちらでも profile を指定しないと、incremental で、変更 file が 10 件以下・影響文書が 15 件以下・削除以外の変更 file の現在のサイズの合計が 65536 byte 以下（差分のサイズではない）・直近の `standard` の outcome の verdict が `CONSISTENT`・変更 path に機微な語がない（大小文字を区別しない部分文字列の照合: `auth`・`security`・`permission`・`access-control`・`iam`・`crypto`・`billing`・`session`・`token`・`oauth`・`acl`・`rbac`・`secret`・`.env`）、をすべて満たすと `focused` を提案します。その profile に anchor がある場合だけ `focused` を使い、なければ plan と manifest の `profileSelectionSource` を `classifier-demoted` として `standard` のままにします。それ以外は `standard` を使います。`focused` では `L-PROJECT` が走りません。

`extended` は `enabledLayers` が 7 層すべてを列挙しているときだけ選べます。そうでなければ engine は `capability-missing:<layer>` で run を開くことを拒否します（終了値 3）。`L-SECURITY`・`L-ADVERSARIAL`・`L-CLAIM` の 3 層は文書をどの backend で検証していても Codex で動く（Fast を選んでいればそれも適用される）ので、利用可能性検査に合格する Codex が必要です。無ければ `L-CLAIM` が incomplete となり run は `undecided` で終わります。理由は、文書の検証が agy 経由なら `codex-unavailable`、Claude Code の agent 経由なら `workflow-adapter-unavailable` です。影響対象の文書があれば `L-SECURITY` と `L-ADVERSARIAL` も同じ理由で incomplete になり、無ければ呼び出しなしで complete になります。`extended` は Codex CLI が利用可能性検査に合格する環境で使ってください。

`extended` では、adversarial 層が影響を受けた文書ごとに根拠付きの矛盾を求め、security 層が文書化された手順・設定・秘密情報の扱い・権限を run ごとに 1 回レビューし、claim 層が adversarial の `FAIL` をひとつずつリポジトリと照合します。`extended` が加える 4 層（`L-ENRICH` を含む）の所見のうち、verdict を `NEEDS_FIX` にできるのは confirmed な claim だけで、adversarial・security・enrich の所見は情報提供です。文書の `FAIL` はどの profile でも verdict を `NEEDS_FIX` にし、project check の `FAIL` は `L-PROJECT` を実行する profile（`standard`・`extended`）で `NEEDS_FIX` にします。

## 9. プロジェクトチェック

`L-PROJECT` は封印 corpus に 4 種類の読み取り専用チェックを適用します。必須の front matter 項目（WARN）、ローカルな Markdown リンク（リンク先がなければ blocking な FAIL）、何も指していない path 風のバッククォート token（WARN）、他の文書や index file からリンクされていない文書（WARN）です。`documentChecks.indexFiles` に挙げた文書自身は orphan と報告されません。`documentChecks.layerGlobs` は一致する文書をチェックから外します。たとえば生成された index を orphan チェックから除外できます。封印 corpus が 1 文書だけの場合、orphan チェックは実行されず `orphan: skipped` と記録されます。

`links` と `orphan` では、リンク先とタイトルを CommonMark 0.31.2 の対応する形式で読み取ります。対応するのは、最大 32 段までの入れ子の括弧、逆斜線で打ち消した括弧、空白を含む `<...>` のリンク先です。二重引用符・一重引用符・括弧で囲んだタイトルは、リンク先に含めません。リンク文とタイトルが複数行にまたがるときは、空行を挟まなければ読み取ります。区切りの空白を置ける位置では、改行は 1 つまでです。改行を含むリンク先は読み取りません。リンク文の中の入れ子の角括弧・4 文字以上の空白で字下げした参照定義（リスト内も含む）・リスト記号や引用記号に続く参照定義は抽出せず、リンク先の名前付き・数値文字参照（例 `&amp;`）は解釈せず書かれた文字列のまま扱います。

`projectChecks` は自分のコマンドを追加します。各項目はチェック ID・`argv`・timeout・任意の作業ディレクトリを持ちます。コマンドは macOS 上で、`/dev/null` を除き private な一時ディレクトリ以外への書込みを禁じる `sandbox-exec` の profile の中で実行され、`findings` 配列を持つ JSON object を出力しなければなりません。各所見は `id`・`summary`・`severity`（`INFO`・`WARN`・`FAIL`）と任意の `path` を持ちます。`FAIL` の所見は blocking で、すべての文書が合格していても run は `NEEDS_FIX` で終わります。0 以外の終了値・timeout・不正な出力はそれ自体が blocking な `FAIL` 所見になります。`standard`・`extended` ではこの検査が動き、`focused` では動きません。Darwin 以外で空でない `projectChecks` を使うと `undecided sandbox-unavailable` になりますが、Linux では `audit`・`resume`（`--abandon` なし）・`migrate`（変換した設定を書き込むとき）の書込みの見張りが `fcntl.F_GETPATH` を必要とするため、その前に `io-unsupported-platform` で止まる見込みです（`audit`・`resume` は終了値 3、`migrate` は 4、いずれも `runId: null`。macOS 上で `fcntl.F_GETPATH` を取り除いて模擬、Linux 実機では未実行）。`projectChecks` を空にしてもこれらの命令の制限は解消しません。`scope`・`precheck`・`migrate --dry-run`・`resume --abandon` は見張りを使わず、この理由では止まりません。

## 10. 結果を読む

- **結果行。** engine 出力の最終行は 1 つの JSON object です。閉じた run では `nextAction`（`done` か `abort`）・`runId`・`outcome`、あれば `reason` と `reportPath`。workflow への引き渡しでは outcome の代わりに `nextAction: invoke-workflow` と `requestSeq`・`requestPath` が付き、backend の質問では `nextAction: choose-backend`・`runId` が null・`choices` が付いて run は開かれません。終了値 0 は engine が正常に終わったことを意味し、`undecided`・`REFUSED`・引き渡し・backend の質問を含みます。2 はコマンド行の解析で未知の command や option・曖昧な省略形の option（例 `--f`）・未知の profile 名・位置引数の profile と `--profile` の不一致・必須引数の不足（例は `resume` の run ID や option の値）・余分な引数・command のない呼び出し（`--version` を除く）が拒否され、JSON の結果行の代わりに `usage:` で始まる使い方が標準エラーに表示されます。3 は run を開く前、または再開を始める前の拒否で、設定・profile・run 状態の問題に加え、引数の不整合（`accept-baseline-requires-full`・`backend-invalid`・`fast-requires-codex`）や、`audit`・`resume` が対応しない platform（`io-unsupported-platform`）も含み、`reason` に名前が出ます。4 は run を開いた後、または再開が始まった後に続行できなかったことで、後からの `mutex-timeout` や report 名の使い切り（`path-exhausted`）も含みます。開いた run または再開した run の、engine が扱う終了値 4 の中断では、結果行にその run の `runId` が入り、run は開いたままなので再開か放棄を選びます。再開や放棄で `history.jsonl` を読み取れないときや、内容が壊れているときは失敗します。放棄が履歴の理由（`history-corrupt`・`PermissionError`・`OSError`）で失敗すると、終了値 4 で `runId: null` になります。`--dry-run` なしの `migrate` の拒否は `migration-source-missing`・`migration-run-open` が終了値 3、それ以外の理由が 4 です。移行は run を作らず、処理された結果行に `runId` はありません。`runOpen` は既存の開いた run を検出したかを示し、移行を再開・放棄するための run ではありません。`fcntl.F_GETPATH` が無いと、書込みに進む `migrate` は終了値 4・`io-unsupported-platform`・`runId: null` で止まります（macOS 上で模擬、Linux 実機では未実行）。 `audit --ask-backend` は `--backend` なしで利用可能な backend が 2 つ以上なら、先に終了値 0 で `choose-backend` の質問を返します。
- **report。** `report.path` に公開され、固定の front matter と、run・対象文書・所見・判定・anchor・計測・証跡の節を持ちます。見出しと定型句の大半は日本語です。`## 判定` では、判定と、該当する場合の理由および拒否した検査に続けて、`- 文書の判定の数: PASS n、WARN n、FAIL n、判定なし n` と `- 文書以外の所見の数: n` の 2 行を必ず出力します。所見ごとに項目が並びます。文書は verdict と食い違いの要約、それ以外の所見は severity と要約です。文書以外の所見の例は project check、link、claim です。要約内の改行は保持されます。judgement の裏付けとなる証拠の文字列（Codex backend には rationale に `file:line` を引用するよう、Claude Code の agent にはリポジトリ相対の証拠を示すよう求めます）は、run の証拠台帳と履歴の `judgement` 行に、Claude Code の agent で検証した run ではさらに `runs/<runId>/requests/<seq>/judgements/` に保持されます。所見の要約に伏字があれば `- redacted: N` で件数を示します。履歴の `judgement` 行も report と同じ規則で伏字にし、証拠台帳と `requests/<seq>/judgements/` は原文のままです。検証が始まる前に `undecided` で終わった run は report を公開しません。Run 節には `baseline` として基準にした anchor の判定役と engine の版を示し、今回と分かっている値が違えば `基準との違い` を追加します。内容が同じ文書を再検証するときも、前回の判定は引き継ぎません。前回と判定が違えば、report は今回の判定を示します。

  引用の照合では、空白以外の文字を含む引用が文書にあるかを調べます。引用の長さや主張を裏付けるかは確認しないため、原文を正しく写しても読み違いはこの検査では分かりません。読み戻しで確認できるのは指定した行だけです。候補になる行がない場合や、行を選ぶときにエンジンが文書を UTF-8 として読めない場合は、読み戻しで本文を読んだか確かめられません。たとえば文字コードが異なるとき、不正なバイトが 1 つでもあるとき、読み込みが拒否されたときは、文書を読めません。読めない文書では引用も本文と照合しません。

- **state ディレクトリ。** `.claude/state/docaudit/history.jsonl` は run の outcome（ほかに `flip`・`anchor`・`legacy`・`migration` の行）と、安全な path の `L-DOC` の PASS・WARN・FAIL 判定を 1 行ずつ記録します。判定を記録するのは run が `REFUSED` でなく、証拠台帳の検証と判定の照合がすべて通った場合だけです。記録しなかった PASS・WARN・FAIL 判定の数は、0 でなければ outcome の `judgementsSkipped` に残します。`anchors/<profile>.json` が現在の anchor です。engine は封印時に `runs/<runId>/` へ manifest を書きます。検証中は証拠台帳に adapter 結果と judgement を追記し、gate が完了すると `verdict.json` を書きます。たとえば `undecided backend-unavailable` で終わる run には、これらのファイルはありません。state ディレクトリを commit する（履歴を version 管理下で監査可能にする）か無視する（clone ごとのローカル状態にする）かは一度決めてください。engine はどちらでも動きます。

- **範囲の下見。** `scope` は範囲の JSON 自体を返し、run の記録・report・state ディレクトリを作りません。計算できれば終了値 0、拒否するときは `reason` 付きの拒否 JSON と終了値 3 を返します。

- **監査前の点検。** `precheck` は選ばれた監査範囲と組み込み検査の所見を返します。run の記録、report、state ディレクトリは作りません。点検できれば終了値 0、拒否するときは `reason` 付きで終了値 3、引数に誤りがあれば標準エラーに使い方を表示し、終了値 2 を返します。

### outcome 早見表

| outcome | 意味 | 対処 |
|---|---|---|
| `CONSISTENT` | 影響を受けた文書がすべて一致し、blocking な所見もない | なし。anchor が前進した |
| `NEEDS_FIX` | 少なくとも 1 文書が失敗、または blocking な所見（切れたリンク・失敗した project check・confirmed な claim） | report に出たものを直して再実行 |
| `NEEDS_FIX`（`--full --accept-baseline` で受理） | FAIL が文書判定だけ | anchor は前進した。既知 FAIL は文書を直したときに再判定 |
| `undecided anchor-missing` | anchor のない incremental な run | その run が `--profile` を指定していた場合は同じ `--profile` を付け、`--full` で実行 |
| `undecided backend-unavailable` | 能力検出で使える backend がなかった（Codex がなく、使える agy もなく、Claude Code の中でもない）、または `--backend` で指名した backend を使えない | Codex を install または修復するか、agy を設定する（第 7 節）か、Claude Code の中で実行。`--backend` で指名した backend を使えない場合は、その backend を直すか、`--backend` を外すか、別の使える backend を指名する |
| `undecided backend-failed` | Codex・agy で検証した文書がすべての試行に失敗した（例: agy にログインしていない、Codex のアカウントが `gpt-6.1-sol` を使えない）、または `L-ADVERSARIAL`・`L-SECURITY`・`L-CLAIM` の Codex 評価がすべての試行に失敗したか、最初の試行前に私用の一時ディレクトリを作れなかった | 文書の `failure` 記録と `model-call` 記録、または層の `model-call` 記録で理由を確認する。ただし `tmp-unavailable` で終わった文書には `model-call` 記録がない。一時ディレクトリも確認する。失敗した Codex・agy の試行では、`model-call` 記録に `stderrTail` があればエラー出力の末尾も確認する |
| `undecided tmp-unavailable` | Codex・agy で検証する文書のすべてで private な一時ディレクトリを作れず、試行が始まらなかった。または `projectChecks` が自分の private な一時ディレクトリを作れなかった | 一時ディレクトリに書き込めるか確かめて再実行する。前者の場合、文書に `failure`・`model-call` 記録はない |
| `undecided external-incomplete` | Claude Code の agent で検証した文書が 3 回目の request の後も未解決 | request と response の記録を確認し、外部の処理を直して再実行する |
| `undecided impact-limit` | 影響文書が `impact.maxImpactedDocs` を超えた | `--full` で実行するか、`impact.maxImpactedDocs` を上げるか、文書を追加する経路（`impact.map`・`impact.ssotSources`・`impact.heuristics`・`changes.regressionRecheck`）を絞る |
| `undecided corpus-unreadable` | 範囲の計算で engine が file の属性を調べ、file または親フォルダがシンボリックリンクであることと、file が通常 file でないこと以外の理由で検査を拒否すると、reason に `corpus-unreadable:<path>` の形で path が入る。`worktree.tolerateChangesIn` の corpus 検査で同じ拒否が起きると、run を開く前に終了値 3 で止まる。新しい run の作業木の digest 作成時に corpus 外を含む通常 file を読めなかった（`worktree.tolerateChangesIn` で許した場所と、属性だけを記録する Git が無視する通常ファイルを除く）（corpus の文書と `diffGlobs` に合う file は 8 MiB を超えると読めないものとして扱う）、または Workflow の探索準備時（この手順の完了前に中断した run の再開を含む）に封印済みの文書が消失・読み取り不能・通常 file 以外・リンクへの差し替え・親フォルダのリンク化のいずれかになった | 文書を読める通常 file に戻し、親フォルダもリンクでない状態に戻す・file を 8 MiB 以下にするか、8 MiB を超える場合は その file を読むすべての集合から外す（corpus の文書なら `docGlobs` または `excludeDocGlobs` で外し、変更元の `changes.diffGlobs` に合うなら、その file に合わないよう `changes.diffGlobs` を絞る）・再実行する |
| `undecided sandbox-unavailable` | Darwin 以外で `standard`・`extended` に空でない `projectChecks` を設定した・`/usr/bin/sandbox-exec` がない・`/usr/bin/true` の事前確認を起動できないか、0 以外で終わるか、20 秒で時間切れになる | macOS では `sandbox-exec` または事前確認の実行環境を直して再実行する。Linux では `audit`・`resume`（`--abandon` なし）・`migrate`（変換した設定を書き込むとき）は `projectChecks` が空でも、その前に `io-unsupported-platform` で止まる見込み（`audit`・`resume` は終了値 3、`migrate` は 4、いずれも `runId: null`）。`scope`・`precheck`・`migrate --dry-run`・`resume --abandon` は見張りを使わず、この理由では止まらない（macOS 上で `fcntl.F_GETPATH` を取り除いて模擬、Linux 実機では未実行） |
| `undecided workflow-adapter-unavailable` | 文書を Claude Code の agent 経由で検証する `extended` で、使える Codex がない | Codex を使えるようにする |
| `undecided codex-unavailable` | 文書を agy 経由で検証する `extended` で、使える Codex がない | Codex を使えるようにする |
| `undecided abandoned` | 放棄成功時の命令の結果行。履歴は結果が未記録の場合だけこれを記録し、記録済みならその結果を保つ | 再実行 |
| `REFUSED worktree-modified` | 大目に見られない作業木の変化があり、`worktreeDiff` には拒否した path だけが並ぶ | 書込み元を止めて再実行するか、Git が無視する場所を `worktree.tolerateChangesIn` に書くか、書き出し先を作業木の外へ移す。`worktreeDiffIgnoredOnly` が true のときは report の案内を参照 |
| `REFUSED config-drift` | run 中に `.claude/docaudit.json` が変わった、または manifest に `configHash` がなく設定 file が読める | 再実行 |
| その他の `REFUSED` | 封印された run の契約が整合性検査に失敗（lease・封印・証拠ハッシュ・層の集合・backend・judgement・adversarial と claim の整合性） | `runs/<runId>/` を確認して再実行 |

## 11. 困ったとき

- **毎回 `config-invalid:<detail>` になる。** detail が問題のキーを示します。path はリポジトリ相対で、親ディレクトリ参照の `..` という path セグメント（例: `a/../b`）を含まず、`report.path` は `.md` で終わり、path 全体で `<YYYY-MM-DD>` をちょうど 1 つ、`[_NN]` を高々 1 つ含む必要があります。日付が basename 内にある場合はその前に空でない文字列が必要で、日付が directory 側にある場合は basename 全体が空でなければ有効です。`config-invalid:worktree.tolerateChangesIn` なら、path の書き方と Git が無視するかを調べ、追跡 file・corpus 文書・engine の設定・state・report の場所を含まないか確認してください。
- **`config-needs-migration`。** legacy file しかありません。`migrate` を実行してください（第 6 節）。
- **最初の run で front matter や orphan の警告が大量に出る。** どれも non-blocking です。時間をかけて直すか、`documentChecks.layerGlobs` で文書を除外してください。
- **`history-corrupt`。** `history.jsonl` に壊れた行がある・通常ファイルでない・UTF-8 でない・行が長すぎる、`anchors/` 配下の anchor file が読めない・形式が不正、または `CONSISTENT` の run または `--accept-baseline` で受理した `NEEDS_FIX` の run が指す anchor 候補が読めない・記録されたハッシュと一致しない状態です。`history.jsonl` が壊れている場合は履歴の読み書きが止まります。anchor file だけが壊れている場合、その profile の incremental run は `history-corrupt` で止まりますが、`--full` は壊れた anchor を無視して履歴を読み、outcome を追記します。anchor を進める outcome（`CONSISTENT`、または `--accept-baseline` で受理した `NEEDS_FIX`）のときだけ、記録の段で `history-corrupt`・終了値 4 になります。state ディレクトリ全体の複製を取ってから、どの file が壊れているかを特定します。履歴の行が壊れていれば、有効な行を別名で残しつつ履歴 file を退避して新しい履歴 file を始めます。anchor が壊れていれば、その profile の anchor file を退避し、その profile は新たに `--full` の run が必要になります。run の途中で起きた場合（終了値 4）はその run が開いたままです。`history.jsonl` 自体が壊れている間は放棄も失敗するため、先に履歴を直してから次の監査の前に再開か放棄をします。anchor file だけが壊れている場合は放棄できます。`history.jsonl` 自体が権限で読めない場合は `history-corrupt` ではなく終了値 4・reason `PermissionError` で止まります。権限を直してから、その run を再開か放棄してください。`history.jsonl` が symbolic link の場合は、通常ファイルでない場合の例外として、その run は `history-corrupt` ではなく終了値 4・reason `OSError` で止まるため、link を通常ファイルに置き換えてから、その run を再開か放棄してください。
- **`mutex-timeout`。** 別の engine process が state の操作（例: `migrate`）をしています。終了後にやり直し、`run-open.json` に開いたままの run があれば再開または放棄してください。**`run-in-progress`。** 別の engine process が run を保持しています。終了を待つか、どの process も保持しなくなってから `run-open.json` にある run を再開または放棄してください。
- **インストール後に skill が一覧に出ない。** 新しい Claude Code session を開くか `/reload-plugins` を実行してください。`claude plugin list` でプラグインを確認し、`/docaudit:audit`・`/docaudit:preflight`・`/docaudit:update` が使えるか確かめてください。
- **想定外の file がリポジトリに書かれた。** 監査が書くのは report と `.claude/state/docaudit/` 配下の state です。2.0.0 から、リポジトリ直下の `.mdq/` は通常の文書候補、作業木の記録、変更パスとして扱います。監査中に別のツールが書き込む場所として許可するには、Git の無視規則に `.mdq/` を入れ、第 4 節の条件を満たしたうえで `worktree.tolerateChangesIn: [".mdq/"]` を設定します。別のツールが run 中に書いたとき、engine が許容するのは、設定した場所の中にあり、gate 時点で Git が無視し、封印した corpus と snapshot の外にある変更だけです。それ以外の変化は、新しく作った親ディレクトリも含めて `REFUSED worktree-modified` です。回避策は、対象となる場所を `worktree.tolerateChangesIn` に書くか、書き出し先を作業木の外へ移すことです。走査の上限を超えると `undecided worktree-too-large` になります。項目の数を減らすか、容量が原因なら中身をハッシュに含めるファイルの容量を減らしてください。

### 更新結果の理由

| reason | 意味 |
|---|---|
| `update-busy` | 別の更新操作がロックを保持しています。 |
| `update-install-manifest` | インストール済みの plugin.json を読めないか、名前や版が不正です。 |
| `update-marketplace-install: …` | marketplace のキャッシュにインストールされています。理由に表示される marketplace の命令を使ってください。 |
| `update-development-checkout` | インストール先に `.git` があり、開発用の作業場所とみなされました。 |
| `update-install-overlaps-state` | インストール先と更新の記録先が重なっています。 |
| `update-already-installed`, `update-target-tag-missing` | 指定のタグはインストール済みか、タグが無効または取得先に存在しません。 |
| `update-installed-tag-missing` | 現在の版のタグがありません。計画の作成は成功し、最大 20 件のコミットが表示されます。差分の要約はありません。 |
| `update-plugin-name-mismatch`, `staging-plugin-name` | 入れ替え候補のプラグイン名が異なるか、名前を読み取れませんでした。 |
| `update-filesystem`, `update-path-exists` | 準備先・退避先・インストール先の親が同じファイルシステムにないか、移動先がすでに使われています。 |
| `update-rollback-aside-missing`, `update-rollback-install-changed` | 使える退避版が記録されていないか、前回の入れ替えからインストール済みの版が変わっています。 |
| `update-check-git`, `update-clone`, `update-tags`, `update-tag-commit`, `update-notes`, `update-log`, `update-diffstat` | 版の取得や内容の確認に使う Git コマンドが失敗しました。 |
| `staging-archive`, `staging-tree`, `staging-version`, `staging-plugin-validate` | 展開または入れ替え候補の検証が失敗しました。 |
| `update-aside`, `update-place`, `installed-version`, `installed-plugin-list`, `installed-plugin-details`, `update-post-install` | ファイルの移動か、入れ替え後の検証が失敗しました。記録されたパスと検証結果を確認してください。 |
| `update-history`, `update-restore-failed`, `tmp-unavailable` | 履歴の書き込み、元の版への復元、または専用の一時領域の作成に失敗しました。 |

コマンドが時間切れになると、理由の末尾に `-timeout` が付きます。復元後の版の検証結果は `checks` の `restored-version` に記録します。更新処理が入れ替え中の中断を捕捉し、復元と履歴の書き込みに成功すると、履歴に例外名を記録します。例は `KeyboardInterrupt` です。復元に失敗したときに履歴へ書く理由は `update-restore-failed` です。

## Workflow の文書探索：索引の廃止

Workflow の文書探索は PATH にあるツールにかかわらず、対象を絞った文字列検索と Read を使います。外部の文書索引の作成や健全性検査は行いません。記録する reason は `mdq-retired` です。新しい Workflow run の方式は常に `grep` で、`indexDb`・`indexCwd`・`indexLang` はすべて null です。`index` の方式は 1.7.0 より前に封印された記録にだけ出ます。文書の複製は作りませんが、探索準備時には封印済みの各パスを検査し、文書や親フォルダがリンクに変わっていれば `undecided` / `corpus-unreadable` で閉じます。この検査は準備の完了前に中断した run の再開時にも行います。旧一時索引の片付け経路は残りますが、対象のリポジトリと run が所有することを確認できた索引に限ります。更新前に、旧 engine で開いた run を完了または放棄してください。

プロジェクトの文書をまたぐ Markdown 調査には mdrg（公開準備中）を推奨します。engine は mdrg を呼び出しません。
