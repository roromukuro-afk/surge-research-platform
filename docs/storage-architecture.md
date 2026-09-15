# Storage Architecture

状態: **草案（Phase 0.1 監査指摘 #3 により改訂）** — 2026-09-15

## 1. 原則

1. **PostgreSQL は全 Raw データの保存先にしない。** 状態・索引・監査・結果（行数が「銘柄数×日数×列」で膨らまないもの）を置く。
2. **大量の履歴データは Parquet + Object Storage** に置く。書き込みは一度きり（immutable）で、訂正は新しいバージョンのオブジェクトとして追加する。
3. Postgres の `storage.dataset_manifests` が Parquet/オブジェクトの**索引の正本**。どのオブジェクトがいつ作られ、何を置き換えたかを記録する。
4. Object Storage プロバイダは**固定しない**。S3 互換 API を共通の最小仕様とし、`ObjectStore` インターフェース（[interfaces.md](interfaces.md)）経由でのみ読み書きする。

## 2. 置き場所の割り当て

### 2-1. PostgreSQL（制御・索引・状態）

| 区分 | 内容 |
|---|---|
| Security Master | 銘柄、識別子履歴、上場/廃止、Universe 定義版、取引カレンダー、コーポレートアクション |
| FX | 為替観測値（系列が小さいため Postgres。観測時刻 `fx_observed_at` 付き） |
| Material Event index | 文書メタデータ（URL・ハッシュ・時刻・ソース）、ノイズ判定、Material Event、Event↔文書、Entity Link、材料属性 |
| Runs / 監査 | runs、job_requests、run_errors、source_fetch_log、coverage_snapshots、dataset_manifests |
| 候補・分析 | 当日の候補集合、Stage 2 判定結果（候補のみ）、LLM分析のメタデータと構造化出力 |
| Production | SETUP_EOD、Watch、Prediction、Prediction Episode、State Transition（append-only） |
| Outcomes | Prediction/Episode の Outcome、パス解決結果 |
| Labels | Objective / Interpretive ラベル、見逃しレビュー |
| Model versions | model_registry、評価結果サマリ |
| current state | 最新の Universe 判定、銘柄ごとの現在ステージ、Web 表示用の派生キャッシュ（再生成可能・正本ではない） |

### 2-2. Parquet + Object Storage（大量履歴・成果物）

| データセット | 粒度 | 対象 |
|---|---|---|
| `curated/ohlcv_daily` | 銘柄×日 | 全 Universe（無調整価格 + 調整係数） |
| `curated/ohlcv_minute` | 銘柄×分 | **Stage 2・Watch・SETUP_EOD・ENTRY 候補・Open Episode の銘柄のみ**（全銘柄の分足は保存しない） |
| `curated/universe_eligibility` | 銘柄×日 | 全銘柄の3,000円判定・除外理由の履歴 |
| `features/daily` | 銘柄×日×feature_version | 全 Eligible 銘柄の Technical Feature 履歴 |
| `features/stage1_routes` | 銘柄×日×rule_version | Route A〜H の条件ごとの判定詳細（全 Eligible 銘柄） |
| `outcomes/universe` | 銘柄×日×horizon | 全 Eligible 銘柄の将来 Outcome（見逃し研究用） |
| `raw/api_responses` | 取得単位 | 価格・マスタ等 API の生レスポンス（圧縮） |
| `raw/documents` | 文書単位 | 開示・ニュース本文（**利用規約上保存可能なもののみ**） |
| `artifacts/chart_images` | 画像単位 | OHLCV から生成したチャート画像（候補・事例のみ） |
| `artifacts/llm_io` | 分析単位 | LLM 入力バンドル・生出力（内容ハッシュで参照） |
| `research/*` | 実行単位 | Historical Replay 出力、特徴量重要度、学習用データセット、見逃し分析 |
| `artifacts/models` | モデル単位 | 学習済みモデル |
| `exports/excel` | 出力単位 | Excel |

## 3. オブジェクトのレイアウト

```
<bucket>/
  raw/<source_id>/<yyyy>/<mm>/<dd>/<content_sha256>.<ext>.zst
  curated/ohlcv_daily/market=JP/trade_date=2026-09-15/v=<dataset_version>/part-0000.parquet
  curated/ohlcv_minute/market=US/trade_date=2026-09-15/v=<dataset_version>/part-0000.parquet
  features/daily/feature_version=feat-1.0.0/market=JP/as_of_date=2026-09-15/part-0000.parquet
  artifacts/llm_io/<yyyy>/<mm>/<input_sha256>.json.zst
  research/replay/<replay_run_id>/...
```

