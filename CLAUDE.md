# CLAUDE.md — 短期急騰AI研究プラットフォーム

Claude Code はこのプロジェクトの**主任開発エージェント**である。
ただし投資分析思想・教師データ設計・予測ロジックを**独断で簡略化・変更してはならない**。

## 0. 正本ドキュメントと優先順位

| 優先 | 文書 |
|---|---|
| 1 | 最新の ChatGPT 監査原文 — [docs/requirements/audit-2026-09-15-phase-0.1.original.txt](docs/requirements/audit-2026-09-15-phase-0.1.original.txt) |
| 2 | 実装指示書 v1.0 原文 — [docs/requirements/implementation-instructions-v1.0.original.txt](docs/requirements/implementation-instructions-v1.0.original.txt) |
| 3 | v5.1 原文 — `docs/prompts/short-surge-v5.1.md`（**未受領**） |
| 詳細仕様 | [docs/specs/](docs/specs/)（lifecycle / universe / teacher-labels / regression-fixtures） |

`*.formatted.md` は閲覧用の整形版であり、原文として扱わない。

---

## 1. 絶対ルール（違反禁止）

### 1-1. 完全新規プロジェクト
- 既存のプロジェクト・リポジトリ・Vercel Project・Supabase Project・既存サイトを**使用・流用・改造しない**。
- 既存の似たプロジェクト（例: `C:\Users\rorom\jp_surge_radar`）のコードをコピー・統合しない。読みに行かない。
- 既存システムとの統合はユーザーが後から明示した場合のみ。

### 1-2. 原文保存
- v5.1 は `docs/prompts/short-surge-v5.1.md` に**一字一句そのまま**保存し、SHA-256 を `docs/prompts/MANIFEST.md` に記録する。
- 要約・短縮・リライト・条件削除・配点変更・整形をしない。**整形したものは原文と呼ばない**（必要なら `*.formatted.*` として別ファイル）。
- 旧版は削除しない。新版は別ファイル（v5.2 等）。
- v5.1 以後の確定仕様は `docs/prompts/addenda/` に別ファイルで置く。優先順位は 新しい addendum > 古い addendum > v5.1。
- **v5.1 原文の受領前に Phase 1 へ進まない。**

### 1-3. 投資ロジックを勝手に簡略化しない
- Route A〜H、スコア、閾値、ラベル定義、Entry 判断基準を「実装しやすいから」で削る・まとめる・固定値化しない。
- データ制約で実装できない場合は、黙って妥協せず **Decision Needed** として報告する。
- 自分の解釈で仕様を補った箇所は「Claude Code 解釈（要確認）」と明記する。

### 1-4. Prediction = 現在 Entry 可能のみ
- 判定状態: `SETUP_EOD` / `ENTRY` / `WATCH_BREAKOUT` / `WATCH_PULLBACK` / `WATCH_OTHER` / `REJECT`。
- **EOD 分析は ENTRY を出さない。** EOD でセットアップを見つけたら `SETUP_EOD` または WATCH とし、次の取引可能時点で再分析する。
- 正式 Prediction は、場中の ENTRY 判断分析が「現在価格から ENTRY 可能」と判断した場合のみ作る。
- `signal_reference_price`（セットアップ検出時点）と `entry_reference_price`（ENTRY 判断時点の実際の価格）を分ける。**+20% Threshold は必ず `entry_reference_price` × 1.20。**
- Prediction は作成後に書き換えない（append-only、DB で UPDATE/DELETE を拒否）。
- 詳細: [docs/specs/entry-and-episode-lifecycle.md](docs/specs/entry-and-episode-lifecycle.md)

### 1-5. Watch は Prediction ではない
- `WATCH_*` / `SETUP_EOD` は成績計測しない。
- Watch 条件に到達しても自動で ENTRY にしない。`TRIGGER_HIT → REANALYSIS` を必ず経る。
- 状態遷移はすべて保存し、教師データにする。

### 1-6. Prediction Episode
- 同一銘柄・同一仮説では、初回 ENTRY から Target / Failure / Thesis invalidation / Horizon end までを 1 Episode とする。
- Episode 中の再評価は State Transition として保存し、新しい Prediction を作らない。成績は Episode 単位で数える。

### 1-7. 時刻の同期
- 米国株の円換算は `fx_observed_at <= decision_cutoff_at` を必須とする。
- `entry_price_observed_at <= decision_cutoff_at`。
- 価格カットオフ後に取得した材料を、そのカットオフの価格に対して「未織り込み」と評価しない。

### 1-8. 分足の範囲
- 全 Universe は日足。分足は Stage 2・SETUP_EOD・Watch・ENTRY 候補・Open Episode の銘柄のみ取得する。全銘柄の分足保存を前提にしない。

### 1-9. パス解決
- 同一の最小足の中で Target と Failure の両方に触れ、順序を解決できない場合は `AMBIGUOUS_PATH`。成功にも失敗にも分類しない。

### 1-10. Universe 定義は版管理
- 現行は `universe-1.0.0`（[docs/specs/universe-definition-v1.0.0.md](docs/specs/universe-definition-v1.0.0.md)）。変更は新しい版のファイルで行い、`universe_version` を保存する。
- 種別を判定できない銘柄を黙って含めない。

### 1-11. 過去高値を上値余地にしない
- 過去の急騰高値までの距離を Potential Upside / Reachable Zone として扱わない。過去高値は Supply Overhang・戻り売り・Distribution の兆候として扱う。
- Reachable Zone は現在の材料・需給・支持抵抗・出来高構造からのみ作り、各境界に根拠の種類を保存する。

