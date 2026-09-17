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
| D-03d | Supabase Free 上限への対応 | 監査は当初 (c) ローカル Supabase を採用。その後ユーザー指示で `loop-vocabulary` を **Pause（削除しない）** し、空いた枠に本プロジェクト専用の新規 Free Project `surge-research-platform`（ap-northeast-1、$0/月）を作成。`kaiji-radar` は無変更。既存 Project の流用なし。有料化なし。ローカル Supabase は開発・テスト用に併用 | 2026-09-16 / ユーザー指示 | ユーザー指示（2026-09-16） |
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
| D-00 | v5.1 Canonical の登録 | **RESOLVED**: `docs/prompts/short-surge-v5.1.original.md`（32,012 バイト、2,275行、LF、SHA-256 `32bf001e…f877a`）を ChatGPT が直接コミットし（`1a4f34e`）、Claude Code が実ファイルから SHA-256 を実測して MANIFEST に登録。post-v5.1 用語の混入がないことを確認（RF-14b） | 2026-09-16 / ユーザー + ChatGPT | audit 2026-09-16 引継ぎ |
| D-10b（JP 判定ソース） | JP の普通株判定に使うソース | **JPX「東証上場銘柄一覧」（data_j.xlsx）の「市場・商品区分」を採用**（認証不要、市場区分と商品区分が1列）。J-Quants `ProdCat` は不使用。外国株式5件・出資証券2件は引き続き UNRESOLVED | 2026-09-16 / Claude Code（Phase 1 実装） | [universe-definition-v1.0.0.md](specs/universe-definition-v1.0.0.md) |
| D-34 | Security / Issuer の同一性 | **公的レジストリ由来のキーのみを同一性とする**（US = SEC CIK、JP = EDINET コード、Security の JP = JPX ローカルコード）。Ticker と正規化名称は同一性にしない。作れないときは `PROVISIONAL` と明示。`STRONG` キーの衝突は統合せず両方を降格し、run 警告に残す | 2026-09-16 / 監査 Phase 1.1 #1〜#3 | [security-identity.md](specs/security-identity.md) |
| D-35 | 属性変更の履歴 | Ticker・名称・識別子・上場区分/状態は **SCD2**（変化時のみ open 行を閉じて新規行、無変化は `last_confirmed_at` のみ前進、open 行は常に1行）。as-of 読み出しは `ref.listing_states` 経由 | 2026-09-16 / 監査 Phase 1.1 #4・#5 | 同上 |
| D-36 | Object Storage からの一括読み込み | **短命な署名 URL のみ**受け付ける。https 必須、host allowlist 必須、DB へ raw API key を渡さない・DB は Authorization ヘッダを送らない。allowlist は環境設定であり migration では seed しない | 2026-09-16 / 監査 Phase 1.1 #6 | [phase-1-universe-sync.md](runbooks/phase-1-universe-sync.md) |
| D-37 | Production worker の DB principal | 専用の LOGIN role `surge_worker_prod_app`（`surge_worker_prod` のメンバー）で接続する。パスワードは DB 内で生成し Supabase Vault に保管、チャット・ログ・Git に出さない。履歴テーブルへの DELETE と research スキーマへのアクセスを持たない | 2026-09-16 / 監査 Phase 1.1 #7 | 同上 |
| D-38 | 下流工程の取得対象 | **価格・FX の取得対象は `INCLUDED` ∪ `UNRESOLVED`。** `UNRESOLVED` を黙って落とさない（落とすと判定確定前の価格が欠落して後追い評価ができない）。ただし `UNRESOLVED` は ENTRY 候補にしない | 2026-09-16 / 監査 Phase 1.1 #12 | [universe-definition-v1.0.0.md](specs/universe-definition-v1.0.0.md) §5 |
| D-39 | 同一性を変える再構築の手順 | 旧 ID を `ref.identity_migration_map` に記録 → master/判定/coverage/staging を削除（`pipeline.runs`・`source_fetches` は保持）→ 公式ソースから再取得 → 新旧 ID 対応を埋める。手順は versioned migration と worker code に残す | 2026-09-16 / 監査 Phase 1.1 #10 | [security-identity.md](specs/security-identity.md) §5 |
| D-44 | Identity confidence の語彙 | `STRONG`（そのものに対するレジストリ識別子）/ `REGISTRY_ANCHORED`（レジストリ識別子＋provider テキスト由来の属性）/ `PROVISIONAL` の3段階。US の `US:CIK:<cik>:<type>:<class>` は表示名由来の要素を含むため `REGISTRY_ANCHORED`。昇格は自動で行わず `identity_version` を伴う記録された migration とする。降格判定は「再利用可能な tier の集合」で行い、単一値比較にしない | 2026-09-16 / 監査 Phase 1.1a #6 | [security-identity.md](specs/security-identity.md) §1 |
| D-45 | レジストリ識別子が無い発行体 | 名称一致で統合しない。fallback キーは security の座標（`ISSUER-OF:<security identity key>`）。名称は `ref.issuer_names` の `ALIAS`。**false merge より false split を優先**し、stable identifier が得られた時点で identity migration として統合する | 2026-09-16 / 監査 Phase 1.1a #5 | 同上 §1・§2 |
| D-46 | 発行体名の出所 | `ref.issuers.legal_name` はレジストリ名（SEC registrant name / EDINET 提出者名）。商品名を発行体名にしない。履歴は `ref.issuer_names`（LEGAL / FORMER / ALIAS）に SCD2 で保持 | 2026-09-16 / 監査 Phase 1.1a #4 | 同上 §2 |
| D-47 | runtime worker の権限境界 | 設定・参照データ（allowlist / sources / exchanges / definitions / decision_reasons / identity_migration_map）は runtime SELECT のみ。変更は migration / DB 管理者。default privileges も SELECT のみに変更し、新規テーブルは明示 grant が無い限り書き込み不可。`DELETE` はどのスキーマにも与えない | 2026-09-16 / 監査 Phase 1.1a #1・#2 | [phase-1-universe-sync.md](runbooks/phase-1-universe-sync.md) |
| D-48 | 過去 Ticker の正本 | `ref.listing_symbols`（SCD2）。`ref.listings.local_code` は current materialized state であり as-of の正本にしない。`ref.listings_as_of()` は同一カットオフで symbol を返す | 2026-09-16 / 監査 Phase 1.1a #3 | [security-identity.md](specs/security-identity.md) §4 |
| D-49 | Rebuild 手順の再現性 | 旧→新 ID の充填は migration ではなく post-reload の手続き（`ref.finalize_identity_rebuild`）。master が空なら例外で拒否する。手順は `scripts/rebuild_security_master.sh` と runbook に残す | 2026-09-16 / 監査 Phase 1.1a #9 | 同上 §5 |
| D-50 | Coverage の診断内訳 | `provider_error_count` / `data_quality_warning_count` / `identity_collision_record_count` / `identity_collision_key_count` に分離。run_errors の全行がいずれか1つに入る | 2026-09-16 / 監査 Phase 1.1a #7 | [phase-1-universe-sync.md](runbooks/phase-1-universe-sync.md) |
| D-51 | `ref.listing_status_history` | Phase 1.1a で廃止（空・後継は `ref.listing_states`）。行が存在する環境では DROP せず DEPRECATED を明示する | 2026-09-16 / 監査 Phase 1.1a #8 | `20260916160500` |
| D-52 | Phase 2 の Raw Market Data キー | `security_id` を唯一の復元キーにしない。`provider_id` / native symbol / exchange / `observed_at` / source record id（provider security id）/ `identity_version` を必ず保存する | 2026-09-16 / 監査 Phase 1.1a #6 | [security-identity.md](specs/security-identity.md) §6 |
| D-53 | 権威ある Universe run | **publication が決める**。`pipeline.run_publications` に載った run だけが authoritative で、`pipeline.validate_run` を通らないものは publish できない。読み出しは `universe.authoritative_run_at(market, version, knowledge_cutoff)` / `universe.eligibility_as_of(...)` / `universe.current_eligibility`。`published_at <= cutoff` で絞るため、後から再構築した run は過去の Replay に逆流しない | 2026-09-16 / 監査 Phase 1.1b #4 | [security-identity.md](specs/security-identity.md) §5 |
| D-54 | DB からの HTTP 実行権限 | loader を **SECURITY DEFINER** にし、runtime role から `extensions` スキーマの USAGE と `extensions.http*` の EXECUTE を剥奪する。HTTP を出す権限は「allowlist を検査する関数」に属し、ロールには属さない | 2026-09-16 / 監査 Phase 1.1b #1 | `20260916170000` |
| D-55 | 新規 function の既定権限 | project schema では PUBLIC EXECUTE を残さない。default privileges（正の grant を作ってから revoke）+ event trigger + テスト不変条件の3段で担保する。素の `alter default privileges ... revoke ... from public` だけでは entry が作られず効かないことを実測 | 2026-09-16 / 監査 Phase 1.1b #2 | `20260916170400` |
| D-56 | JP の普通株判定 | 市場区分は「どこで売買されるか」であって種別ではない。優先株式 → `PREFERRED`、種類株式（社債型含む）→ `NOT_COMMON_STOCK`、優先出資証券 → UNRESOLVED、判定不能の特殊コード → `TYPE_UNKNOWN`。**普通株と推測しない**。universe-1.0.0 の意味変更ではなく実装の bug fix | 2026-09-16 / 監査 Phase 1.1b #3 | [universe-definition-v1.0.0.md](specs/universe-definition-v1.0.0.md) |
| D-57 | Production run の provenance | `git_sha` / `config_hash` / `job_version` / `universe_version` / `identity_version` / provider_bindings を必須化（CLI が拒否し、DB の CHECK が拒否する）。`JOB_VERSION` は出力が変わるたびに上げる | 2026-09-16 / 監査 Phase 1.1b #5 | `20260916170300` |
| D-58 | idempotency の意味 | key = job + run_mode + market + as_of + source_data_version + universe_version + identity_version + job_version + config_hash のハッシュ。**run_id を含めない**。同一 logical invocation の retry は同じ key、source snapshot や設定が変われば別 key | 2026-09-16 / 監査 Phase 1.1b #6 | [test_run_provenance.py](../workers/tests/test_run_provenance.py) |
| D-59 | 知識時刻と有効時刻 | as-of は `available_at <= knowledge_cutoff` と `effective_from <= effective_at < effective_to` の両方で絞る。2引数版が正式形、1引数版は両方同じ時刻 | 2026-09-16 / 監査 Phase 1.1b #7 | [security-identity.md](specs/security-identity.md) §4 |
| D-60 | SIC membership の provenance | SIC コード + ソート済み CIK 集合の SHA-256 を content hash とする（ページ本文は取得ごとに変わるため）。membership が変われば `source_data_version` も変わる | 2026-09-16 / 監査 Phase 1.1b #8 | `workers/src/surge/providers/sec_edgar.py` |
| D-61 | Object Storage の認証情報 | worker secret store の bucket-scoped / service credential のみ。**匿名ポリシーを作らない**。既定の投入経路は DB への直接接続（`scripts/load_snapshot_direct.py`）で、storage 経由は「job が DB へ到達できない場合」に限る | 2026-09-16 / 監査 Phase 1.1b #9 | [phase-1-universe-sync.md](runbooks/phase-1-universe-sync.md) |
| D-62 | Published run の不変性 | publish した run の artifact は INSERT/UPDATE/DELETE 禁止（trigger、owner を含む全ロール）。訂正は新 run を publish して supersede。`ref.*` は materialized 現在状態なので対象外 | 2026-09-16 / 監査 Phase 1.1c #1 | `20260916180000` |
| D-63 | publish の直列化 | run 単位の advisory lock（書き込み shared / publish exclusive）＋ publish 後の再 validate。validate と publication の間に内容が動いたら publication ごと中止 | 2026-09-16 / 監査 Phase 1.1c #2 | 同上 |
| D-64 | `data_cutoff` の定義 | `max(source_fetches.available_at)`（最後の source）。最初の source の時刻は `params.first_source_available_at` に別途保持 | 2026-09-16 / 監査 Phase 1.1c #3 | [security-identity.md](specs/security-identity.md) §4 |
| D-65 | snapshot 行の `available_at` | run が依存する全 source の `available_at` 最大値。identity 依存行が identity 解決前に見えないよう安全側（遅い側）に倒す | 2026-09-16 / 監査 Phase 1.1c #4・#7 | 同上 |
| D-66 | identifier / issuer name の provenance | `source_id` / `observed_at` / `source_record_id` は**値を供給した source**（CIK = SEC ticker file、EDINET = code list）。`available_at` は dependency max。既存行は provenance のみ修復し、値と有効期間は変えない。`available_at` は後ろにしか動かさない | 2026-09-16 / 監査 Phase 1.1c #5・#6 | `20260916180100` / `20260916180300` |
| D-67 | publication の検証範囲 | 市場一致・source_data_version 単一かつ run と一致・identity_version / universe_version 一致・全 source に `available_at`・必須 source に 64hex digest・`data_cutoff >= max(available_at)`・critical source が truncated でないこと | 2026-09-16 / 監査 Phase 1.1c #8 | `20260916180200` |
| D-68 | critical source の不完全取得 | SEC SIC（SPAC / REIT）の truncated は `CRITICAL_SOURCE_INCOMPLETE` とし **publication を拒否**する。一般の data quality 警告は拒否しない | 2026-09-16 / 監査 Phase 1.1c #8 | 同上 |
| D-69 | content_sha256 の意味 | 64桁 hex の SHA-256 か NULL。複数ファイルを1つの source として読む場合は各 digest を連結して再度 SHA-256 する（成分は run notes に保持） | 2026-09-16 / 監査 Phase 1.1c #9 | `workers/src/surge/providers/nasdaq_trader.py` |
| D-71 | 必須なのは provider ではなく dataset | 1つの provider が独立に必須な複数 dataset を供給する場合（SEC SIC 6770 / 6798）、`dataset_key` で dataset ごとに必須判定する。dataset_key を持たない既存 run は `pipeline.fetch_dataset_key` が endpoint から導出する（published run を書き換えない） | 2026-09-16 / 監査 Phase 2.0 (A) | `20260916190000` / `20260916190100` |
| D-72 | provider_bindings の粒度 | `{dataset: {provider, endpoints[]}}`。source_id で束ねると同一 provider の複数 endpoint が1つしか残らない | 2026-09-16 / 監査 Phase 2.0 (B) | `workers/src/surge/jobs/universe_sync.py` |
| D-73 | validation_version の由来 | `pipeline.validation_ruleset_version()`（現行 `publication-1.1c.0`）を publish 時の既定値にする。ruleset を変えたら上げる。**過去の publication は当時の版のまま**（`publication-1.0.0` の 2 run は現行 ruleset では validate しない。これは記録であって欠陥ではない） | 2026-09-16 / 監査 Phase 2.0 (C) | `20260916190000` |
| D-74 | ingestion_run_id と last_provenance_run_id | 前者は「その version を最初に作った run」、後者は「provenance を最後に確認・訂正した run」。再確認で前者を動かさない | 2026-09-16 / 監査 Phase 2.0 (D) | `20260916190000` |
| D-75 | metadata correction の意味 | published run の artifact は不変。`ref` master の provenance 訂正は**将来の past-cutoff query の結果を変え得る**（Phase 1.1c の「retro-hide しない」は不正確だった）。訂正そのものを `ref.provenance_corrections` に追記のみで記録し、Historical Replay はどちらを読むかを明示する | 2026-09-16 / 監査 Phase 2.0 (E) | `20260916190200` / [security-identity.md](specs/security-identity.md) §4 |
| D-79 | License Mode を第一級データにする | `market.provider_license_policies` に provider x dataset x policy_version で記録し、append-only。`assert_license_allows` は **ALLOWED 以外（PROHIBITED / NOT_SPECIFIED / UNKNOWN / policy 無し）をすべて拒否**する。規約が触れていない事項を true/false に断定せず `NOT_SPECIFIED` とする（EODHD の削除義務がこれ） | 2026-09-17 / 監査 Phase 2.1 (1) | `20260917100000` |
| D-80 | Licensed Data Purge | 全 raw object を `market.raw_objects` に登録（provider / dataset / entitlement_plan / required_min_plan / license_policy_version / sha256 / 期間）。purge は **enumerate → delete → 1件ずつ記録 → 派生行削除 → close** の4段。**dry run は完了扱いにならない**。purge は `surge_purge` ロール専用で、`surge_worker_prod` には DELETE も purge 関数の EXECUTE も無い。**J-Quants の raw object に無期限 Bucket Lock を設定しない** | 2026-09-17 / 監査 Phase 2.1 (2) | `20260917100100` / `workers/src/surge/purge.py` |
| D-81 | R2 の write-once | key は content-addressed（`raw/<provider>/<dataset>/<sha256>.<ext>`）。書き込みは常に `If-None-Match: *` を付け、**ストア自身に上書きを拒否させる**（規約ではなく機構で担保）。R2 は衝突時に **412** を返す（AWS の 409 ではない）。同一バイト列の再書き込みは `created=False` の冪等な no-op、異なるバイト列は `ImmutableObjectConflict` | 2026-09-17 / 監査 Phase 2.1 (3) | `workers/src/surge/storage/r2.py` |
| D-82 | 価格の basis を列ごとに宣言する | `open_basis` / `high_basis` / `low_basis` / `close_basis` / `volume_basis` を `RAW` / `SPLIT_ADJUSTED` / `SPLIT_AND_DIVIDEND_ADJUSTED` / `PROVIDER_UNSPECIFIED` で持つ。**EODHD の volume は `SPLIT_ADJUSTED`** であり raw と呼ばない。Provider の調整済み系列は `market.daily_bars_adjusted` に分離し、raw と同じ行に置かない | 2026-09-17 / 監査 Phase 2.1 (4)(5) | `20260917100200` |
| D-83 | 再構築した raw volume | `market.derived_volumes` に `reconstruction_method` / `split_source_dataset` / `coverage_quality` 付きで**別保存**し、vendor-native の volume を上書きしない。2018年より前に上場廃止した銘柄は splits が無く復元できないため `corporate_action_completeness = PARTIAL_KNOWN_GAP` を `market.security_coverage` に記録する | 2026-09-17 / 監査 Phase 2.1 (5) | 同上 |
| D-84 | silent overwrite の検出 | J-Quants は版番号も ETag も差分も持たず訂正を上書きで行う。rolling refetch で digest を比較し、変化を `market.source_revisions`（object 単位）と `market.daily_bar_revisions` / `market.fx_rate_revisions`（行単位、trigger）に記録する | 2026-09-17 / 監査 Phase 2.1 (4)(7) | 同上 |
| D-85 | EODHD の API quota 会計 | bulk は **HTTP リクエスト1本 = API コール100本**。`QuotaLedger` は provider が数えるもの（コール）を数え、transport が数えるもの（リクエスト）ではない | 2026-09-17 / 監査 Phase 2.1 (6) | `workers/src/surge/providers/eodhd.py` |
| D-86 | ECB FX の保存形 | 両レッグ（`eur_usd` / `eur_jpy`）を raw 保存し、`derived_usd_jpy` と `derivation_method` を併記。`rate_kind = REFERENCE_RATE`（取引レートではない）。`source_published_at` は `Last-Modified`、`available_at` は**自分の受信時刻**。「翌営業日以降は改訂されない」を仮定せず rolling refetch で hash 比較する。**16:00 CET = xx ET のような固定変換をコードに埋めない** | 2026-09-17 / 監査 Phase 2.1 (7) | `20260917100200` / `workers/src/surge/providers/ecb_fx.py` |
| D-87 | FX Eligibility の読み方 | `market.usdjpy_as_of(cutoff)` は `available_at <= cutoff` の最新1件を返し、`source_date` と `fx_age_seconds` を同時に返す。**同日レートが無いとき直前レートを同日扱いにしない**（rate_date と price_date を別に保存する） | 2026-09-17 / 監査 Phase 2.1 (8) | 同上 |
| D-88 | OpenFIGI は Research Enrichment | 全 match を `market.figi_mappings` に保存し、`mapping_status` を EXACT / AMBIGUOUS / UNMAPPED / ERROR で持つ。**複数 match を自動で1件に決めない**（`FRCB` は2発行体を返す）。**既存 security_id を変更しない**。昇格は coverage report と identity migration 監査の後に限る。CUSIP は Security Master へ新規取得も保存もしない | 2026-09-17 / 監査 Phase 2.1 (9) | 同上 / `workers/src/surge/providers/openfigi.py` |
| D-89 | availability_basis | `OBSERVED_NOW` / `PROVIDER_PUBLISHED_TIMESTAMP` / `DOCUMENTED_SCHEDULE` / `HISTORICAL_REPLAY_ASSUMPTION`。取り込みは**常に `OBSERVED_NOW`**（今日取った10年前の bar を、10年前に観測したことにしない）。`source_published_at` は provider の主張として別に保存し、別の availability model を使う Replay は basis を明示して宣言する | 2026-09-17 / 監査 Phase 2.1 (10) | `20260917100100` |
| D-90 | project schema の一覧を1箇所にする | PUBLIC EXECUTE 剥奪の対象スキーマを3箇所に書いていたため、`market` 追加時に全関数が public 実行可能なまま残った（CI が検出）。`pipeline.project_schemas()` を正本とし、event trigger・sweep・テストがこれを読む | 2026-09-17 / Phase 2.1 実装中に発見 | `20260917100300` |
| D-91 | J-Quants `ProdCat` | 2026-05-26 に JPX が追加した商品区分（011 内国株券 / 012 優先出資証券 / 013 REIT / 014 ETF / 021-024 外国）。Phase 1 は種別を銘柄名から推定していたが、これはレジストリが種別を宣言している。**ただし普通株と優先株式・種類株式は両方 011 なので銘柄名判定を置き換えない**。突き合わせて差分を監査する | 2026-09-17 / Phase 2.1 仕様確定時に発見 | [phase-2-1-provider-api-specs.md](research/phase-2-1-provider-api-specs.md) §1-3 |
| D-94 | Zero-Cost Core を構造で担保する | `market.providers.cost_class` と `market.provider_role_bindings` で表現。paid provider は削除せず `OPTIONAL_PAID` + `enabled=false` + `priority=9` で保持し、`market.recurring_cost` が 0 を返すことをテストで検査する。役割が埋まっていないことは `market.unfilled_roles` で可視化し、沈黙させない | 2026-09-17 / 方針変更 Zero-Cost Core | `20260917110300` |
| D-95 | Provider 評価結果をデータとして残す | 却下した候補と**その決め手になった条項**を `market.provider_evaluations` に記録する。「高かった」と「規約が保存を禁じている」では次の一手が違うため、理由まで残す。検証で結論が逆転した2件は `reversed_by_verification` で明示 | 2026-09-17 / 同上 | `20260917120000` / [zero-cost-provider-evaluation.md](research/zero-cost-provider-evaluation.md) |
| D-96 | 比較可能系列は自前で作る | Vendor の adjusted 系列は分割のたびに遡って再計算されるため as-of 時点の事実ではない。raw 価格と**as-of 時点で既知の**コーポレートアクションだけから `SPLIT_ADJUSTED_TO_AS_OF` を構築する。読めない分割が1件でもあれば、その銘柄の feature は**一切生成しない** | 2026-09-17 / Phase 2 実装 | `workers/src/surge/market/series.py` |
| D-97 | Vendor が調整済みの volume の扱い | provider が自分の取得時点へ調整した volume は、as-of 以降に分割があると as-of 基準へ戻せない（未来情報が要る）。その場合 volume を**落として落としたと記録する**。推測で埋めない | 2026-09-17 / 同上 | 同上 |
| D-98 | Stage 1 は数値を正本にする | `screening.features_daily` に約60の数値 feature を保存し、Route は**発火した測定値を `route_evidence` に残す**。pattern label だけにしない。Route は OR 型で `discovery_routes[]` に全て残す | 2026-09-17 / Phase 3 実装 | `20260917110200` |
| D-99 | Route F の turnover 閾値は市場別 | turnover は当該証券の通貨建ての金額なので、単一閾値では米国銘柄が日本銘柄の100分の1に見える。`max_turnover_avg_20d_jpy` と `max_turnover_avg_20d_usd` を分ける。turnover を供給しない provider では Route F は**発火しない**（coverage gap として記録） | 2026-09-17 / 同上 | 同上 |
| D-100 | 空の1日は失敗として扱う | 休場日と障害は同じ「0行」に見える。取得ジョブは空を `EMPTY_RESULT` の失敗として返し、取引カレンダーを知る呼び出し側が判断する。検証済みカレンダーが無いうちに休場日表を推測で作らない | 2026-09-17 / Phase 2 実装 | `workers/src/surge/market/ingest.py` |
| D-101 | 3000円フィルタの staleness 境界 | `price-filter-1.0.0`: 閾値3000円、価格は as-of から5暦日以内、FX は 345,600秒（4日）以内。超過は `STALE_PRICE` / `STALE_FX` として**独立の結果**にする。境界は「止まったフィードを捕まえる」ためのもので、静かな銘柄を排除するためではない。変更は新 rule_version | 2026-09-17 / Phase 2 実装 | `20260917110100` |
| D-106 | Phase ごとの停止監査を廃止 | 連続実装へ移行。停止するのは6つ（契約・支払い / credential・account 操作 / 法的・利用規約上の本人確認 / 不可逆な削除 / Canonical 仕様との正面衝突 / プロジェクト目的の変更）だけ。可逆・安全側・versioned に決められる事項は自分で決めて記録する | 2026-09-17 / ChatGPT 監査（Phase 2 Core・Phase 3 Stage 1 承認） | [CLAUDE.md §3](../CLAUDE.md) / [development-phases.md](development-phases.md) |
| D-107 | `IMPLEMENTED_NOT_LIVE_VERIFIED` を状態として持つ | Provider 未確定・credential 未取得の領域は実装を止めず、interface と schema と pipeline を完成させ、入力は合成 fixture で満たす。**「実データで検証していない」ことを報告と DB の両方に明示する。** `ui.not_live_verified` が画面に出す | 2026-09-17 / 同上 | `news.sources.live_verified_at` / `analysis.llm_providers.live_verified_at` / `chart.concept_evidence_grade` |
| D-108 | 材料の `available_to_model_at` は取得時刻から導出する | 4つの時刻のうち**判断を制御するのはこれだけ**。`source_published_at` から導出しない。backfill した記事は backfill した時刻に知り得たのであって、公表時刻に知り得たのではない。replay が反実仮想を使いたい場合は `replay_assumed_available_at` に**理由を書いて**分離し、production の読み出し関数はその列を返さない | 2026-09-17 / Phase 4 実装 | `20260917130000` / `workers/src/surge/news/models.py` |
| D-109 | 全文保存は明示許諾がある source のみ | source policy が `full_text_storage_allowed = ALLOWED` でない限り `METADATA_ONLY` で保存し、**body を取得しにも行かない**。沈黙は `NOT_SPECIFIED` であって許諾ではない。DB 側でも insert と同一トランザクションで `news.assert_storage_allows` を呼び、Python 側の判断が古くても書けないようにする | 2026-09-17 / Phase 4 実装 | `20260917130000` / `workers/src/surge/news/collector.py` |
| D-110 | news は自前の object manifest を持つ | `market.raw_objects` は market の licence policy と provider plan に3本の FK で縛られており、日銀のプレスリリースを入れるには中央銀行用の market data 購読プランを捏造する必要があった。`news.raw_documents` を分ける。共有するのは purge request だけ（ライセンス義務は1つなので、半分だけ履行できる形にしない）| 2026-09-17 / CI が検出 | `20260917150000` |
| D-111 | Event merge は false split を優先する | 同一性と同じ非対称性: 分かれたものは後で統合できるが、統合したものは戻せない。統合するのは (a) 同一 entity かつ**分単位で一致する公表時刻**、(b) 明示的な相互参照、の2つだけ。それ以外は分けたまま**「統合候補」として一覧に出す**（見えない split は悪い merge と同じくらい悪い）| 2026-09-17 / Phase 5 実装 | `workers/src/surge/material/merge.py` |
| D-112 | Discovery / Verification / Corroboration は link の属性 | publisher の属性ではない。同一 publisher の続報は CORROBORATION であって確認ではない。`independent_source_count` は DISCOVERY と VERIFICATION の**distinct publisher** のみを数える。IR > News の固定順位は作らない（`news.sources.fetch_priority` は取得順であって材料の強さではない、とコメントで明記）| 2026-09-17 / Phase 5 実装 | `20260917140000` |
| D-113 | Noise filter は構造シグナルだけで判定する | キーワード除外は禁止。判定は resolved entity 数 / 因果経路の有無 / 規制開示か / 定量化された magnitude から行い、**根拠とした signals を必ず保存**する。`UNCERTAIN` は除外ではない（mechanism を抽出できなかった macro は保持してレビューへ）。`NOT_RELEVANT` は「紐付く証券が1つも無い」場合のみ、それでも event 行は残す | 2026-09-17 / Phase 5 実装 | `workers/src/surge/material/relevance.py` |
| D-114 | Material Route は M1〜M6、`WEAK_ASSOCIATION` はどの route も受け取らない | engine・route 定義表・CHECK 制約の3箇所で担保。macro route (M4/M5) は causal_path が無いと発火しない。**測定できなかった feature は閾値を超えない**（None を通過扱いにしない）| 2026-09-17 / Phase 5 実装 | `20260917140000` / `workers/src/surge/material/routes.py` |
| D-115 | Chart concept は mechanism・negative context・counterexample が無いと保存できない | さらに **failure example が1件も無い concept は enable できない**（trigger）。「pattern 名の辞書にしない」を制約にした。seed した6 concept は synthetic example で enable しているため `chart.concept_evidence_grade` が全件 `SYNTHETIC_ONLY` を返す。合成例は mechanism を示すが証拠ではない | 2026-09-17 / Phase 6 実装 | `20260917160000` |
| D-116 | Stage 2 は数値が正本、concept 名は導出 | label から測定値は復元できないが、測定値から label は復元できる。測定できなかったものは `measurement_gaps` に名前を書く。volume が as-of の株数へ戻せない系列では **volume 系の測定を一切出さない**（単位の違う2つの比を ratio と呼ばない）| 2026-09-17 / Phase 6 実装 | `20260917160000` / `workers/src/surge/chart/stage2.py` |
| D-117 | 価格障害は upside にならない構造にする | `ObstacleReport.upside_from_obstacles()` は常に例外を投げる。「どこまで行けるか」を探すコードが現価格より上の水準リストに手を伸ばすのは自然なので、間違いの着地点を用意した。overhang は失効するので、減衰定数ではなく**水準ごとの `weakening_evidence`** で表現する | 2026-09-17 / Phase 6 実装 | `workers/src/surge/chart/obstacles.py` |
| D-118 | Stage 3 に ENTRY state を作らない | enum に member が無いので表現不能。EOD 分析は live price を持たないので entry を出せない。出力検証器は「buy now」等の指示的な文言と「旧高値まで戻る」型の upside 表現も reject する（抵抗としての言及は歓迎するので、言及ではなく**復帰の枠組み**にマッチさせる）| 2026-09-17 / Phase 7 実装 | `20260917170000` / `workers/src/surge/analysis/validate.py` |
| D-119 | 20% Threshold と Reachable Zone を列ごと分ける | 前者は参照価格への算術、後者は到達可能性の判断。`reachable_zone_basis_kinds` は `PRIOR_HIGH` を含まない閉集合で、zone を出すなら1つ以上が必須（CHECK）。両者が同じ値になったら検証器が reject する（片方がもう片方から導出されている）| 2026-09-17 / Phase 7 実装 | `20260917170000` |
| D-120 | 有料 LLM を Production 必須にしない | `LLMProvider` interface と deterministic mock で pipeline を完成させ、実モデル接続は `analysis.llm_providers` の1行にする。mock は**自分が mock であることを rationale に書く**（結果表に無記名の mock 判定が混ざるのは罠）| 2026-09-17 / Phase 7 実装 | `20260917170000` / `workers/src/surge/analysis/llm.py` |
| D-121 | Web は `surge_web` で接続する | `surge_readonly` はベーステーブル79個を読めるので、「画面は契約しか見ない」が規約でなく事実になるよう専用ロールを作った。`ui` スキーマの USAGE と view の SELECT だけを持ち、契約の外へ手を伸ばすと権限エラーになる。読み出し関数2つは同じ理由で SECURITY DEFINER | 2026-09-17 / UI 実装 | `20260917190000` |
| D-122 | ~~**TDnet は採用しない**~~ → **2026-09-17 撤回（D-125 が置き換え）** | **当初の判定**: (a) `release.tdnet.info/robots.txt` が全ホスト `Disallow: /`、(b) 開示ページが「無断で転用、複製又は販売等を行うことは固く禁じます」、(c) 有料 API は 月額70,000円 + 100,000円〜。**この3点は今も正しい**。誤っていたのは「したがって JP 適時開示は取得不能」という結論のほうで、**TDnet 本文**の取得不能から**適時開示の存在・時刻・発行体**の取得不能を導いてしまった。index と body は別物で、前者を提供する第三者 API が存在する（D-125）。**TDnet 本文を保存しない**という部分は撤回しない | 2026-09-17 / ユーザー指示により撤回 | [yanoshin-tdnet-api.md](research/yanoshin-tdnet-api.md) |
| D-123 | 却下した news source もレジストリに残す | `enabled=false` + 全 permission `PROHIBITED` の policy 付きで登録する。後からコレクタを書こうとすると `assert_may_collect()` が条項を添えて落ちる。market 側と同じ「一度却下したものは理由つきで却下のままにする」原則 | 2026-09-17 / 同上 | `20260917210000` |
| D-124 | robots と licence は別の列で持つ | JPX が実例: robots.txt は巡回を許可し、規約は保持を許可しない。「取りに行ってよいか」と「持っていてよいか」は別の問いなので `robots_allows_path` と `full_text_storage_allowed` を分ける | 2026-09-17 / 同上 | `20260917130000` |
| D-125 | **Yanoshin TDnet WebAPI を JP 適時開示の Discovery Source として採用** | 認証不要・0円。`news.sources.yanoshin_tdnet`。運営自身が「当サービスはインデックス情報を提供するものであり…書類データは提供元のサイトを必ずご確認ください」と書いており、index と body の線引きは**運営の言葉どおり**。robots.txt は `*.json` 等を **Googlebot にのみ**禁止し、他の全 UA には `Allow:/` | 2026-09-17 / ユーザー指示 | `20260917220000` / [yanoshin-tdnet-api.md](research/yanoshin-tdnet-api.md) |
| D-126 | **Yanoshin の利用可能性と TDnet 本文の保存許諾を同一視しない** | policy の `full_text_storage_allowed = PROHIBITED`。`news.assert_storage_allows` が全文書き込みを拒否し、adapter の `fetch_body()` は無条件に `None` を返す。本文の Verification は issuer 公式 IR / EDINET など**保存許諾が明示された source** から取り、同一 event に source link を足す | 2026-09-17 / 同上 | `20260917220000` / `workers/src/surge/news/sources/yanoshin_tdnet.py` |
| D-127 | **`since_id` / `by_id` は使わない**（documented だが実装されていない） | `llms.txt` は `since_id` を「ID より新しい item を返す」と明記するが、実測では **Unix timestamp として解釈**される（`since_id=1281122` → 「期間開始日1970/01/16」、`by_id=1281122` → 「期間終了日1970/01/16」で0件）。documented どおりに実装するとカーソルが永久に進まないか、常に0件で「静かな日」に見える。adapter は両パラメータを**例外で拒否**する。カーソルは `last_seen_id` の**クライアント側** high-water mark、日付レンジは `YYYYmmdd-YYYYmmdd` と `start_datetime`/`end_datetime`（こちらは正しく動く） | 2026-09-17 / 実測 | 同上 |
| D-128 | id は insert 順であって publication 順ではない | 実測: id 1281124 が 12:20、id 1281125 が 12:15。**新着判定の watermark としては正しく、時刻のソートキーとしては誤り**。`recent` が満杯で返ったら truncate された可能性があるので `window_is_safe()` が false を返し、呼び出し側は日付レンジで読み直す（初回の live pass で実際に発火） | 2026-09-17 / 実測 | 同上 |
| D-129 | company_code の末尾0を無条件に削除しない | 5文字目が `0` のときだけ落とす（`72030`→`7203`、`130A0`→`130A`）。`0` 以外なら**5文字すべて保持**して Security Master へ照合する（`587A4`、`13264`、`15574` はいずれも ETF/ETN）。実データ300件では 280 strip / 20 keep で、keep された20件は全て fund。`raw_company_code` を必ず保存し、DB の CHECK 制約が branch と結果の整合を強制する | 2026-09-17 / 実測 | 同上 |
| D-130 | Discovery 時刻は後から来た公式 IR で上書きしない | `material.events.first_known_at` は source 群の `min(available_to_model_at)`。後から届く VERIFICATION source は時刻が遅いので min は動かない。**コードの規律ではなく trigger の性質**として成立する | 2026-09-17 / 同上 | `20260917140000` / `workers/tests/test_db_tdnet_discovery.py` |
| D-131 | タイトル分類は type を付けるだけで、除外はしない | 本文が取れないので headline から type を導く。未知タイトルは `OTHER` になり、**それでも material candidate を生成する**（relevance は Phase 5 が構造シグナルで判定する）。confidence は常に `PROVISIONAL`、マッチしたパターンを evidence に残す。訂正・開示事項の経過は type ではなく**フラグ**（訂正された決算短信は依然として決算短信） | 2026-09-17 / 同上 | `workers/src/surge/material/tdnet_classify.py` |
| D-132 | mapping confidence は relation へそのまま運ぶ（再宣言しない） | `from_tdnet.to_relation()` が全件 `REGISTRY_ANCHORED` を返していた。exact 一致は `REGISTRY_ANCHORED`、4文字ベースでしか当たらなかったものは `PROVISIONAL` で、**PROVISIONAL のまま**運ぶ。境界で言い直すと弱い主張が誰も見ていない場所で強い主張に昇格する。cloud smoke の実データで 8 exact / 4 base、`news.tdnet_items.mapping_confidence` と `material.entity_relations.confidence` が12件すべて一致 | 2026-09-17 / ChatGPT 統合監査 | `workers/src/surge/material/from_tdnet.py` |
| D-133 | TDnet code の解決は bitemporal（現在行への直接クエリを使わない） | `ref.listings_as_of(effective_at, known_at)` を通す。`effective_at` = 開示の `pubdate`（当時どの上場が有効だったか）、`known_at` = その item を得た時刻（master が何を知っていたか）。**今の master で過去の開示を解決するのは可**、**当時それを知っていたことにするのは不可**。cache key は `(code, effective_at 日, known_at 日)` の3要素で、code だけで引くと最初の答えが以後すべての質問に居座る | 2026-09-17 / 同上 | `workers/src/surge/jobs/tdnet_discovery.py` / `workers/tests/test_db_tdnet_resolver.py` |
| D-134 | Authoritative Universe Gate は Material 側にも掛ける | Technical / Material の**ルート独立は維持**する。universe はルートではなく「この基盤が何を予測対象にするか」の全体規則なので、片側だけに掛けると ETF が開示経由で Stage 3 に入る。`INCLUDED` → 正式候補可、`UNRESOLVED` → event・relation・feature は保存し research からは見えるが正式候補にしない、`EXCLUDED` → event は保存、Stage 2 / Stage 3 に入れない。**universe を読んでいない場合も `UNRESOLVED`**（読まなかったことは通行許可ではない）。cloud smoke の実データで ETF 4件が EXCLUDED、候補生成 0件 | 2026-09-17 / 同上 | `workers/src/surge/material/universe_gate.py` |
| D-135 | `after_close` を 3値にする（`PRE_CLOSE` / `POST_CLOSE` / `UNKNOWN`） | 検証済み取引カレンダーが無い間は `UNKNOWN`。`UNKNOWN` からは `TECHNICAL_SETUP_EOD` も `POST_CLOSE_CATALYST_SETUP` も**主張しない**（`WATCH_OTHER` に落とす）。引けが既に織り込んだかどうかが両者を分けるので、分からないときに都合のよい方を選ばない。field 欠落は `None` ではなく `UNKNOWN` へ正規化する | 2026-09-17 / 同上 | `workers/src/surge/material/models.py` / `workers/src/surge/analysis/llm.py` |
| D-136 | classifier の confidence と mapping の confidence を同じ名前にしない | 同じ evidence JSON に並ぶので、前者を `classification_confidence` に改名した。`"confidence": "PROVISIONAL"` が `mapping_confidence` の隣にあると**同じ問いへの2つ目の意見**に読めるが、実際には「開示の種類がどれだけ確かか」と「どの銘柄の話かがどれだけ確かか」で別の問いである | 2026-09-17 / cloud smoke の生成 SQL を読んで発見 | `workers/src/surge/material/tdnet_classify.py` |
| D-137 | Cloud smoke は production の INSERT 文をそのまま描画して流す | cloud へは SQL 実行経路しか無いため、`news.db` / `material.db` の**文そのもの**を読み込み、`%(name)s` を literal に置換して1トランザクションにする。列リストと cast を smoke 側で書き直すと、migration との差異が smoke の成功として通ってしまう。DB が採番する `document_id` / `event_id` のみ自然キーの副問い合わせに置換する。**2026-09-17 実行結果**: documents 12（body_text 0 / 全件 `METADATA_ONLY`）、`news.raw_documents` 0、tdnet_items 12（全件 mapped）、events 12、event_sources 12（全件 `DISCOVERY`、`VERIFICATION` 0）、entity_relations 12、features 12、coverage 1、`material.candidates` 0、`first_known_at` = discovery 時刻 12/12。**全文取得 0 / TDnet PDF・XBRL body 0** | 2026-09-17 / ユーザー指示の1回限りの smoke | `workers/src/surge/jobs/tdnet_smoke.py` |

