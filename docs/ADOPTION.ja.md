# プロジェクトへの docaudit 導入ガイド

English: [ADOPTION.md](ADOPTION.md)

このガイドは、リポジトリを「監査なし」から「変更のたびに文書との整合を確認する」状態まで導きます。docaudit 1.3.1 が [README](../README.md) の手順で install 済みであることを前提とし、設定の仕様は [CONFIG-1.0.0.md](CONFIG-1.0.0.md)（英語）、コピーして使えるプロンプトは [PROMPTS.ja.md](PROMPTS.ja.md) にあります。skill の指示 file（`SKILL.md`）は日本語です。

このガイドのコマンドは skills-dir install の engine path `~/.claude/skills/docaudit/skills/audit/engine` を使います。marketplace 経由で install した場合は、README の install 節にある engine path に読み替えてください。

## 1. 5 分で始める

1. plugin を一度だけ install し（README の「Install」）、新しい Claude Code session を開く。
2. リポジトリに `.claude/docaudit.json` を作り（第 4 節）、commit する。
3. `/docaudit:audit --full` を実行する。公開された report を読み、指摘された文書を直す。
4. run が `CONSISTENT` で終わるまで `/docaudit:audit --full` を繰り返す。その run が最初の anchor を書く。`NEEDS_FIX` が文書判定だけで止まる場合は `/docaudit:audit --full --accept-baseline` で baseline を受理し、以後は incremental にできる。切れたローカルリンク、失敗した project check、その他の文書判定以外の blocking は受理されないため、report の所見を先に直す。
5. 以後は変更のたびに `/docaudit:audit` を実行する。変更の影響を受ける文書を判定し、`changes.regressionRecheck` が有効なら、この profile で最新の判定が `FAIL` の文書も判定する。有効な組み込み `L-PROJECT` チェックは、影響文書だけに限らず corpus の文書を調べる。

1.0.1 へ更新する前に、open な run を `resume <runId> --abandon` で閉じてください。閉じずに 1.0.1 で再開した run は、旧 scope に `.mdq/` 配下の path が含まれていれば `REFUSED seal-drift`、`.mdq/` が旧い作業木スナップショットだけに含まれていれば `REFUSED worktree-modified` になります。いずれも再実行で回復します。

この版に更新する前に、前の版が開いたままにした run（`.claude/state/docaudit/run-open.json` があり、`state` が `closed` でない状態）を、前の版の engine で閉じてください。最後まで resume するか、`resume <runId> --abandon` を実行します。封印済みの `manifest` に記録された backend と model が、この版の既定から解決した値と異なる場合、gate の backend 判定は `REFUSED backend-mismatch` を返します。

## 2. 考え方

docaudit は Markdown 文書を、それが説明しているコードや設定と突き合わせ、合わなくなった文書を報告します。文書を編集することはありません。すべての run は報告専用で、リポジトリ内に書くのは report と `.claude/state/docaudit/` 配下の run 状態だけです。

次の 5 つの考え方で、残りはすべて説明できます。

- **corpus（対象文書）。** `corpus.docGlobs` が監査対象の文書を選びます。git が無視する file は、`corpus.respectGitignore` を false にしない限り対象外です。
- **changes（変更）。** `changes.diffGlobs` が、変更されたら監査を起動する file を選びます。incremental な run は、同じ profile の最後に受理された run と作業木を比べます。
- **impact map（影響対応表）。** `impact.map` が、変更されたソースとそれを説明する文書を結び付けます。変更された file・対応表・唯一情報源の項目・（設定した場合は）file 名ヒューリスティクス・（有効なときは）`changes.regressionRecheck` による profile の最新 FAIL 文書が影響を受ける文書の集合を決め、`impact.maxImpactedDocs` がその上限になります。
- **層と profile。** run は層（layer）を実行します。`L-SCOPE` は影響集合の計算、`L-DOC` は影響を受けた各文書の検証、`L-PROJECT` は組み込みの文書チェックと `projectChecks` の実行、`L-ENRICH`・`L-SECURITY`・`L-ADVERSARIAL`・`L-CLAIM` は追加のレビュー層です。profile（`focused`・`standard`・`extended`）がどの層を走らせるかを選び、設定の `enabledLayers` がリポジトリとして許可する層を宣言します。
- **verdict と anchor。** verdict は `CONSISTENT` か `NEEDS_FIX` のどちらかです。`CONSISTENT` の run はその profile の anchor を前進させ、`--accept-baseline` で受理された full の `NEEDS_FIX` の run も同様に anchor を前進させます。次の incremental な run はどちらの場合も、その anchor からの変更を測ります。verdict に到達できない run は理由付きの `undecided`、封印された run の契約が gate の整合性検査に失敗した run は `REFUSED` で終わります。

