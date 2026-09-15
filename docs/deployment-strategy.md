# デプロイ・運用方針（草案）

## 1. 新規リソース（すべて新規作成、既存流用禁止）

| リソース | 用途 | 作成タイミング | 備考 |
|---|---|---|---|
| GitHub プライベートリポジトリ | ソース管理・Actions | Phase 1 開始時（ユーザー確認後） | 現在はローカル Git のみ |
| Supabase プロジェクト（本番） | DB / Storage / Auth | Phase 1 開始時 | Free 枠の空き（2プロジェクト上限）を確認（D-03） |
| Vercel プロジェクト | Web UI | Phase 1 後半（画面が出来てから） | プラン D-04 |
| Collector 実行環境 | ニュース高頻度収集 | Phase 4 | D-05 |
| 各データAPI契約 | J-Quants / 米国価格 / ニュース | Phase 1〜4 | D-06, D-07, D-12 |

クラウドリソース作成・有料プラン契約は、Claude Code が勝手に行わずユーザー確認後に実施する。

## 2. 環境

| 環境 | Web | DB | Worker |
|---|---|---|---|
| local | `next dev` | ローカル Supabase（Docker） | ローカル Python |
| preview | Vercel Preview（認証必須） | 本番DB読み取り専用ロール or ステージングDB（D-14） | — |
| production | Vercel Production（認証必須） | Supabase 本番 | GitHub Actions / Collector |

## 3. スケジュール（案）

| ジョブ | 時刻（JST） | 実行環境 |
|---|---|---|
| JP 日次バッチ（Master→価格→フィルタ→Feature→Stage1〜3→判定） | 取引日 16:30 以降（データ提供時刻確認後に確定） | GitHub Actions |
| US 日次バッチ | 取引日翌朝 7:00 以降（夏時間/冬時間で変動） | GitHub Actions |
| Outcome 追跡 | 各日次バッチ末尾 | GitHub Actions |
| Watch Monitor | 日次（分足導入時は場中、D-08） | GitHub Actions / Collector |
| ニュース・開示収集 | 5〜15分間隔 | Collector |
| 学習・評価（Phase 11 以降） | 週次 | GitHub Actions / ローカル |
| Excel Export | 日次バッチ末尾 | GitHub Actions |

cron は取引カレンダーを参照し、休場日はスキップして `pipeline.runs` にスキップ理由を記録する。

## 4. 秘密情報

- GitHub Secrets: Supabase service role キー、DB 接続文字列、各API キー、LLM キー。
- Vercel 環境変数: Supabase URL・anon/publishable キー（RLS 前提）。service role キーは Web に置かない。
- `.env.example` にキー名のみ記載。

## 5. マイグレーション

- `supabase/migrations/` で管理。本番適用は CI またはユーザー確認後の手動実行。
- `prod` スキーマの append-only トリガーはマイグレーションで定義し、削除マイグレーションは監査対象。

## 6. 監視・障害時

- 日次バッチ失敗時: `pipeline.runs.status = failed`、Dashboard / Pipeline 画面に表示。通知手段は D-14。
- 部分失敗（一部銘柄の価格欠損など）は失敗扱いにせず、欠損リストをカバレッジに記録。
- 再実行は `run_id` 単位で冪等。Production Prediction の再生成は行わない（失敗した日の判定は「未実施」として記録）。

## 7. バックアップ

- Supabase Pro の日次バックアップ（Pro 移行後）。
- Raw Storage は再処理の正本なので削除しない。容量逼迫時の方針は D-03。
