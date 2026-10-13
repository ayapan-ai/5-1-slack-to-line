# 楽天市場 価格・在庫 → Googleスプレッドシート 自動反映

楽天ウェブサービスの「楽天市場商品検索API」で、監視したい商品の価格・在庫・ポイント倍率を取得し、
Googleスプレッドシートに整理して書き込むスクリプトです。
値下がり・在庫切れ・在庫復活があった時だけLINEに通知することもできます(任意)。

```
launchd(毎日1回) ─▶ スプレッドシート「監視リスト」読み込み
                        └▶ 楽天市場商品検索API(1件ずつ・1秒間隔)
                              └▶ スプレッドシート「最新」上書き /「履歴」追記
                                    └▶ (任意) 変動があればLINE通知
```

## スプレッドシートの構成(初回実行時に自動作成)

| シート | 内容 |
|---|---|
| **監視リスト** | A列に itemCode(`shop:12345`)**または商品ページのURL**を貼るだけ。B列はメモ(任意) |
| **最新** | 毎回上書き。価格・前回価格・変動(↓↑→)・ポイント倍率・実質価格(目安)・送料・在庫・レビューなど |
| **履歴** | 毎回追記。価格推移のグラフ作成などに使う |

> 「実質価格」は `価格 − 価格×ポイント倍率%` で計算した比較用の目安です
> (実際のポイントは税抜価格が基準で、上限もあります)。

---

## 1. 楽天側の準備

1. https://webservice.rakuten.co.jp/ に楽天会員IDでログイン
2. 「アプリID発行」でアプリを新規登録
3. 発行された **アプリID(applicationId)** と **アクセスキー(accessKey)** を控える
   - 2026年にAPIが新しいバージョンへ移行し、**アクセスキーが必須**になりました。
     以前に発行したアプリIDだけでは動きません(再登録が必要な場合があります)
4. (任意)楽天アフィリエイトIDを控えておくと、シートのURLがアフィリエイトリンクになります

## 2. Google側の準備

> **セットアップ済み**: 「4-3AIP連携課題」の `service_account.json` を `credentials.json` としてコピー済みで、
> 書き込み先のスプレッドシートにも共有済みです。以下は新しく作り直す場合の手順です。

1. https://console.cloud.google.com/ でプロジェクトを作成(既存のものでも可)
2. 「APIとサービス」→「ライブラリ」で **Google Sheets API** と **Google Drive API** を有効化
3. 「IAMと管理」→「サービスアカウント」→ 作成 →「鍵」タブ →「鍵を追加」→ **JSON**
4. ダウンロードしたJSONを、このフォルダに **`credentials.json`** として置く(`.gitignore`済み)
5. 書き込み先のスプレッドシートを新規作成し、右上「共有」から
   JSON内の `client_email`(`xxx@xxx.iam.gserviceaccount.com`)を **編集者** として追加
6. スプレッドシートのURL `https://docs.google.com/spreadsheets/d/【ここ】/edit` を控える

## 3. セットアップ

```bash
cd "/Users/katouayaka/Documents/5-1実戦課題/rakuten-to-sheets"

python3 -m venv venv           # 作成済みならスキップ
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
```

`.env` に以下を記入します。

| 変数 | 必須 | 内容 |
|---|---|---|
| `RAKUTEN_APP_ID` | ○ | 手順1のアプリID |
| `RAKUTEN_ACCESS_KEY` | ○ | 手順1のアクセスキー |
| `SPREADSHEET_ID` | ○ | 手順2-6のID |
| `GOOGLE_CREDENTIALS` | | JSONキーのパス(デフォルト `credentials.json`) |
| `RAKUTEN_ORIGIN` | | アプリを「許可するWebサイト」で制限している場合のみ、そのURL(IPアドレス制限なら空でよい) |
| `RAKUTEN_AFFILIATE_ID` | | 楽天アフィリエイトID |
| `LINE_CHANNEL_ACCESS_TOKEN` / `LINE_TO` | | LINE通知したい場合。`slack-to-line/.env` と同じ値をコピーすればOK |