文書の検証は backend が行います。backend は 3 つあります: Codex CLI、agy CLI、Claude Code の agent です。非対話の起動では、Codex、agy、Claude Code の agent の順で最初に使えるものを取ります。skill が監査を起動し、使えるものが 2 つ以上あるときは、どれを使うかを聞きます（第 7 節）。選択は run に封印され、再開しても同じ backend が使われます。Claude Code の agent を使うときは、skill が workflow を通じて agent に検証を渡し、終わったら engine を再開します。

## 3. 前提条件

| 要件 | 補足 |
|---|---|
| macOS または Linux | `projectChecks` は macOS でのみ動きます（`sandbox-exec` を使うため）。 |
| git | git が差分とスナップショットを提供します。Claude Code が必要なのは `/docaudit:audit` の skill と Claude Code agents の backend です。 |
| Python 3.12 以上 | engine は標準ライブラリだけを使います。 |
| Codex CLI（任意） | `codex` が `PATH` にあり、`codex --version` と `codex exec --help` が動き、Codex home の `auth.json` が読める通常 file（symlink 等の非通常 file は不可）であるときに使われます。Codex のアカウントが model `gpt-6-luna` を使えることが必要です（第 7 節）。`extended` profile の `L-SECURITY`・`L-ADVERSARIAL`・`L-CLAIM` の 3 層は model を呼ぶときは Codex を使います（第 8 節）。 |
| agy CLI（任意） | `AGY_HOME` で指定した専用ホームディレクトリにログイン済みで、道具の制限用の hook file があるときだけ使われます（第 7 節「agy backend」）。 |
| Node.js（任意） | リポジトリ自身のテストを走らせるときだけ必要です。 |

リポジトリ側に必要なのは設定 file だけです。`corpus.docGlobs` が選ぶ Markdown 文書は tree 内のどこにあってもよく、state ディレクトリは、必要になったときに engine が自分で作ります（例えば run を開くときや `migrate` を行うとき）。report のディレクトリと file は第 4 節の `report.path` を参照してください。

## 4. 設定を書く

`.claude/docaudit.json` を作ります。README の最小例から始め、次の 4 点を調整します。

1. `corpus.docGlobs`: 監査する文書。生成物や取り込んだ文書は `corpus.excludeDocGlobs` で除外します。
2. `changes.diffGlobs`: 変更が監査を起動する file。通常はソース・設定・文書のディレクトリをまとめて指定します。
3. `impact.map`: ソース領域ごとに最低 1 項目（第 5 節）。
4. `report.path`: `.md` で終わり、path 全体で `<YYYY-MM-DD>` をちょうど 1 つ、`[_NN]` を高々 1 つ含む Markdown の path。日付が basename 内にある場合はその前に空でない文字列が必要で、日付が directory 側にある場合は basename 全体が空でなければ有効です。例: `reports/audit_<YYYY-MM-DD>[_NN].md`。report のディレクトリは retrieval 後の run 準備中に作られ、report を公開せずに終わっても残ります。それより前に拒否された run では作られません。report は上書きされず、同じ名前があれば `_02` から最初の空き番号を、`[_NN]` がある位置か、無ければ日付の直後に付けます。

`extended` profile を使う予定がなければ `enabledLayers` は `["L-SCOPE", "L-DOC", "L-PROJECT"]` のままにし（第 8 節）、`documentChecks.frontMatterFields` と `documentChecks.indexFiles` は文書がその規約に従うようになるまで空にしておきます。未知のキーは拒否されるので、綴り間違いは無視されずに `config-invalid:<detail>` として検証に失敗します。

