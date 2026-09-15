# 市場データ Provider 比較（公式情報のみ）

状態: **比較表（選定はしていない）** — 2026-09-15 確認
方針: 監査指摘 #14 により、**第三者の比較記事を根拠にしない**。各社の公式サイト・公式ドキュメントで確認できた事項のみ記載し、確認できなかった項目は「未確認」と書く。価格・仕様は変わるため、採用前に再確認する。

Provider は [interfaces.md](interfaces.md) の役割（role）ごとに別々に選べる。

---

## 1. 米国株

### 1-1. プランと遅延区分

| 項目 | Massive | Alpaca（Trading API 個人向け） | Tiingo |
|---|---|---|---|
| 無料プラン | Stocks Basic $0: End of Day Data、履歴2年、API 5回/分、WebSocket なし | Basic（無料）: 株式のリアルタイムは IEX のみ、WebSocket 30銘柄、200回/分 | Starter $0: 1日1,000リクエスト、月500銘柄、帯域1GB、IEX 分足なし、Internal Use Only |
| 有料プラン | Starter $29/月: 15分遅延、5年 / Developer $79/月: 15分遅延、10年 / Advanced $199/月: リアルタイム、20年以上 | Algo Trader Plus $99/月: 全米取引所、WebSocket 無制限、10,000回/分 | Power $30/月（個人）: 10,000回/時・100,000回/日、IEX 分足あり、帯域40GB / Business $50/月（内部利用） |
| 履歴 | プランにより2年〜20年以上 | 2016年以降 | EOD 30年以上（プランページ記載） |
| 利用者区分 | 個人プランは「Non-pros only」 | 認証必須（口座が必要） | Internal Use Only |

### 1-2. 用途ごとの機能

| 用途 | Massive | Alpaca | Tiingo |
|---|---|---|---|
| **全銘柄の日足一括**（`EOD_UNIVERSE_US`） | `GET /v2/aggs/grouped/locale/us/market/stocks/{date}` で全米国株の日足 OHLC・出来高・VWAP。`adjusted` は既定 true（false で無調整）、`include_otc` は既定 false | 全銘柄一括のエンドポイントは本ラウンドでは未確認。`GET /v2/stocks/bars` は `symbols` にカンマ区切りで複数銘柄、`limit` 最大10,000、ページトークンあり | 本ラウンドで確認したページには全銘柄一括の記載なし（銘柄ごとの取得） |
| 無調整価格 | `adjusted=false` | `adjustment=raw`（他に split / dividend / spin-off / all） | raw（open/high/low/close/volume）と adjusted（adjOpen 等）の両方、splitFactor・divCash |
| 銘柄マスタ・上場廃止（`SECURITY_MASTER_US`） | All Tickers: `type` / `exchange` / `active` / `date` で絞り込み、`active=false` で上場廃止銘柄、`delisted_utc` あり。`type` の値一覧は Ticker Types API（**値は未確認**） | 本ラウンドでは未確認 | `supported_tickers.zip`（日次更新）。含まれる列と上場廃止銘柄の有無は**未確認** |
| 分足履歴（`INTRADAY_HISTORY_US`） | 全プランで分足 aggregates あり | `timeframe=1Min` 等。Basic は「latest 15 minutes」の取得に制限 | IEX の過去分足（`resampleFreq`）。遡れる期間は**未確認** |
| リアルタイム（`REALTIME_DECISION_US`） | 分足 WebSocket: Starter / Developer は15分遅延、Advanced・Business はリアルタイム | Basic: IEX のみリアルタイム。Algo Trader Plus: 全取引所（CTA/UTP） | IEX データ。2025-02-01 以降、完全な TOPS には IEX Exchange との契約が必要、派生データは契約なしで利用可 |
| EOD データの提供時刻 | 本ラウンドでは未確認 | 本ラウンドでは未確認 | 多くの米国株は 5:30 PM EST に取得可能、訂正は 8 PM EST まで |

### 1-3. 本プロジェクトの要件との照合（事実のみ）

| 要件 | 所見 |
|---|---|
| Stage 1 は全銘柄の日足を毎日取得 | 一括取得を公式に確認できたのは Massive の grouped daily のみ |
| ENTRY 判断は「現在価格」 | 15分遅延プランのデータは、判断時点の価格ではない（D-21） |
| 上場廃止銘柄の保持（生存者バイアス対策） | 公式に確認できたのは Massive の `active=false` / `delisted_utc` |
| IEX 単独のデータ | 単一取引所の約定・気配であり、全市場の価格と一致しない場合がある |

