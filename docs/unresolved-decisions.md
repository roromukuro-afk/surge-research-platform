# 決定事項・未決事項（Decision Log）

行は削除しない。決定したら「決定済み」へ移し、日付・決定者・内容・根拠文書を書く。
種別: 投資ロジック（ユーザー/ChatGPT 判断必須） / 技術 / 費用 / 運用 / 規約

---

## A. 決定済み

| ID | 論点 | 決定内容 | 決定日 / 決定者 | 根拠 |
|---|---|---|---|---|
| D-00 | v5.1 の Canonical Source | 会話内でユーザーが確定させた v5.1 全文そのものを Canonical とし、`docs/prompts/short-surge-v5.1.original.md` に一字一句変更せず保存、保存時点の SHA-256 を MANIFEST に記録。formatting / normalization / typo correction をしない。**登録そのものは全文の受領待ち** | 2026-09-15 / 監査 0.2 最終 #8 | audit final #8 |
| D-01 | Entry の基準価格 | signal / decision / entry を分離。成績・Threshold は `entry_reference_price`。`actual_fill` は任意。算出方式は D-01a | 2026-09-15 / 監査 0.1 #6 → 0.2 #3 | audit 0.1 #6, 0.2 #3 |
| D-02 | USD/JPY の時点 | `fx_observed_at <= decision_cutoff_at` 必須 | 2026-09-15 / 監査 0.1 #7 | audit 0.1 #7 |
| D-03 | ストレージ設計 | Postgres = 状態・索引・結果、大量の履歴 = Parquet + Object Storage | 2026-09-15 / 監査 0.1 #3 | audit 0.1 #3 |
| D-03a | Supabase のプラン | **Phase 1 開発は Free で開始してよい。** Production Architecture を Free 上限に合わせて縮小しない。Bulk historical data の分離を維持。**Production 開始前にプランを再評価** | 2026-09-15 / 監査 0.2 最終 #7 | audit final #7 |
| D-05 | Worker 実行環境 | `JobRunner` / `Scheduler` で抽象化 | 2026-09-15 / 監査 0.1 #4 | audit 0.1 #4 |
| D-06 | J-Quants の位置付け | Free = 開発用、Production EOD は Light 以上を候補、Standard は必須にしない。分足・ティックは日次16:30頃更新のため場中 ENTRY 判断に使わない | 2026-09-15 / 監査 0.1 #15、0.2 #2 | audit 0.2 #2、公式更新スケジュール |
| D-07 | 米国株データの選び方 | `MarketDataProvider`、用途別 Provider、公式仕様のみで比較 | 2026-09-15 / 監査 0.1 #14 | audit 0.1 #14 |
| D-08 | 分足の要否 | 全 Universe は日足、Stage 2・Watch・ENTRY 候補のみ分足 | 2026-09-15 / 監査 0.1 #8 | audit 0.1 #8 |
| D-09 | 同一足で両方に触れた場合 | `AMBIGUOUS_PATH` | 2026-09-15 / 監査 0.1 #9 | audit 0.1 #9 |
| D-09a | データ欠損の扱い | 始値 → 日足 → 分足 → 約定 → AMBIGUOUS_PATH。降りられないデータ不足は `UNRESOLVED_MISSING_DATA` | 2026-09-15 / 監査 0.2 #11 | audit 0.2 #11 |
| D-09b | 「データなし」と「欠損」の区別 | 細かいデータが仕様上存在しない → `AMBIGUOUS_PATH`、本来あるはずのデータが欠損 → `UNRESOLVED_MISSING_DATA`（Claude Code 案を採用） | 2026-09-15 / 監査 0.2 最終 #6 | audit final #6 |
| D-10 | Universe の範囲 | `universe-1.0.0` | 2026-09-15 / 監査 0.1 #10 | audit 0.1 #10 |
| D-13 | 教師ラベルの構造 | Objective / Interpretive | 2026-09-15 / 監査 0.1 #11 | audit 0.1 #11 |
| D-17 | 同一銘柄の重複 ENTRY | Prediction Episode | 2026-09-15 / 監査 0.1 #12 | audit 0.1 #12 |
| D-17c | Horizon と Failure Line | ENTRY から 20 trading sessions、リセットしない。`initial_failure_line`（固定・Primary）と `current_risk_line`（研究用） | 2026-09-15 / 監査 0.2 #6・#7 | audit 0.2 #6, #7 |
| D-17d | Horizon の表記 | S0 = ENTRY 成立セッション（ENTRY 時刻以降の S0 の値動きを含む）、S1 = 翌取引セッション、Primary Horizon は S20 close まで（Claude Code 案を採用） | 2026-09-15 / 監査 0.2 最終 #5 | audit final #5 |
| D-17e | THESIS_INVALIDATED 後の Outcome | `primary_episode_outcome`（Episode 終了まで、正式評価）と `counterfactual_horizon_outcome`（当初 S20 close まで、研究用）に分離。THESIS_INVALIDATED 後の +20% 到達を Primary の成功に戻さない。**Claude Code の暫定案（Primary を S20 まで計算）は不採用** | 2026-09-15 / 監査 0.2 最終 #2 | audit final #2 |
| D-18 | ENTRY 時の3,000円再判定 | `decision_price` で再判定 | 2026-09-15 / 監査 0.2 #4 | audit 0.2 #4 |
| D-19 | 引け後に取得した材料 | `TECHNICAL_SETUP_EOD` と `POST_CLOSE_CATALYST_SETUP` を分離 | 2026-09-15 / 監査 0.2 #5 | audit 0.2 #5 |
| D-22 | 指示書・監査の原文 | D-00 と同じ考え方（会話内で確定した本文を Canonical とする）を適用し、既存の `*.original.txt` を正本として扱う。**Claude Code 解釈。監査は v5.1 について明示したもので、指示書・監査への適用に異論があれば再オープンする** | 2026-09-15 / Claude Code（監査 0.2 最終 #8 からの類推） | audit final #8 |
| D-23 | 見逃し判定と backfill | `ACTIONABLE_FALSE_NEGATIVE` / `PIPELINE_MISSED_ACTIONABLE_SIGNAL` / `OUT_OF_SCOPE_SHOCK` の3分類。backfill 情報を当時 AI が知っていたことにしない | 2026-09-15 / 監査 0.2 最終 #3 | audit final #3 |
| D-25 | 米国株の Outcome 通貨 | Eligibility = JPY 換算、Threshold・価格 Outcome = USD | 2026-09-15 / 監査 0.2 #8 | audit 0.2 #8 |
| D-26 | 判断後の entry 価格が3,000円超 | `entry_reference_price` でも再確認し、超過なら Prediction・Episode を作らず `ENTRY_ABORTED_PRICE_LIMIT`（研究ログ）。再び3,000円以下になったら再分析。**Claude Code の暫定案（Prediction は有効）は不採用** | 2026-09-15 / 監査 0.2 最終 #4 | audit final #4 |
| D-27 | 材料の利用可能時刻 | 4つの時刻。Production・Replay は `available_to_model_at <= decision_cutoff_at` のみ | 2026-09-15 / 監査 0.2 #9 | audit 0.2 #9 |
| D-28 | 分割・併合・配当 | raw を保存、Outcome は比較可能な系列、配当は Target に加算しない | 2026-09-15 / 監査 0.2 #10 | audit 0.2 #10 |
| D-29 | 過去急騰高値の役割 | 上昇根拠・Potential Upside への使用は禁止。Resistance / Supply Overhang / Historical obstacle / 高値掴み保有者として保存・評価。**存在するだけで Reachable Zone 上限を機械的・単調に引き下げる DB 制約は設けない**（Supply Overhang の意味は失効・低下しうる） | 2026-09-15 / 監査 0.2 #1 → **0.2 最終 #1 で緩和** | audit 0.2 #1, final #1 |
| D-30 | v5.1 の管理 | Canonical v5.1（immutable original）と post-v5.1 decisions（versioned addenda）を混ぜない | 2026-09-15 / 監査 0.2 #12、最終 #8 | audit 0.2 #12, final #8 |

