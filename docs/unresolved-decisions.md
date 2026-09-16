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

---

## B. 未決（Remaining）

### B-1. Phase 1 の前に必要

| ID | 種別 | 論点 | Claude Code の意見 |
|---|---|---|---|
| ~~D-00（登録）~~ | 前提 | **解決済み（A 表 D-00 参照）**。以下は経緯の記録: v5.1 全文の受領と Canonical 登録（Phase 1 の唯一の開始条件）。2026-09-16 の監査は「直前にユーザーが提示した全文」を Canonical と確定したが、**その本文は Claude Code 側のセッションには渡っていない**（ChatGPT 側の会話に存在すると考えられる）。捜索済み: 本セッションの文脈、全 Claude Code セッション記録（`v5.1` / `3000円以下限定` / `何銘柄を実際に評価できたか` / `織り込み判定` / `マルチスクリーニング` / `3段階ファネル` / `Dynamic Driver Score` / `Bigdata.com`）、Downloads / Documents / Desktop / OneDrive 配下、リポジトリ内。Google Drive コネクタは未認可で検索不可 | 記憶からの再生成・近似復元は禁止されているため作成しない。受領方法は (a) チャットへ全文貼り付け (b) ローカルにファイル保存してパスを伝える（バイト列をそのまま保持できるためこちらが確実）。受領後ただちに `short-surge-v5.1.original.md` へ無加工保存し、SHA-256 を MANIFEST に登録して D-00 を RESOLVED にし、Phase 1 に着手する |

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
| D-40 | 技術 | US の `PROVISIONAL` 証券 6,328 件（SEC `company_tickers_exchange.json` に CIK がない ETF・ワラント等）と `REGISTRY_ANCHORED` 6,909 件を、security-level の安定識別子（FIGI / share-class 識別子 / provider の安定 ID）で `STRONG` 化できるか。**Phase 2 の US Provider 選定時に取得可否を確認し、取得できた場合にのみ昇格する**（D-44） | Phase 2 の Provider 選定時 | ほぼ ETF・ユニット・ワラントで、`INCLUDED` には入らない。Phase 2 で `INCLUDED` ∪ `UNRESOLVED` を追跡する範囲では影響が小さい |
| D-41 | 技術 | JP の `PROVISIONAL` 発行体 732 件（EDINET コード一覧に載らない ETF・REIT・出資証券等）の扱い | Phase 2 前 | 発行体の統合が必要になるのは主に普通株。EDINET 未突合は UNRESOLVED/EXCLUDED 側に偏っている |
| D-42 | 投資ロジック/技術 | 同一 CIK に複数の証券がぶら下がる 640 CIK（クラス株・優先株・ワラント等）のうち、どこまでを「同一発行体の別クラス」として扱い、どこからを別発行体とみなすか | Phase 3 前 | 現状は CIK = 発行体、クラスは証券側で分離。例外（合併・持株会社化で CIK が変わる場合）の扱いは未定 |
| D-43 | 技術 | US の identity collision 240 レコード / **67 distinct キー**（同一 CIK・同一種別・同一クラスに複数銘柄。最大は `US:CIK:0000927971:ETF:` の 42 銘柄）を、どの追加属性で分離するか | Phase 3 前 | 現状は両方を PROVISIONAL に降格し、`context` 付きで警告に残す。大半は ETN / レバレッジ ETF。discriminator を広げると 240 件の `security_id` が変わるため、rebuild としてしか実施できない |
| D-53 | 技術 | 同一 `as_of_date` に同じ市場の run が複数ある場合（再取得・再構築）、下流（Phase 2 の価格取得、現在 Eligibility の参照）はどの run の `universe.evaluations` を正とするか。現在は run ごとに全件が残り、最新 run を選ぶ規則が未定義 | Phase 2 前 | `run_id` の最新（`finished_at` 最大）を採る素直な規則で足りるはずだが、再構築時に「途中まで失敗した run」を選ばないためのガードが要る |
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
