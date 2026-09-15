# データソース戦略（Phase 0）

状態: **草案**。各ソースは実装前に利用規約・API条件を再確認し、`materials.sources.terms_checked_at` に記録する。

## 1. 基本方針

1. **公式API・公式配布ファイル・RSSを優先**し、利用規約で自動取得が禁止されているサイトはスクレイピングしない。
2. 全文保存が許されないソースは metadata / URL / snippet / content hash / 抽出特徴量のみ保存する。
3. すべての取得について `published_at`（ソース記載時刻）・`fetched_at`（取得時刻）・`first_seen_at`（システムが初めて観測した時刻）を記録する。
4. **情報源による固定序列は持たない**。ソース属性は「どこから来たか」の記録であり、材料の強さは材料属性（新規性・サプライズ等）で評価する。
5. Discovery Source と Verification Source を分離する。
6. 取得失敗・欠損は `pipeline.source_fetch_log` と `coverage_snapshots` に必ず残し、黙って欠損させない。

## 2. 価格・Universe

### 日本株
| 用途 | 候補 | 確認済み事項（2026-09-15） | 課題 |
|---|---|---|---|
| 上場銘柄一覧・日足・取引カレンダー | J-Quants API（JPX公式） | 全プランに上場銘柄一覧・日足OHLC・決算発表日・取引カレンダー。Free は「直近12週間を除く2年分」で**運用不可**。Light ¥1,650/月（5年・60件/分）、Standard ¥3,300/月（10年・120件/分、信用取引データ含む）、Premium ¥16,500/月（20年・500件/分、前場四本値・分足/TDnetはアドオン） | プラン選択（D-06）。日次更新タイミングの確認 |
| 信用残など需給 | J-Quants Standard 以上 | 信用取引データは Standard+ | D-06 |
| 分足・VWAP | J-Quants Premium アドオン等 | Premium 限定 | D-08 |

### 米国株
| 用途 | 候補 | 確認済み事項 | 課題 |
|---|---|---|---|
| 上場銘柄一覧 | Nasdaq Trader Symbol Directory（`nasdaqlisted.txt` / `otherlisted.txt`） | ETFフラグ・Test Issueフラグ・Financial Status・取引所コードあり。優先株/ワラント/ユニットは Security Name から判別が必要 | 普通株判定ルールの確定（D-10） |
| CIK 対応 | SEC `company_tickers.json` / `company_tickers_exchange.json` | 公式配布 | — |
| 日足（全銘柄一括） | Massive（旧Polygon.io）grouped daily、Tiingo、EODHD 等 | 各社有料プランあり。価格は未確定 | ベンダー選定（D-07） |
| 浮動株・発行済株式数 | SEC XBRL（dei:EntityCommonStockSharesOutstanding）＋ベンダー | 公式APIで取得可能だが更新は提出ベース | D-07 |

### FX
- USD/JPY。米国株の3,000円判定は「同時点」の為替を使うため、**どの時点のレートを使うか**を D-02 で決める。

## 3. 開示・一次情報

| 地域 | ソース | アクセス方法 | 備考 |
|---|---|---|---|
| JP | TDnet | JPX公式「TDnet API サービス」（有料契約・利用規約同意が必要）/ J-Quants Premium アドオン / 非公式Web API | 非公式APIの利用可否は規約確認のうえ D-12 |
| JP | EDINET | EDINET API v2（金融庁、APIキー発行制） | 仕様書 PDF を Phase 4 で精読 |
| JP | 企業IR | 各社サイト/RSS | 規約・robots を個別確認 |
| JP | 政府・省庁・日銀 | 公式サイト/RSS | — |
| US | SEC EDGAR | data.sec.gov API、daily index | **最大 10 req/s、User-Agent に連絡先必須** |
| US | Company IR | 各社サイト/RSS | 個別確認 |
| US | Fed / Treasury / White House / DOE / DoD / FDA 等 | 公式サイト/RSS/API | — |

## 4. ニュース・金融メディア

対象候補（指示書 §10）: 株探、フィスコ、Reuters、日経、Yahoo!ファイナンス、みんかぶ、アイフィス、トレーダーズ・ウェブ、米国金融ニュース ほか。

- 多くは**有料・会員制・自動取得禁止**の可能性が高い。Phase 4 で各サイトの利用規約を個別に確認し、以下のいずれかに分類する。
  - `API_LICENSED`: 公式API/有料ライセンスあり
  - `RSS_HEADLINE`: RSS等で見出し・要約・URLのみ取得可
  - `MANUAL_ONLY`: 自動取得不可（取得しない）
- 分類結果と費用を D-12 として報告する。規約上不可のものを「取れるから」取得しない。

## 5. マクロ・国際

原油・天然ガス・金属・金利・為替・関税・戦争・制裁・AI・半導体・防衛・電力・原子力・データセンター・サプライチェーン・暗号資産 など。

- 価格系（商品・金利・為替）は数値系列として `market` に保存し、急変を材料候補イベントとして検知する。
- 事象系（関税・制裁・紛争・政策）はニュース/公式発表から `material_event` 化し、**因果経路（causal_path）を必須**として銘柄に紐付ける。

## 6. ノイズ除去方針

- キーワード単純除外は禁止。
- 判定は `market_relevance`（市場・企業利益への接続性）で行い、判定理由と `filter_version` を保存。
- 例: 通常の台風ニュースは除外候補だが、工場停止・港湾停止・電力設備・農産物・保険・原材料価格に接続すれば材料になり得る。

## 7. 収集頻度（案）

| 対象 | 頻度案 | 理由 |
|---|---|---|
| JP 日足 | 取引日 15:30 JST 以降（データ提供時刻を確認） | 日次バッチ |
| US 日足 | 取引日 16:00 ET 以降（JST 翌朝） | 日次バッチ |
| 開示・ニュース | 5〜15分間隔（D-05） | `first_seen_at` 精度 |
| Security Master | 日次 | 新規上場・廃止 |
| マクロ価格 | 日次（急変検知は高頻度化を検討） | — |
