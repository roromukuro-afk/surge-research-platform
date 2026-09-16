# Security Master の同一性（Identity）と履歴 — Phase 1.1 / 1.1a

状態: **Phase 1.1 監査（2026-09-16）+ Phase 1.1a 是正（2026-09-16）**
対象 migration: `20260916150000` / `150100` / `150400` / `150500` / `160000` / `160100` / `160200` / `160300` / `160400` / `160500` / `160600` / `160700`
対象コード: `workers/src/surge/identity.py`（`IDENTITY_VERSION = identity-1.1a`）、`workers/src/surge/jobs/universe_sync.py`

## 0. 原則

- **Ticker は属性であって同一性ではない。** Ticker が変わっても同じ銘柄、同じ Ticker でも別の銘柄になりうる。
- **正規化した名称は証拠であって同一性ではない。** 名称一致で発行体を統合しない（Phase 1.1a で fallback からも名称を排除）。
- 同一性は **公的レジストリの識別子**から作る。作れないときは `PROVISIONAL` と明示し、黙って推測しない。
- **confidence を実態より高く言わない。** レジストリ識別子＋テキスト由来の属性で作った鍵は `STRONG` ではなく `REGISTRY_ANCHORED`。
- 同一性が確定できない衝突は、**統合せず両方を降格**し、run の警告として記録する。
- **false merge より false split を優先する。** 分かれたものは後から統合できるが、統合してしまったものは元に戻せない。

## 1. 同一性キーと confidence

| 階層 | 条件 | キー | confidence |
|---|---|---|---|
| Issuer（US） | SEC CIK あり | `CIK:<cik>` | `STRONG` |
| Issuer（JP） | EDINET コードあり | `EDINET:<code>` | `STRONG` |
| Issuer（共通） | 上記なし | `ISSUER-OF:<security identity key>` | `PROVISIONAL` |
| Security（JP） | 常に | `JP:JPX:<local code>` | `STRONG` |
| Security（US） | CIK あり | `US:CIK:<cik>:<security type>:<class token>` | **`REGISTRY_ANCHORED`** |
| Security（US） | CIK なし | `US:<exchange>:SYMBOL:<symbol>` | `PROVISIONAL` |
| Listing | 常に | `<exchange_id>` + Security 同一性キー | Security に従う |

### confidence の定義（`ref.identity_confidence_rank` が順序を持つ）

| 値 | 意味 |
|---|---|
| `STRONG` | **そのもの**に対してレジストリが発行した識別子（issuer の CIK / EDINET、JP security の JPX コード） |
| `REGISTRY_ANCHORED` | レジストリ識別子（issuer 単位）＋ provider のテキストから読んだ属性。実務上は安定だが保証はない |
| `PROVISIONAL` | レジストリ識別子がない |

`US:CIK:<cik>:<type>:<class token>` の `class token` は表示名（"Class A Common Stock" 等）から抽出する。Provider が表記だけ変えても鍵が動きうるため **STRONG と呼ばない**（Phase 1.1a 監査 #6）。昇格は自動で行わず、`identity_version` を伴う**記録された identity migration** として実施する。

### 衝突（collision）

同じ `STRONG` / `REGISTRY_ANCHORED` キーに解決された2件以上が別の銘柄であるとき:

1. 双方を `PROVISIONAL` キー（`US:<exchange>:SYMBOL:<symbol>`）へ降格する。
2. `pipeline.run_errors` に `error_type = 'IDENTITY_COLLISION'` と `context = {identity_key, symbol, exchange_id}` を記録する（本文の regex 解析に依存しない）。
3. 統合も破棄もしない。coverage には**レコード数と distinct キー数**を分けて記録する。

**降格の判定は `confidence in DEMOTABLE`（STRONG と REGISTRY_ANCHORED）で行う。** 単一値との比較（`== STRONG`）にすると、新しい tier が衝突検知から抜け落ちて黙って統合されるため、`workers/tests/test_identity_resolution.py` にその回帰テストを置いている。

## 2. 発行体の名称（Phase 1.1a）

- **レジストリ識別子を持つ発行体の `ref.issuers.legal_name` はレジストリの名称**（SEC の registrant name / EDINET の提出者名）。Provider の商品名（"Apple Inc. - Common Stock"）を発行体名にしない。
- レジストリ識別子が無い発行体（2026-09-16 実測 6,820 / 16,567）は、そもそもレジストリ名が存在しない。`legal_name` には provider の表示名が入り、`ref.issuer_names` では **`ALIAS`** として記録する（`LEGAL` ではない）。
- `normalized_name`（照合用）もレジストリ名から作る。商品名の正規化を発行体の照合キーにしない。
- Security 自身の名称は従来どおり `ref.security_names`。
- `ref.issuer_names` に SCD2 で保持する。

