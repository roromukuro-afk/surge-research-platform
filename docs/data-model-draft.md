# データモデル草案

状態: **草案 v0.2.1（Phase 0.2 最終パッチ反映）** — 2026-09-15
列名・型は Phase ごとのマイグレーションで確定する。投資ロジックに関わる列は v5.1 原文受領後に見直す。

## 0. 共通規約

- 銘柄は内部サロゲートキー `security_id` で参照する。
- 時刻は `timestamptz`（UTC 保存）。
- 取得データは「出来事の時刻」と「システム側の時刻」を両方持つ。材料系は `source_published_at` / `system_first_seen_at` / `ingested_at` / `available_to_model_at`。
- 結果系は `run_id` を持つ。
- version 列: `prompt_version` / `rule_version` / `feature_version` / `linker_version` / `label_version` / `ml_version` / `universe_version` / `label_admission_policy_version`。
- `prod.*` は append-only（UPDATE/DELETE をトリガーで拒否）。
- 価格は銘柄の取引通貨建て（JP = JPY、US = USD）。raw（無調整）を正本とする。
- 「Postgres」はテーブル、「Parquet」は Object Storage 上のデータセット（索引は `storage.dataset_manifests`）。

---

## 1. Postgres

本ファイルは設計ドラフトであり、**実装済みスキーマの正本は `supabase/migrations/`**。Phase 1 / 1.1 で実装した `ref`（`issuers` / `securities` / `listings` / `listing_states` / `listing_symbols` / `security_names` / `security_identifiers` / `identity_migration_map`）の同一性キー・SCD2・as-of の規則は [specs/security-identity.md](specs/security-identity.md) を参照する。以下の `securities` 行のような「local_code/ticker を銘柄の identity として持つ」書き方は、Phase 1.1 で否定された（Ticker は属性）。

### ref
| テーブル | 主な列 |
|---|---|
| markets | market_code, timezone, currency, regular_session_open/close |
| trading_calendar | market_code, date, is_trading_day, session_open, session_close |
| securities | security_id, market_code, local_code/ticker, name, exchange, security_type, provider_type_code, sic_code, market_segment_code, listing_date, delisting_date, first_seen_at, last_seen_at |
| security_identifier_history | security_id, id_type, value, valid_from, valid_to, known_at |
| corporate_actions | action_id, security_id, action_type (SPLIT / REVERSE_SPLIT / DIVIDEND / SPINOFF / …), ex_date, **split_ratio_r**（旧1株あたりの新株数。2-for-1 = 2、1-for-10 = 0.1）, cash_amount, announced_at, known_at, source |
| universe_definitions | universe_version, spec_path, spec_sha256, effective_from, effective_to |
| fx_observations | pair, rate, basis, **observed_at**, provider_id, fetched_at |

### universe
| テーブル | 主な列 |
|---|---|
| current_eligibility | security_id, as_of_date, universe_version, included, exclusion_reason, price_local, fx_observation_id, price_jpy, current_stage, routes_matched |
| daily_summary | market_code, as_of_date, universe_count, eligible_count, type_unknown_count, price_missing_count, run_id |

### pipeline / storage
| テーブル | 主な列 |
|---|---|
| job_requests | request_id, job_name, job_version, run_mode, market, params, **idempotency_key (unique)**, status, claimed_by, lease_expires_at, heartbeat_at, attempt, run_id |
| runs | run_id, job_name, run_mode, started_at, finished_at, data_cutoff, price_cutoff_at, decision_cutoff_at, git_sha, config_hash, versions, provider_bindings, runner_id, status |
| run_errors | run_id, stage, security_id, source, error_type, message, payload_ref |
| source_fetch_log | run_id, provider_id / source_id, endpoint, requested_at, http_status, item_count, raw_object_key, acquisition_mode (LIVE / BACKFILL) |
| coverage_snapshots | run_id, market_code, date, 各段階の件数, missing_list_object_key |
| storage.dataset_manifests | manifest_id, dataset, partition_key, object_key, sha256, bytes, row_count, schema_version, dataset_version, produced_by_run_id, data_cutoff, created_at, supersedes_manifest_id |

