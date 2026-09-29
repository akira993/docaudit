---
name: audit
description: Run the documentation audit engine with a selected profile.
argument-hint: "[--full] [--accept-baseline] [--profile focused|standard|extended] [--backend agy|codex|claude] [--fast]"
---
`python3 -B "$CLAUDE_SKILL_DIR/engine" audit --ask-backend $ARGUMENTS` を実行する。
stdout の最終行にある `nextAction` JSON だけを次の制御に使う。起動し直した場合も同じ。
`choose-backend` なら、質問用の道具で `choices` をそのまま選択肢に出す（先頭を推奨とする）。
答えがあれば `python3 -B "$CLAUDE_SKILL_DIR/engine" audit --backend <backend> $ARGUMENTS` を実行する（`fast` が true なら `--fast` も付ける）。
答えずに閉じられたら、`choices` の先頭の `backend` で同じコマンドを実行する。
質問用の道具が使えない、または拒否されたら、`python3 -B "$CLAUDE_SKILL_DIR/engine" audit $ARGUMENTS` を実行する。
`invoke-workflow` の間は `Workflow({name: "docaudit:docaudit-verify", args: {runId, requestPath}})`、続けて `python3 -B "$CLAUDE_SKILL_DIR/engine" resume <runId>` を実行する。
この組を最大 3 回繰り返し、超えたら `python3 -B "$CLAUDE_SKILL_DIR/engine" resume <runId> --abandon` を実行する。
`done` または `abort` なら outcome、reason、reportPath の要約を表示して終了する。
本文に列挙したコマンド以外を実行しない。`done`／`abort`（`undecided`・`REFUSED` を含む）の要約を表示したら、追加の調査コマンド（`git status` 等）を実行せず終了する。
