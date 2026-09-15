# デプロイ・運用方針

状態: **草案 v0.1（Phase 0.1 監査是正後）** — 2026-09-15

## 1. 新規リソース（すべて新規作成、既存流用禁止）

| リソース | 用途 | 作成タイミング | 決定事項 |
|---|---|---|---|
| Git リポジトリ（リモート） | ソース管理・CI | **2026-09-15 作成済み**（GitHub Public、`roromukuro-afk/surge-research-platform`） | Public。secret scanning と push protection を有効化済み。コード・設計文書・Prompt・Schema・Test のみ。秘密情報・研究データ・Raw データは commit しない |
| PostgreSQL（Supabase 新規 Project） | 状態・索引・結果・認証 | **2026-09-16 作成済み**（Free、ap-northeast-1、本プロジェクト専用）。Project ID・URL・キーは `.env`（Git 管理外）にのみ置き、コードに hard-code しない | Phase 1 は Free（D-03a）。Production 前に再評価（D-03c）。開発・テストはローカル Supabase（Docker）を併用し、migration / schema / seed / test は Cloud にそのまま適用できる形で version 管理する |
| Object Storage（新規バケット） | Parquet・raw・画像・成果物 | Phase 2 まで（Phase 1 はローカル実装） | プロバイダ D-03b |
| Web（Vercel 新規 Project） | 閲覧用 UI | 画面ができてから | プラン D-04 |
| JobRunner の実行環境 | 日次バッチ / 場中監視 / 収集 | 日次は Phase 1〜2、常駐型は Phase 4・8 まで | D-05a |
| 市場データ・ニュースの契約 | Provider | Phase 1〜8 の必要時 | D-06a / D-06b / D-07a / D-12 |

クラウドリソースの作成・有料契約は、ユーザー確認後に行う。

## 2. 環境

| 環境 | Web | DB | Object Storage | Job 実行 |
|---|---|---|---|---|
| development（正式） | `next dev` | **専用 Cloud Supabase（Free、Phase 1 以降の正式な開発 DB）** | ローカルファイルシステム実装 | `LocalRunner` |
| local（補助、任意） | `next dev` | ローカル Supabase（Docker、`supabase start`）。migration のローカル検証・integration test・オフライン開発用。Phase 1 の blocker ではない | ローカルファイルシステム実装 | `LocalRunner` |
| production | Vercel（認証必須） | Supabase Production | 選定したプロバイダ | 選定した Runner（複数可） |

ステージング環境は当面持たない（D-14）。

## 3. Job 実行の構成

実行環境は `JobRunner` / `Scheduler` interface の実装として差し替える（[interfaces.md §1](interfaces.md)）。

| Job 群 | 性質 | 想定する Runner の種類（未選定） |
|---|---|---|
| EOD バッチ（マスタ〜Stage 3、Outcome、Export） | 1日1〜2回、数十分 | バッチ型（例: `GitHubActionsRunner`） |
| 場中 `entry_decision` / `watch_monitor` / `episode_monitor` | 取引時間中に数分間隔、低遅延 | 常駐型（`QueueWorkerRunner`） |
| ニュース・開示の収集 | 常時、数分間隔 | 常駐型 |
| 学習・Replay | 週次など、長時間 | バッチ型 |

- スケジュールは実行環境から独立した定義（`config/schedules.yaml` 予定）に書き、取引カレンダーで休場日をスキップする。
- 複数の Scheduler・Runner が同時に動いても、`idempotency_key` とリースにより二重実行しない。
- Runner を切り替えても Job のコード・入出力・監査記録は変わらない。

## 4. 秘密情報

- Runner 環境の secret: DB 接続（Production ロール / Research ロールを分ける）、Object Storage キー、Provider キー、LLM キー。
- Web: DB の公開用キー（RLS 前提）と、読み取り専用の Object Storage 署名発行権限のみ。Production の書き込みロールは Web に置かない。
- `.env.example` にキー名のみ記載。

## 5. マイグレーション

- **`supabase/migrations/` が DB 設計の唯一の正本。** Dashboard 上の手作業を正本にしない。Cloud 側に適用した DDL と Git 上の migration が一致する状態を維持する（乖離が見つかったら migration 側を直してから再適用）。
- Cloud への適用は Supabase CLI（`supabase db push`）で行い、接続情報は CLI の認証・ローカル環境変数・GitHub Secrets のいずれかに置く。本番相当の運用開始後はユーザー確認後に適用。
- `prod` の append-only トリガー、Episode の一意制約、Prediction の CHECK 制約はマイグレーションで定義する。これらを外すマイグレーションは監査対象。

## 6. 監視・障害時

- 失敗は `pipeline.runs.status` と Web の Pipeline 画面に表示。通知手段は D-14。
- 一部銘柄の欠損は失敗扱いにせず、欠損リストをカバレッジに記録する。
- 再実行は `idempotency_key` 単位。**Production の ENTRY 判断は後から作り直さない**（取引時間を過ぎた判断は「未実施」として記録）。
- 場中 Runner が停止していた時間帯は、Watch 監視・ENTRY 判断の欠落としてカバレッジに記録する。

## 7. バックアップ

- Object Storage の raw・Parquet は削除しない（上書きも禁止）。
- Postgres のバックアップはプランに依存（D-03a）。manifest と `prod` を優先して保全する。