### screening
| テーブル | 主な列 |
|---|---|
| candidates | run_id, security_id, as_of_date, via (TECHNICAL / MATERIAL / BOTH), stage1_detail_ref, material_event_ids |
| stage2_results | run_id, security_id, feature_version, knowledge_matches, intraday_window, chart_image_key, passed, detail |

### materials
| テーブル | 主な列 |
|---|---|
| sources | source_id, name, region, source_kind, access_method, terms_url, terms_checked_at, full_text_allowed（固定の重み・序列の列は持たない） |
| documents | document_id, source_id, url, url_hash, title, snippet, body_object_key, content_sha256, **source_published_at, system_first_seen_at, ingested_at, available_to_model_at**, acquisition_mode (LIVE / BACKFILL) |
| noise_decisions | document_id, market_relevance, is_noise, reason, filter_version, decided_by, decided_at |
| material_events | material_event_id, canonical_title, event_type, scope, **market_first_published_at, system_first_seen_at, available_to_model_at**, discovery_document_id, clustering_version |
| event_documents | material_event_id, document_id, role (DISCOVERY / VERIFICATION / COVERAGE / UPDATE), added_at |
| event_attributes（版管理） | material_event_id, attribute_version, as_of, novelty, surprise, directness, economic_impact, persistence, market_reaction, priced_in_status (EVALUATED / NOT_EVALUATED_AGAINST_EOD / …), produced_by, analysis_id |
| entity_links | material_event_id, security_id, relation_type, causal_path（マクロ系は必須）, direction, confidence, linker_version, linked_at |

- backfill 時は `system_first_seen_at` / `ingested_at` / `available_to_model_at` に backfill 実行時刻を入れる（`source_published_at` で埋めない）。
- `available_to_model_at` は、その文書・Event が分析に使える状態になった時刻。`>= ingested_at >= system_first_seen_at`（CHECK）。

### knowledge
| テーブル | 主な列 |
|---|---|
| chart_concepts | concept_id, name, version, definition, mechanism, positive_context, negative_context, counterexamples, numerical_features |
| chart_concept_examples | example_id, concept_id, security_id, date_from, date_to, example_type, chart_image_key, available_from |

### analysis
| テーブル | 主な列 |
|---|---|
| llm_analyses | analysis_id, run_id, security_id, **analysis_kind (STAGE3_EOD / POST_CLOSE_MATERIAL / ENTRY_DECISION / REANALYSIS / MATERIAL / RESEARCH)**, price_cutoff_at, decision_cutoff_at, decision_completed_at, prompt_version, rule_version, feature_version, llm_provider, llm_model, input_object_key, input_sha256, prompt_original_sha256, prompt_addenda_sha256s, output, decision, created_at |
| llm_derived_features | analysis_id, novelty_score, material_duration, market_psychology, priced_in, supply_overhang, reachable_zone_quality, chart_meaning |
| price_obstacles | analysis_id（またはルール層の run_id）, security_id, price_level, obstacle_type (PRIOR_SURGE_HIGH / VOLUME_SHELF / RESISTANCE / GAP_FILL / …), **role (RESISTANCE / SUPPLY_OVERHANG / HISTORICAL_OBSTACLE / TRAPPED_HOLDERS)**, **status (ACTIVE / WEAKENED / INVALIDATED)**, status_evidence（新材料・出来高・価格受容・高値突破など）, evidence |
| reachable_zones | analysis_id, bound (LOWER / UPPER), price, anchor_type, **anchor_role (BULLISH_BASIS / OBSTACLE_CONSIDERED / SUPPORT)**, obstacle_ref, evidence |

- CHECK: `anchor_type = 'PRIOR_SURGE_HIGH'` のとき `anchor_role <> 'BULLISH_BASIS'`（過去高値まで戻ることを上昇根拠にしない）。
- **過去高値が存在するだけで Reachable Zone の上限を機械的・単調に引き下げる DB 制約は設けない**（最終パッチ #1）。障害の効き方は `price_obstacles.status` と分析で評価する。

