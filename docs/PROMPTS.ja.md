# docaudit プロンプト例

English: [PROMPTS.md](PROMPTS.md)

Claude Code の中で docaudit を動かすための、コピーして使えるプロンプト集です。各例には期待される結果を添えているので、正常な結果と問題を見分けられます。skill は `/docaudit:audit`、オプションは `--full` と `--profile focused|standard|extended` です。監査の報告はすべて読み取り専用で、docaudit が文書を編集することはありません。見つかった問題を直すのは別の作業で、明示的に依頼します。

以下の engine コマンドは skills-dir の path `~/.claude/skills/docaudit/skills/audit/engine` を使います。marketplace 経由で install した場合は README の install 節にある engine path に読み替えてください。

## 1. リポジトリの最初の監査

設定 file ができてから使います（[ADOPTION.ja.md](ADOPTION.ja.md) 第 4 節）。

```
このリポジトリで /docaudit:audit --full を実行して。終わったら公開された report を開き、失敗と判定された文書を要約と一緒に列挙して。その後、それぞれについて .claude/state/docaudit/runs/<runId>/ の証拠台帳にある judgement の証拠文字列を見せて（Claude Code の agent で検証した run では requests/<seq>/judgements/ にもある）。文書は編集しないで。
```

期待: `NEEDS_FIX` または `CONSISTENT`。corpus の最初の run では `NEEDS_FIX` が普通です。証拠台帳は原文のまま、履歴の judgement は伏字です。`undecided backend-unavailable` は能力検出で backend がひとつも見つからなかったという意味で、Claude Code の外で Codex CLI が使えないときに起きます。文書判定だけで `NEEDS_FIX` のままなら、`/docaudit:audit --full --accept-baseline` がその run から anchor を書けます。その他の blocking finding は先に直してください。

## 2. 最初の監査の指摘を直して、もう一度

```
最新の docaudit report は reports/<file>.md にある。失敗した文書ごとに、食い違いとそれが矛盾しているコードを見せて、文書を正しくする最小の編集を提案して。私が承認した編集だけを適用して、それ以外は触らないで。その後 /docaudit:audit --full をもう一度実行して。
```

期待: すべての文書が合格し、blocking な所見が残っていなければ `CONSISTENT`（切れたローカルリンクや失敗した project check があると run は `NEEDS_FIX` のままです）。`CONSISTENT` の run が profile の anchor を書き、以後の run は incremental にできます。

## 3. 変更後の通常の incremental な監査

```
直前の commit で file を変更した。/docaudit:audit を実行して、影響を受けた文書・verdict・report の path を教えて。
```

期待: 文書がまだ一致していれば `CONSISTENT`、そうでなければ失敗した文書付きの `NEEDS_FIX`。`undecided anchor-missing` はこの profile にまだ anchor がないという意味なので、先に例 1 を `--full`（または `--full --accept-baseline`）で実行してください。`undecided impact-limit` は変更の影響文書が `impact.maxImpactedDocs` を超えたという意味で、`--full` で実行するか impact map を絞ります。

## 4. focused profile での素早い確認

```
/docaudit:audit --profile focused --full を実行して、文書検証の結果だけを要約して。プロジェクトチェックは今は省いて。
```

期待: `focused` profile は scope と文書検証だけを走らせます。独自の anchor を持つので、最初の run はやはり `--full` が必要です。

## 5. extended profile でのリリース前総点検

Codex CLI が利用可能性検査に合格し、`enabledLayers` が 7 層すべてを列挙していることが必要です。

```
/docaudit:audit --profile extended --full を実行して。verdict を報告した後、adversarial と security の所見を重大度順に列挙し、confirmed な claim ごとに証拠 file と行を示して。
```

期待: `CONSISTENT`、または confirmed な claim が blocking として示された `NEEDS_FIX`。engine が `capability-missing:<layer>` で拒否したら、その層を `enabledLayers` に追加します。run が `undecided workflow-adapter-unavailable` で終わったら、検証が Codex ではなく Claude Code の agent を経由したということで、agent はレビュー層を実行できません。

