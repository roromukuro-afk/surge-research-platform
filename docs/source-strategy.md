# データソース戦略

状態: **草案 v0.2（Phase 0.2 監査是正後）** — 2026-09-15
各ソースは実装前に利用規約・API 条件を再確認し、`materials.sources.terms_checked_at` 等に記録する。

## 1. 基本方針

1. **公式 API・公式配布ファイル・RSS を優先**する。利用規約で自動取得が禁止されているサイトはスクレイピングしない。
2. 全文保存が許されないソースは metadata / URL / snippet / content hash / 抽出特徴量のみ保存する。
3. すべての材料について `source_published_at`・`system_first_seen_at`・`ingested_at`・`available_to_model_at` を記録する。backfill した文書は、システム側の3つの時刻を backfill 実行時刻で記録する（`source_published_at` で埋めない）。
4. **情報源による固定序列は持たない。** 材料の強さは材料属性で評価する。Discovery Source と Verification Source を分離する。
5. 取得の失敗・欠損は取得ログとカバレッジに必ず残す。
6. **Provider・ソースの比較と選定の根拠は公式情報のみ。** 第三者の比較記事を採用根拠にしない。
7. 市場データは `MarketDataProvider` の役割ごとに Provider を割り当てる（[interfaces.md §2](interfaces.md)）。どの Provider も固定しない。

## 2. 価格・銘柄マスタ

比較表: [provider-comparison.md](provider-comparison.md)

| 役割 | JP | US |
|---|---|---|
| 銘柄マスタ | J-Quants `equities/master`（`Mkt` / `ProdCat`） | Nasdaq Trader Symbol Directory、Provider のマスタ API、SEC の SIC（SPAC = 6770、REIT = 6798） |
| 全銘柄日足（Stage 1） | J-Quants（Free は開発用、Production は Light 以上を候補） | 候補: Massive / Alpaca / Tiingo 等（D-07a） |
| 分足・約定履歴（Stage 2・パス解決・Replay・教師データ） | J-Quants 分足・ティックアドオン（候補。日次 16:30頃更新） | 候補: Massive / Alpaca / Tiingo 等 |
| リアルタイム（ENTRY 判断・Watch 監視） | **未選定（D-06b）。J-Quants は日次更新のため対象外** | 候補: 遅延区分がリアルタイムのプラン（D-07a、D-21） |
| 需給（信用残等） | J-Quants Standard 以上。**有効性が教師データで確認されるまで必須にしない** | — |
| 発行済株式数・浮動株 | 未調査 | SEC XBRL、Provider（未調査） |
| FX（USD/JPY） | 未選定（D-02a）。`fx_observed_at <= decision_cutoff_at` を満たす観測時刻付きのデータが必要 | 同左 |

## 3. 開示・一次情報

| 地域 | ソース | アクセス方法 | 備考 |
|---|---|---|---|
| JP | TDnet | JPX 公式「TDnet API サービス」（有料契約・利用規約同意が必要）/ J-Quants のアドオン / 非公式 API | 非公式 API は規約確認のうえ D-12 |
| JP | EDINET | EDINET API v2（金融庁） | 仕様書 PDF を Phase 4 で確認 |
| JP | 企業 IR / 政府・省庁・日銀 | 公式サイト / RSS | 個別に規約確認 |
| US | SEC EDGAR | data.sec.gov API、daily index | 最大 10 req/s、User-Agent に連絡先が必須 |
| US | Company IR / Fed / Treasury / White House / DOE / DoD / FDA 等 | 公式サイト / RSS / API | 個別に規約確認 |

## 4. ニュース・金融メディア

対象候補（指示書 §10）: 株探、フィスコ、Reuters、日経、Yahoo!ファイナンス、みんかぶ、アイフィス、トレーダーズ・ウェブ、米国金融ニュース ほか。

Phase 4 で各社の利用規約を個別に確認し、次のいずれかに分類する（D-12）。

| 分類 | 意味 |
|---|---|
| `API_LICENSED` | 公式 API / 有料ライセンスで取得 |
| `RSS_HEADLINE` | RSS 等で見出し・要約・URL のみ |
| `MANUAL_ONLY` | 自動取得しない |

## 5. マクロ・国際

- 商品・金利・為替などの価格は数値系列として保存し、急変を材料候補として検知する。
- 関税・制裁・紛争・政策などの出来事は `material_event` とし、銘柄への紐付けには因果経路を必須とする。

## 6. ノイズ除去

- キーワードによる単純除外は禁止。`market_relevance`（市場・企業利益への接続性）で判定し、理由と `filter_version` を保存する。

## 7. 収集頻度（案）

| 対象 | 頻度 | 実行 |
|---|---|---|
| JP 日足・分足・ティック・マスタ | J-Quants 公式の更新スケジュール: 株価四本値・分足・ティックは日次 16:30頃、上場銘柄一覧は 17:30頃と翌営業日 8:00頃（確約ではない） | Scheduler → JobRunner |
| US 日足 | 取引日の引け後 | 同上 |
| 開示・ニュース | 数分間隔（`system_first_seen_at` の精度に直結） | 常駐型 Runner（D-05a） |
| 場中の分足・リアルタイム価格 | Setup / Watch / Open Episode の銘柄のみ、取引時間中（J-Quants は対象外） | 常駐型 Runner（D-05a） |