選定は D-07a。役割ごとに別 Provider を選ぶことを前提とする。

出典: [Massive pricing](https://massive.com/pricing)、[Massive Daily Market Summary](https://massive.com/docs/rest/stocks/aggregates/daily-market-summary)、[Massive All Tickers](https://massive.com/docs/rest/stocks/tickers/all-tickers)、[Massive minute aggregates WebSocket](https://massive.com/docs/websocket/stocks/aggregates-per-minute)、[Alpaca Market Data API](https://docs.alpaca.markets/docs/about-market-data-api)、[Alpaca Historical Bars](https://docs.alpaca.markets/reference/stockbars)、[Tiingo pricing](https://www.tiingo.com/about/pricing)、[Tiingo EOD](https://www.tiingo.com/documentation/end-of-day)、[Tiingo IEX](https://www.tiingo.com/documentation/iex)

---

## 2. 日本株

### 2-1. J-Quants（JPX 公式）

| 項目 | Free | Light | Standard | Premium |
|---|---|---|---|---|
| 月額 | ¥0 | ¥1,650 | ¥3,300 | ¥16,500 |
| 上場銘柄一覧・株価四本値の期間 | 12週間前〜2年12週間前 | 5年前まで | 10年前まで | 20年前まで |
| レート制限 | 5回/分 | 60回/分 | 120回/分 | 500回/分 |
| 信用取引週末残高 | — | — | 10年前まで | 20年前まで |
| 前場・後場の四本値 | — | — | — | ○ |
| 本プロジェクトでの位置付け | **開発用**（遅延データ） | **Production EOD 候補** | 信用データの有効性が教師データで確認されるまで必須にしない | 前場・後場の四本値が必要な場合の候補 |

アドオン: 株価分足・ティック（格納期間2年前まで）。月額は JPX のお知らせ（2026-01-19）で ¥5,500 とされているが、**そのお知らせのページは 403 で本文を取得できず、検索結果の要約でしか確認できていない**。

API 仕様（確認済み）:
- 株価四本値 `/equities/bars/daily`: 無調整の O/H/L/C/Vo/Va、`AdjFactor`、調整済み AdjO 等、値幅制限フラグ UL/LL、前場・後場項目は Premium のみ。取引がない日は四本値が Null。
- 上場銘柄一覧 `GET /v2/equities/master`: `Mkt`（市場区分コード）、`ProdCat`（商品区分コード）、翌営業日時点の情報は 17時半以降に取得可能。
- 分足 `GET /v2/equities/bars/minute`: 1分足の四本値・出来高・売買代金、過去2年、東証上場銘柄のみ、銘柄コードまたは日付の指定が必須、取引のない分は返らない。
- **更新スケジュール（公式）**: 株価四本値・株価分足・株価ティックは日次 16:30頃、上場銘柄一覧は 17:30頃と翌営業日 8:00頃。更新時刻は確約ではなく前後しうる。ヘルプにも「分足データは日次で更新されます。リアルタイムでの配信ではございません」とある。
- **結論**: J-Quants の分足・ティックは Historical research / EOD / Replay / Teacher data 用。**場中の ENTRY 判断・Watch 監視には使わない**（監査 0.2 #2）。

**未解決の重要事項**: 日本株の場中 ENTRY 判断・Watch 監視に使うリアルタイム Provider は未選定（D-06b）。約定・気配を提供するかは `entry_price_method`（D-01a）にも影響する。

出典（更新時刻）: [データ更新スケジュール](https://jpx-jquants.com/ja/spec/data-update)、[ヘルプ: データ内容・仕様](https://jpx-jquants.com/ja/help/data)

出典: [J-Quants](https://jpx-jquants.com/)、[データ格納期間](https://jpx-jquants.com/ja/spec/data-spec)、[株価四本値](https://jpx-jquants.com/ja/spec/eq-bars-daily)、[上場銘柄一覧](https://jpx-jquants.com/ja/spec/eq-master)、[市場区分コード](https://jpx-jquants.com/ja/spec/eq-master/marketcode)、[分足](https://jpx-jquants.com/ja/spec/eq-bars-minute)

---

## 3. 本ラウンドで確認していないもの

- USD/JPY の Provider（D-02a）
- 日本株のリアルタイム価格の入手手段（D-06b）
- Alpaca の銘柄マスタ API、Massive の Ticker Types の値一覧、Tiingo の `supported_tickers.zip` の列
- 上記以外の Provider（EODHD 等）。必要なら同じ方針（公式情報のみ）で追加する
