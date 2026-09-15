# 短期急騰AI研究プラットフォーム (surge-research-platform)

日本株・米国株の全上場普通株を毎営業日監視し、**3,000円以下**（米国株は `株価 × 同時点USD/JPY`）のEligible Universe全体をスクリーニングし、
チャート・需給・ニュース・材料を統合して、**現在価格からEntry可能な「約1か月以内に+20%以上」を狙える銘柄だけ**を正式Predictionとして記録し、
その後の結果を細かく教師データ化してシステム全体を継続改善する、**非公開の研究プラットフォーム**。

> 目的は「結果的に上がった株を説明するAI」ではなく、
> **現在地点からEntryでき、上昇前または上昇初期で発見するAIを育てること**。

## ステータス

| Phase | 内容 | 状態 |
|---|---|---|
| 0 | Requirements / architecture / repository setup | **完了・ChatGPT監査待ち** |
| 1〜12 | [docs/development-phases.md](docs/development-phases.md) 参照 | 未着手 |

## 共同開発体制

| 役割 | 担当 |
|---|---|
| 最終方針・投資思想・優先順位・仕様承認 | ユーザー |
| 設計・実装・DB・API連携・Web・パイプライン・ML・テスト・デプロイ | Claude Code |
| 要件監査・投資ロジック監査・データリーク監査・教師データ監査・修正指示 | ChatGPT |

大きなPhaseごとに Claude Code が報告 → ChatGPT が監査 → 次の指示、の順で進める。

## ドキュメント

| ファイル | 内容 |
|---|---|
| [CLAUDE.md](CLAUDE.md) | Claude Code が必ず守るプロジェクト規約 |
| [docs/requirements/implementation-instructions-v1.0.md](docs/requirements/implementation-instructions-v1.0.md) | 実装指示書 v1.0（ユーザー提供の原文） |
| [docs/architecture.md](docs/architecture.md) | アーキテクチャ案・技術選定根拠 |
| [docs/data-model-draft.md](docs/data-model-draft.md) | データモデル草案 |
| [docs/source-strategy.md](docs/source-strategy.md) | データソース戦略・利用条件確認方針 |
| [docs/prompt-versioning-policy.md](docs/prompt-versioning-policy.md) | プロンプト原文保存・バージョニング方針 |
| [docs/development-phases.md](docs/development-phases.md) | Phase 0〜12 の範囲と完了条件 |
| [docs/test-strategy.md](docs/test-strategy.md) | テスト戦略（データリーク検査を含む） |
| [docs/deployment-strategy.md](docs/deployment-strategy.md) | デプロイ・運用・環境分離 |
| [docs/unresolved-decisions.md](docs/unresolved-decisions.md) | 未決事項（Decision Needed） |
| [docs/prompts/](docs/prompts/) | v5.1 原文ほか、プロンプト原文の保管場所 |

## リポジトリ構成（予定）

```
apps/web/            Next.js (TypeScript) — 閲覧用Web UI / API（軽量処理のみ）
workers/             Python — バッチ・収集・特徴量・スクリーニング・LLM分析・追跡・学習
supabase/migrations/ DBマイグレーション（SQL）
.github/workflows/   スケジュールジョブ・CI
docs/                設計・方針・プロンプト原文
```

Phase 0 時点ではドキュメントのみ。コードは Phase 1 以降、監査通過後に追加する。

## 注意

- 本システムは非公開の研究システム。一般公開しない。
- 投資助言を目的としたものではない。
- 既存プロジェクト（既存リポジトリ・Vercel・Supabase・サイト）は一切流用しない。
