# CLAUDE.md — 短期急騰AI研究プラットフォーム

Claude Code はこのプロジェクトの**主任開発エージェント**である。
ただし投資分析思想・教師データ設計・予測ロジックを**独断で簡略化・変更してはならない**。

## 0. 正本ドキュメントと優先順位

| 優先 | 文書 |
|---|---|
| 1 | 最新の ChatGPT 監査原文 — [Phase 1 開始条件（2026-09-16）](docs/requirements/audit-2026-09-16-phase-1-start-conditions.original.txt) > [Phase 0.2 最終パッチ](docs/requirements/audit-2026-09-15-phase-0.2-final-patch.original.txt) > [Phase 0.2](docs/requirements/audit-2026-09-15-phase-0.2.original.txt) > [Phase 0.1](docs/requirements/audit-2026-09-15-phase-0.1.original.txt) |
| 2 | 実装指示書 v1.0 原文 — [docs/requirements/implementation-instructions-v1.0.original.txt](docs/requirements/implementation-instructions-v1.0.original.txt) |
| 3 | v5.1 原文 — `docs/prompts/short-surge-v5.1.original.md`（**未受領。会話内でユーザーが確定させた全文を受領後に登録**） |
| 詳細仕様 | [docs/specs/](docs/specs/)（lifecycle / universe / teacher-labels / regression-fixtures） |

`*.formatted.md` は閲覧用の整形版であり、原文として扱わない。

---

## 1. 絶対ルール（違反禁止）

### 1-1. 完全新規プロジェクト
- 既存のプロジェクト・リポジトリ・Vercel Project・Supabase Project・既存サイトを**使用・流用・改造しない**。
- 既存の似たプロジェクト（例: `jp_surge_radar`）のコードをコピー・統合しない。読みに行かない。変更もしない。

### 1-2. 原文保存
- v5.1 は、**会話内でユーザーが確定させた全文そのものを Canonical Source とする**（別の「元ファイル」を待たない）。受領したら `docs/prompts/short-surge-v5.1.original.md` に一字一句変更せず保存し、保存時点の SHA-256 を `docs/prompts/MANIFEST.md` に記録する。formatting / normalization / typo correction をしない。
- **Canonical v5.1（immutable original）と post-v5.1 decisions（versioned addenda）を混ぜない。** v5.1 ファイルに addendum の文言を書き込まない。LLM 入力でも別セクション・別ハッシュで持つ。
- 要約・短縮・リライト・条件削除・配点変更・整形をしない。整形したものは原文と呼ばない。
- 旧版は削除しない。addenda の優先順位は 新しい addendum > 古い addendum > v5.1。
- **v5.1 Canonical の登録前に Phase 1 へ進まない。** Phase 1 の終了時には必ず停止し、ChatGPT 監査を受けてから Phase 2 へ進む。

### 1-3. 投資ロジックを勝手に簡略化しない
- Route A〜H、スコア、閾値、ラベル定義、Entry 判断基準を「実装しやすいから」で削る・まとめる・固定値化しない。
- データ制約で実装できない場合は Decision Needed として報告する。自分の解釈で補った箇所は「Claude Code 解釈（要確認）」と明記する。

### 1-4. Setup・ENTRY・価格
- 判定状態: `TECHNICAL_SETUP_EOD` / `POST_CLOSE_CATALYST_SETUP` / `ENTRY` / `WATCH_BREAKOUT` / `WATCH_PULLBACK` / `WATCH_OTHER` / `REJECT`。
- **EOD 分析・引け後の材料分析は ENTRY を出さない。** 正式 Prediction は、場中の ENTRY 判断分析が「現在価格から ENTRY 可能」と判断した場合のみ。
- 引けまでのチャート・価格・出来高によるセットアップ（`TECHNICAL_SETUP_EOD`）と、引け後の新規材料によるセットアップ（`POST_CLOSE_CATALYST_SETUP`）を分ける。引け後の材料を EOD 価格の未織り込み評価に使わない。
- 価格は3つを分ける: `signal_reference_price`（記録用）、`decision_price` / `decision_price_observed_at`（AI が判断時に参照した価格）、`entry_reference_price` / `entry_price_observed_at`（判断後に現実に取引可能だったとみなす評価価格）。
- **成績・+20% Threshold は `entry_reference_price` だけを基準にする。** 算出方式は D-01a で決めるまで固定しない。
- **3,000円 Hard Filter を ENTRY 時に2回確認する。** `decision_price` で超えていれば ENTRY にしない。`decision_price` が3,000円以下でも `entry_reference_price` が超えていれば、Prediction・Episode を作らず `ENTRY_ABORTED_PRICE_LIMIT`（研究ログのみ、成績に含めない）。再び3,000円以下になったら再分析して新しい ENTRY 判断とする。
- Prediction は作成後に書き換えない（append-only）。
- 詳細: [docs/specs/entry-and-episode-lifecycle.md](docs/specs/entry-and-episode-lifecycle.md)

### 1-5. Watch は Prediction ではない
- Watch 条件に到達しても自動で ENTRY にしない。`TRIGGER_HIT → REANALYSIS` を必ず経る。状態遷移はすべて保存する。

