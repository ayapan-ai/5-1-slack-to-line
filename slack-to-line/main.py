#!/usr/bin/env python3
"""
Slack の指定チャンネルの新着メッセージを取得し、OpenAI(ChatGPT API)で要約して
LINE Messaging API(公式LINEアカウント)から配信するスクリプト。

1日1回の定期実行(launchd / cron)を想定。
state.json に前回実行時点の最終メッセージ ts を保存し、
次回はそれ以降の新着のみを対象にすることで重複送信を防ぐ。

環境変数名は「4-3AIP連携課題」の.envと揃えてあるので、
SLACK_BOT_TOKEN / SLACK_CHANNEL / OPENAI_API_KEY /
LINE_CHANNEL_ACCESS_TOKEN / LINE_TO をそのままコピーして使える。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import requests
from openai import OpenAI

# ---------------------------------------------------------------------------
# .env 読み込み(依存ライブラリ不使用の簡易実装)
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "state.json"
ENV_FILE = BASE_DIR / ".env"


def load_dotenv_if_present() -> None:
    """.env があれば読み込んで os.environ に設定する。"""
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


# 設定を読み込む定数を参照する前に必ず.envを読み込んでおく
load_dotenv_if_present()

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
# 「4-3AIP連携課題」の.envと同じ変数名(SLACK_CHANNEL)。
# チャンネルID("C0...")・チャンネル名("#general")のどちらでもよい
# (読み取りAPIはID指定が必須なため、名前の場合は実行時に自動でIDへ解決する)。
SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL") or os.environ.get("SLACK_CHANNEL_ID")
# 「4-3AIP連携課題」の.envに既にあるOPENAI_API_KEYを再利用する。
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
# 「4-3AIP連携課題」の.envと同じ変数名(LINE_TO)。
# 指定したuserId/groupIdへpush送信。未設定なら公式LINEアカウントの
# 友だち全員へブロードキャスト送信する。
LINE_TO = os.environ.get("LINE_TO") or os.environ.get("LINE_TARGET_ID")

# 要約に使うモデル。コスト・速度を優先したモデルをデフォルトにしつつ、
# .envの OPENAI_MODEL で上書きできるようにしている。
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
SLACK_PAGE_LIMIT = 200
LINE_TEXT_LIMIT = 4800  # LINEの上限5000文字より少し余裕を持たせる

REQUIRED_ENV_VARS = {
    "SLACK_BOT_TOKEN": SLACK_BOT_TOKEN,
    "SLACK_CHANNEL": SLACK_CHANNEL,
    "OPENAI_API_KEY": OPENAI_API_KEY,
    "LINE_CHANNEL_ACCESS_TOKEN": LINE_CHANNEL_ACCESS_TOKEN,
}


def check_env() -> None:
    missing = [name for name, value in REQUIRED_ENV_VARS.items() if not value]
    if missing:
        print(f"[ERROR] 必須の環境変数が未設定です: {', '.join(missing)}", file=sys.stderr)
        print("        .env.example を参考に .env を作成してください。", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# state (前回実行時の最終タイムスタンプ)
# ---------------------------------------------------------------------------

def load_last_ts() -> str:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            return str(data.get("last_ts", "0"))
        except (json.JSONDecodeError, OSError):
            pass
    # state が無い初回は「24時間前」から取得する
    return str(time.time() - 24 * 60 * 60)


def save_last_ts(ts: str) -> None:
    STATE_FILE.write_text(
        json.dumps({"last_ts": ts}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------

def resolve_channel_id(channel: str) -> str:
    """'#general' のようなチャンネル名が渡された場合はIDへ解決する。
    すでにID形式("C"/"G"始まり)ならそのまま返す。
    """
    if not channel.startswith("#"):
        return channel

    target_name = channel.lstrip("#")
    url = "https://slack.com/api/conversations.list"
    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
    cursor: str | None = None

    while True:
        params = {"limit": 200, "types": "public_channel,private_channel"}
        if cursor:
            params["cursor"] = cursor

        resp = requests.get(url, headers=headers, params=params, timeout=30)
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Slack APIエラー(チャンネル名解決): {data.get('error')}")

        for ch in data.get("channels", []):
            if ch.get("name") == target_name:
                return ch["id"]

        cursor = data.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    raise RuntimeError(f"チャンネル '{channel}' が見つかりません(Botが未参加の可能性があります)")


def fetch_slack_messages(channel_id: str, oldest_ts: str) -> list[dict]:
    """channel の oldest_ts より新しいメッセージを全件取得する(ページネーション対応)。"""
    url = "https://slack.com/api/conversations.history"
    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
    messages: list[dict] = []
    cursor: str | None = None

    while True:
        params = {
            "channel": channel_id,
            "oldest": oldest_ts,
            "limit": SLACK_PAGE_LIMIT,
        }
        if cursor:
            params["cursor"] = cursor

        resp = requests.get(url, headers=headers, params=params, timeout=30)
        data = resp.json()

        if not data.get("ok"):
            error = data.get("error", "unknown_error")
            raise RuntimeError(f"Slack APIエラー: {error}")

        messages.extend(data.get("messages", []))

        cursor = data.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    # Slackは新しい順で返るので古い順に並べ替える
    messages.sort(key=lambda m: float(m.get("ts", 0)))
    return messages


def resolve_user_names(user_ids: set[str]) -> dict[str, str]:
    """user_id -> 表示名 のマップを作る。失敗しても処理は続行する。"""
    url = "https://slack.com/api/users.info"
    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
    names: dict[str, str] = {}

    for uid in user_ids:
        try:
            resp = requests.get(url, headers=headers, params={"user": uid}, timeout=10)
            data = resp.json()
            if data.get("ok"):
                profile = data["user"].get("profile", {})
                names[uid] = (
                    profile.get("display_name")
                    or profile.get("real_name")
                    or data["user"].get("name", uid)
                )
            else:
                names[uid] = uid
        except requests.RequestException:
            names[uid] = uid

    return names


def build_log_text(messages: list[dict]) -> tuple[str, str | None]:
    """Slackメッセージ一覧を 'ユーザー: テキスト' 形式のログ文字列に変換する。
    戻り値は (ログ文字列, 対象期間内の最新メッセージts)。
    """
    last_ts = messages[-1]["ts"] if messages else None

    # 他Botの投稿(通知連携など)は要約対象から除外
    real_messages = [
        m for m in messages
        if not m.get("bot_id") and m.get("type") == "message" and m.get("text")
    ]

    if not real_messages:
        return "", last_ts

    user_ids = {m["user"] for m in real_messages if m.get("user")}
    names = resolve_user_names(user_ids)

    lines = []
    for m in real_messages:
        user = names.get(m.get("user", ""), m.get("user", "unknown"))
        text = m.get("text", "").replace("\n", " ")
        lines.append(f"{user}: {text}")

    return "\n".join(lines), last_ts


# ---------------------------------------------------------------------------
# OpenAI (要約)
# ---------------------------------------------------------------------------

def summarize_with_openai(log_text: str) -> str:
    client = OpenAI(api_key=OPENAI_API_KEY)

    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "あなたはSlackの1日分のチャンネルログを要約するアシスタントです。"
                    "以下のルールで日本語の要約を作成してください。\n"
                    "- 全体で400〜600文字程度\n"
                    "- 話題ごとに箇条書きでまとめる\n"
                    "- 決定事項や要対応タスクがあれば「▶要対応」として明記する\n"
                    "- 雑談のみで要約すべき内容が無い場合は「本日は特に共有事項はありませんでした」と出力する\n"
                    "- 前置きや確認の言葉は書かず、要約本文のみを出力する"
                ),
            },
            {"role": "user", "content": log_text},
        ],
        max_tokens=1024,
    )

    return (response.choices[0].message.content or "").strip()


# ---------------------------------------------------------------------------
# LINE
# ---------------------------------------------------------------------------

def send_to_line(text: str) -> None:
    if len(text) > LINE_TEXT_LIMIT:
        text = text[: LINE_TEXT_LIMIT - 20] + "\n…(以下省略)"

    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {"messages": [{"type": "text", "text": text}]}

    if LINE_TO:
        # 指定した個人/グループへpush送信(例: 4-3AIP連携課題のLINE_TO)
        url = "https://api.line.me/v2/bot/message/push"
        payload["to"] = LINE_TO
    else:
        # 公式LINEアカウントの友だち全員へブロードキャスト送信
        url = "https://api.line.me/v2/bot/message/broadcast"

    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"LINE送信エラー: {resp.status_code} {resp.text}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    check_env()

    channel_id = resolve_channel_id(SLACK_CHANNEL)
    oldest_ts = load_last_ts()
    print(f"[INFO] {oldest_ts} 以降のSlackメッセージを取得します(channel={channel_id})")

    messages = fetch_slack_messages(channel_id, oldest_ts)

    if not messages:
        print("[INFO] 新着メッセージはありませんでした。終了します。")
        return

    log_text, last_ts = build_log_text(messages)

    if not log_text:
        print("[INFO] 要約対象のメッセージ(人間の発言)がありませんでした。終了します。")
        if last_ts:
            save_last_ts(last_ts)
        return

    print(f"[INFO] {len(messages)}件のメッセージを要約します")
    summary = summarize_with_openai(log_text)

    today = time.strftime("%Y/%m/%d")
    final_text = f"【Slack要約 {today}】\n\n{summary}"

    send_to_line(final_text)
    print("[INFO] LINEへ送信しました")

    if last_ts:
        save_last_ts(last_ts)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - 実行失敗はログに出して非ゼロ終了する
        print(f"[FATAL] {exc}", file=sys.stderr)
        sys.exit(1)
