# データモデル草案（Phase 0）

状態: **草案**。列名・型は Phase ごとのマイグレーションで確定する。投資ロジックに関わる列（スコア定義など）は v5.1 原文受領後に見直す。

## 0. 共通規約

- 主キーは UUID または自然キー + 版。銘柄は `security_id`（内部サロゲートキー）で参照し、ティッカー変更に耐える。
- 時刻はすべて `timestamptz`（UTC保存、表示時に JST/ET 変換）。
- 取得データは **イベント時刻** と **取得時刻（`fetched_at`）** を両方持つ。
- 結果系テーブルは `run_id` を持つ。
- version 列: `prompt_version` / `rule_version` / `feature_version` / `linker_version` / `ml_version` / `label_version`。
- `prod.*` の Prediction 系は append-only（UPDATE/DELETE をトリガーで拒否）。

## 1. スキーマ一覧

| スキーマ | 役割 |
|---|---|
| `ref` | 市場・取引カレンダー・Security Master・識別子履歴・コーポレートアクション・FX |
| `market` | 価格（日足・将来は分足）、発行済株式数・浮動株 |
| `pipeline` | run・エラー・ソース取得ログ・カバレッジ |
| `universe` | 日次の3,000円判定結果・除外理由・到達Stage |
| `features` | Feature 定義と値 |
| `screening` | Stage 1 / Stage 2 結果、候補集合 |
| `materials` | ソース・生文書・ノイズ判定・Material Event・紐付け |
| `knowledge` | チャート知識ベース・事例・チャート画像 |
| `analysis` | LLM分析（Stage 3・再分析） |
| `prod` | 正式Prediction・Watch・State Transition |
| `outcomes` | Prediction と Eligible Universe 全件の将来結果 |
| `labels` | 教師ラベル・見逃しレビュー |
| `research` | Historical Replay 結果（Production と分離） |
| `ml` | モデル登録・評価 |
| `exports` | Excel 出力履歴 |

## 2. 主要テーブル

### ref
- **markets**: market_code (`JP`,`US`), timezone, currency, close_time
- **trading_calendar**: market_code, date, is_trading_day, session_open, session_close, note
- **securities**: security_id, market_code, local_code / ticker, name, exchange, security_type（普通株/ETF/REIT/優先株/ワラント/ユニット/ADR…）, is_common_stock, listing_date, delisting_date, source, first_seen_at, last_seen_at
- **security_identifier_history**: security_id, id_type (ticker/code/CIK/ISIN), value, valid_from, valid_to, known_at
- **corporate_actions**: security_id, action_type（分割・併合・上場廃止・社名変更…）, ex_date, ratio, announced_at, known_at, source
- **fx_rates**: pair (`USDJPY`), rate, observed_at, rate_type（仲値/終値/指定時刻）, source, fetched_at

### market
- **daily_bars**: security_id, trade_date, open, high, low, close, volume, turnover, vwap（取得可能な場合）, is_adjusted=false, source, fetched_at, run_id
- **adjustment_factors**: security_id, effective_date, factor, known_at
- **share_counts**: security_id, as_of_date, shares_outstanding, float_shares, source, known_at（Float turnover 用。ソースは D-07）
- **intraday_bars**（D-08 次第）: security_id, ts, o/h/l/c/v, source, fetched_at

### pipeline
- **runs**: run_id, job_name, market_code, started_at, finished_at, data_cutoff, git_sha, config_hash, versions jsonb, status, trigger (schedule/manual/replay)
- **run_errors**: run_id, stage, security_id?, source?, error_type, message, payload_ref, created_at
- **source_fetch_log**: run_id, source_id, endpoint, requested_at, http_status, item_count, bytes, duration_ms, raw_ref
- **coverage_snapshots**: run_id, market_code, date, universe_count, price_fetched_count, eligible_count, stage1_count, stage2_count, ai_analyzed_count, entry_count, watch_count, missing_security_ids_ref

### universe
- **daily_eligibility**: run_id, as_of_date, security_id, price_local, currency, fx_rate_id, price_jpy, **eligible**, exclusion_reason（価格超過/価格欠損/非普通株/上場廃止/売買停止…）, stage_reached (0〜3), routes_matched text[]

