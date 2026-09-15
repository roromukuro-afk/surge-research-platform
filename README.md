# 短期急騰AI研究プラットフォーム (surge-research-platform)

日本株・米国株の全上場普通株を毎営業日監視し、**3,000円以下**（米国株は `株価 × 判定時点に同期した USD/JPY`）の Eligible Universe 全体をスクリーニングし、
チャート・需給・ニュース・材料を統合して、**現在価格から Entry 可能な「約1か月以内に+20%以上」を狙える銘柄だけ**を正式 Prediction として記録し、
その後の結果を細かく教師データ化してシステム全体を継続改善する、**非公開の研究プラットフォーム**。

> 目的は「結果的に上がった株を説明するAI」ではなく、
> **現在地点から Entry でき、上昇前または上昇初期で発見するAIを育てること**。

## ステータス

| 段階 | 内容 | 状態 |
|---|---|---|
| Phase 0 | Requirements / architecture / repository setup | 完了（概ね合格） |
| Phase 0.1 | 監査是正（ストレージ分離、interface 抽象化、Entry/Episode、Universe、ラベル、回帰テスト仕様） | 条件付き合格 |
| Phase 0.2 | 最終是正（decision / entry 価格、Setup 分離、Horizon・Failure Line、USD Outcome、材料の利用可能時刻、corporate action、パス解決、v5.1 と addenda の分離） | 概ね合格 |
| Phase 0.2 最終パッチ | PRIOR_SURGE_HIGH の制約緩和、Outcome 二層、見逃し3分類、entry 価格での3,000円再確認、Horizon 表記、v5.1 Canonical の定義 | **完了** |
| Phase 1〜12 | [docs/development-phases.md](docs/development-phases.md) | 未着手（**v5.1 Canonical 登録後に Phase 1 開始可**。Phase 1 終了時に監査） |

## 共同開発体制

| 役割 | 担当 |
|---|---|
| 最終方針・投資思想・優先順位・仕様承認 | ユーザー |
| 設計・実装・DB・API連携・Web・パイプライン・ML・テスト・デプロイ | Claude Code |
| 要件監査・投資ロジック監査・データリーク監査・教師データ監査・修正指示 | ChatGPT |

## ドキュメント

### 規約・原文
| ファイル | 内容 |
|---|---|
| [CLAUDE.md](CLAUDE.md) | Claude Code が必ず守るプロジェクト規約 |
| [docs/requirements/implementation-instructions-v1.0.original.txt](docs/requirements/implementation-instructions-v1.0.original.txt) | 実装指示書 v1.0（原文） |
| [docs/requirements/implementation-instructions-v1.0.formatted.md](docs/requirements/implementation-instructions-v1.0.formatted.md) | 同・閲覧用整形版（原文ではない） |
| [docs/requirements/audit-2026-09-15-phase-0.1.original.txt](docs/requirements/audit-2026-09-15-phase-0.1.original.txt) | ChatGPT 監査 Phase 0.1（原文） |
| [docs/requirements/audit-2026-09-15-phase-0.2.original.txt](docs/requirements/audit-2026-09-15-phase-0.2.original.txt) | ChatGPT 監査 Phase 0.2（原文） |
| [docs/requirements/audit-2026-09-15-phase-0.2-final-patch.original.txt](docs/requirements/audit-2026-09-15-phase-0.2-final-patch.original.txt) | ChatGPT 監査 Phase 0.2 最終パッチ（原文） |
| [docs/prompts/](docs/prompts/) | v5.1 原文（未受領）、MANIFEST（SHA-256）、addenda |

### 設計
| ファイル | 内容 |
|---|---|
| [docs/architecture.md](docs/architecture.md) | 全体構成・処理フロー・技術選定の根拠 |
| [docs/storage-architecture.md](docs/storage-architecture.md) | Postgres と Parquet + Object Storage の分担、manifest、as-of 読み取り |
| [docs/interfaces.md](docs/interfaces.md) | JobRunner / Scheduler / MarketDataProvider / ObjectStore |
| [docs/data-model-draft.md](docs/data-model-draft.md) | データモデル草案 |
| [docs/source-strategy.md](docs/source-strategy.md) | データソース戦略・利用規約確認方針 |
| [docs/provider-comparison.md](docs/provider-comparison.md) | 市場データ Provider 比較（公式情報のみ） |
| [docs/prompt-versioning-policy.md](docs/prompt-versioning-policy.md) | 原文保存・バージョニング |
| [docs/development-phases.md](docs/development-phases.md) | Phase 0〜12 |
| [docs/test-strategy.md](docs/test-strategy.md) | テスト戦略 |
| [docs/deployment-strategy.md](docs/deployment-strategy.md) | デプロイ・運用 |
| [docs/unresolved-decisions.md](docs/unresolved-decisions.md) | 決定済み・未決事項 |

### 仕様
| ファイル | 内容 |
|---|---|
| [docs/specs/entry-and-episode-lifecycle.md](docs/specs/entry-and-episode-lifecycle.md) | Setup（技術／引け後材料）/ Watch / decision・entry 価格 / Episode / Horizon / Failure Line / Outcome / パス解決 |
| [docs/specs/universe-definition-v1.0.0.md](docs/specs/universe-definition-v1.0.0.md) | Initial Tradable Universe `universe-1.0.0` |
| [docs/specs/teacher-labels.md](docs/specs/teacher-labels.md) | Objective / Interpretive ラベル |
| [docs/specs/regression-fixtures.md](docs/specs/regression-fixtures.md) | 投資ロジック回帰テスト fixture |

## リポジトリ構成（予定）

```
apps/web/            Next.js (TypeScript) — 閲覧用 Web UI（軽量処理のみ）
workers/             Python — ジョブ本体（収集・特徴量・スクリーニング・LLM分析・追跡・学習）
supabase/migrations/ DB マイグレーション
config/              スケジュール定義・Provider 割り当て
docs/                設計・仕様・原文
```

現時点ではドキュメントのみ。コードは監査通過後の Phase 1 から追加する。

## 注意

- 非公開の研究システム。一般公開しない。投資助言を目的としたものではない。
- 既存プロジェクト（既存リポジトリ・Vercel・Supabase・サイト）は一切流用しない。