## 4. 動作確認(手動実行)

```bash
source venv/bin/activate
python3 main.py
```

1. 初回は3つのシートが作られ、「監視リストに商品を追加してください」と表示されて終了します
2. 「監視リスト」のA2以降に、監視したい商品のURLを貼ります
   (例: `https://item.rakuten.co.jp/shopname/10001234/`)
3. もう一度 `python3 main.py` → 「最新」「履歴」シートに書き込まれれば成功です
4. 2回目以降は「前回価格」「変動」が埋まり、変動があればLINEに通知されます

## 5. 1日1回の定期実行(launchd)

`launchd/com.rakutentosheets.daily.plist` は、このフォルダのパスを設定済みです(毎日9:00に実行)。

```bash
cp launchd/com.rakutentosheets.daily.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.rakutentosheets.daily.plist

# すぐ実行してログを確認
launchctl start com.rakutentosheets.daily
cat run.log launchd.err.log
```

停止する場合:
```bash
launchctl unload ~/Library/LaunchAgents/com.rakutentosheets.daily.plist
rm ~/Library/LaunchAgents/com.rakutentosheets.daily.plist
```

※ Macがスリープ中・シャットダウン中は実行されません。

---

## トラブルシューティング

| 症状 | 原因・対処 |
|---|---|
| `楽天APIエラー: 403 ... Invalid Access Key` | `RAKUTEN_ACCESS_KEY` が誤っているか、未設定 |
| `楽天APIエラー: 403 ... REFERRER_MISSING` | `.env` の `RAKUTEN_ORIGIN` に、アプリ登録時の「許可するWebサイト」のURLを設定 |
| `楽天APIエラー: 403 ... IP関連のエラー` | 「許可されたIPアドレス」と実行中のMacのIPが違う。`curl -4 https://api.ipify.org` で確認し、アプリ設定を更新(自宅回線はIPが変わることがある) |
| `楽天APIエラー: 403 ... HTTP_REFERRER_NOT_ALLOWED` | `RAKUTEN_ORIGIN` が「許可するWebサイト」と一致していない。`https://` から末尾の`/`有無まで同じにする |
| `楽天APIエラー: 400` | itemCodeの形式が誤っている(`ショップID:商品管理番号`) |
| `429 リトライ上限` | リクエストが多すぎる。`REQUEST_INTERVAL_SEC` を2秒などに増やす |
| 最新シートが「取得不可」 | 販売終了・URLの誤り。商品ページを開いて確認 |
| `FileNotFoundError: credentials.json` | 手順2-4のJSONキーが置かれていない |
| `SpreadsheetNotFound` / `PERMISSION_DENIED` | サービスアカウントのメールアドレスにシートを共有していない(手順2-5) |
| `APIError: ... has not been used in project` | Sheets API / Drive API が有効になっていない(手順2-2) |
| 「最新」シートの列を並べ替えたらエラー | 前回値の読み込みに見出し行を使っています。並べ替えたい場合は別シートで `=QUERY(最新!A:O, ...)` などを使う |
| `FutureWarning: Python 3.9` などの警告 | macOS標準Pythonの警告で、動作には影響なし。気になる場合は新しいPythonでvenvを作り直す |

## ファイル構成

```
rakuten-to-sheets/
├── main.py                 # 本体スクリプト
├── requirements.txt        # 依存パッケージ(gspread, requests)
├── .env.example            # 環境変数テンプレート
├── .env                    # 実際の設定(自分で作成・.gitignore対象)
├── credentials.json        # GoogleサービスアカウントのJSON(自分で配置・.gitignore対象)
├── .gitignore
├── run.sh                  # launchd用の実行ラッパー
└── launchd/
    └── com.rakutentosheets.daily.plist
```