### 1-6. Episode・Horizon・Failure Line
- 同一銘柄・同一仮説では、初回 ENTRY から Target / Failure / Thesis invalidation / Horizon end までを 1 Episode とし、成績は Episode 単位で数える。Episode 中の再評価は State Transition。
- **Horizon: S0 = ENTRY 成立セッション（ENTRY 時刻以降の S0 の値動きを含む）、S1 = 翌取引セッション、Primary Horizon は S20 close まで。** Watch/Setup の開始から数えない。REAFFIRMED などでリセットしない。新しい Episode のときだけ新しい Horizon。
- **`initial_failure_line` は Prediction 作成時に固定し、変更しない。** Primary Outcome・Teacher Label・`INITIAL_FAILURE_HIT` はこれで判定する。`current_risk_line` は State Transition として変更でき、研究用に別に評価する。
- **Outcome は二層。** `primary_episode_outcome`（正式評価、Episode 終了まで: TARGET_HIT / INITIAL_FAILURE_HIT / THESIS_INVALIDATED / HORIZON_EXPIRED 等）と `counterfactual_horizon_outcome`（研究用、当初 S20 close まで）。THESIS_INVALIDATED 後の +20% 到達を Primary の成功に戻さない。

### 1-7. 時刻と情報の利用可能性
- 材料は `source_published_at` / `system_first_seen_at` / `ingested_at` / `available_to_model_at` を持つ。
- **Production と Historical Replay は `available_to_model_at <= decision_cutoff_at` の情報だけを使う。** `source_published_at` を利用可能時刻の代わりにしない。
- backfill した情報を、過去の Prediction・Replay が知っていたことにしない。
- 米国株の円換算は `fx_observed_at <= decision_cutoff_at`（ENTRY）/ `<= price_cutoff_at`（EOD）。

### 1-8. データの範囲と用途
- 全 Universe は日足。分足は Stage 2・Setup・Watch・ENTRY 候補・Open Episode のみ。約定データはパス解決・entry 価格算出に必要な時間帯のみ。
- **J-Quants の分足・ティックは日次更新（16:30頃）なので、場中の ENTRY 判断・Watch 監視に使わない。** Historical research / EOD / Replay / Teacher data 用に限る。

### 1-9. Outcome
- **米国株: 3,000円 Eligibility は JPY 換算、+20% Threshold と価格 Outcome は USD 建て。** JPY リターンは補助 Outcome。
- raw（取引された無調整価格）を保存し、Outcome は分割・併合を ENTRY 時点の株数ベースに換算した比較可能な系列で計算する。**配当は +20% Target に加算しない。**
- パス解決: 始値で既に跨いでいれば始値のイベント → 日足 → より細かい分足 → 利用可能なら約定 → それでも順序不明なら `AMBIGUOUS_PATH`。より細かいデータが仕様上存在しないため順序不明なら `AMBIGUOUS_PATH`、本来あるはずの細かいデータが欠損していれば `UNRESOLVED_MISSING_DATA`。成功・失敗に恣意的に寄せない。

### 1-10. Universe 定義は版管理
- 現行は `universe-1.0.0`。変更は新しい版のファイルで行う。種別を判定できない銘柄を黙って含めない。

### 1-11. 過去高値
- **過去の急騰高値まで戻ることを上昇根拠・Potential Upside にしない。**
- **過去高値の参照は禁止ではない。** Resistance・Supply Overhang・戻り売り候補・高値掴み保有者の存在・Reachable Zone までの障害として、必要に応じて積極的に使う。
- 過去高値は Resistance・Supply Overhang・Historical obstacle・高値掴み保有者の存在可能性として保存・評価する。
- Reachable Zone は現在の材料・需給・支持抵抗・出来高構造から作る。**過去高値が存在するだけで Reachable Zone の上限を機械的・単調に引き下げる制約は設けない**（新しい強材料・出来高・価格受容・高値突破で Supply Overhang の意味が弱まる・失効することがある）。ただし過去高値が遠いほど上昇余地が増える作りにしてはならない。

### 1-12. News と IR に固定序列を作らない
- 材料の強さは 新規性・サプライズ・直接性・経済的インパクト・継続性・市場反応・未織り込み度 で評価する。Discovery Source と Verification Source を分離し、同一出来事は 1 つの `material_event` に統合する。

### 1-13. ノイズ除去・紐付け
- 単純なキーワード除外は禁止（`market_relevance`）。`relation_type` を必ず保存し、`WEAK_ASSOCIATION` 単独では強材料扱いしない。マクロ材料は因果経路が必須。

### 1-14. 教師ラベル
- Objective（価格パスからコードで確定）と Interpretive（AI の Research 判定）を分ける。Interpretive には `labeler_model_version` / `confidence` / `evidence` / `human_review_status` を保存し、版管理された採用ポリシーを通さずに Production ML 教師データへ入れない。
- 見逃しは3分類: `ACTIONABLE_FALSE_NEGATIVE`（cutoff 時点でシステムが実際に利用可能だった情報から拾えたのに落とした）/ `PIPELINE_MISSED_ACTIONABLE_SIGNAL`（市場には cutoff 前から情報があったが、収集障害・遅延で `available_to_model_at` が cutoff 後。Prediction Model の False Negative にせず Pipeline 改善用）/ `OUT_OF_SCOPE_SHOCK`（市場にも合理的な前兆がなかった）。
- backfill された情報を、当時 AI が知っていたことにしない。事象の種類だけで一律に除外もしない。