| `name_type` | 意味 |
|---|---|
| `LEGAL` | レジストリ由来の名称 |
| `ALIAS` | レジストリ名が無いときの provider 表示名。**照合の証拠であり、同一性ではない** |
| `FORMER` | 旧名称（レジストリが旧名を提供する場合に使用） |

open 行の一意制約は `(issuer_id, name_type)` なので、**ALIAS と LEGAL は同時に open でよい**。レジストリ名が後から得られた発行体は LEGAL 行が開き、それまでの ALIAS 行は「照合に使える別名」として開いたまま残る（意図した挙動）。

既知の弱点: レジストリ名が**取得できなくなった**場合、`ref.issuers.legal_name` は provider の商品名（ALIAS 相当）へ戻る。現在の provider では起きていないが、後退を検知するなら「LEGAL が一度付いた発行体は ALIAS へ降格しない」ガードが必要（D-46 の運用課題）。

### 既知の限界: 衝突集合が変わるとキーが動く

`REGISTRY_ANCHORED` のキーは、**同一 snapshot 内の兄弟銘柄との衝突**によって降格される。したがって次の場合、rebuild 手順を経ずに `security_id` が変わりうる。

- 衝突していた兄弟が上場廃止・非掲載になり、衝突が解消されて `US:CIK:...` へ戻る
- provider が表示名を変更し、`class token` が変わる

これは「テキスト由来の属性を含むキー」の構造的な帰結であり、confidence を `REGISTRY_ANCHORED` に留めている理由そのものでもある。Phase 2 以降の Raw Market Data が provider の native key と `identity_version` を必ず保持するのは、この再割当を後から追えるようにするため（§6）。現時点では検出も記録もされないので、**Phase 2 で「前回 run に存在した security_id が今回消えた」件数を監視対象にする**（D-40 で恒久対応を決める）。

## 3. 履歴（SCD2）

`ref.listing_states`（区分・状態）、`ref.listing_symbols`（Ticker）、`ref.security_names`（名称）、`ref.security_identifiers`（CIK / EDINET / 法人番号 / Ticker）、`ref.issuer_names`（発行体名）は SCD2 で持つ。

- 値が変わったときだけ現行行を `effective_to` で閉じ、新しい行を開く。
- 値が変わっていない再観測では行を増やさず、`last_confirmed_at` だけ進める。
- 同一 subject に対して**開いている行は常に1行**（部分 unique index で強制）。
- 履歴テーブルに `DELETE` 権限を与えない（append-only）。
- `ref.listing_status_history` は Phase 1.1a で廃止（`ref.listing_states` が唯一の正本）。

## 4. as-of 読み出し

`ref.listings_as_of(p_available_at)` は次を返す:

- 属性（`market_segment_code` / `listing_status`）: `ref.listing_states` から `available_at <= p` かつ `effective_to` が未設定または `p` より後の行
- **Ticker（`symbol`）: `ref.listing_symbols_as_of(p)` から同じカットオフで**

`ref.listings.local_code` は **current materialized state** であり、過去の Ticker の正本ではない（Phase 1.1a 監査 #3）。as-of 結果に現在値が混ざらないよう、この関数は materialized 行から属性を読まない。

### 知識時刻と有効時刻（Phase 1.1b）

as-of 読み出しは**2つの時間軸**で絞る。

| 軸 | 列 | 意味 |
|---|---|---|
| 知識時刻 | `available_at` | システムがその行を知り得た時刻 |
| 有効時刻 | `effective_from` / `effective_to` | 事実そのものが成り立つ期間 |

`ref.listings_as_of(p_effective_at, p_known_at)` / `ref.listing_symbols_as_of(p_effective_at, p_known_at)` が正式形。1引数版は両方に同じ時刻を入れた「その時点の姿を、その時点の知識で」。

Phase 1.1b 以前は `effective_from <= cutoff` を見ていなかったため、「T1 に判明した、T2 から有効な変更」が T1 の読み出しに現れていた（未来の混入）。

### 時刻の意味（Phase 1.1c）