設定は commit してください。設定はリポジトリの契約の一部で、engine はそのバイト列を毎回の run に封印します。run が開いている間に file を変えると、その run は `REFUSED config-drift` になります。

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

作業木の走査は、列挙した場所の中も含め、Git が無視する entry を記録し続けます。
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
判定できるよう `tree.before.json` を保存します。この snapshot は 64 MiB
までで、上限を超えると `undecided worktree-too-large` になります。

## 5. 良い impact map を作る

`impact.map` は `{"source": <glob>, "docs": [<path>, ...]}` の並びです。変更された file が `source` に一致すると、列挙された文書のうち corpus にあるものだけが影響対象になり、corpus にないものは除かれます。役に立つ対応表にする規則は 2 つです。

- **ディレクトリではなく責務で対応付ける。** デプロイ手順を説明する文書は、置き場所を問わず、デプロイ script とそれが引用する設定 file から対応付けます。
- **広い 1 項目より、小さい複数項目。** corpus 内の全文書を列挙した `src/**` の 1 項目は、変更された path が `src/**` に一致したときにそれらの文書を影響対象にします。影響対象となった corpus 内の文書の総数が `impact.maxImpactedDocs` を超えると、run は `undecided impact-limit` で終わります。

`impact.ssotSources` は、ある値の唯一の情報源になる file（版 file、port 一覧など）について対応表と同じように働きます。情報源の変更で影響対象になるのは、列挙された文書のうち corpus にあるものだけです。corpus にないものは除かれます。

`impact.heuristics` は任意で、キーが無ければ動きません。設定すると `L-SCOPE` は変更された各 file の名前を拡張子あり・なしの 2 形で取り出し、文書本文がそのどれかを素の部分文字列として含むときにその文書を影響対象にします。`minIdentifierLength` は短い名前を落とし、`excludeBasenames` は `index.ts` や `README` のようにどこにでも一致する名前を落とします（除外名の照合では大小文字を区別しません）。`excludeDocPathTokens` は変更された文書自身の名前を無視し、`saturationWarnRatio` はこの照合だけで影響対象になった文書が corpus 全体に占める割合がその比率に達したときに警告します。まずはヒューリスティクスなしで始め、対応表だけでは変更 file の名前を挙げている文書を取りこぼすときに追加してください。

`impact.maxImpactedDocs` は、1 回の incremental な run で検証してよい最大の文書数にします。full の run はこの上限を受けません。

## 6. 旧設定を移行する

リポジトリに legacy な .claude/doc-audit.json が残っている場合、`migrate` サブコマンドが変換します。

```sh
python3 ~/.claude/skills/docaudit/skills/audit/engine migrate --dry-run --repo-root .   # 変換内容を表示。終了値 1 = 変換不可
python3 ~/.claude/skills/docaudit/skills/audit/engine migrate --repo-root .             # 対象が無いときだけ .claude/docaudit.json を書く
```

dry run は `convertible`・`not-convertible`・`unchanged` のいずれかの結果と、変換後の設定、`counts`（legacy 履歴の件数・last-run 記録の有無・破棄されたキーの一覧）、読んだ入力とそのハッシュ、`anchor`（移行されない）、`runOpen` を表示します。プロジェクトの事実（文書 glob・差分 glob・impact map・report path・front matter と index の設定・ヒューリスティクス・唯一情報源の項目）は 1.0.0 のキーに対応付けられ、ヒューリスティクスは以前の版の既定値を補って書かれます。旧 install の詳細・コマンド対応・任意ツールを記述していたキーは破棄されます。1.0.0 はツールの利用可能性を設定ではなく検出で決めるためです。対応の全表は CONFIG-1.0.0.md の「Old-key disposition」にあります。

変換後の設定は常に `L-SCOPE`・`L-DOC`・`L-PROJECT` を有効にし、`projectChecks` は空です。legacy な履歴 file があれば、その項目は新しい `history.jsonl` に `legacy` 行として写されます。legacy な anchor は移行されないので、各 profile の最初の run は `--full` でなければなりません。移行が完了すると履歴に完了印が残ります。同じ legacy 入力で `migrate` をもう一度実行すると、run が開いていても `unchanged` を報告して何も書きません。legacy 入力が変わっていれば `migration-input-changed` で拒否されます。それ以外の本番の migrate は run が開いている間は拒否され（`migration-run-open`）、変換結果とバイト列が異なる既存の `.claude/docaudit.json` を置き換えることも拒否します（`migration-target-exists`）。

