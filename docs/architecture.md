# アーキテクチャ案

状態: **草案 v0.2（Phase 0.2 監査是正後）** — 2026-09-15
変更点（0.1）: ストレージ分離（#3）、Runner 抽象化（#4）、Vercel 制約更新（#5）、Setup と場中 ENTRY 判断（#6〜#8）、Provider 抽象化（#14・#15）
変更点（0.2）: Setup の2種別、decision / entry 価格、ENTRY 時の3,000円再判定、材料の利用可能時刻、J-Quants 分足・ティックを場中判断に使わない

## 1. 全体像

```
 外部データ ─────────────────────────────────────────────────────────────────────────────
  価格(JP/US)・分足・FX・銘柄マスタ         開示(TDnet/EDINET/EDGAR)・ニュース・政府・商品
        │  MarketDataProvider / FxProvider               │  Source collectors
        ▼                                                ▼
 ┌──────────────────────────── Workers（Python、Job 単位）──────────────────────────────┐
 │  EOD jobs:    master → daily bars → FX → Universe/3000円 → features → Stage1          │
 │               (Technical ∪ Material) → Stage2(分足) → Stage3 EOD → SETUP*/WATCH      │
 │  Intraday:    entry_decision / watch_monitor（分足・リアルタイム価格）→ ENTRY/Episode │
 │  Collectors:  news / disclosure（4 timestamps）→ noise → event → entity link       │
 │  Post:        outcome/path resolution → labels → exports                           │
 │  Research:    replay / walk-forward / training（Production と分離）                  │
 └──────────────▲──────────────────────────────┬────────────────────────────────────────┘
                │ JobRequest                    │ 読み書き
   Scheduler ───┤                               ▼
   (取引カレンダー)  JobRunner          ┌──────────────────────┐   ┌──────────────────────────┐
                 (GitHub Actions /     │ PostgreSQL（新規）     │   │ Object Storage（未選定）   │
                  常駐 Worker / Local)  │ 状態・索引・監査・結果  │◀─▶│ Parquet: OHLCV/分足/Feature│
                                       │ manifests             │   │ raw / images / llm_io     │
                                       └──────────┬───────────┘   │ research / models / excel │
                                                  │               └──────────────────────────┘
                                                  ▼
                                       ┌──────────────────────────────┐
                                       │ Web（Next.js、新規 Vercel）    │
                                       │ 認証必須・スマホ対応・読み取り中心│
                                       └──────────────────────────────┘
```

詳細: [storage-architecture.md](storage-architecture.md) / [interfaces.md](interfaces.md) / [specs/entry-and-episode-lifecycle.md](specs/entry-and-episode-lifecycle.md)

## 2. 処理フロー

### 2-1. EOD（市場ごと、毎営業日）

| # | Job | 出力 | LLM |
|---|---|---|---|
| 1 | Security Master 更新（Universe 定義 `universe-1.0.0` 適用） | Postgres `ref.*` | × |
| 2 | 全銘柄の日足取得（`EOD_UNIVERSE_*` Provider） | Parquet `curated/ohlcv_daily` | × |
| 3 | USD/JPY 取得（`fx_observed_at <= price_cutoff_at`） | Postgres `ref.fx_observations` | × |
| 4 | 3,000円 Hard Filter → Eligible Universe | Postgres current + Parquet 履歴 | × |
| 5 | 全 Eligible 銘柄の Technical Feature | Parquet `features/daily` | × |
| 6a | Stage 1 Technical（v5.1 Route A〜H をコード化） | Parquet 詳細 + Postgres 候補 | × |
| 6b | Material 候補（Technical と独立） | Postgres 候補 | × |
| 7 | 候補集合 = Technical ∪ Material | Postgres | × |
| 8 | Stage 2（候補のみ分足取得、知識ベース照合、チャート画像） | Postgres 結果 + Parquet/画像 | × |
| 9 | Stage 3 EOD 分析（材料は `available_to_model_at <= price_cutoff_at` のみ）→ `TECHNICAL_SETUP_EOD` / `WATCH_*` / `REJECT`（**ENTRY は出さない**） | Postgres | ○ |
| 9b | 引け後の材料分析（引け後に利用可能になった新規材料）→ `POST_CLOSE_CATALYST_SETUP`（EOD 価格に対する未織り込み評価はしない） | Postgres | ○ |
| 10 | Outcome 追跡・パス解決（Episode / 全 Eligible） | Postgres / Parquet | × |
| 11 | Excel Export | Object Storage | × |

### 2-2. 場中（市場の取引時間）

| # | Job | 内容 | LLM |
|---|---|---|---|
| 1 | `entry_decision` | 次の取引可能時点で Setup（両種別）の銘柄を再分析。`REALTIME_DECISION_*` Provider の `decision_price` と当日分足、`available_to_model_at <= decision_cutoff_at` の材料を使う。`decision_price` で3,000円を再判定。ENTRY なら判断後に `entry_reference_price` を観測し、それでも3,000円以下なら Prediction・Episode、超過なら `ENTRY_ABORTED_PRICE_LIMIT`（研究ログ） | ○ |
| 2 | `watch_monitor` | Watch 銘柄の分足を監視し、条件到達で `TRIGGER_HIT` → `REANALYSIS` を要求 | 再分析時のみ |
| 3 | `episode_monitor` | Open Episode の Target / Failure 到達を記録 | × |

### 2-3. 材料（常時）