### features
- **feature_definitions**: feature_name, feature_version, formula_ref, window, description, created_at
- **daily_features**: security_id, as_of_date, feature_version, data_cutoff, run_id, 列群（return_1d/3d/5d/10d/20d, high/low distance, atr, atr_pct, realized_vol, volume, rel_volume, turnover, float_turnover, sma/ema/slope, rsi, macd, adx, bb_width, gap, body, upper_wick, lower_wick, close_position, breakout_distance, sr_candidates, vol_contraction …）
  - 保存形式（ワイド列 / jsonb / Parquet 退避）は D-03 と合わせて決める。

### screening
- **stage1_results**: run_id, security_id, as_of_date, rule_version, route_code (A〜H), matched bool, route_detail jsonb（どの条件を満たした/満たさなかったか）
- **material_candidates**: run_id, security_id, material_event_id, reason, relation_type
- **candidates**: run_id, security_id, via (`TECHNICAL` / `MATERIAL` / `BOTH`), stage1_ref, material_ref
- **stage2_results**: run_id, security_id, feature_version, knowledge_matches jsonb, chart_image_ref, stage2_pass, detail jsonb

### materials
- **sources**: source_id, name, region, source_kind（取引所開示/規制当局/政府/通信社/金融メディア/企業IR/商品市況…）, access_method（API/RSS/公式サイト）, terms_url, terms_checked_at, full_text_allowed, retention_policy
  - 情報源による**固定の重み・序列列は持たない**。
- **raw_documents**: document_id, source_id, url, url_hash, title, snippet, body_ref（許可時のみ）, content_hash, language, **published_at**, **fetched_at**, **first_seen_at**, raw_metadata jsonb
- **noise_decisions**: document_id, market_relevance（数値/区分）, is_noise, reason, filter_version, decided_by (rule/llm), analysis_ref
- **material_events**: material_event_id, canonical_title, event_type, **first_seen_at**, discovery_document_id, occurred_at?, scope (company/industry/macro/geopolitical…), clustering_version, created_at
- **event_documents**: material_event_id, document_id, role (`DISCOVERY` / `VERIFICATION` / `COVERAGE` / `UPDATE`), added_at
- **event_attributes**（版管理、上書きしない）: material_event_id, attribute_version, as_of, novelty, surprise, directness, economic_impact, persistence, market_reaction, priced_in, produced_by (rule/llm), llm_analysis_ref
- **entity_links**: material_event_id, security_id, **relation_type**（`DIRECT_COMPANY` / `SUBSIDIARY` / `PRODUCT` / `CUSTOMER` / `SUPPLIER` / `COMPETITOR` / `INDUSTRY` / `POLICY_EXPOSURE` / `COMMODITY_EXPOSURE` / `FX_EXPOSURE` / `RATE_EXPOSURE` / `GEOPOLITICAL_EXPOSURE` / `WEAK_ASSOCIATION`）, **causal_path jsonb**（マクロ系は必須: 事象→供給/価格→売上/コスト→利益→株価）, direction (+/−/不明), confidence, linker_version, linked_at
  - CHECK: マクロ系 relation_type は causal_path 必須。

### knowledge
- **chart_concepts**: concept_id, name（下ヒゲ/上ヒゲ/包み足/三角持ち合い/フラッグ/ボックス/GC/MA Reclaim/VWAP/Anchor VWAP/出来高/売り枯れ/Distribution/Failed Breakout/急騰後崩壊/健全な押し目…）, version, definition, mechanism, positive_context, negative_context, counterexamples, numerical_features jsonb, source_note
- **chart_concept_examples**: example_id, concept_id, security_id, date_from, date_to, example_type (`POSITIVE` / `FAILED` / `COUNTEREXAMPLE`), chart_image_ref, feature_snapshot jsonb, note
  - 事例の日付以降のデータを、その事例より前の予測に使わない（リーク防止）。
- **chart_images**: image_id, security_id, as_of_date, window, render_version, storage_ref, content_hash

### analysis
- **llm_analyses**: analysis_id, run_id, security_id, analysis_kind (`STAGE3` / `REANALYSIS` / `MATERIAL` / `RESEARCH`), data_cutoff, prompt_version, rule_version, feature_version, llm_provider, llm_model, input_bundle_ref, input_hash, output jsonb, decision (`ENTRY` / `WATCH_BREAKOUT` / `WATCH_PULLBACK` / `WATCH_OTHER` / `REJECT`), created_at, cost_tokens
- **llm_derived_features**: analysis_id, novelty_score, material_duration, market_psychology, priced_in, supply_overhang, reachable_zone_quality, chart_meaning（seller exhaustion / absorption / accumulation / distribution / breakout acceptance / failed breakout / supply overhang / demand vacuum / dead-cat bounce / healthy pullback …）
  - LLM出力は「真実」ではなく検証対象の Feature として保存。

