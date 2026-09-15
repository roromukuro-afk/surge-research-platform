# 開発フェーズ

各 Phase 完了時に停止し、CLAUDE.md の報告フォーマットで報告 → ChatGPT 監査 → ユーザー承認 → 次 Phase。

| Phase | 名称 | 主な成果物 | 完了条件（案） | 前提となる Decision |
|---|---|---|---|---|
| 0 | Requirements / architecture / repository setup | README, CLAUDE.md, docs 一式, Git 初期化 | 監査で承認 | — |
| 1 | Security Master + Universe | 新規 Supabase・マイグレーション基盤、`ref.securities`、JP/US 銘柄一覧取得、普通株判定、上場廃止履歴、`pipeline.runs`、Pipeline 画面の最小版、認証 | JP/US 全普通株が日次で取り込まれ、件数・除外理由がDBと画面で確認できる | v5.1受領, D-03, D-04, D-06, D-07, D-10, D-14 |
| 2 | Market Data + 3000円 Hard Filter | 日足取得、FX 取得、`universe.daily_eligibility`、カバレッジ監視、Universe 画面 | 全銘柄の価格取得率と Eligible 件数が毎営業日記録され、境界値テストが通る | D-02, D-06, D-07 |
| 3 | Stage 1 Technical Screening | Feature Engine（§15 の全 Feature）、v5.1 Route A〜H のコード化、as-of 取得層、リーク検査テスト | Route ごとの該当理由が再現可能。Feature 計算のゴールデンテスト合格 | v5.1 精読結果の監査 |
| 4 | News / Disclosure Collection | ソース規約調査結果、コレクタ、raw 保存、`first_seen_at`、Materials 画面（一覧） | 規約確認済みソースから継続収集でき、取得ログとカバレッジが見える | D-05, D-12 |
| 5 | Noise Filter + Entity Linking + Material Event | `market_relevance`、同一出来事クラスタリング、Discovery/Verification 分離、`relation_type`・因果経路、Material Candidates | 評価用サンプルでの精度報告。Technical と独立した材料ルートが候補を生成 | D-11 |
| 6 | Chart Knowledge Base + Stage 2 | 知識ベース（定義〜失敗例）、チャート画像生成、Stage 2 判定 | 各概念に正例・失敗例・反例が登録され、Stage 2 結果が根拠付きで保存 | — |
| 7 | LLM Stage 3 Analysis | 入力バンドル組み立て、プロンプト組み立て（原文+addenda）、LLM呼び出し抽象化、出力スキーマ検証、LLM派生 Feature 保存 | 候補集合に対し ENTRY/WATCH_*/REJECT と根拠が再現可能な形で保存 | D-11 |
| 8 | ENTRY / WATCH / Prediction Snapshot | append-only Prediction、Watch、Watch Monitor、再分析、State Transition、Predictions/Watch 画面 | Watch 条件到達で自動 ENTRY にならず再分析されることをテストで証明 | D-01, D-08 |
| 9 | Outcome Tracking + Excel Export | Prediction/Universe 全件の 1/3/5/10/20D 追跡、MFE/MAE 等、Results 画面、Excel 出力 | 分割・上場廃止・休場を含むケースで正しく計算 | D-09, D-15 |
| 10 | Teacher Dataset | ラベル判定、見逃し Research Mode、State Transition 教師データ | ラベル基準が監査済みで、突発急騰が False Negative にならないことをテストで証明 | D-13 |
| 11 | ML / Weight Learning | 4層の学習（候補生成・解釈・Entry・状態遷移）、walk-forward、条件付き Weight、Feature interaction | Champion/Challenger 比較レポートがリークなしで生成される | 教師データ量の十分性判断 |
| 12 | Model Lab / Continuous Improvement | Model Lab 画面、Route/Driver/Feature 別成績、LLM評価精度、version 比較、昇格フロー | Challenger の昇格が監査ログ付きで行える | — |

## 各 Phase 共通の Definition of Done

- マイグレーション・コード・テストがコミットされている
- `run_id` / `data_cutoff` / version / error log が保存される
- テストが通り、リーク検査（該当 Phase）が通る
- 未実装・妥協・データ制約が報告に明記されている
