# 開発フェーズ

状態: **v0.3（連続実装モード）** — 2026-09-17

## 進め方（2026-09-17 改定）

**旧方針は廃止した。** 「各 Phase（または監査ラウンド）の完了時に停止 → 報告 → ChatGPT 監査 → ユーザー承認 → 次へ」は、Phase 3 Stage 1 の承認をもって終了。

現在の方針:

- Claude Code は**主任開発エージェントとして連続実装する**。Phase 完了ごとに止まらない。
- 可逆的・安全側・versioned に決められる事項は自分で決め、[unresolved-decisions.md](unresolved-decisions.md) に記録する。
- unit / integration / regression test を追加しながら進む。
- **停止するのは6つの場合だけ**（[CLAUDE.md §3](../CLAUDE.md) が正本）: ユーザー本人の契約・支払い / credential・account 操作 / 法的・利用規約上の本人確認 / 不可逆な削除 / Canonical 仕様との正面衝突 / プロジェクト目的の変更。
- **次の統合報告地点は Phase 7 完了時**（Universe → Market Data → Feature → Route A-H → Materials → Entity Linking → Stage 2 → Stage 3 → Setup / Watch / Reject が end-to-end でつながった段階）。**Phase 8（リアルタイム ENTRY）に到達したらもう一度停止する** — 本物の外部依存（Live Provider）が出るため。

### 未解決の外部依存は開発を止める理由にしない

実 Provider が未確定・credential 未取得の領域は **`IMPLEMENTED_NOT_LIVE_VERIFIED`** として扱う。interface・schema・pipeline は完成させ、入力は合成 fixture / deterministic mock で満たし、「実データで検証していない」ことを報告と DB の両方に明示する。

現在 `IMPLEMENTED_NOT_LIVE_VERIFIED` の領域:

| 領域 | 待っているもの |
|---|---|
| JP EOD 価格取得 | D-102（JPX への書面照会） |
| US EOD 価格取得 | D-103（Alpaca の居住地条項と SIP/IEX 確認） |
| Object Storage 実体 | D-105（R2 のカード登録） |
| LLM 実モデル接続 | 有料 LLM API を Production 必須にしない方針のため、deterministic mock で pipeline を完成させる |