---

## B. 未決（Remaining）

### B-1. Phase 1 の前に必要

| ID | 種別 | 論点 | Claude Code の意見 |
|---|---|---|---|
| ~~D-00（登録）~~ | 前提 | **解決済み（A 表 D-00 参照）**。以下は経緯の記録: v5.1 全文の受領と Canonical 登録（Phase 1 の唯一の開始条件）。2026-09-16 の監査は「直前にユーザーが提示した全文」を Canonical と確定したが、**その本文は Claude Code 側のセッションには渡っていない**（ChatGPT 側の会話に存在すると考えられる）。捜索済み: 本セッションの文脈、全 Claude Code セッション記録（`v5.1` / `3000円以下限定` / `何銘柄を実際に評価できたか` / `織り込み判定` / `マルチスクリーニング` / `3段階ファネル` / `Dynamic Driver Score` / `Bigdata.com`）、Downloads / Documents / Desktop / OneDrive 配下、リポジトリ内。Google Drive コネクタは未認可で検索不可 | 記憶からの再生成・近似復元は禁止されているため作成しない。受領方法は (a) チャットへ全文貼り付け (b) ローカルにファイル保存してパスを伝える（バイト列をそのまま保持できるためこちらが確実）。受領後ただちに `short-surge-v5.1.original.md` へ無加工保存し、SHA-256 を MANIFEST に登録して D-00 を RESOLVED にし、Phase 1 に着手する |