### 1-12. News と IR に固定序列を作らない
- 情報源による固定序列を実装しない。材料の強さは 新規性・サプライズ・直接性・経済的インパクト・継続性・市場反応・未織り込み度 で評価する。
- Discovery Source と Verification Source を分離し、同一出来事は 1 つの `material_event`（`first_seen_at` 付き）に統合する。

### 1-13. ノイズ除去・紐付け
- 単純なキーワード除外は禁止。判定基準は `market_relevance`。
- `relation_type` を必ず保存。`WEAK_ASSOCIATION` 単独では強材料扱いしない。マクロ材料は因果経路が必須。

### 1-14. 教師ラベル
- Objective（価格パスからコードで確定）と Interpretive（AI の Research 判定）を分ける。
- Interpretive には `labeler_model_version` / `confidence` / `evidence` / `human_review_status` を保存する。
- 低 confidence の Interpretive ラベルを、版管理された採用ポリシーを通さずに Production ML 教師データへ入れない。
- 事前に取得可能な兆候がない突発急騰を `ACTIONABLE_FALSE_NEGATIVE` にしない。ただし事象の種類だけで一律に除外もしない。
- 詳細: [docs/specs/teacher-labels.md](docs/specs/teacher-labels.md)

### 1-15. Production と Research を分離
- Production は採用済みのモデル・ルールのみ使用。Historical Replay は `research` 側にのみ保存し、Production と混ぜない。
- Research の結果を無検証で Production に入れない。Champion / Challenger を walk-forward で比較してから昇格する。

### 1-16. データリーク禁止
- 特徴量・分析は `data_cutoff` 時点で**取得済みだった**データのみを使う（`fetched_at` / `first_seen_at` / manifest の `created_at` で判定）。
- ランダムシャッフルのみの train/test split 禁止。
- 3,000円判定は無調整価格。後日公表の分割係数や訂正データを過去時点の判断に混ぜない。
- 上場廃止銘柄も Security Master と履歴に残す。

### 1-17. 確率表示
- 十分な教師データと校正ができるまで確率の数値を表示しない。初期は ENTRY / WATCH / REJECT と根拠を表示する。

### 1-18. 監査可能性
- 重要処理には `run_id`・timestamp・`data_cutoff`・source・各種 version・error log を保存する。LLM 入力は内容ハッシュとともに保存する。

### 1-19. 回帰テスト
- [docs/specs/regression-fixtures.md](docs/specs/regression-fixtures.md) の fixture を、該当 Phase で必ず実装する。未実装の fixture は `pending` として可視化し、削除しない。

---

## 2. アーキテクチャ規約

- **ストレージ**: PostgreSQL は状態・索引・監査・結果。OHLCV・履歴 Feature・分足・チャート画像・Raw・研究成果物は Parquet + Object Storage。Postgres を全 Raw データの保存先にしない。詳細: [docs/storage-architecture.md](docs/storage-architecture.md)
- **Object Storage はプロバイダ非依存**（`ObjectStore` interface、上書き禁止）。
- **Job 実行は `JobRunner` / `Scheduler` interface 経由。** GitHub Actions を Production の実行環境として固定しない。冪等性と二重実行防止は DB で保証する。
- **市場データは `MarketDataProvider` interface 経由。** 用途（EOD / 分足履歴 / リアルタイム判断 / FX / マスタ）ごとに Provider を割り当て、run ごとに記録する。J-Quants もその実装の1つ。
- Provider やサービスの比較・選定の根拠は**公式情報のみ**。第三者の比較記事を根拠にしない。
- Web（Vercel）に重い全市場処理・学習を載せない。
- 詳細: [docs/interfaces.md](docs/interfaces.md)

---

## 3. 進め方

- **一気に最後まで作らない。** Phase（または監査ラウンド）ごとに停止して報告し、ChatGPT 監査後に次へ進む。
- 投資ロジックに関わる曖昧な判断は Decision Needed に回す。技術的判断は理由を添えて記録する。
- 未決事項は [docs/unresolved-decisions.md](docs/unresolved-decisions.md) に集約し、決まったら決定日と決定者を追記する（行は削除しない）。

### Phase 完了報告フォーマット（指示書 §49）

```
### Completed
### Files changed
### Tests
### Coverage
### Known limitations
### Decisions made
### Decisions needed
### Risks
### Next proposed phase
```

監査ラウンドで別の形式が指定された場合はそれに従う（例: Phase 0.1 は Completed / Files changed / Specification changes / Tests specified / Decisions resolved / Remaining decisions / Risks / Next proposed phase）。
「すべて完成しました」で終えない。未実装・妥協・データ制約を必ず書く。

---

## 4. 技術規約

- Web: `apps/web`（Next.js / TypeScript）。Worker: `workers/`（Python 3.12）。
- ジョブは冪等に作り、`run_id` / `idempotency_key` 単位で再実行できるようにする。
- DB スキーマ変更は `supabase/migrations/` のマイグレーションで行う。本番 DB を手作業で変更しない。
- 秘密情報はコミットしない。`.env.example` にキー名のみ記載。
- 各データソースの利用規約を確認してから取得コードを書く。全文保存不可のソースは metadata / URL / snippet / hash / 抽出特徴量のみ保存する。
- テストは本番 DB・実データの保存先に書き込まない（ローカル DB・一時ディレクトリ・テスト用バケットで隔離）。
- 外部に影響する操作（リポジトリ作成・push・クラウドリソース作成・有料契約）はユーザー確認後に行う。