| Phase | 名称 | 主な成果物 | 完了条件（案） | 実装する回帰 fixture | 前提 |
|---|---|---|---|---|---|
| 0 | Requirements / architecture / setup | 文書一式、ローカル Git | 監査で概ね合格 | — | — |
| 0.1 | 監査是正 | ストレージ分離、interface 設計、lifecycle / universe / labels / fixtures 仕様、Provider 比較 | 条件付き合格 | 仕様のみ | — |
| **0.2** | 最終是正 | 過去高値の役割、decision / entry 価格、Setup 分離、Horizon・Failure Line 二層、USD Outcome、材料の利用可能時刻、corporate action、パス解決手順、v5.1 と addenda の分離 | 概ね合格 | 仕様のみ（RF-17〜22 追加） | — |
| **0.2 最終パッチ** | 最終パッチ | PRIOR_SURGE_HIGH の制約緩和、Outcome 二層、見逃し3分類、entry 価格での3,000円再確認、Horizon 表記、D-09b、Supabase Free、v5.1 Canonical の定義 | 修正 + v5.1 Canonical 登録で Phase 1 開始可 | 仕様のみ（RF-23・24 追加） | — |
| 1 ✅ | Security Master + Universe（2026-09-16 完了） | 10本の migration（ref / pipeline / universe / prod / research、ロール分離、RLS、スナップショット展開関数、オブジェクトストレージからの一括読み込み）、`SecurityMasterProvider` 実装3種（JPX / Nasdaq Trader / SEC）、`universe-1.0.0`、coverage、provenance、原文ハッシュ CI、RF-12 / RF-14a / RF-14b / RF-15 | JP/US の銘柄が Universe 定義どおりに取り込まれ、除外理由・`TYPE_UNKNOWN` の件数が DB と画面で確認できる | RF-12, RF-14a, RF-14b（v5.1 保存・ハッシュ部分）, RF-15 | **開始条件は v5.1 Canonical 登録・SHA-256 確認の1件のみ（2026-09-16 監査で確定）。** DB は専用 Cloud Supabase（作成済み）。Docker / ローカル Supabase は blocker ではない。D-07a・D-10a〜e・D-14 は pending で開始可。**範囲を超えて価格スクリーニング・ニュース収集・AI 分析へ先行しない。終了時に停止して監査（報告項目: Completed / Files changed / Tests / Universe coverage / Known limitations / Decisions / Risks）** |
| **1.1** ✅ | Security Master Integrity Fix（2026-09-16 完了） | 公的レジストリ由来の同一性（CIK / EDINET / JPX コード）、SCD2 履歴と `last_confirmed_at`、`ref.listing_states` と as-of 読み出し、EDINET provider、Depositary 系の判別修正、署名 URL 限定の一括読み込み、最小権限の実行 principal、identity rebuild と旧→新 ID 対応表 | Ticker 変更・無変化再取得・as-of・判定重複・最小権限が DB 実機とテストで示され、CIK が複数発行体に割れていないこと | Fixture A〜G, I（[security-identity.md](specs/security-identity.md)） | **Phase 1 完了後の是正ラウンド。完了まで Phase 2 へ進まない。** |
| **1.1a** ✅ | Foundation Hardening（2026-09-16 完了） | runtime 権限の縮小（設定表は SELECT のみ・default privileges も読み取り専用）、as-of の historical ticker、発行体名をレジストリ由来に（`ref.issuer_names`）、名称一致による発行体統合の廃止、identity confidence 3段階＋`identity_version`、coverage の error/warning 分離、`listing_status_history` 廃止、rebuild 手順の再現性 | worker が allowlist/設定表へ書けないこと・as-of が当時の Ticker を返すこと・issuer 名がレジストリ名であることを DB 実機とテストで示す | Fixture 1.1a-1〜8（[security-identity.md](specs/security-identity.md) §7） | **Phase 2 開始前の最終是正。完了まで Phase 2 へ進まない。** |
| **1.1b** ✅ | Phase 2 Preflight Final Gate（2026-09-16 完了） | loader の SECURITY DEFINER 化と `extensions` USAGE 剥奪、function の PUBLIC EXECUTE 恒久禁止、JP 特殊株式7件の是正、run publication model（validate / publish / authoritative_run_at / current_eligibility）、Production provenance（git_sha・config_hash・JOB_VERSION）、決定的 idempotency key、as-of の effective_from、SIC content hash、storage 認証モデルの是正 | worker が HTTP を直接出せない・未公開 run が authoritative にならない・知識時刻と有効時刻が分離していることを DB 実機とテストで示す | Fixture 1.1b-1〜8（[security-identity.md](specs/security-identity.md) §8） | **Phase 2 開始前の最終ゲート。完了まで Phase 2 へ進まない。** |
| **1.1c** ✅ | Publication Immutability & Temporal Provenance（2026-09-16 完了） | published run の凍結（trigger）、publish の直列化と再 validate、`data_cutoff` = 全 source の最大 available_at、snapshot 行の可視時刻を dependency max へ、identifier / issuer name provenance を実供給 source へ、publication 検証の強化、Nasdaq content hash の単一 SHA-256 化 | published run へのあらゆる書き込みが拒否され、未 publish run は書けること・provenance が実供給 source を指すことを DB 実機とテストで示す | Fixture 1.1c-1〜9（[security-identity.md](specs/security-identity.md) §8） | **Phase 2 開始前の最終データ完全性パッチ。** |
| 2 ✅ | Market Data + 3000円 Hard Filter（2026-09-17 完了） | `market` スキーマ（license policy / raw object manifest / purge / daily_bars の列別 basis / corporate actions / FX / FIGI）、`ObjectStore`（content-addressed・write-once）、Provider adapter 4種、`price-filter-1.0.0`、coverage、Parquet 分割 | schema・purge・eligibility・provenance が DB 実機とテストで示される。**実価格データの取得は D-102 / D-103 待ちで `IMPLEMENTED_NOT_LIVE_VERIFIED`** | RF-09（Universe 部分）, RF-11, RF-16 | **完了。** 取得対象は `INCLUDED` ∪ `UNRESOLVED`、Prediction 対象は Eligibility 解決済みの `INCLUDED` のみ。Raw Market Data は `provider_id` / native symbol / exchange / `observed_at` / source record id / `identity_version` を必ず保存（D-52） |
| 3 ✅ | Stage 1 Technical Screening（2026-09-17 完了） | `screening.features_daily`（数値 feature 74列）、Wilder 平滑の指標19種、Route A〜H を **OR 型 Candidate Generation** として実装、`discovery_routes[]`、`route_evidence`、as-of 比較可能系列（`SPLIT_ADJUSTED_TO_AS_OF`） | Route ごとの該当理由が測定値として再現でき、リーク検査が通る。**合成 fixture で検証済み、実価格データ未投入** | RF-01 / RF-01-M / RF-01-C（Feature・価格障害の部分） | **完了。** 数値を正本にし pattern label にしない（D-98）。Route F の turnover 閾値は市場別（D-99） |
| 4 ✅ | News / Disclosure Collection（2026-09-17 完了） | `news` スキーマ（source registry / quad-state の source policy / documents の4時刻 / raw_documents manifest / fetch cursors / coverage / purge）、stdlib のみの RSS・Atom parser、licence 先行のコレクタ、`news.documents_as_of()` | 4つの時刻が DB 制約で強制され、backfill が過去の知識にならないことをテストで示す。**source は未 enable のため `IMPLEMENTED_NOT_LIVE_VERIFIED`** | RF-17（時刻記録の部分） | **完了。** `available_to_model_at` は取得時刻から導出（D-108）。全文保存は明示許諾のみ（D-109）。news は自前 manifest（D-110） |
| 5 ✅ | Noise Filter + Entity Linking + Material Event（2026-09-17 完了） | `material` スキーマ（events / event_sources / entity_relations / event_security_features / relevance_decisions / route_definitions / candidates）、event merge、構造シグナル型 relevance filter、Material Routes M1〜M6、`material.stage2_candidates()` の full outer union | Technical 側と Material 側が互いを filter しないこと、`WEAK_ASSOCIATION` がどの route も発火させないことをテストで示す | RF-04, RF-05 / RF-05-C（Setup 分離・材料部分）, RF-17 | **完了。** merge は false split 優先（D-111）。Discovery/Verification は link の属性（D-112）。keyword 除外は禁止（D-113）。macro には causal_path 必須（D-114） |
| 6 ✅ | Chart Knowledge Base + Stage 2（2026-09-17 完了） | `chart` スキーマ（concepts / concept_examples / stage2_assessments / price_obstacles / concept_evidence_grade）、概念6件（mechanism・negative context・counterexample つき）、Stage 2 測定エンジン、価格障害検出 | mechanism・negative context・counterexample なしでは保存できず、failure example なしでは enable できないことを DB で示す。**seed は synthetic のみで `SYNTHETIC_ONLY`** | — | **完了。** 数値が正本・concept 名は導出（D-116）。価格障害は upside にならない（D-117）。分足は未取得（provider 未確定のため） |
| 7 ✅ | LLM Stage 3 EOD Analysis（2026-09-17 完了） | `analysis` スキーマ（input_bundles / stage3_outputs / llm_providers / stage3_state_counts）、section ごとに hash する bundle、canonical と addenda の分離、`LLMProvider` interface + deterministic mock、出力検証器 | ENTRY state が表現不能であること、旧高値を upside にした出力が reject されること、threshold と zone が同値なら reject されることをテストで示す。**実モデル未接続** | RF-01 / RF-01-M / RF-01-C（出力検証器の部分）, RF-05（分析部分）, RF-08, RF-14b（入力バンドル） | **完了。** ENTRY state を作らない（D-118）。Threshold と Zone を列ごと分離（D-119）。有料 LLM を必須にしない（D-120） |
| 8 | ENTRY 判断 / Watch / Prediction / Episode | 場中 Runner、リアルタイム Provider、`entry_decision`・`watch_monitor`、append-only の Prediction、Episode、State Transition、Predictions / Watch 画面 | Watch 到達だけで ENTRY にならない・Threshold が entry 価格基準・Episode の重複計上がないことをテストで示す | RF-03, RF-06, RF-07, RF-09（ENTRY 部分）, RF-19, RF-20, RF-21（記録部分） | **D-06b（Phase 8 までの blocker）**, D-01a, D-01b, D-17a, D-17b, D-20, D-21, D-31  **ここで停止して統合報告する（Live Provider という本物の外部依存が出るため）。** |
| 9 | Outcome Tracking + Excel Export | Episode と全 Eligible 銘柄の Outcome、パス解決、Results 画面、Excel | 分割・上場廃止・休場・分足欠損を含むケースで正しく計算される | RF-06（集計部分）, RF-10, RF-18, RF-20（Outcome 部分）, RF-21（Outcome 部分）, RF-22, RF-24 | D-15, D-24 |
| 10 | Teacher Dataset | Objective / Interpretive ラベル、採用ポリシー、見逃しの Research 判定、状態遷移の教師データ | ラベル基準が監査済みで、突発急騰が ACTIONABLE_FALSE_NEGATIVE にならないことをテストで示す | RF-02, RF-13, RF-23, RF-24（ラベル部分） | D-13a, D-13b, D-33 |
| 11 | ML / Weight Learning | 4層の学習、walk-forward、条件付き Weight、Feature interaction | Champion / Challenger 比較がリークなしで生成される | RF-15（Replay 部分） | 教師データ量の十分性の判断 |
| 12 | Model Lab / Continuous Improvement | Model Lab 画面、Route/Driver/Feature 別成績、LLM 評価精度、version 比較、昇格フロー | Challenger の昇格が監査ログ付きで行える | — | — |