### B-2. Phase 1 中に決めればよい（pending で開始可）

| ID | 種別 | 論点 | Claude Code の意見 |
|---|---|---|---|
| D-07a | 費用 | 米国株の役割ごとの Provider（Phase 1 はマスタ用） | **調査完了・提案あり（ユーザー承認待ち）**: EODHD All World $19.99/月。根拠と対抗馬は [phase-2-provider-evaluation.md](research/phase-2-provider-evaluation.md) §2 |
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
| D-40 | 技術 | US の `PROVISIONAL` 証券 6,328 件（SEC `company_tickers_exchange.json` に CIK がない ETF・ワラント等）と `REGISTRY_ANCHORED` 6,909 件を、security-level の安定識別子（FIGI / share-class 識別子 / provider の安定 ID）で `STRONG` 化できるか。**Phase 2 の US Provider 選定時に取得可否を確認し、取得できた場合にのみ昇格する**（D-44） | Phase 2 の Provider 選定時 | **調査完了・提案あり**: FIGI（`shareClassFIGI` / `compositeFIGI`）はパブリックドメインで DB 保存も再配布も明示的に許諾されており、唯一採用できる share-class 識別子。CUSIP は規約が master DB 化を否定し $46,825/年、ISIN は US ISIN に埋め込まれた CUSIP の権利関係が未解決、SEC は証券レベルの識別子を発行していない。取得経路は OpenFIGI（無料）/ Massive `/v3/reference/tickers`（$0 プラン）/ EODHD ID Mapping（`filter[cik]` が使える）。**昇格は identity_version を伴う記録された migration として別途行う**。[評価記録](research/phase-2-provider-evaluation.md) §5 |
| D-41 | 技術 | JP の `PROVISIONAL` 発行体 732 件（EDINET コード一覧に載らない ETF・REIT・出資証券等）の扱い | Phase 2 前 | 発行体の統合が必要になるのは主に普通株。EDINET 未突合は UNRESOLVED/EXCLUDED 側に偏っている |
| D-42 | 投資ロジック/技術 | 同一 CIK に複数の証券がぶら下がる 640 CIK（クラス株・優先株・ワラント等）のうち、どこまでを「同一発行体の別クラス」として扱い、どこからを別発行体とみなすか | Phase 3 前 | 現状は CIK = 発行体、クラスは証券側で分離。例外（合併・持株会社化で CIK が変わる場合）の扱いは未定 |
| D-43 | 技術 | US の identity collision 240 レコード / **67 distinct キー**（同一 CIK・同一種別・同一クラスに複数銘柄。最大は `US:CIK:0000927971:ETF:` の 42 銘柄）を、どの追加属性で分離するか | Phase 3 前 | 現状は両方を PROVISIONAL に降格し、`context` 付きで警告に残す。大半は ETN / レバレッジ ETF。discriminator を広げると 240 件の `security_id` が変わるため、rebuild としてしか実施できない |
| D-70 | 規約/技術 | JP の5桁証券コードの公式定義（証券コード協議会の仕様書 PDF）を取得し、コード形ガードを「公式根拠あり」へ格上げするか | Phase 3 前（銘柄名が決定的なため blocker ではない） | 公式ファイルの実測（4桁=普通株 4,434件 / 5桁=特殊株式 7件）で代用している |
| ~~D-53~~ | 技術 | **解決済み（A 表 D-53 参照）**: publication model を実装 | — | — |
| D-01b | 投資ロジック | 「次の取引可能時点」の定義 | Phase 8 前 | 判断しない |
| D-02a | 技術/投資ロジック | FX の Provider と許容遅延 | Phase 2 前 | **調査完了・提案あり**: 一次 = ECB 参照レート（無料・訂正されない）、二次 = EODHD `USDJPY.FOREX`（US プランに同梱）。[評価記録](research/phase-2-provider-evaluation.md) §3。**場中 ENTRY 用のレート選択は未決**（ECB は日次1本） |
| D-03b | 費用/技術 | Object Storage プロバイダ | Phase 2 前 | **調査完了・提案あり**: Cloudflare R2（egress 無料・バケット単位の資格情報）。Versioning が無いため write-once キー運用と Bucket Lock で代替。日本リージョン保証が要るなら S3 ap-northeast-1。[評価記録](research/phase-2-provider-evaluation.md) §4 |
| D-03c | 費用 | Supabase の Production プラン（D-03a の再評価） | Production 開始前 | — |
| D-05a | 費用/技術 | Production の Runner・Scheduler | Phase 4・8 前 | 場中とニュース収集は常駐型が必要な見込み |
| D-06a | 費用 | J-Quants の Production プランと分足・ティックアドオン | Phase 2 前 | **調査完了・提案あり**: EOD のみなら Standard ¥3,300/月（日足10年）。Light ¥1,650 は5年。配当は Premium ¥16,500 限定だが、1-9 により配当は +20% Target に加算しないため Phase 2 では不要。**ダウングレードすると上位プランでしか取れなかったデータの削除義務が生じる**点に注意。[評価記録](research/phase-2-provider-evaluation.md) §1 |
| **D-76** | **規約** | **J-Quants の利用条件（本人の私的利用に限る／法人不可／学術不可／解約・ダウングレード時の削除義務）を本プロジェクトが恒久的に満たし続けるかの確認** | **Phase 2 の取得開始前（blocker）** | Claude Code は判断しない。現状（個人1名の私的研究、GitHub には**コードのみ**公開、生データは Private）は規約の範囲内に読めるが、閲覧者が増える・法人化する・分析結果を継続反復的に公開する場合は範囲外になる |
| **D-77** | **技術/投資ロジック** | **US EOD の出来高が分割調整済みであることへの対処**（EODHD は OHLC が raw、Volume のみ split 調整済み）。splits フィードから raw 株数を復元するか、`volume_adjustment_state` を列として持って調整済みのまま扱うか | Phase 2 のスキーマ確定前 | 1-9 は「raw（取引された無調整価格）を保存」と定めており、出来高もその対象と読むのが自然。復元は分割履歴が完全な期間に限られる（EODHD は2018年より前の廃止銘柄の splits を持たない） |
| **D-78** | **技術** | **EOD 公表時刻が公式に明示されない Provider（EODHD の US EOD、EODHD FX）について、`available_at` をどう決めるか** | Phase 2 の取得実装前 | 自分の取得時刻（`received_at`）を `available_at` とし、Provider の公表時刻を推定値として混ぜない。1-7 の「`source_published_at` を利用可能時刻の代わりにしない」と同じ原則 |
| **D-92** | **規約/技術** | **EODHD `/api/id-mapping` のレスポンス形**。散文ページは `{meta, data[{symbol,isin,figi,lei,cusip,cik}], links}`、公式 OpenAPI は裸の配列 `[{Code,Exchange,Name,ISIN,FIGI,LEI,CUSIP,CIK}]` と**両立しない**。実レスポンスで決着させるまで使わない | 使用する前（Phase 2.2 以降） | どちらでもない側に実装すると全列が黙って null になる。CUSIP を返す点でも CGS の間接エンドユーザー条項に触れるため、取り込み時に cusip/isin を破棄する運用が要る |
| **D-93** | **技術** | **J-Quants `ProdCat` と Phase 1 の銘柄名判定の突き合わせ結果をどう扱うか**（不一致銘柄を UNRESOLVED にするか、ProdCat を優先するか） | Phase 2.2 の JP universe 再構築前 | Claude Code は判断しない。ProdCat は普通株と優先株式を区別しないため、単純な置き換えはできない |
| **D-102** | **規約（blocker）** | **JPX 統計ファイルの「二次利用」の解釈。** 公表された統計ファイルとその値を、本人だけが閲覧する私的DBに保持し自分の投資判断にのみ使うことが二次利用に当たるか。**JP の当日EOD価格を0円で得る唯一の経路**で、可否がここで決まる | JP 価格取得の着手前 | 東京証券取引所 株式部データサービス室へ書面照会。robots.txt は全許可、規約は二次利用を未定義のまま禁止しており、公式文書だけでは決着しない |
| **D-103** | **規約/技術** | **Alpaca Basic を US EOD に使えるか。** (a) 規約の「intended for United States residents only」と非米国口座の販売が自社文書内で矛盾している、(b) 無料枠が full SIP を返すか IEX のみかがドキュメント内で矛盾している | US 価格取得の着手前 | (b) は無料APIキーで `feed=sip` を1回叩けば確定する。(a) は Alpaca への書面確認が要る。どちらも口座保有者本人しかできない |
| **D-104** | **規約** | **GitHub Actions を日次取得の実行環境にしない判断の確認。** Actions 追加規約は GitHub-hosted runner の用途を当該リポジトリのソフトウェアの production/testing/deployment/publication に限定し、違反時の措置はリポジトリ無効化・アカウント停止 | 日次運用の開始前 | Claude Code の判断: **使わない**。代替はローカル実行 / Oracle Always Free (A1 2 OCPU・回収リスクあり) / GitHub への書面確認。CI（テスト）としての利用は規約どおりの用途で問題ない |
| **D-105** | **費用/技術** | **Cloudflare R2 のカード登録。** バケット作成前に subscription checkout が必須で、超過は自動停止せず課金される。支払い失敗時はバケット利用不可、30日でデータ削除の可能性 | R2 バケット作成前 | 無料枠内に留めるのは**運用ポリシー**であって Cloudflare は止めてくれない。lifecycle と保持期間で 10 GB-月を守る設計が前提 |
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