### 1-15. Production と Research を分離
- Historical Replay は `research` 側にのみ保存する。Research の結果を無検証で Production に入れない。Champion / Challenger は walk-forward で比較する。

### 1-16. データリーク禁止
- 特徴量・分析は `data_cutoff` 時点で利用可能だったデータのみ（`available_to_model_at` / `fetched_at` / manifest `created_at` で判定）。
- ランダムシャッフルのみの train/test split 禁止。3,000円判定は raw 価格。後日公表の分割係数・訂正データを過去の判断に混ぜない。上場廃止銘柄を残す。

### 1-17. 確率表示
- 十分な教師データと校正ができるまで確率の数値を表示しない。

### 1-18. 監査可能性
- 重要処理には `run_id`・timestamp・cutoff 類・source・各種 version・error log を保存する。LLM 入力はハッシュとともに保存する。

### 1-19. 回帰テスト
- [docs/specs/regression-fixtures.md](docs/specs/regression-fixtures.md)（RF-01〜RF-24）を該当 Phase で必ず実装する。未実装のものは `pending` として残し、削除しない。

---

## 2. アーキテクチャ規約

- **ストレージ**: PostgreSQL は状態・索引・監査・結果。OHLCV・分足・約定・履歴 Feature・チャート画像・Raw・研究成果物は Parquet + Object Storage。詳細: [docs/storage-architecture.md](docs/storage-architecture.md)
- **Object Storage はプロバイダ非依存**（`ObjectStore` interface、上書き禁止）。
- **Job 実行は `JobRunner` / `Scheduler` interface 経由。** GitHub Actions を Production の実行環境として固定しない。
- **市場データは `MarketDataProvider` interface 経由。** 用途ごとに Provider を割り当て、run ごとに記録する。
- Provider・サービスの比較と選定の根拠は**公式情報のみ**。
- Web（Vercel）に重い全市場処理・場中監視・学習を載せない。
- Supabase は Phase 1 開発を Free で始めてよいが、**Production Architecture を Free の上限に合わせて縮小しない。** Production 開始前にプランを再評価する。
- 詳細: [docs/interfaces.md](docs/interfaces.md)

---

## 3. 進め方

- **一気に最後まで作らない。** Phase（または監査ラウンド）ごとに停止して報告し、ChatGPT 監査後に次へ進む。
- 投資ロジックに関わる曖昧な判断は Decision Needed に回す。
- 決定・未決事項は [docs/unresolved-decisions.md](docs/unresolved-decisions.md) に記録する（行は削除しない）。

### 報告フォーマット

Phase 完了報告（指示書 §49）:

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

監査ラウンドで別の形式が指定された場合はそれに従う。
「すべて完成しました」で終えない。未実装・妥協・データ制約を必ず書く。

---

## 4. 技術規約

- Web: `apps/web`（Next.js / TypeScript）。Worker: `workers/`（Python 3.12）。
- ジョブは冪等に作り、`run_id` / `idempotency_key` 単位で再実行できるようにする。
- DB は本プロジェクト専用の Cloud Supabase（Free）。`loop-vocabulary`（Pause 中、削除禁止）・`kaiji-radar` を流用しない。
- **`supabase/migrations/` が DB 設計の唯一の正本。** Dashboard の手作業を正本にしない。Cloud に適用した DDL と Git 上の migration を一致させる。
- ローカル Supabase（Docker）は migration 検証・integration test・オフライン開発の補助であり、Phase の blocker にしない。
- 接続情報は Supabase CLI・ローカル環境変数・GitHub Secrets に置く。hard-code・commit・チャット出力・service role key のログ出力を禁止。
- **このリポジトリは Public。** 公開してよいのはコード・設計文書・Prompt・Schema・Test 等のみ。
- **絶対に commit しない**: API key / secret / token、`.env` / `.env.local` 等、Supabase service role key、Provider の認証情報、利用規約上再配布できない Raw ニュース等のデータ、Raw market data の大量ダンプ、Production DB dump、Object Storage 内の研究データ、Prediction の実データ、その他の認証情報。
- 研究データ・Prediction 実データ・Raw 取得データは Private な Supabase / Object Storage 側にのみ保持する。テスト fixture は合成データのみ。
- `.gitignore` と GitHub の secret scanning / push protection を前提にし、commit 前に `git status` と差分で対象ファイルを確認する。ローカルパス・個人のメールアドレスも書かない。
- 各データソースの利用規約を確認してから取得コードを書く。
- テストは本番 DB・実データの保存先に書き込まない。
- 外部に影響する操作（リポジトリ作成・push・クラウドリソース作成・有料契約）はユーザー確認後に行う。
