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

- **`ref.issuers.legal_name` はレジストリの名称**（SEC の registrant name / EDINET の提出者名）。Provider の商品名（"Apple Inc. - Common Stock"）を発行体名にしない。
- Security 自身の名称は従来どおり `ref.security_names`。
- `ref.issuer_names` に SCD2 で保持する。

| `name_type` | 意味 |
|---|---|
| `LEGAL` | レジストリ由来の名称 |
| `ALIAS` | レジストリ名が無いときの provider 表示名。**照合の証拠であり、同一性ではない** |
| `FORMER` | 旧名称（レジストリが旧名を提供する場合に使用） |

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

## 5. 同一性が変わるときの手順（rebuild）

in-place 移行ができない変更は次の手順に限る。**migration を適用しただけでは旧 DB の移行は完了しない。**

1. `ref.identity_migration_map` へ旧 `security_id` / `listing_id` / `issuer_id` と provider 座標を記録し、master・universe 判定・coverage・staging を削除する（`pipeline.runs` と `pipeline.source_fetches` は残す）。
2. 公式ソースから**再取得**し、新しい同一性規則で snapshot を load する。
3. `select ref.finalize_identity_rebuild('<label>')` で `new_*` を埋める。**master が空のときは例外を投げて拒否する**ので、reload 前に実行して「移行できたつもり」になることがない。
4. before / after の件数と、変わった Universe 判定の件数を報告する。

実行手順は [phase-1-universe-sync.md](../runbooks/phase-1-universe-sync.md) と `scripts/rebuild_security_master.sh` にある。

## 6. Phase 2 以降への申し送り

- **Raw Market Data の唯一の復元キーを `security_id` にしない。** 各レコードに `provider_id` / provider の native symbol / exchange / `observed_at` / provider の source record id（あれば provider security id）/ `identity_version` を必ず残す。Identity が昇格・変更されても再割当できるようにするため。
- US の security-level 安定識別子（FIGI、share-class レベルの識別子、provider の安定 ID 等）を Provider 選定時に確認し、取得できた場合にのみ `STRONG` へ昇格する（D-40 / D-44）。

## 7. 回帰テスト

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