| 列 | 意味 |
|---|---|
| `observed_at` | **その値を供給した source** を読んだ時刻（CIK なら SEC ticker file、EDINET コードなら EDINET code list） |
| `available_at` | その行を**システムが知り得た**時刻。identity に依存する行では **run が依存する全 source の `available_at` の最大値** |
| `effective_from` / `effective_to` | 事実そのものが成り立つ期間 |
| `pipeline.runs.data_cutoff` | `max(source_fetches.available_at)`。最初の source ではなく**最後の source** |

理由: US の run は Nasdaq → SEC ticker → SIC 6770 → SIC 6798 の順に取得し、最後の source は primary より **91秒** 遅い。primary の時刻を cutoff にすると、その run に含まれる情報より早い時刻を「知っていた」ことになる。CIK から作られた `security_id` は、CIK を取得する前の as-of 読み出しに現れてはならない（Phase 1.1c 監査 #7）。

provenance（どこから来たか）と visibility（いつ知り得たか）は別物として扱う:

| 行 | source_id / observed_at | available_at |
|---|---|---|
| `TICKER` / `LOCAL_CODE` | market data provider | dependency max |
| `CIK` | `sec_company_tickers`（record id = ticker） | dependency max |
| `EDINET` / 法人番号 | `edinet_code_list`（record id = EDINET コード） | dependency max |
| issuer name（LEGAL） | 名称を供給したレジストリ | dependency max |

既存行の provenance は `ref.refresh_identifier_provenance` / `ref.refresh_issuer_name_matching` が修復する。**値と有効期間は変えない**（それは新しい version の仕事）。`available_at` は**後ろにしか動かさない**（早すぎた可視性は直すが、既に答えた as-of 読み出しから遡って隠さない）。

## 5. どの run が Universe か（Phase 1.1b / 1.1c）

### 公開後は変更できない（Phase 1.1c）

publish された run に属する行は **INSERT / UPDATE / DELETE すべて拒否**される（owner を含む全ロール）。対象:

`pipeline.runs` / `pipeline.source_fetches` / `pipeline.run_errors` / `pipeline.master_snapshot` / `universe.evaluations` / `universe.coverage`

trigger が `pipeline.assert_run_mutable(run_id)` を呼び、publish 済みなら例外（SQLSTATE `25006`）。**訂正は「新しい run を publish して supersede する」**という形でのみ行う。

publish と artifact 書き込みは run 単位の advisory lock で直列化する（書き込み側は shared、publish は exclusive）。`publish_run` は insert 後に再 validate し、内容が動いていたら publication ごと中止する。

`ref.*`（materialized master）は凍結対象ではない: SCD2 の現在状態であり、published artifact ではない。

同一 as-of 日に複数の run（再取得・再構築）が存在しうる。**`finished_at` が新しい run を自動的に正とはしない。**

- `pipeline.run_publications` に載った run だけが authoritative。
- 公開の前提は `pipeline.validate_run`（SUCCEEDED / finished_at / git_sha / config_hash / job_version / universe_version / identity_version / provider_bindings / source_fetches / MARKET coverage / fatal error なし / 件数整合）。
- 読み出しは `universe.authoritative_run_at(market, universe_version, knowledge_cutoff)` と `universe.eligibility_as_of(...)`。`published_at <= knowledge_cutoff` で絞るので、**後から再構築した run が過去の Replay に逆流しない**。
- `universe.current_eligibility` は「いま」の Universe。

## 6. 同一性が変わるときの手順（rebuild）

in-place 移行ができない変更は次の手順に限る。**migration を適用しただけでは旧 DB の移行は完了しない。**

1. `ref.identity_migration_map` へ旧 `security_id` / `listing_id` / `issuer_id` と provider 座標を記録し、master・universe 判定・coverage・staging を削除する（`pipeline.runs` と `pipeline.source_fetches` は残す）。
2. 公式ソースから**再取得**し、新しい同一性規則で snapshot を load する。
3. `select ref.finalize_identity_rebuild('<label>')` で `new_*` を埋める。**master が空のときは例外を投げて拒否する**ので、reload 前に実行して「移行できたつもり」になることがない。
4. before / after の件数と、変わった Universe 判定の件数を報告する。

実行手順は [phase-1-universe-sync.md](../runbooks/phase-1-universe-sync.md) と `scripts/rebuild_security_master.sh` にある。再構築した run も validate → publish を経て初めて Universe になる。

## 7. Phase 2 以降への申し送り