### prod（append-only）
- **predictions**: prediction_id, run_id, analysis_id, created_at (timestamp), security_id, ticker_at_prediction, **entry_reference_price**, entry_price_basis（D-01）, currency, fx_rate, fx_rate_id, driver_type, technical_state, material_event_ids uuid[], **threshold_20 = entry_reference_price × 1.20**, reachable_zone jsonb, failure_line, failure_distance, rationale, falsifiers, prompt_version, rule_version, ml_version, llm_provider, llm_model, feature_version, data_cutoff, source_watch_id?, snapshot_hash
- **watches**: watch_id, analysis_id, security_id, created_at, watch_type (`WATCH_BREAKOUT` / `WATCH_PULLBACK` / `WATCH_OTHER`), trigger_condition jsonb, invalidation_condition jsonb, expires_at, reference_price_at_watch（**評価基準には使わない**）
- **state_transitions**: transition_id, subject_type (watch/prediction), subject_id, security_id, from_state, to_state（`WATCH_*` / `TRIGGER_HIT` / `REANALYSIS` / `ENTRY` / `FAILED_BREAKOUT` / `EXPIRED` / `REJECT` …）, cause, evidence jsonb, run_id, analysis_id?, occurred_at, detected_at

### outcomes
- **prediction_outcomes**: prediction_id, horizon_days (1/3/5/10/20), computed_at, price_basis, close_return, mfe, mae, hit_10, hit_20, hit_30, days_to_20, failure_line_hit, hit_20_before_failure, failure_before_20, same_bar_ambiguity（D-09）, corporate_action_flag, delisted_flag
- **universe_outcomes**: security_id, as_of_date, horizon_days, 上記と同等の指標（Entry Reference Price の代わりに as_of 時点の価格基準、D-01 と整合させる）
  - 見逃し研究用。3,000円以下だった全銘柄について保存。

### labels
- **teacher_labels**: label_id, subject_type (prediction/universe_row/watch/transition), subject_id, label, label_version, labeled_by (rule/llm/human), evidence jsonb, created_at
  - label 候補: `PREDICTIVE_SUCCESS` / `STATE_CONFIRMED_SUCCESS` / `FALSE_POSITIVE` / `FAILED_BEFORE_TARGET` / `PRICED_IN_ERROR` / `REACHABLE_ZONE_ERROR` / `DISTRIBUTION_ERROR` / `FALSE_PULLBACK` / `ACTIONABLE_FALSE_NEGATIVE` / `PRICE_SUCCESS_EXOGENOUS` / `OUT_OF_SCOPE_SHOCK` / `OUT_OF_SCOPE_LATE`
  - 各ラベルの判定基準は Phase 10 で ChatGPT 監査のうえ確定（D-13）。
- **false_negative_reviews**: review_id, security_id, surge_start_date, available_info_cutoff, research_analysis_id, verdict (`ACTIONABLE` / `NOT_ACTIONABLE_SHOCK` / `LATE` …), reasoning, reviewed_by

### research
- **replay_runs**: replay_run_id, base_period, rule_version, model_version, git_sha, created_at, purpose
- **replay_predictions / replay_outcomes**: prod 系と同形だが `replay_run_id` を必須とし、**prod テーブルと物理的に別テーブル**。

### ml
- **model_registry**: model_id, ml_version, role (`CHAMPION` / `CHALLENGER` / `RETIRED`), layer (`CANDIDATE_GENERATION` / `INTERPRETATION` / `ENTRY_PREDICTION` / `STATE_TRANSITION`), algorithm, feature_version, train_start, train_end, artifact_ref, promoted_at, promoted_by
- **model_evaluations**: model_id, fold_id, train_end, test_start, test_end, metrics jsonb（Route別/Driver別/Feature別）, calibration jsonb

### exports
- **excel_exports**: export_id, run_id, created_at, scope, storage_ref, row_counts

## 3. 未確定点（本草案に関係する Decision Needed）

D-01 Entry Reference Price の定義 / D-03 特徴量の保存形式と容量 / D-07 浮動株データ源 / D-08 分足の要否 / D-09 同一足で+20%と失敗ラインの両方に触れた場合の扱い / D-13 教師ラベルの判定基準。詳細は [unresolved-decisions.md](unresolved-decisions.md)。