- Parquet は列指向・zstd 圧縮。スキーマ版を列メタデータと manifest の両方に持つ。
- raw はコンテンツハッシュでアドレスを決め、同一内容は重複保存しない。

## 4. Manifest とデータリーク防止

`storage.dataset_manifests`（Postgres）:

| 列 | 意味 |
|---|---|
| manifest_id | ID |
| dataset / partition_key | 例: `curated/ohlcv_daily` / `market=JP/trade_date=2026-09-15` |
| object_key, sha256, bytes, row_count | オブジェクト実体 |
| schema_version, dataset_version | スキーマ・データ版 |
| produced_by_run_id | 生成した run |
| data_cutoff | 含まれるデータの取得時刻上限 |
| created_at | **manifest が作られた時刻（= システムがそのデータを「知った」時刻）** |
| supersedes_manifest_id | 訂正で置き換えた旧 manifest |

- 読み取りは必ず `as_of(knowledge_time)` を指定し、`created_at <= knowledge_time` の manifest のうち最新版だけを使う。
  → 後日の価格訂正・再計算版が、過去時点の判断に紛れ込まない。
- 旧版のオブジェクトは削除しない（Production 当時の判断を再現するため）。

## 5. 読み取り経路

| 利用者 | 経路 |
|---|---|
| Worker（特徴量・スクリーニング・研究） | DuckDB 等で Object Storage 上の Parquet を manifest 経由で読む |
| Web（Next.js） | **Parquet を直接読まない。** Postgres の状態テーブルと、Worker が作る表示用の派生キャッシュ（候補・Watch・Episode 銘柄のチャート用データ等）を読む |
| Excel Export | Worker が Postgres + Parquet から生成し `exports/` に保存。Web からは署名付き URL で取得 |

## 6. Object Storage 候補の比較（公式ページのみ、2026-09-15 確認）

| 項目 | Cloudflare R2 | Backblaze B2 | Supabase Storage | AWS S3 | ローカルディスク |
|---|---|---|---|---|---|
| 保存単価 | Standard $0.015/GB-月、Infrequent Access $0.01/GB-月 | $6.95/TB-月 | Pro: 100GB 込み、超過 $0.021/GB | 未確認 | — |
| 無料枠 | Standard 10 GB-月、Class A 100万回/月、Class B 1,000万回/月 | 最初の 10GB | Free: 1GB | 未確認 | — |
| 操作課金 | Class A $4.50/100万回、Class B $0.36/100万回（Standard） | Class A/B/C 無料、Class D $0.004/1万回（1日2,500回まで無料） | 記載確認対象外 | 未確認 | — |
| 取り出し（egress） | 無料 | 月間保存量の 3倍まで無料、超過 $0.01/GB | Egress 枠: Free 5GB / Pro 250GB 込み、超過 $0.09/GB（組織全体） | 未確認 | — |
| 用途 | 本番候補 | 本番候補 | 小規模時の候補（DB と同一サービス） | 本番候補 | 開発・テスト |

出典: [R2 pricing](https://developers.cloudflare.com/r2/pricing/)、[B2 pricing](https://www.backblaze.com/cloud-storage/pricing)、[Supabase billing](https://supabase.com/docs/guides/platform/billing-on-supabase)。AWS S3 は本ラウンドで公式価格を確認していないため空欄。

選定は Remaining decision **D-03b**。判断材料として Phase 2〜3 で実データ量を測定する。

## 7. 容量の見積もり（概算・要実測）

| データ | 見積もり根拠 | 規模感 |
|---|---|---|
| 日足 | JP 約3,800 + US 約6,000 銘柄 × 約250 営業日 | 年 約250万行（Parquet で数十〜百MB程度の見込み） |
| 日次 Feature | 約1万銘柄 × 250日 × 数十列 | 年 数億値（Parquet で GB 未満〜数GB の見込み） |
| 分足 | 対象銘柄のみ（数百銘柄 × 取引分数） | 対象銘柄数に比例。Phase 8 で実測 |
| チャート画像 | 候補数 × 画像サイズ | 候補数に比例 |

銘柄数は Universe 定義 v1.0.0 の実数で置き換える。