---

## B. 未決（Remaining）

### B-1. Phase 1 の前に必要

| ID | 種別 | 論点 | Claude Code の意見 |
|---|---|---|---|
| **D-00（登録）** | 前提 | **v5.1 全文の受領と Canonical 登録** | 受領したら無加工で `short-surge-v5.1.original.md` に保存し、SHA-256 を記録してから Phase 1 に進む |
| **D-03d** | 費用/運用 | **Supabase Free プロジェクトを作成できない**（2026-09-15 作成を試行し、Free の上限「稼働中2件（ユーザー単位・組織横断）」で拒否。費用確認は $0/月）。選択肢: (a) ユーザーが既存の Free プロジェクトのどれかを一時停止する (b) 組織を有料プランにする (c) Phase 1 はローカル Supabase（Docker）で開発し、クラウドは後で作る | Claude Code は既存プロジェクトを停止しない（他システムへの影響があるため）。(c) なら Phase 1 は進められる（Docker Desktop の起動が必要）。新しい組織を作っても、上限はユーザー単位なので解決しない |

### B-2. Phase 1 中に決めればよい（pending で開始可）

| ID | 種別 | 論点 | Claude Code の意見 |
|---|---|---|---|
| D-07a | 費用 | 米国株の役割ごとの Provider（Phase 1 はマスタ用） | 未確認項目を公式ドキュメント・API で確認してから |
| D-10a | 投資ロジック | 「適格 ADR」の定義 | 判断しない。確定まで ADR は pending |
| D-10b | 投資ロジック/技術 | JP の普通株判定（`ProdCat` の値）、東証上場の外国株式、出資証券・優先出資証券、`0109 その他` | Phase 1 の最初に値を確認して提示 |
| D-10c | 投資ロジック/技術 | 米国 REIT の判定方法 | — |
| D-10d | 投資ロジック | NYSE Arca / Cboe BZX / IEX にのみ上場する普通株 | 判断しない |
| D-10e | 投資ロジック | 売買停止・監理/整理・Nasdaq Financial Status 異常 | 判断しない |
| D-14 | 運用 | 認証方式、障害通知 | Supabase Auth + 本人メールの allowlist |
| D-16 | 運用 | プロジェクト名（仮 `surge-research-platform`） | 仮名のまま |

