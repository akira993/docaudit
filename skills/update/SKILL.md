---
name: update
description: Review docaudit releases, apply an update, restore the previous version, or set update notifications.
argument-hint: "[--to vX.Y.Z] | rollback | notify on|off"
---

# docaudit の更新

結果は利用者が使っている言葉で伝える。どの手順でも engine の最終行に出る JSON を読む。
engine の命令は書いたとおりに実行し、リダイレクト（例: `2>&1`）やパイプ、別の命令を付け足さない。付け足すと、更新の予定を見るだけの命令でも Claude Code が許可を求める。

## 通知の設定

引数が `notify on` または `notify off` なら、指定された方を実行し、通知の設定結果を伝えて終える。

~~~sh
python3 -B "${CLAUDE_PLUGIN_ROOT}/skills/audit/engine" update notify on
python3 -B "${CLAUDE_PLUGIN_ROOT}/skills/audit/engine" update notify off
~~~

## 前の版に戻す

引数が `rollback` なら、まず次の命令を実行する。

~~~sh
python3 -B "${CLAUDE_PLUGIN_ROOT}/skills/audit/engine" update rollback --dry-run
~~~

`from` にある現在の版と `to` にある戻し先の版を示す。戻せない場合は理由を伝えて終える。戻せる場合は次の命令を実行する。Claude Code が許可画面で利用者に承認を求める。利用者が拒否したら、命令を再実行せず、拒否されたと伝えて終える。

復元を待てるよう、実行時の制限時間を 10 分（600,000 ミリ秒）に設定する。

~~~sh
python3 -B "${CLAUDE_PLUGIN_ROOT}/skills/audit/engine" update rollback
~~~

## 更新する

上のどれにも当てはまらない場合は、次の命令を実行する。引数がなければ、版の指定を省く。

~~~sh
python3 -B "${CLAUDE_PLUGIN_ROOT}/skills/audit/engine" update plan $ARGUMENTS
~~~

計画を取得できなければ理由を伝えて終える。`target` の先頭の `v` を外して `installed` と比べる。一致したら、版の指定がなければ最新の版、指定があれば指定された版がすでに入っていると伝えて終える。

更新先（`target`）と、公開時の変更点（`notes`）、変更履歴（`log`）、差分の概要（`diffstat`）を利用者に分かる言葉で示す。差分の概要を取得できなかった理由が `reason` にあれば伝える。

更新する前に、進行中の監査があれば先に終えるか、engine の `resume <runId> --abandon` で中止するよう利用者に伝える。

`<対象>` を `target` の値に置き換える。更新を待てるよう、実行時の制限時間を 10 分（600,000 ミリ秒）に設定する。次の命令を実行すると、Claude Code が許可画面で利用者に承認を求める。利用者が拒否したら、命令を再実行せず、拒否されたと伝えて終える。

~~~sh
python3 -B "${CLAUDE_PLUGIN_ROOT}/skills/audit/engine" update apply --to <対象>
~~~

## 更新・復元の結果を伝える

成功したら、新しいセッションを始めるか `/reload-plugins` を実行すると切り替えた版が読み込まれると伝える。

失敗したら `reason` を伝える。`update-restore-failed` の場合は、実行前の版に戻せなかったことと `installDir`、`asideDir`、`stagingDir` の場所を伝える。それ以外の失敗では、実行前の版が残っていると伝える。

`update-restore-failed` が出たときや、更新・復元が途中で止まったときは、手で戻す方法を伝える。途中で止まって出力が得られない場合は、`${CLAUDE_CONFIG_DIR:-~/.claude}/docaudit-update/history.jsonl` で該当する操作の `outcome: "started"` の行を探し、`asideDir` と `installDir` を確認する。

`versions/` の下にある `asideDir` に実行前の版が残っていれば、そのフォルダを `installDir` の場所へ移すと戻せる。`installDir` が埋まっている場合は、今あるフォルダを先に `skills/` の外の空いている場所へ移す。
