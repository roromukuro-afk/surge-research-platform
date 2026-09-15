# データモデル草案

状態: **草案 v0.1（Phase 0.1 監査是正後）** — 2026-09-15
列名・型は Phase ごとのマイグレーションで確定する。投資ロジックに関わる列は v5.1 原文受領後に見直す。

## 0. 共通規約

- 銘柄は内部サロゲートキー `security_id` で参照する（ティッカー変更に耐える）。
- 時刻は `timestamptz`（UTC 保存）。
- 取得データは「出来事の時刻」と「取得時刻（`fetched_at` / `first_seen_at`）」を両方持つ。
- 結果系は `run_id` を持つ。
- version 列: `prompt_version` / `rule_version` / `feature_version` / `linker_version` / `label_version` / `ml_version` / `universe_version` / `label_admission_policy_version`。
- `prod.*` は append-only（UPDATE/DELETE をトリガーで拒否）。
- **置き場所**: 本書の「Postgres」はテーブル、「Parquet」は Object Storage 上のデータセット（索引は `storage.dataset_manifests`）。

---

## 1. Postgres

### ref
| テーブル | 主な列 |
|---|---|
| markets | market_code, timezone, currency, regular_session_open/close |
| trading_calendar | market_code, date, is_trading_day, session_open, session_close |
| securities | security_id, market_code, local_code/ticker, name, exchange, security_type, provider_type_code, sic_code, market_segment_code, listing_date, delisting_date, first_seen_at, last_seen_at |
| security_identifier_history | security_id, id_type, value, valid_from, valid_to, known_at |
| corporate_actions | security_id, action_type, ex_date, ratio, announced_at, known_at, source |
| universe_definitions | universe_version, spec_path, spec_sha256, effective_from, effective_to |
| fx_observations | pair, rate, basis, **observed_at**, provider_id, fetched_at |

### universe
| テーブル | 主な列 |
|---|---|
| current_eligibility | security_id, as_of_date, universe_version, included, exclusion_reason, price_local, fx_observation_id, price_jpy, current_stage, routes_matched |
| daily_summary | market_code, as_of_date, universe_count, eligible_count, type_unknown_count, price_missing_count, run_id |

（全履歴は Parquet `curated/universe_eligibility`）

### pipeline / storage
| テーブル | 主な列 |
|---|---|
| job_requests | request_id, job_name, job_version, run_mode, market, params, **idempotency_key (unique)**, status, claimed_by, lease_expires_at, heartbeat_at, attempt, run_id |
| runs | run_id, job_name, run_mode, started_at, finished_at, data_cutoff, price_cutoff_at, material_cutoff_at, git_sha, config_hash, versions jsonb, **provider_bindings jsonb**, runner_id, status |
| run_errors | run_id, stage, security_id, source, error_type, message, payload_ref |
| source_fetch_log | run_id, provider_id / source_id, endpoint, requested_at, http_status, item_count, raw_object_key |
| coverage_snapshots | run_id, market_code, date, 各段階の件数, missing_list_object_key |
| storage.dataset_manifests | manifest_id, dataset, partition_key, object_key, sha256, bytes, row_count, schema_version, dataset_version, produced_by_run_id, data_cutoff, created_at, supersedes_manifest_id |

### screening（当日の候補のみ）
| テーブル | 主な列 |
|---|---|
| candidates | run_id, security_id, as_of_date, via (TECHNICAL / MATERIAL / BOTH), stage1_detail_ref, material_event_ids |
| stage2_results | run_id, security_id, feature_version, knowledge_matches, intraday_window, chart_image_key, passed, detail |

