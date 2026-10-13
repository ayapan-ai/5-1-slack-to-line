#!/usr/bin/env python3
"""
指定した楽天ショップの取扱商品から、レビュー件数が多い順に上位N件を取得し、
Googleスプレッドシートの「レビュー上位_<ショップコード>」シートに書き出す。

使い方:
    python3 shop_top_reviews.py irisplaza-r        # 上位5件
    python3 shop_top_reviews.py irisplaza-r 10     # 上位10件

ショップコードは商品URL https://item.rakuten.co.jp/【ここ】/... の部分。
設定(.env)・API呼び出し・シート操作は main.py と共通。
"""

from __future__ import annotations

import sys
from datetime import datetime

import requests

import main as m

HEADER = ["順位", "商品コード", "商品名", "価格(円)", "在庫状況", "商品URL", "レビュー件数", "レビュー平均", "取得日時"]


def fetch_top_reviewed(shop_code: str, count: int) -> list[dict]:
    params = {
        "applicationId": m.RAKUTEN_APP_ID,
        "accessKey": m.RAKUTEN_ACCESS_KEY,
        "shopCode": shop_code,
        "sort": "-reviewCount",  # レビュー件数の多い順
        "hits": count,
        "format": "json",
        "formatVersion": 2,
    }
    if m.RAKUTEN_AFFILIATE_ID:
        params["affiliateId"] = m.RAKUTEN_AFFILIATE_ID
    headers = {"Origin": m.RAKUTEN_ORIGIN} if m.RAKUTEN_ORIGIN else {}

    resp = requests.get(m.RAKUTEN_API_URL, params=params, headers=headers, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"楽天APIエラー: {resp.status_code} {resp.text}")
    return resp.json().get("Items", [])


def main() -> None:
    if len(sys.argv) < 2:
        print("使い方: python3 shop_top_reviews.py <ショップコード> [件数(1〜30)]", file=sys.stderr)
        sys.exit(1)
    shop_code = sys.argv[1]
    count = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    if not 1 <= count <= 30:
        sys.exit("件数は1〜30で指定してください(APIの1回あたりの上限が30件)")

    m.check_env()
    items = fetch_top_reviewed(shop_code, count)
    if not items:
        print(f"[INFO] ショップ '{shop_code}' の商品が見つかりませんでした。ショップコードを確認してください。")
        return

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    rows = [
        [
            rank,
            item["itemCode"],
            item["itemName"],
            int(item["itemPrice"]),
            "在庫あり" if item.get("availability") == 1 else "在庫なし",
            item.get("affiliateUrl") or item["itemUrl"],
            item.get("reviewCount", 0),
            item.get("reviewAverage", ""),
            now,
        ]
        for rank, item in enumerate(items, start=1)
    ]

    shop_name = items[0].get("shopName", shop_code)
    title = f"レビュー上位_{shop_code}"
    ws = m.get_or_create_sheet(m.open_spreadsheet(), title, HEADER)
    ws.clear()
    ws.update(range_name="A1", values=[HEADER] + rows, value_input_option="USER_ENTERED")

    print(f"[INFO] {shop_name} のレビュー件数上位{len(rows)}件をシート「{title}」に書き出しました")
    for r in rows:
        print(f"  {r[0]}. {r[2][:40]}  {r[3]:,}円  レビュー{r[6]:,}件(★{r[7]})")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - 実行失敗はログに出して非ゼロ終了する
        print(f"[FATAL] {exc}", file=sys.stderr)
        sys.exit(1)