- **Raw Market Data の唯一の復元キーを `security_id` にしない。** 各レコードに `provider_id` / provider の native symbol / exchange / `observed_at` / provider の source record id（あれば provider security id）/ `identity_version` を必ず残す。Identity が昇格・変更されても再割当できるようにするため。
- US の security-level 安定識別子（FIGI、share-class レベルの識別子、provider の安定 ID 等）を Provider 選定時に確認し、取得できた場合にのみ `STRONG` へ昇格する（D-40 / D-44）。

## 8. 回帰テスト

| Fixture | 内容 | 実装 |
|---|---|---|
| A | 同一 CIK・複数証券 → 発行体は1つ、証券は別 | `workers/tests/test_identity_resolution.py` |
| B | 別 CIK・名称衝突 → 発行体を統合しない | 同上 |
| C | Ticker 変更 → 同じ security/listing、Ticker 履歴が2行 | 同上 + `test_db_master_semantics.py` |
| D | 名称変更のみ → 同一性は不変 | `test_identity_resolution.py` |
| E | 無変化の再取得 → 履歴が増えず `last_confirmed_at` が進む | `test_db_master_semantics.py` |
| F | as-of → T1 と T2 で別の状態を返す | 同上 |
| G | listing 未解決の判定 → 再適用で重複しない | 同上 |
| I | worker principal の最小権限 | 同上 |
| 1.1a-1 | worker は allowlist / 設定表へ書き込めない、run 出力へは書ける | `test_db_privileges_and_names.py` |
| 1.1a-2 | as-of が当時の Ticker を返す（ABC → XYZ） | 同上 |
| 1.1a-3 | issuer の legal_name がレジストリ名、商品名にならない | 同上 |
| 1.1a-4 | レジストリ名が無い発行体は ALIAS として保持、名称一致でも統合しない | 同上 |
| 1.1a-5 | coverage が provider error / data quality / identity collision（record と key）を分ける | 同上 |
| 1.1a-6 | US の CIK 由来 security identity は `REGISTRY_ANCHORED`、表記変更で STRONG を名乗らない | `test_identity_resolution.py` |
| 1.1a-7 | `REGISTRY_ANCHORED` の衝突も降格される（`== STRONG` 退行の防止） | 同上 |
| 1.1a-8 | TSV の列数と SQL loader の期待値が一致する | `test_snapshot_width.py` |
| 1.1b-1 | worker は `extensions.http*` を直接実行できない / loader 経由のみ | `test_db_publication_and_time.py` |
| 1.1b-2 | project schema に PUBLIC 実行可能な function が存在しない（新規作成分も） | 同上 |
| 1.1b-3 | JP の特殊株式7件（実データ）が普通株にならない・将来の同種も捕捉 | `test_jp_special_shares.py` |
| 1.1b-4 | 未公開 / RUNNING / FAILED の run は authoritative にならない、publish 後のみ切り替わる | `test_db_publication_and_time.py` |
| 1.1b-5 | 知識時刻 T1・有効時刻 T2 の変更が T1 の読み出しに現れない | 同上 |
| 1.1b-6 | 同一 logical invocation は同じ idempotency key、snapshot/config/version が変われば別 key | `test_run_provenance.py` |
| 1.1b-7 | `--git-sha` なしの PRODUCTION artifact を作らない | 同上 |
| 1.1b-8 | SIC membership が1件変われば source_data_version も変わる | `test_providers_classification.py` |
| 1.1c-1 | published run の artifact は更新・追記・削除できない（owner / worker とも） | `test_db_published_immutability.py` |
| 1.1c-2 | 未 publish run には通常どおり書ける | 同上 |
| 1.1c-3 | truncated な critical source は publish を拒否される | 同上 |
| 1.1c-4 | 市場混在・version 不一致・digest 欠落・cutoff 不足を validation が弾く（6ケース） | 同上 |
| 1.1c-5 | identifier は値を供給した source の provenance を持つ（CIK = SEC、record id = ticker） | 同上 |
| 1.1c-6 | issuer name は名称を供給したレジストリの provenance を持つ | 同上 |
| 1.1c-7 | snapshot 行の available_at は最後の source に合わせる | 同上 + `test_run_provenance.py` |
| 1.1c-8 | `data_cutoff` は最初ではなく最後の source | `test_run_provenance.py` |
| 1.1c-9 | Nasdaq の combined content hash が 64hex の SHA-256 | `test_providers_classification.py` |