### materials（Material Event index）
| テーブル | 主な列 |
|---|---|
| sources | source_id, name, region, source_kind, access_method, terms_url, terms_checked_at, full_text_allowed（**固定の重み・序列の列は持たない**） |
| documents | document_id, source_id, url, url_hash, title, snippet, body_object_key（許可時のみ）, content_sha256, **published_at, fetched_at, first_seen_at** |
| noise_decisions | document_id, market_relevance, is_noise, reason, filter_version, decided_by |
| material_events | material_event_id, canonical_title, event_type, scope, **first_seen_at**, discovery_document_id, clustering_version |
| event_documents | material_event_id, document_id, role (DISCOVERY / VERIFICATION / COVERAGE / UPDATE) |
| event_attributes（版管理） | material_event_id, attribute_version, as_of, novelty, surprise, directness, economic_impact, persistence, market_reaction, priced_in_status, produced_by, analysis_id |
| entity_links | material_event_id, security_id, relation_type, **causal_path**（マクロ系は必須、CHECK 制約）, direction, confidence, linker_version |

### knowledge
| テーブル | 主な列 |
|---|---|
| chart_concepts | concept_id, name, version, definition, mechanism, positive_context, negative_context, counterexamples, numerical_features |
| chart_concept_examples | example_id, concept_id, security_id, date_from, date_to, example_type (POSITIVE / FAILED / COUNTEREXAMPLE), chart_image_key, available_from（この日以降の判断でのみ参照可） |

### analysis
| テーブル | 主な列 |
|---|---|
| llm_analyses | analysis_id, run_id, security_id, **analysis_kind (STAGE3_EOD / ENTRY_DECISION / REANALYSIS / MATERIAL / RESEARCH)**, price_cutoff_at, material_cutoff_at, decision_cutoff_at, prompt_version, rule_version, feature_version, llm_provider, llm_model, input_object_key, input_sha256, output, decision, created_at |
| llm_derived_features | analysis_id, novelty_score, material_duration, market_psychology, priced_in, supply_overhang, reachable_zone_quality, chart_meaning |
| reachable_zones | analysis_id, bound (LOWER / UPPER), price, **anchor_type**（許可リスト。`PRIOR_SURGE_HIGH` は上値側に使えない）, evidence |

### prod（append-only）
| テーブル | 主な列 |
|---|---|
| setups_eod | setup_id, analysis_id, security_id, signal_cutoff_at, **signal_reference_price**, valid_until_session (D-20), created_at |
| watches | watch_id, analysis_id, security_id, watch_type, trigger_condition, invalidation_condition, expires_at, reference_price_at_watch（評価基準には使わない） |
| predictions | 下記 |
| prediction_episodes | episode_id, security_id, thesis_key, opening_prediction_id (unique), opened_at, horizon_end_at |
| episode_closures | episode_id (unique), close_reason (TARGET_HIT / FAILURE_HIT / THESIS_INVALIDATED / HORIZON_END / AMBIGUOUS_PATH / UNRESOLVED_MISSING_DATA), closed_at, evidence, run_id |
| state_transitions | transition_id, subject_type (setup / watch / episode), subject_id, security_id, from_state, to_state (SETUP_EOD / WATCH_* / TRIGGER_HIT / REANALYSIS / ENTRY / REAFFIRMED / DOWNGRADED / THESIS_INVALIDATED / FAILED_BREAKOUT / EXPIRED / REJECT …), cause, evidence, analysis_id, occurred_at, detected_at, run_id |

**prod.predictions**

| 列 | 備考 |
|---|---|
| prediction_id, episode_id, run_id, analysis_id | analysis_kind は ENTRY_DECISION / REANALYSIS のみ（制約） |
| created_at | |
| security_id, ticker_at_prediction, universe_version | |
| source_setup_id / source_watch_id | 由来 |
| signal_reference_price, signal_cutoff_at | 記録用 |
| **decision_cutoff_at**（= data_cutoff） | |
| **entry_reference_price, entry_price_observed_at, entry_price_basis, entry_price_provider_id, entry_price_latency_class** | `entry_price_observed_at <= decision_cutoff_at` |
| currency, **fx_rate, fx_observed_at, fx_observation_id**, price_jpy_at_entry | `fx_observed_at <= decision_cutoff_at` |
| **threshold_20** | `= entry_reference_price × 1.20`（生成列または CHECK） |
| failure_line, failure_distance, reachable_zone_ref | |
| driver_type, technical_state, material_event_ids | |
| rationale, falsifiers | |
| prompt_version, rule_version, ml_version, feature_version, llm_provider, llm_model | |
| snapshot_sha256 | |