### prod（append-only）
| テーブル | 主な列 |
|---|---|
| setups | setup_id, **setup_type (TECHNICAL_SETUP_EOD / POST_CLOSE_CATALYST_SETUP)**, analysis_id, security_id, signal_cutoff_at, signal_reference_price, triggering_material_event_ids, priced_in_status, valid_until（D-20）, created_at |
| watches | watch_id, analysis_id, security_id, watch_type, trigger_condition, invalidation_condition, expires_at, reference_price_at_watch（評価基準に使わない） |
| predictions | 下記 |
| entry_attempts | attempt_id, analysis_id, security_id, source_setup_ids, source_watch_id, decision_cutoff_at, decision_price, decision_price_observed_at, decision_price_jpy, decision_completed_at, entry_reference_price, entry_price_observed_at, entry_price_method, entry_reference_price_jpy, fx_observation_ids, **status (PREDICTION_CREATED / ENTRY_ABORTED_PRICE_LIMIT / …)**, prediction_id（作成時のみ）, created_at。**研究ログ。ABORTED は成績に含めない** |
| actual_fills | prediction_id, actual_fill_price, actual_fill_at, broker_ref（自動売買導入まで空で可） |
| prediction_episodes | episode_id, security_id, thesis_key, opening_prediction_id (unique), opened_at, **horizon_start_session_date (S0), horizon_end_session_date (S20)** |
| episode_closures | episode_id (unique), close_reason (TARGET_HIT / INITIAL_FAILURE_HIT / THESIS_INVALIDATED / HORIZON_EXPIRED / AMBIGUOUS_PATH / UNRESOLVED_MISSING_DATA / CORPORATE_ACTION_SUSPECTED), closed_at, closed_session_index, evidence, run_id |
| risk_line_updates | update_id, episode_id, current_risk_line, effective_at, transition_id, analysis_id（初期行 = initial_failure_line） |
| state_transitions | transition_id, subject_type (setup / watch / episode), subject_id, security_id, from_state, to_state (TECHNICAL_SETUP_EOD / POST_CLOSE_CATALYST_SETUP / WATCH_* / TRIGGER_HIT / REANALYSIS / ENTRY / REAFFIRMED / DOWNGRADED / RISK_LINE_UPDATED / RISK_LINE_HIT / THESIS_INVALIDATED / FAILED_BREAKOUT / EXPIRED / REJECT …), cause, evidence, analysis_id, occurred_at, detected_at, run_id |

**prod.predictions**

| 列 | 備考 |
|---|---|
| prediction_id, episode_id, run_id, analysis_id | analysis_kind は ENTRY_DECISION / REANALYSIS のみ |
| created_at | |
| security_id, ticker_at_prediction, universe_version | |
| source_setup_ids, source_watch_id | 由来（Setup は複数可） |
| signal_reference_price | 記録用 |
| **decision_cutoff_at**（= data_cutoff）, decision_completed_at | |
| **decision_price, decision_price_observed_at**, decision_price_basis, decision_price_provider_id, decision_price_latency_class | `decision_price_observed_at <= decision_cutoff_at` |
| fx_rate, fx_observed_at, fx_observation_id, **decision_price_jpy** | 3,000円再判定に使用。`fx_observed_at <= decision_cutoff_at`、`decision_price_jpy <= 3000` |
| **entry_reference_price, entry_price_observed_at, entry_price_method**, entry_price_provider_id, entry_reference_price_jpy | `entry_price_observed_at >= decision_completed_at`、**`entry_reference_price_jpy <= 3000`**（超過時は Prediction を作らず `entry_attempts` に ABORTED） |
| price_currency | JPY / USD |
| **target_price** | `= entry_reference_price × 1.20`（CHECK） |
| **initial_failure_line**, failure_distance | 変更不可 |
| horizon_sessions | 20（CHECK） |
| reachable_zone_ref, price_obstacle_refs | |
| driver_type, technical_state, material_event_ids | |
| rationale, falsifiers | |
| prompt_version, rule_version, ml_version, feature_version, llm_provider, llm_model | |
| snapshot_sha256 | |

