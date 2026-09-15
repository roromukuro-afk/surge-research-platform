# 開発フェーズ

状態: **v0.1（Phase 0.1 監査是正後）** — 2026-09-15
各 Phase（または監査ラウンド）の完了時に停止 → 報告 → ChatGPT 監査 → ユーザー承認 → 次へ。

| Phase | 名称 | 主な成果物 | 完了条件（案） | 実装する回帰 fixture | 前提 |
|---|---|---|---|---|---|
| 0 | Requirements / architecture / setup | 文書一式、ローカル Git | 監査で概ね合格 | — | — |
| **0.1** | 監査是正 | ストレージ分離、interface 設計、lifecycle / universe / labels / fixtures 仕様、Provider 比較 | 監査で承認 | 仕様のみ | — |
| 1 | Security Master + Universe | 新規 Postgres・マイグレーション基盤、`JobRunner`/`Scheduler`/`ObjectStore`/`MarketDataProvider` の最小実装（Local + 1実装）、manifest、`universe-1.0.0` の実装、DB ロール分離、原文ハッシュの CI、認証付き Pipeline 画面の最小版 | JP/US の銘柄が Universe 定義どおりに取り込まれ、除外理由・`TYPE_UNKNOWN` の件数が DB と画面で確認できる | RF-12, RF-14, RF-15 | **v5.1 原文（D-00）**、監査通過、D-03a, D-06a（開発は Free で可）, D-07a（マスタ用）, D-10a〜e, D-14, D-22 |
| 2 | Market Data + 3000円 Hard Filter | 全銘柄日足（Parquet）、FX（`fx_observed_at`）、Universe 判定履歴、カバレッジ監視、Universe 画面 | 取得率と Eligible 件数が毎営業日記録され、境界値・FX 同期・訂正データのテストが通る | RF-09（Universe 部分）, RF-11, RF-16 | D-02a, D-03b, D-07a（EOD 用） |
| 3 | Stage 1 Technical Screening | Feature Engine（§15）、v5.1 Route A〜H のコード化、as-of 読み取り層 | Route ごとの該当理由が再現でき、Feature のゴールデンテストとリーク検査が通る | RF-01（Feature 部分） | v5.1 精読結果の監査 |
| 4 | News / Disclosure Collection | ソース規約調査表、コレクタ（常駐型 Runner）、raw 保存、`first_seen_at`、Materials 画面 | 規約確認済みソースから継続収集でき、取得ログとカバレッジが見える | — | D-05a, D-12 |
| 5 | Noise Filter + Entity Linking + Material Event | `market_relevance`、同一出来事の統合、Discovery/Verification、`relation_type`・因果経路、Material 候補、価格カットオフと材料の扱い | 評価用サンプルでの精度を報告。Technical と独立に候補を生成 | RF-04, RF-05（材料部分） | D-11, D-19 |
| 6 | Chart Knowledge Base + Stage 2 | 知識ベース、候補の分足取得、チャート画像、Stage 2 判定 | 各概念に正例・失敗例・反例があり、Stage 2 の結果が根拠付きで保存される | — | D-08a |
| 7 | LLM Stage 3 EOD Analysis | 入力バンドル、プロンプト組み立て（原文 + addenda）、出力検証器（Reachable Zone の根拠種類の検査を含む）、`SETUP_EOD`/`WATCH_*`/`REJECT` | 候補に対する判定と根拠が再現可能な形で保存される | RF-01（検証器部分）, RF-05（分析部分）, RF-08 | D-11 |
| 8 | ENTRY 判断 / Watch / Prediction / Episode | 場中 Runner、リアルタイム Provider、`entry_decision`・`watch_monitor`、append-only の Prediction、Episode、State Transition、Predictions / Watch 画面 | Watch 到達だけで ENTRY にならない・Threshold が entry 価格基準・Episode の重複計上がないことをテストで示す | RF-03, RF-06, RF-07, RF-09（ENTRY 部分） | **D-06b**, D-01a, D-01b, D-17a〜c, D-18, D-20, D-21 |
| 9 | Outcome Tracking + Excel Export | Episode と全 Eligible 銘柄の Outcome、パス解決、Results 画面、Excel | 分割・上場廃止・休場・分足欠損を含むケースで正しく計算される | RF-06（集計部分）, RF-10 | D-09a, D-15 |
| 10 | Teacher Dataset | Objective / Interpretive ラベル、採用ポリシー、見逃しの Research 判定、状態遷移の教師データ | ラベル基準が監査済みで、突発急騰が ACTIONABLE_FALSE_NEGATIVE にならないことをテストで示す | RF-02, RF-13 | D-13a, D-13b |
| 11 | ML / Weight Learning | 4層の学習、walk-forward、条件付き Weight、Feature interaction | Champion / Challenger 比較がリークなしで生成される | RF-15（Replay 部分） | 教師データ量の十分性の判断 |
| 12 | Model Lab / Continuous Improvement | Model Lab 画面、Route/Driver/Feature 別成績、LLM 評価精度、version 比較、昇格フロー | Challenger の昇格が監査ログ付きで行える | — | — |

## 各 Phase 共通の Definition of Done

- マイグレーション・コード・テストがコミットされている
- `run_id` / cutoff 類 / version / provider_bindings / error log が保存される
- その Phase で実装すべき回帰 fixture が通る（未到達のものは pending のまま残す）
- 未実装・妥協・データ制約が報告に書かれている