## End-to-end（2026-09-17 完了）

`surge.pipeline.eod.EodPipeline` が Universe → 3,000円 Hard Filter → Feature Engine → Route A-H →（独立に）Materials → Entity Linking → Material Routes → **union** → Stage 2 → Stage 3 → Setup / Watch / Reject を一本でつなぐ。

- **union は union**。Technical 側と Material 側は互いを filter しない。チャート信号だけの銘柄も、材料だけの銘柄も残る。
- **各 stage が「何で動いたか」を記録する**（`RAN` / `FIXTURE_ONLY` / `SKIPPED_NO_INPUT` / `SKIPPED_NO_CREDENTIAL` / `FAILED`）。`ran_on_real_data` は**全 stage が実データを見たときだけ** true を返す。
- 現状は price provider 未確定のため、ほぼ全 stage が `FIXTURE_ONLY`。

## UI（Phase 4〜7 と並走）

**バックエンドだけで終わらせない。** 各 Phase の成果物には、対応する読み出し契約（API / view）を含める。

| 画面 | 主なデータ契約 | 依存 Phase |
|---|---|---|
| Dashboard | 当日の候補件数・Route 別内訳・pipeline の健全性・未充足ロール | 3, 4, 5 |
| Universe | `universe.eligibility_as_of` / 除外理由 / `TYPE_UNKNOWN` 件数 | 1, 2 |
| Materials | `material_event` とその `material_source`、4つの時刻、`relation_type` | 4, 5 |
| Stock Detail | 価格系列・feature 行・発火 Route・紐づく材料・価格障害（過去高値は obstacle として） | 2, 3, 5, 6 |
| Watch / Setup | Stage 3 の出力 state と根拠 | 7 |
| Coverage | provider 失敗・品質警告・identity collision の**分離**表示 | 1, 2, 4 |
| Pipeline diagnostics | run / publication / error log / idempotency / `IMPLEMENTED_NOT_LIVE_VERIFIED` の領域 | 全て |

Next.js UI 本体（`apps/web`）も既存 Phase 設計に沿って進めてよい。重い全市場処理・場中監視・学習を Web に載せない（[CLAUDE.md §2](../CLAUDE.md)）。

## 各 Phase 共通の Definition of Done

- マイグレーション・コード・テストがコミットされている
- `run_id` / cutoff 類 / version / provider_bindings / error log が保存される
- その Phase で実装すべき回帰 fixture が通る（未到達のものは pending のまま残す）
- 未実装・妥協・データ制約が報告に書かれている
- 実データで検証していない部分は `IMPLEMENTED_NOT_LIVE_VERIFIED` と明示する（「動いた」と書かない）