### outcomes
| テーブル | 主な列 |
|---|---|
| primary_episode_outcomes | **正式評価。Episode 終了時点まで。** episode_id, close_reason, closed_session_index, computed_at, price_currency, price_basis (COMPARABLE), corporate_action_ids_applied, close_return, mfe, mae, hit_10, hit_20, hit_30, days_to_20, failure_line_hit, hit_20_before_failure, failure_before_20, outcome_status (FINAL / CORPORATE_ACTION_SUSPECTED / …), delisted_flag |
| counterfactual_horizon_outcomes | **研究用。当初の S20 close まで（Episode 終了に関係なく）。** episode_id, horizon_days (1/3/5/10/20), computed_at, price_currency, corporate_action_ids_applied, counterfactual_mfe, counterfactual_mae, later_target_hit, later_target_hit_session_index, close_return |
| episode_path_resolution | episode_id, scope (PRIMARY_EPISODE / COUNTERFACTUAL_HORIZON), line_kind (PRIMARY_INITIAL / RESEARCH_RISK_LINE), path_resolution, resolution_granularity (SESSION_OPEN / DAY / INTRADAY_BAR / TRADE), resolved_session_index, resolved_at_ts, label_version |
| auxiliary_outcomes | episode_id, horizon_days, jpy_return（米国株）, fx_at_entry, fx_at_evaluation, dividend_inclusive_return |

### labels / research / ml / exports / serving

v0.1 から変更なし（[teacher-labels.md](specs/teacher-labels.md) に Objective の計算前提を追記）。

| テーブル | 主な列 |
|---|---|
| labels.objective_labels | label_id, subject_type, subject_id, label, value, label_version, computed_at |
| labels.interpretive_labels | label_id, subject_type, subject_id, label, labeler_model_version, confidence, evidence, human_review_status, information_cutoff_at, input_sha256, objective_label_ref, supersedes_label_id, created_at |
| labels.label_admission_policies | policy_version, rules, created_at |
| labels.false_negative_reviews | review_id, security_id, surge_start_at, prediction_cutoff_at, analysis_id, **verdict (ACTIONABLE_FALSE_NEGATIVE / PIPELINE_MISSED_ACTIONABLE_SIGNAL / OUT_OF_SCOPE_SHOCK / OUT_OF_SCOPE_LATE)**, available_info_doc_ids, late_info_doc_ids, interpretive_label_id |
| labels.pipeline_miss_records | review_id, document_id, source_id, source_published_at, available_to_model_at, delay_seconds, cause (COLLECTOR_OUTAGE / FETCH_DELAY / BACKFILL / …), created_at。Pipeline 改善用（Prediction Model の学習には使わない） |
| research.replay_runs / replay_predictions / replay_outcomes | prod 系と同形 + replay_run_id（prod とは別テーブル）。材料は `available_to_model_at <= decision_cutoff_at` のみ |
| ml.model_registry / model_evaluations | v0.1 と同じ |
| exports.excel_exports | export_id, run_id, created_at, scope, object_key, row_counts |
| serving.* | Web 表示用の派生キャッシュ（正本ではない） |

---

## 2. Parquet データセット

| dataset | パーティション | 主な列 |
|---|---|---|
| curated/ohlcv_daily | market, trade_date | security_id, open, high, low, close, volume, turnover, vwap（raw）, provider_id, fetched_at |
| curated/ohlcv_minute | market, trade_date | security_id, bar_ts, open, high, low, close, volume（raw、対象銘柄のみ） |
| curated/trades | market, trade_date | security_id, trade_ts, sequence（提供されれば）, price, size（raw、パス解決・entry 価格算出に必要な時間帯のみ） |
| curated/universe_eligibility | market, as_of_date | Postgres current_eligibility と同形 |
| features/daily | feature_version, market, as_of_date | security_id, data_cutoff, 指示書 §15 の Feature 群 |
| features/stage1_routes | rule_version, market, as_of_date | security_id, route_code, matched, 条件ごとの判定 |
| outcomes/universe | market, as_of_date | security_id, horizon_days, 各 Outcome 指標（comparable path） |
| raw/*, artifacts/*, research/* | — | v0.1 と同じ |

## 3. 関係する未決事項

D-01a / D-01b / D-02a / D-17a / D-17b / D-20 / D-21 / D-24 / D-31 / D-32 / D-33 / D-13a / D-13b（[unresolved-decisions.md](unresolved-decisions.md)）