収集（`source_published_at` / `system_first_seen_at` / `ingested_at` を記録）→ 重複排除・同一出来事の統合 → `market_relevance` によるノイズ判定 → Entity Linking（`relation_type`、マクロは因果経路必須）→ 材料属性（Feature として保存）。

## 3. 技術選定と根拠（2026-09-15 時点の公式ドキュメント確認に基づく）

### 3-1. Web: Next.js (TypeScript) on Vercel（新規 Project）

Vercel Functions の実行時間上限（Fluid Compute 有効時、公式ドキュメント）:

| プラン | 既定 | 最大 | Extended maximum |
|---|---|---|---|
| Hobby | 300秒 | 300秒 | — |
| Pro | 300秒 | 800秒 | 1800秒（対応ランタイム: Node.js 20/22/24、Bun、Python 3.12〜3.14。公式ドキュメント上は Beta 表記） |
| Enterprise | 300秒 | 800秒 | 1800秒（同上） |

- Hobby の Cron は1日1回・±59分の精度。
- **本設計では、上限が延びても重い全市場処理・場中監視・学習を Vercel に移さない。** Web は Postgres の読み取りと軽い操作だけを行う。
- プランは D-04。

### 3-2. PostgreSQL: Supabase（新規 Project）

- Postgres・認証・行レベルセキュリティ・トリガー（append-only の強制）を1サービスで使える。
- 大量の履歴データを Postgres に置かない設計にしたので、DB 容量の問題は小さくなった。一方、Free プランは7日間低アクティビティで自動停止する。**Phase 1 開発は Free で開始する（監査で許可）。Production Architecture は Free の上限に合わせて縮小せず、Production 開始前にプランを再評価する**（D-03c）。

### 3-3. Object Storage: 未選定

- S3 互換 API を共通の最小仕様にし、`ObjectStore` interface で交換可能にする。候補比較は [storage-architecture.md §6](storage-architecture.md)（D-03b）。

### 3-4. Job 実行: `JobRunner` / `Scheduler`（実装は未選定）

- 日次バッチは `GitHubActionsRunner`（1ジョブ最大6時間、非公開リポジトリの無料枠は月2,000分）でも動かせる。
- 場中の `entry_decision` / `watch_monitor` とニュース収集は、数分間隔・低遅延が必要なため常駐型（`QueueWorkerRunner`）が必要になる見込み。
- どちらも同じ Job コード・同じ DB 上のリース/冪等性の仕組みで動く（D-05a）。

### 3-5. 市場データ: `MarketDataProvider`（実装は未選定）

- 役割（マスタ / EOD / 分足履歴 / リアルタイム判断 / FX）ごとに Provider を割り当てる。
- JP の EOD は J-Quants（Free = 開発用、Light 以上 = Production 候補）。
- **J-Quants の分足・ティックは日次 16:30頃の更新（公式）なので、場中の ENTRY 判断には使わない。** Historical research / EOD / Replay / Teacher data 用。
- **JP の場中用リアルタイム Provider は未選定**（D-06b）。場中 ENTRY 判断の前提なので、Phase 8 より前に調査する。
- 比較は [provider-comparison.md](provider-comparison.md)。

### 3-6. LLM / ML

- LLM は `LLMProvider` で抽象化し、`llm_provider` / `llm_model` / `prompt_version` / `input_sha256` を保存（D-11）。
- ML は Phase 11 以降、LightGBM / XGBoost / CatBoost 等の tabular model から。学習データは Parquet、モデルは Object Storage、メタデータは Postgres。

## 4. 環境分離

| 区分 | 内容 | 分離の方法 |
|---|---|---|
| Production | 採用済みルール/モデルによる運用 | `prod` スキーマ（append-only）、書き込みは Production 用 DB ロールのみ |
| Research | Historical Replay、学習、見逃し分析 | `research` スキーマ + `research/` プレフィックス。Research ロールは `prod` に書けない |
| Development | ローカル | ローカル Postgres + ローカル ObjectStore |

## 5. データリーク防止

1. すべてのデータに「出来事の時刻」と「システム側の時刻（`fetched_at` / `system_first_seen_at` / `available_to_model_at` / manifest `created_at`）」を持たせる。材料は `available_to_model_at <= decision_cutoff_at` のみ使う。backfill は実行時刻で記録する。
2. 読み取りは `as_of` を指定する共通の入口を通す（Postgres・Parquet とも）。
3. 無調整価格を正本とし、調整係数は `known_at` 付きで別に持つ。
4. カットオフを分けて記録する（`price_cutoff_at` / `decision_cutoff_at` / `decision_completed_at`）。価格は `decision_price`（判断時）と `entry_reference_price`（判断後）を分ける。Outcome は raw を保存したうえで分割・併合を換算した系列で計算する。
5. LLM 入力はハッシュ付きで保存し再現可能にする。
6. 上場廃止銘柄を残す。学習・評価は walk-forward のみ。

## 6. 認証・非公開

- Supabase Auth + 本人メールアドレスの allowlist（D-14）。全テーブルで RLS を有効にし、匿名アクセスを拒否。
- Object Storage は非公開バケットのみ。Web からは署名付き URL で一時取得。

## 7. 監査ログ

- `pipeline.runs`（run_id / job / data_cutoff / git_sha / 設定ハッシュ / 各 version / provider_bindings / status）
- `pipeline.job_requests`（冪等キー・リース）、`pipeline.run_errors`、`pipeline.source_fetch_log`、`pipeline.coverage_snapshots`
- `storage.dataset_manifests`
