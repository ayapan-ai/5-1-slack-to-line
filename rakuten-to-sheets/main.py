#!/usr/bin/env python3
"""
楽天市場の商品データ(価格・在庫・ポイント倍率など)を楽天ウェブサービスの
「楽天市場商品検索API」で取得し、Googleスプレッドシートに反映するスクリプト。

スプレッドシートの構成(シートが無ければ自動作成):
- 「監視リスト」: A列に監視したい商品の itemCode(または商品ページURL)を書く
- 「最新」      : 実行のたびに最新状態で上書き。前回価格との差分・実質価格も表示
- 「履歴」      : 実行のたびに追記(価格推移の記録用)

LINE_CHANNEL_ACCESS_TOKEN を設定すると、値下がり・在庫切れ・在庫復活があった時だけ
LINEに通知する(slack-to-line と同じ公式LINEアカウントを流用できる)。

1日1回などの定期実行(launchd / cron)を想定。
"""

from __future__ import annotations

import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import gspread
import requests

# ---------------------------------------------------------------------------
# .env 読み込み(依存ライブラリ不使用の簡易実装)
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
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

RAKUTEN_APP_ID = os.environ.get("RAKUTEN_APP_ID")
RAKUTEN_ACCESS_KEY = os.environ.get("RAKUTEN_ACCESS_KEY")
# 任意: アプリを「許可するWebサイト」で制限している場合、そのURLをOriginヘッダーとして送る
# (「許可されたIPアドレス」で制限している場合は空のままでよい)
RAKUTEN_ORIGIN = os.environ.get("RAKUTEN_ORIGIN")
# 任意: 設定するとURLがアフィリエイトリンクになる
RAKUTEN_AFFILIATE_ID = os.environ.get("RAKUTEN_AFFILIATE_ID")

SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")
GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS", "credentials.json")

# 任意: 設定すると変動があった時だけLINE通知する(slack-to-line と同じ値を使える)
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_TO = os.environ.get("LINE_TO")

RAKUTEN_API_URL = "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20260701"
REQUEST_INTERVAL_SEC = 1.0  # 連続リクエストで429にならないよう1秒空ける
MAX_RETRIES = 3
LINE_TEXT_LIMIT = 4800

SHEET_WATCHLIST = "監視リスト"
SHEET_LATEST = "最新"
SHEET_HISTORY = "履歴"

WATCHLIST_HEADER = ["itemCode または商品URL", "メモ"]
LATEST_HEADER = [
    "itemCode", "商品名", "ショップ", "価格(円)", "前回価格(円)", "変動",
    "ポイント倍率", "実質価格(円・目安)", "送料", "在庫",
    "レビュー平均", "レビュー件数", "URL", "メモ", "更新日時",
]
HISTORY_HEADER = ["取得日時", "itemCode", "商品名", "価格(円)", "ポイント倍率", "在庫"]

REQUIRED_ENV_VARS = {
    "RAKUTEN_APP_ID": RAKUTEN_APP_ID,
    "RAKUTEN_ACCESS_KEY": RAKUTEN_ACCESS_KEY,
    "SPREADSHEET_ID": SPREADSHEET_ID,
}