## 6. 旧設定の移行

```
このリポジトリには legacy な .claude/doc-audit.json がある。docaudit の migrate サブコマンドを --dry-run 付きで実行して（python3 ~/.claude/skills/docaudit/skills/audit/engine migrate --dry-run --repo-root .）、変換後の設定・counts・破棄されるキーを見せて、そこで止まって。まだ何も書かないで。
```

確認後に:

```
同じ migrate コマンドを --dry-run なしで実行して、できあがった .claude/docaudit.json を見せて。その後 /docaudit:audit --full を実行して。
```

期待: dry run は `convertible`、または同じ legacy 入力での移行完了がすでに記録されていれば `unchanged` で終了値 0。終了値 1 は `not-convertible` で、結果の `reason` に理由が出ます。本番の移行は新しい file を書きます。移行完了後に同じ legacy 入力でもう一度実行すると、run が開いていても `unchanged` を報告して何も書きません。それ以外では run が開いている間は拒否され（`migration-run-open`）、変換結果とバイト列が異なる既存の `.claude/docaudit.json` の置き換えも拒否し（`migration-target-exists`）、移行完了後に legacy 入力が変わっていれば `migration-input-changed` で拒否します。

## 7. undecided や REFUSED の結果を理解する

```
直前の /docaudit:audit が outcome <undecided|REFUSED>、reason <reason> で終わった。plugin ディレクトリの docs/CONFIG-1.0.0.md を使って、docaudit 1.1.0 でこの理由が何を意味するか説明し、anchor が動いたかどうかと、次に何を実行すべきかを教えて。file は変更しないで。
```

期待: 説明と次の一手。よくある場合: `anchor-missing`（`--full` で実行）、`worktree-modified`（監査中に何かがリポジトリへ書き込んだ。他のツールを止めて再実行）、`config-drift`（run 中に設定が変わった。再実行）、`run-awaiting-external` または `run-interrupted-resume-required`（前の run が開いたまま。例 8 を参照）。

## 8. 開いたままの run を再開または放棄する

skill は前の session から残った run を回復しないので、明示的に行います。

```
前の run が開いたままで docaudit が開始を拒否した。結果行の reason は <run-in-progress|run-awaiting-external|run-interrupted-resume-required> だった。.claude/state/docaudit/run-open.json を読んで run ID を教えて。判断はその file の state ではなく reason で行って: reason が run-in-progress なら、別の process が run を保持しているので待つように言って。run-awaiting-external か run-interrupted-resume-required なら、次のコマンドで再開して: python3 ~/.claude/skills/docaudit/skills/audit/engine resume <runId> --repo-root . 結果が nextAction invoke-workflow なら、その request に対して docaudit の検証 workflow を実行してもう一度再開して。再開が失敗し続けるなら、同じコマンドに --abandon を付けて放棄し、理由を教えて。
```

期待: run が `closed` に到達し、次の監査を始められます。放棄は verdict なしで run を closed として記録し（`undecided abandoned`）、anchor は動きません。

## 9. 定期実行・非対話での実行

Claude Code の外では engine に Codex backend が必要です。shell やスケジューラから:

```sh
python3 ~/.claude/skills/docaudit/skills/audit/engine audit --profile standard --repo-root /path/to/repo
```

出力の最終行を JSON として読み、`outcome` と `reportPath` を使います。終了値 0 は `CONSISTENT`・`NEEDS_FIX`・`undecided`・`REFUSED` のすべてを含むので、判断には終了値ではなく `outcome` を使ってください。終了値 3 は engine が run を開くことを拒否したことで、`reason` に理由が出ます。

skill 自体を非対話で動かすには、リポジトリで Claude Code を print mode で使います。

```sh
claude -p '/docaudit:audit' --permission-mode acceptEdits --allowedTools 'Bash,Read,Grep,Glob,Write,Workflow,Skill,Agent'
```

期待: 対話実行と同じ outcome。session が workflow とその agent を実行できる必要があり、上のツール一覧がそれを許可します。