### B-3. Phase 2〜8 の前に必要

| ID | 種別 | 論点 | 必要な時期 | Claude Code の意見 |
|---|---|---|---|---|
| D-01a | 投資ロジック | `entry_price_method` | Phase 8 前 | 場中 Provider の能力を確認してから |
| D-01b | 投資ロジック | 「次の取引可能時点」の定義 | Phase 8 前 | 判断しない |
| D-02a | 技術/投資ロジック | FX の Provider と許容遅延 | Phase 2 前 | — |
| D-03b | 費用/技術 | Object Storage プロバイダ | Phase 2 前 | Phase 1 はローカル実装 |
| D-03c | 費用 | Supabase の Production プラン（D-03a の再評価） | Production 開始前 | — |
| D-05a | 費用/技術 | Production の Runner・Scheduler | Phase 4・8 前 | 場中とニュース収集は常駐型が必要な見込み |
| D-06a | 費用 | J-Quants の Production プランと分足・ティックアドオン | Phase 2 前 | v5.1 が前場・後場の四本値を要求するかで判断 |
| **D-06b** | 費用/技術 | **日本株の場中 ENTRY 判断・Watch 監視用リアルタイム Provider** | **Phase 8 までの blocker（Phase 1 の blocker ではない）** | 公式情報のみで候補を調査 |
| D-08a | 投資ロジック | Stage 2 で取得する分足の期間 | Phase 6 前 | v5.1 を確認してから |
| D-11 | 費用/技術 | LLM プロバイダ・モデル・月額上限 | Phase 5〜7 前 | — |
| D-12 | 費用/規約 | ニュース・開示ソースの取得手段 | Phase 4 | 規約調査表を提出 |
| D-17a | 投資ロジック | `thesis_key`（同一仮説）の定義 | Phase 8 前 | 判断しない |
| D-17b | 投資ロジック | Open Episode 中の別仮説、クローズ後の再 ENTRY | Phase 8 前 | 判断しない |
| D-20 | 投資ロジック | Setup（両種別）の有効期間 | Phase 8 前 | 判断しない |
| D-21 | 投資ロジック | 15分遅延データを `decision_price` / `entry_reference_price` に使うことを許すか | Phase 8 前 | 許す場合も `latency_class` で区別 |
| D-31 | 投資ロジック | ENTRY と判断したが `entry_reference_price` を観測できなかった場合（Provider 障害・約定なし等）の `entry_attempts.status` と扱い | Phase 8 前 | 判断しない。少なくとも Prediction は作らず理由を記録すべき |
| D-32 | 投資ロジック | `price_obstacles.status` を WEAKENED / INVALIDATED にする基準（新材料・出来高・価格受容・高値突破の条件） | Phase 7 前 | 判断しない。v5.1 の記述を確認してから |

### B-4. Phase 9 以降

| ID | 種別 | 論点 | 必要な時期 |
|---|---|---|---|
| D-04 | 費用/運用 | Vercel のプラン | Web 公開時 |
| D-13a | 投資ロジック | 12ラベルの分類と判定基準、「合理的に拾えた」の基準 | Phase 10 前 |
| D-13b | 投資ロジック | `label_admission_policy` の内容 | Phase 10 前 |
| D-15 | 運用 | Excel の受け取り方法 | Phase 9 |
| D-24 | 投資ロジック | 分割・併合・配当以外の corporate action の Outcome での扱い | Phase 9 前 |
| D-33 | 投資ロジック | そもそも収集対象にしていなかったソースの情報を `PIPELINE_MISSED_ACTIONABLE_SIGNAL` に含めるか（監査は「Collector 障害・取得遅延等」と記載） | Phase 10 前 |