heuristic だけで影響対象になった文書も `impact.maxImpactedDocs` に数えられるため、上限を超えると run は `impact-limit` で止まります。以前の harness は超過した heuristic の一致を切り捨てて続けていました。以前の版で移行した設定には以前の既定値が無いことがあります。同じ legacy 入力で migrate を再実行すると `unchanged` になり既存 file は書き換えられないため、`impact.heuristics` は手で整合させてください。無ければ次の object を設定し、既にあればその key と値を保ったまま、次の object にある不足 key だけを補い、`excludeBasenames` には次の object の順で既存 list に大小文字を区別せず無い名前だけを末尾に足します。

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
/docaudit:audit --profile focused     # profile の選択。既定は standard
```

skill は engine を `--ask-backend` 付きで起動します（下の「backend を選ぶ」）。engine を実行し、engine が検証を Claude Code の agent に渡したとき（`nextAction: invoke-workflow`）は検証 workflow を起動して engine を再開します。これを最大 3 回繰り返し、超えたら run を放棄します。最後に outcome・理由・report の path を表示します。

Claude Code の外では同じ run をコマンドとして実行します。Claude Code の agent が使えないため、Codex backend か設定済みの agy が必要です（`--backend agy` または `--backend codex` で選べます）。

```sh
python3 ~/.claude/skills/docaudit/skills/audit/engine audit --full --profile standard --repo-root /path/to/repo
```

### backend を選ぶ

文書を検証できる backend は 3 つです: Codex CLI、agy CLI、Claude Code の agent（Claude Code の中だけ）。

- `--backend agy|codex|claude` で backend を指名します。`--fast` は `--backend codex` と一緒のときだけ指定でき、Codex の priority service tier を使います。この 3 つ以外の `--backend` の値は `backend-invalid`、`--backend codex` なしの `--fast` は `fast-requires-codex` で拒否され、どちらも run を開く前の拒否です（終了値 3）。指名した backend を使えないときは、run が `undecided backend-unavailable` で終わります。
- skill は常に engine を `--ask-backend` 付きで起動します。`--backend` を指定せず、使える backend が 2 つ以上あるとき、engine は run を開かず、`nextAction: choose-backend`・`runId: null`・`choices` 配列を出力して終了値 0 で終わります。`choices` には使える backend だけが次の順で並びます: Codex、`"fast":true` の Codex、agy（`{"backend":"agy","fast":false}`）、Claude Code の agent（`{"backend":"claude","fast":false}`）。skill は 1 つ選ぶよう尋ね（先頭が推奨）、`--backend`（`fast` が true なら `--fast` も）を付けて engine を起動し直します。答えずに閉じられたら先頭の選択肢を使い、質問できないときは skill が `--backend` なしで engine を起動するので、次の既定になります。
- `--backend` も `--ask-backend` もないとき（素のコマンド、または上の最後の場合）、engine は Codex、agy、Claude Code の agent の順で最初に使える backend を取ります。したがって、agy は使えるが Codex が動かない環境では、主担当が Claude Code の agent から agy に変わります。Claude Code の agent を使い続けるには `--backend claude` を付けます。
- 選択は run に書かれる（`run-options` 記録と `capability.json` の `backendSelection`）ので、`resume` でも同じ backend・同じ Fast 設定になります。

Codex backend は model `gpt-6-luna` を reasoning effort `xhigh` で動かし、Fast では `-c service_tier=priority` が加わります。1 回の呼び出しの時間上限は 900 秒で、文書ごとに最大 3 回試し、同時に 4 回まで呼びます。Codex のアカウントが `gpt-6-luna` を使えず、影響文書がある場合は、呼び出しが失敗して文書の判定はなく、run は `undecided backend-failed` で終わります。影響文書が 0 件なら model は呼び出されません。docaudit が自分で別の backend に切り替えることはないので、`--backend` で選んでください。

Claude Code の agent backend は判定役の agent `docaudit:doc-impact-verifier` を model `claude-sonnet-5-5`、effort `high` で動かします。

### agy backend

agy は agy CLI と model `gemini-3.8-flash-medium` で文書を検証します。下の条件がすべて成り立つときだけ使われます。1 つでも欠けると、agy は何も表示されないまま `choices` と既定の順から外れ、理由は run の `capability.json` に記録されます（ホームと道具については `agyReason`、リポジトリについては `agyRepoBlock`）。

**専用ホームを用意する。**

1. 空のディレクトリを作り、engine を動かす環境で `AGY_HOME` にそれを指定します。例: `mkdir "$HOME/.agy-docaudit"` のあと `export AGY_HOME="$HOME/.agy-docaudit"`。`AGY_HOME` は必須です。未設定、または存在するディレクトリでなければ agy は使われません（ホームが無いと agy 自身が作ってしまうので、先にディレクトリを作ってください）。docaudit は agy を起動するたびに `HOME` を `AGY_HOME` に差し替え、それ以外は `PATH`・`LANG`・`LC_ALL`・`TMPDIR`・`AGY_CLI_DISABLE_AUTO_UPDATE=true` だけを渡すので、普段使いの agy のログイン・設定・plugin は使われません。
2. そのホームでログインします。`HOME="$AGY_HOME" agy` で agy を起動し、求められたら sign in して終了します。docaudit は代わりにログインしません。`AGY_HOME` の `.gemini/antigravity-cli/antigravity-oauth-token` があり読めることを確認するだけです。
3. 道具の制限（hook）を置きます。agy の Web 検索は権限設定では止まらないため、docaudit は、file を読む・探す 5 つの道具（`view_file`・`list_dir`・`find_by_name`・`grep_search`・`finish`）以外のすべての道具を拒否する hook を必須にしています。先に `mkdir -p "$AGY_HOME/.gemini/config"` を実行し、`AGY_HOME` の `.gemini/config/hooks.json` に、次の内容をそのまま置いてください。

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

   engine はこの file を JSON として読み、この文書と比べます。key の順序や空白は問いませんが、値の変更・欠け・余計な key があると agy は使えません。
4. ホームに許可を置かないでください。`AGY_HOME` の `.gemini/antigravity-cli/settings.json` は無くてもかまいませんが、あるなら、空でない `permissions.allow` も `hooks` という key も持ってはいけません。別の目的で一時的に足した許可も含みます。

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
| `agy-model-missing` | `agy models` の出力の先頭 1 MiB を空白で区切った語に `gemini-3.8-flash-medium` がない |

監査対象のリポジトリも調べ、`agyRepoBlock` に記録します。リポジトリに `.gemini/antigravity-cli` がある場合（中身は問いません。先に調べます）は `agy-repo-settings-present`、最上位に `.agents` がある場合は `agy-workspace-customizations` です。workspace の設定や hook は、リポジトリ自身の内容から command を実行でき、道具を許可することもできるため、そのようなリポジトリでは agy を使いません。

**agy backend にできないこと。**

- agy はシェルのコマンドを実行できない状態で動かします（必須の deny hook により、コマンドの道具は止まりますが判定は続きます。hook が無い環境で agy を直接使うと、権限の要る道具が自動で拒否され、返答全体が空になります）。そのため agy 用の judge 依頼文からは git を使う確認（`git ls-files`・`git check-ignore`）を外しています。agy では docaudit は追跡されている file と除外されている file を区別できません。入れ子の checkout の規則（独自の `.git` file またはディレクトリを持つディレクトリの下でしか見つからない複製は、別の checkout にあるもので、証拠にならない）は、その `.git` の有無だけで判定します。
- agy は起動のたびに `AGY_HOME` へ会話と log を書くので、ディレクトリは時間とともに大きくなります。
- docaudit は agy のホームに入れた plugin を検査しません。plugin は独自の hook を持てます。
- 1 回の呼び出しは agy 側で 15 分（`--print-timeout 15m`）に制限され、engine は 960 秒で待つのをやめます。同時に走るのは 2 本で、文書ごとに最大 3 回試します。

hook が拒否した道具は失敗ではありません。道具は実行されず、agy は続けます。すべての試行に失敗した文書は判定を持たず、run は `undecided backend-failed` で終わります。最後の試行の reason は文書の `failure` 記録に、各試行の reason は `model-call` 記録に残ります。

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
| `tmp-unavailable` | private な一時ディレクトリを作れなかった |

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
- `run-interrupted-resume-required`: engine process が run の途中で終了しました。`resume <runId>` で再開するか、`resume <runId> --abandon` で verdict なしに閉じます。

skill は前の session から残った run を回復しません。手作業か、PROMPTS.ja.md 第 8 節のプロンプトで行ってください。

## 8. profile と層

| profile | 層 | backend |
|---|---|---|
| `focused` | `L-SCOPE`・`L-DOC` | 第 7 節で選んだ、または既定になった backend |
| `standard`（既定） | focused + `L-PROJECT` | 同上 |
| `extended` | standard + `L-ENRICH`・`L-SECURITY`・`L-ADVERSARIAL`・`L-CLAIM` | 文書の検証は同じ選択だが、`L-SECURITY`・`L-ADVERSARIAL`・`L-CLAIM` の 3 層は model を呼ぶときは Codex を使い、`L-ENRICH` はモデルを呼ばない |

`extended` は `enabledLayers` が 7 層すべてを列挙しているときだけ選べます。そうでなければ engine は `capability-missing:<layer>` で run を開くことを拒否します（終了値 3）。`L-SECURITY`・`L-ADVERSARIAL`・`L-CLAIM` の 3 層は文書をどの backend で検証していても Codex で動く（Fast を選んでいればそれも適用される）ので、利用可能性検査に合格する Codex が必要です。無ければ `L-CLAIM` が incomplete となり run は `undecided` で終わります。理由は、文書の検証が agy 経由なら `codex-unavailable`、Claude Code の agent 経由なら `workflow-adapter-unavailable` です。影響対象の文書があれば `L-SECURITY` と `L-ADVERSARIAL` も同じ理由で incomplete になり、無ければ呼び出しなしで complete になります。`extended` は Codex CLI が利用可能性検査に合格する環境で使ってください。

`extended` では、adversarial 層が影響を受けた文書ごとに根拠付きの矛盾を求め、security 層が文書化された手順・設定・秘密情報の扱い・権限を run ごとに 1 回レビューし、claim 層が adversarial の `FAIL` をひとつずつリポジトリと照合します。`extended` が加える 4 層（`L-ENRICH` を含む）の所見のうち、verdict を `NEEDS_FIX` にできるのは confirmed な claim だけで、adversarial・security・enrich の所見は情報提供です。文書の `FAIL` はどの profile でも verdict を `NEEDS_FIX` にし、project check の `FAIL` は `L-PROJECT` を実行する profile（`standard`・`extended`）で `NEEDS_FIX` にします。

## 9. プロジェクトチェック

`L-PROJECT` は封印 corpus に 4 種類の読み取り専用チェックを適用します。必須の front matter 項目（WARN）、ローカルな Markdown リンク（リンク先がなければ blocking な FAIL）、何も指していない path 風のバッククォート token（WARN）、他の文書や index file からリンクされていない文書（WARN）です。`documentChecks.layerGlobs` は一致する文書をチェックから外します。たとえば生成された index を orphan チェックから除外できます。封印 corpus が 1 文書だけの場合、orphan チェックは実行されず `orphan: skipped` と記録されます。

`projectChecks` は自分のコマンドを追加します。各項目はチェック ID・`argv`・timeout・任意の作業ディレクトリを持ちます。コマンドは macOS 上で、`/dev/null` を除き private な一時ディレクトリ以外への書込みを禁じる `sandbox-exec` の profile の中で実行され、`findings` 配列を持つ JSON object を出力しなければなりません。各所見は `id`・`summary`・`severity`（`INFO`・`WARN`・`FAIL`）と任意の `path` を持ちます。`FAIL` の所見は blocking で、すべての文書が合格していても run は `NEEDS_FIX` で終わります。0 以外の終了値・timeout・不正な出力はそれ自体が blocking な `FAIL` 所見になります。Linux では `projectChecks` を空にしてください。空でないと run は `undecided sandbox-unavailable` で終わります。

## 10. 結果を読む

- **結果行。** engine 出力の最終行は 1 つの JSON object です。閉じた run では `nextAction`（`done` か `abort`）・`runId`・`outcome`、あれば `reason` と `reportPath`。workflow への引き渡しでは outcome の代わりに `nextAction: invoke-workflow` と `requestSeq`・`requestPath` が付き、backend の質問では `nextAction: choose-backend`・`runId` が null・`choices` が付いて run は開かれません。終了値 0 は engine が正常に終わったことを意味し、`undecided`・`REFUSED`・引き渡し・backend の質問を含みます。3 は run を開くことを拒否したこと（設定・profile・run 状態の問題。`reason` に名前が出ます）、4 は開いた run が続行できなかったことです。
- **report。** `report.path` に公開され、固定の front matter と、run・対象文書・所見・判定・anchor・計測・証跡の節を持ちます。見出しと定型句の大半は日本語です。所見ごとに 1 行が並びます。文書は verdict と食い違いの 1 行要約、それ以外の所見（project check・link・claim など）は severity と要約です。judgement の裏付けとなる証拠の文字列（Codex backend には rationale に `file:line` を引用するよう、Claude Code の agent にはリポジトリ相対の証拠を示すよう求めます）は、run の証拠台帳と履歴の `judgement` 行に、Claude Code の agent で検証した run ではさらに `runs/<runId>/requests/<seq>/judgements/` に保持されます。所見の安全でない要約と履歴の `judgement` 行は report と同じ規則で伏字にし、証拠台帳と `requests/<seq>/judgements/` は原文のままです。検証が始まる前に `undecided` で終わった run は report を公開しません。
- **state ディレクトリ。** `.claude/state/docaudit/history.jsonl` は run の outcome と judgement を 1 行ずつ記録し（ほかに `flip`・`anchor`・`legacy`・`migration` の行）、`anchors/<profile>.json` が現在の anchor、`runs/<runId>/` には封印された manifest・adapter 結果と judgement を含む証拠台帳・`verdict.json` が置かれることがありますが、`undecided backend-unavailable` で終わる run にはこれらがありません。state ディレクトリを commit する（履歴を version 管理下で監査可能にする）か無視する（clone ごとのローカル状態にする）かは一度決めてください。engine はどちらでも動きます。

### outcome 早見表

| outcome | 意味 | 対処 |
|---|---|---|
| `CONSISTENT` | 影響を受けた文書がすべて一致し、blocking な所見もない | なし。anchor が前進した |
| `NEEDS_FIX` | 少なくとも 1 文書が失敗、または blocking な所見（切れたリンク・失敗した project check・confirmed な claim） | report に出たものを直して再実行 |
| `NEEDS_FIX`（`--full --accept-baseline` で受理） | FAIL が文書判定だけ | anchor は前進した。既知 FAIL は文書を直したときに再判定 |
| `undecided anchor-missing` | anchor のない incremental な run | `--full` で実行 |
| `undecided backend-unavailable` | 能力検出で使える backend がなかった（Codex がなく、使える agy もなく、Claude Code の中でもない）、または `--backend` で指名した backend を使えない | Codex を install または修復するか、agy を設定する（第 7 節）か、Claude Code の中で実行。`--backend` で指名した backend を使えない場合は、その backend を直すか、`--backend` を外すか、別の使える backend を指名する |
| `undecided backend-failed` | 少なくとも 1 件の文書がすべての試行に失敗した（例: agy にログインしていない、Codex のアカウントが `gpt-6-luna` を使えない）、または `L-ADVERSARIAL`・`L-SECURITY`・`L-CLAIM` の Codex による評価がすべての試行に失敗した | 文書の `failure` 記録と `model-call` 記録、または層の `model-call` 記録で理由を確認する |
| `undecided impact-limit` | 影響文書が `impact.maxImpactedDocs` を超えた | `--full` で実行するか、`impact.maxImpactedDocs` を上げるか、文書を追加する経路（`impact.map`・`impact.ssotSources`・`impact.heuristics`・`changes.regressionRecheck`）を絞る |
| `undecided corpus-unreadable` | 検証用 mirror の準備中に corpus 内の文書を読めなかった（Claude Code の agent backend） | file を読めるようにする（権限、壊れた symlink）して再実行 |
| `undecided sandbox-unavailable` | `sandbox-exec` のない環境で `projectChecks` を設定 | Linux では `projectChecks` を空に |
| `undecided workflow-adapter-unavailable` | 文書を Claude Code の agent 経由で検証する `extended` で、使える Codex がない | Codex を使えるようにする |
| `undecided codex-unavailable` | 文書を agy 経由で検証する `extended` で、使える Codex がない | Codex を使えるようにする |
| `undecided abandoned` | run が放棄された | 再実行 |
| `REFUSED worktree-modified` | 大目に見られない作業木の変化があり、`worktreeDiff` には拒否した path だけが並ぶ | 書込み元を止めて再実行するか、Git が無視する場所を `worktree.tolerateChangesIn` に書くか、書き出し先を作業木の外へ移す。`worktreeDiffIgnoredOnly` が true のときは report の案内を参照 |
| `REFUSED config-drift` | run 中に `.claude/docaudit.json` が変わった | 再実行 |
| その他の `REFUSED` | 封印された run の契約が整合性検査に失敗（lease・封印・証拠ハッシュ・層の集合・backend・judgement・adversarial と claim の整合性） | `runs/<runId>/` を確認して再実行 |

## 11. 困ったとき

- **毎回 `config-invalid:<detail>` になる。** detail が問題のキーを示します。path はリポジトリ相対で、親ディレクトリ参照の `..` という path セグメント（例: `a/../b`）を含まず、`report.path` は `.md` で終わり、path 全体で `<YYYY-MM-DD>` をちょうど 1 つ、`[_NN]` を高々 1 つ含む必要があります。日付が basename 内にある場合はその前に空でない文字列が必要で、日付が directory 側にある場合は basename 全体が空でなければ有効です。`config-invalid:worktree.tolerateChangesIn` なら、path の書き方と Git が無視するかを調べ、追跡 file・corpus 文書・engine の設定・state・report の場所を含まないか確認してください。
- **`config-needs-migration`。** legacy file しかありません。`migrate` を実行してください（第 6 節）。
- **最初の run で front matter や orphan の警告が大量に出る。** どれも non-blocking です。時間をかけて直すか、`documentChecks.layerGlobs` で文書を除外してください。
- **`history-corrupt`。** `history.jsonl` に壊れた行がある・通常ファイルでない・UTF-8 でない・行が長すぎる、`anchors/` 配下の anchor file が読めない・形式が不正、または `CONSISTENT` の run が指す anchor 候補が読めない・記録されたハッシュと一致しない状態です。直すまで engine は履歴を読みも書きもしません。state ディレクトリ全体の複製を取ってから、どの file が壊れているかを特定します。履歴の行が壊れていれば、有効な行を別名で残しつつ履歴 file を退避して新しい履歴 file を始めます。anchor が壊れていれば、その profile の anchor file を退避し、その profile は新たに `--full` の run が必要になります。run の途中で起きた場合（終了値 4）はその run が開いたままなので、次の監査の前に再開か放棄をします。`history.jsonl` 自体が権限で読めない場合は `history-corrupt` ではなく終了値 4・reason `PermissionError` で止まります。権限を直して再実行してください。
- **`mutex-timeout`。** 別の engine process が state の操作（例: `migrate`）をしています。終了後にやり直し、`run-open.json` に開いたままの run があれば再開または放棄してください。**`run-in-progress`。** 別の engine process が run を保持しています。終了を待つか、どの process も保持しなくなってから `run-open.json` にある run を再開または放棄してください。
- **install 後に skill が一覧に出ない。** 新しい Claude Code session を開くか `/reload-plugins` を実行し、`claude plugin list` で確認してください。
- **想定外の file がリポジトリに書かれた。** engine が書くのは report と `.claude/state/docaudit/` 配下の state で、repo 直下の `.mdq/` は作業木の走査から外します。別のツールが run 中に書いても、列挙した場所の中にあり、gate 時点で Git が無視し、封印した corpus と snapshot の外にある変化だけを大目に見ます。それ以外の変化は、新しく作った親ディレクトリも含めて `REFUSED worktree-modified` です。回避策は、対象となる場所を `worktree.tolerateChangesIn` に書くか、書き出し先を作業木の外へ移すことです。entry 数か byte 数の上限を超えた場合は、代わりに `undecided worktree-too-large` になります。