一意制約（案）: Open Episode がある `(security_id, thesis_key)` に新しい Episode を作れない（D-17a/b で最終形を決める）。

### outcomes
| テーブル | 主な列 |
|---|---|
| episode_outcomes | episode_id, horizon_days (1/3/5/10/20), computed_at, close_return, mfe, mae, hit_10, hit_20, hit_30, days_to_20, failure_line_hit, hit_20_before_failure, failure_before_20, corporate_action_flag, delisted_flag |
| episode_path_resolution | episode_id, path_resolution, resolution_granularity (DAY_OPEN / DAY / MINUTE), resolved_at_bar_ts, label_version |

（全 Eligible 銘柄の Outcome は Parquet `outcomes/universe`）

### labels
| テーブル | 主な列 |
|---|---|
| objective_labels | label_id, subject_type, subject_id, label, value, label_version, computed_at |
| interpretive_labels | label_id, subject_type, subject_id, label, **labeler_model_version, confidence, evidence, human_review_status**, information_cutoff_at, input_sha256, objective_label_ref, supersedes_label_id, created_at |
| label_admission_policies | policy_version, rules, created_at |
| false_negative_reviews | review_id, security_id, surge_start_at, information_cutoff_at, analysis_id, verdict, interpretive_label_id |

### research / ml
| テーブル | 主な列 |
|---|---|
| research.replay_runs | replay_run_id, purpose, rule_version, model_version, git_sha, created_at |
| research.replay_predictions / replay_outcomes | prod 系と同形 + replay_run_id（**prod とは別テーブル**） |
| ml.model_registry | model_id, ml_version, role (CHAMPION / CHALLENGER / RETIRED), layer (CANDIDATE_GENERATION / INTERPRETATION / ENTRY_PREDICTION / STATE_TRANSITION), algorithm, feature_version, label_admission_policy_version, train_start, train_end, artifact_object_key, promoted_at, promoted_by |
| ml.model_evaluations | model_id, fold_id, train_end, test_start, test_end, metrics, calibration |

### exports / serving
| テーブル | 主な列 |
|---|---|
| exports.excel_exports | export_id, run_id, created_at, scope, object_key, row_counts |
| serving.*（派生・再生成可能） | Web 表示用キャッシュ（候補・Watch・Episode 銘柄の直近チャートデータ等）。正本ではない |

---

## 2. Parquet データセット

| dataset | パーティション | 主な列 |
|---|---|---|
| curated/ohlcv_daily | market, trade_date | security_id, open, high, low, close, volume, turnover, vwap, adjustment_factor, provider_id, fetched_at |
| curated/ohlcv_minute | market, trade_date | security_id, bar_ts, open, high, low, close, volume, provider_id, fetched_at（対象銘柄のみ） |
| curated/universe_eligibility | market, as_of_date | Postgres current_eligibility と同形 |
| features/daily | feature_version, market, as_of_date | security_id, data_cutoff, 指示書 §15 の Feature 群 |
| features/stage1_routes | rule_version, market, as_of_date | security_id, route_code, matched, 条件ごとの判定 |
| outcomes/universe | market, as_of_date | security_id, horizon_days, 各 Outcome 指標 |
| raw/* | source, 日付 | 生レスポンス・文書本文（許可時のみ） |
| artifacts/chart_images, artifacts/llm_io, artifacts/models | — | — |
| research/* | replay_run_id 等 | — |

## 3. 関係する未決事項

D-01a / D-01b / D-02a / D-17a〜c / D-18 / D-19 / D-20 / D-21 / D-13a / D-13b（[unresolved-decisions.md](unresolved-decisions.md)）
