# Slack要約 → LINE 自動送信(1日1回)

指定したSlackチャンネルの1日分のメッセージを取得し、OpenAI(ChatGPT API)で要約して、
「4-3AIP連携課題」で使っているLINE公式アカウント宛(管理者個人push)に配信するスクリプトです。

```
launchd(毎日1回) ─▶ Slack API(conversations.history)
                        └▶ OpenAI API(要約)
                              └▶ LINE Messaging API(push送信)
```

- `.env` は **「4-3AIP連携課題」の`.env`から`SLACK_BOT_TOKEN` / `SLACK_CHANNEL` /
  `OPENAI_API_KEY` / `LINE_CHANNEL_ACCESS_TOKEN` / `LINE_TO` を複製済み**です
  (新しく取得し直す必要はありません)。
- 送信先は `LINE_TO`(4-3と同じ管理者個人のuserId)宛のpushです。`LINE_TO`を空にすると
  公式LINEアカウントの友だち全員へのブロードキャスト送信に切り替わります。
- `state.json` に前回実行時の最終メッセージtsを保存し、重複送信を防ぎます。

---

## 0. 今回必要な作業は実質1つだけ

Slack/LINE/OpenAIの認証情報はすべて複製済みなので、残るセットアップは
**Slack Botの読み取り権限(スコープ)の確認**だけです。新しいAPIキーの発行は不要です。

## 1. Slack側 - 読み取りスコープの確認

4-3の Slack Bot はメッセージ**投稿**(`chat.postMessage`)用に作られているため、
メッセージ**読み取り**に必要なスコープが無い可能性があります。以下を確認してください。

1. https://api.slack.com/apps → 4-3で作ったアプリを選択
2. 左メニュー **OAuth & Permissions** → *Scopes* → *Bot Token Scopes* に以下が無ければ追加
   - `channels:history`(パブリックチャンネルの発言取得)
   - `channels:read`
   - `users:read`(発言者IDを表示名に変換するため)
   - 対象がプライベートチャンネルなら `groups:history` / `groups:read` も追加
3. スコープを追加した場合は画面上部の **Reinstall to Workspace** を実行
   (トークン自体は変わらないことが多いですが、変わった場合は`.env`の`SLACK_BOT_TOKEN`を更新)
4. 対象チャンネル(`.env`の`SLACK_CHANNEL`)にBotが参加しているか確認
   - 未参加なら対象チャンネルで `/invite @アプリ名` を実行
   - 参加済みなら対応不要(4-3で投稿していたチャンネルと同じなら、すでに参加しているはず)

## 2. LINE側 - 確認のみ(追加作業なし)

`LINE_CHANNEL_ACCESS_TOKEN` / `LINE_TO` は4-3と共通なので、追加の設定は不要です。
4-3の`line_push.py`で送信できていれば、このスクリプトからのpushも同じ条件で届きます。

## 3. OpenAI側 - 確認のみ(追加作業なし)

`OPENAI_API_KEY` も4-3と共通のキーを再利用しているため、新規発行は不要です。
要約に使うモデルは `.env` の `OPENAI_MODEL`(デフォルト `gpt-4o-mini`)で変更できます。

## 4. セットアップ

```bash
cd "/path/to/slack-to-line"

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

`.env` は全キー複製済みなので`cp .env.example .env`も追記作業も不要です。

## 5. 動作確認(手動実行)

```bash
source venv/bin/activate
python3 main.py
```

- 初回実行時は「24時間前」からのメッセージを対象に要約・送信します
- 新着が無い場合・要約対象の発言(人間の発言)が無い場合はLINE送信せず終了します
- 成功すると `state.json` が作成/更新されます(次回はこの続きから取得)
- `Slack APIエラー: missing_scope` が出たら手順1のスコープ追加・Reinstallを確認

## 6. 1日1回の定期実行(launchd)

1. `launchd/com.slacktoline.dailysummary.plist` を開き、パスが実際の配置先と
   合っているか確認(デフォルトは `/path/to/slack-to-line`)
2. 実行時刻を変更したい場合は `StartCalendarInterval` の `Hour` / `Minute` を編集(デフォルト9:00)
3. plistを配置して読み込む

```bash
cp launchd/com.slacktoline.dailysummary.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.slacktoline.dailysummary.plist
```

4. 動作確認(即時実行してログを見る)

```bash
launchctl start com.slacktoline.dailysummary
cat launchd.out.log launchd.err.log run.log
```

- 停止/削除したい場合:
  ```bash
  launchctl unload ~/Library/LaunchAgents/com.slacktoline.dailysummary.plist
  rm ~/Library/LaunchAgents/com.slacktoline.dailysummary.plist
  ```
- **注意**: launchdはMacがスリープ/シャットダウンしていると実行されません。
  起動中のMacでの常時稼働を前提とした方式です。PCを閉じても確実に毎日動かしたい
  場合はGitHub Actionsの`schedule`トリガー(cron)への移行を検討してください。

---

## トラブルシューティング

| 症状 | 原因・対処 |
|---|---|
| `Slack APIエラー: not_in_channel` | Botを対象チャンネルに招待していない(`/invite @アプリ名`) |
| `Slack APIエラー: missing_scope` | `channels:history`等のスコープが無い。追加後に**Reinstall to Workspace**が必要 |
| `Slack APIエラー: invalid_auth` | `SLACK_BOT_TOKEN` が誤り、または失効している |
| `チャンネル '...' が見つかりません` | `SLACK_CHANNEL`をチャンネル名で指定した場合、Botがそのチャンネルに未参加 |
| `LINE送信エラー: 401` | `LINE_CHANNEL_ACCESS_TOKEN` が誤り、または失効している(再発行) |
| `LINE送信エラー: 400` | テキストが長すぎる、または`LINE_TO`のID誤り |
| Botや連携アプリの発言が要約に混ざる | `main.py`の`build_log_text`で`bot_id`を持つ発言を除外済みだが、
人型Botの発言は`bot_id`が無いことがあるため、必要なら追加のフィルタを入れる |
| 要約が長すぎる/短すぎる | `summarize_with_openai`内のsystemプロンプトの文字数指定を調整 |
| `openai.AuthenticationError` | `OPENAI_API_KEY`が誤り、または失効している(4-3側で動作確認) |
| `openai.RateLimitError` | OpenAIの利用上限に達している。しばらく待って再実行、または上限を確認 |

---

## ファイル構成

```
slack-to-line/
├── main.py                  # 本体スクリプト
├── requirements.txt         # 依存パッケージ
├── .env                      # 4-3から全キー複製済み(追記不要・.gitignore対象)
├── .env.example              # 環境変数テンプレート(参考用)
├── .gitignore                # .env / state.json / ログを除外
├── run.sh                    # launchd/cron用の実行ラッパー
├── state.json                 # 前回実行時のts(自動生成・.gitignore対象)
└── launchd/
    └── com.slacktoline.dailysummary.plist  # 毎日1回実行の定義
```