def check_env() -> None:
    missing = [name for name, value in REQUIRED_ENV_VARS.items() if not value]
    if missing:
        print(f"[ERROR] 必須の環境変数が未設定です: {', '.join(missing)}", file=sys.stderr)
        print("        .env.example を参考に .env を作成してください。", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# 楽天市場商品検索API
# ---------------------------------------------------------------------------

ITEM_URL_PATTERN = re.compile(r"item\.rakuten\.co\.jp/([^/?#]+)/([^/?#]+)")


def normalize_item_code(value: str) -> str | None:
    """'shop:12345' または商品ページURLを itemCode('shop:12345')に正規化する。"""
    value = value.strip()
    if not value:
        return None
    match = ITEM_URL_PATTERN.search(value)
    if match:
        return f"{match.group(1)}:{match.group(2)}"
    if ":" in value and not value.startswith("http"):
        return value
    return None


def fetch_item(item_code: str) -> dict | None:
    """itemCode で商品を1件取得する。見つからなければ None。"""
    params = {
        "applicationId": RAKUTEN_APP_ID,
        "accessKey": RAKUTEN_ACCESS_KEY,
        "itemCode": item_code,
        "format": "json",
        "formatVersion": 2,  # Items が {"Item": {...}} で包まれないフラットな形式
    }
    if RAKUTEN_AFFILIATE_ID:
        params["affiliateId"] = RAKUTEN_AFFILIATE_ID
    headers = {"Origin": RAKUTEN_ORIGIN} if RAKUTEN_ORIGIN else {}

    for attempt in range(1, MAX_RETRIES + 1):
        resp = requests.get(RAKUTEN_API_URL, params=params, headers=headers, timeout=30)

        if resp.status_code == 429:
            # リクエスト過多。少し待ってから再試行する
            time.sleep(REQUEST_INTERVAL_SEC * 2 * attempt)
            continue
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise RuntimeError(f"楽天APIエラー: {resp.status_code} {resp.text}")

        items = resp.json().get("Items", [])
        if not items:
            return None
        item = items[0]
        # formatVersion=1 で返ってきた場合にも対応
        return item.get("Item", item)

    raise RuntimeError(f"楽天APIエラー: 429 リトライ上限に達しました(itemCode={item_code})")


# ---------------------------------------------------------------------------
# 加工
# ---------------------------------------------------------------------------

def effective_price(price: int, point_rate: int) -> int:
    """ポイント還元を差し引いた実質価格の目安。
    pointRate は「何倍」(1倍=1%還元)なので price × pointRate% を差し引く。
    実際のポイントは税抜価格基準・上限ありなので、あくまで比較用の目安。
    """
    return price - price * point_rate // 100


def to_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def build_latest_row(
    item_code: str, item: dict | None, memo: str, prev: dict | None, now: str
) -> list:
    prev_price = to_int(prev.get("価格(円)")) if prev else None

    if item is None:
        return [
            item_code, "取得不可(販売終了・itemCode誤りの可能性)", "", "", prev_price or "",
            "", "", "", "", "取得不可", "", "", "", memo, now,
        ]

    price = int(item["itemPrice"])
    point_rate = int(item.get("pointRate") or 1)

    if prev_price is None:
        change = "新規"
    elif price < prev_price:
        change = f"↓ {prev_price - price:,}円"
    elif price > prev_price:
        change = f"↑ {price - prev_price:,}円"
    else:
        change = "→"

    return [
        item_code,
        item["itemName"],
        item.get("shopName", ""),
        price,
        prev_price if prev_price is not None else "",
        change,
        f"{point_rate}倍",
        effective_price(price, point_rate),
        "送料込" if item.get("postageFlag") == 0 else "送料別",
        "在庫あり" if item.get("availability") == 1 else "在庫なし",
        item.get("reviewAverage", ""),
        item.get("reviewCount", ""),
        item.get("affiliateUrl") or item.get("itemUrl", ""),
        memo,
        now,
    ]


def detect_alerts(rows: list[list], prev_by_code: dict[str, dict]) -> list[str]:
    """LINE通知用に、値下がり・在庫切れ・在庫復活の行を文章にする。"""
    alerts: list[str] = []
    for row in rows:
        record = dict(zip(LATEST_HEADER, row))
        prev = prev_by_code.get(record["itemCode"])
        if not prev:
            continue
        name = str(record["商品名"])[:40]
        stock, prev_stock = record["在庫"], prev.get("在庫")

        if str(record["変動"]).startswith("↓"):
            alerts.append(f"💰値下がり {record['変動']}\n{name}\n→ {record['価格(円)']:,}円\n{record['URL']}")
        if stock == "在庫なし" and prev_stock == "在庫あり":
            alerts.append(f"⚠在庫切れ\n{name}\n{record['URL']}")
        if stock == "在庫あり" and prev_stock == "在庫なし":
            alerts.append(f"✅在庫復活\n{name}\n{record['URL']}")
    return alerts


# ---------------------------------------------------------------------------
# Googleスプレッドシート
# ---------------------------------------------------------------------------

def open_spreadsheet() -> gspread.Spreadsheet:
    credentials_path = Path(GOOGLE_CREDENTIALS)
    if not credentials_path.is_absolute():
        credentials_path = BASE_DIR / credentials_path
    gc = gspread.service_account(filename=str(credentials_path))
    return gc.open_by_key(SPREADSHEET_ID)


def get_or_create_sheet(book: gspread.Spreadsheet, title: str, header: list[str]) -> gspread.Worksheet:
    try:
        return book.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = book.add_worksheet(title=title, rows=1000, cols=len(header))
        ws.update(range_name="A1", values=[header])
        ws.freeze(rows=1)
        print(f"[INFO] シート「{title}」を作成しました")
        return ws


def read_watchlist(ws: gspread.Worksheet) -> list[tuple[str, str]]:
    """監視リストから (itemCode, メモ) の一覧を返す。重複・不正な値は除外する。"""
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for i, row in enumerate(ws.get_all_values()[1:], start=2):
        if not row or not row[0].strip():
            continue
        code = normalize_item_code(row[0])
        if code is None:
            print(f"[WARN] 監視リスト{i}行目を読み取れません(スキップ): {row[0]}")
            continue
        if code in seen:
            continue
        seen.add(code)
        result.append((code, row[1].strip() if len(row) > 1 else ""))
    return result


def read_previous(ws: gspread.Worksheet) -> dict[str, dict]:
    """前回の「最新」シートを itemCode -> 行データ のdictで返す。"""
    return {
        str(r["itemCode"]): r
        for r in ws.get_all_records(expected_headers=LATEST_HEADER)
        if r.get("itemCode")
    }


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
        url = "https://api.line.me/v2/bot/message/push"
        payload["to"] = LINE_TO
    else:
        url = "https://api.line.me/v2/bot/message/broadcast"

    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"LINE送信エラー: {resp.status_code} {resp.text}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    check_env()

    book = open_spreadsheet()
    ws_watch = get_or_create_sheet(book, SHEET_WATCHLIST, WATCHLIST_HEADER)
    ws_latest = get_or_create_sheet(book, SHEET_LATEST, LATEST_HEADER)
    ws_history = get_or_create_sheet(book, SHEET_HISTORY, HISTORY_HEADER)

    watchlist = read_watchlist(ws_watch)
    if not watchlist:
        print(f"[INFO] シート「{SHEET_WATCHLIST}」のA列に監視したい商品を追加してください。終了します。")
        return

    prev_by_code = read_previous(ws_latest)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"[INFO] {len(watchlist)}件の商品を取得します")

    latest_rows: list[list] = []
    history_rows: list[list] = []
    for i, (code, memo) in enumerate(watchlist):
        if i > 0:
            time.sleep(REQUEST_INTERVAL_SEC)
        item = fetch_item(code)
        row = build_latest_row(code, item, memo, prev_by_code.get(code), now)
        latest_rows.append(row)
        if item is not None:
            record = dict(zip(LATEST_HEADER, row))
            history_rows.append([
                now, code, record["商品名"], record["価格(円)"],
                record["ポイント倍率"], record["在庫"],
            ])

    # 「最新」は上書き、「履歴」は追記
    ws_latest.clear()
    ws_latest.update(
        range_name="A1",
        values=[LATEST_HEADER] + latest_rows,
        value_input_option="USER_ENTERED",
    )
    if history_rows:
        ws_history.append_rows(history_rows, value_input_option="USER_ENTERED")
    print(f"[INFO] スプレッドシートを更新しました(最新{len(latest_rows)}件 / 履歴+{len(history_rows)}件)")

    if LINE_CHANNEL_ACCESS_TOKEN:
        alerts = detect_alerts(latest_rows, prev_by_code)
        if alerts:
            send_to_line(f"【楽天 価格・在庫ウォッチ {now}】\n\n" + "\n\n".join(alerts))
            print(f"[INFO] LINEへ{len(alerts)}件の変動を通知しました")
        else:
            print("[INFO] 通知対象の変動はありませんでした")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - 実行失敗はログに出して非ゼロ終了する
        print(f"[FATAL] {exc}", file=sys.stderr)
        sys.exit(1)
