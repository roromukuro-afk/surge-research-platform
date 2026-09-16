# Security Master の同一性（Identity）と履歴 — Phase 1.1

状態: **Phase 1.1 監査（2026-09-16）による是正**
対象 migration: `20260916150000` / `20260916150100` / `20260916150400` / `20260916150500`
対象コード: `workers/src/surge/identity.py`、`workers/src/surge/jobs/universe_sync.py`

## 0. 原則

- **Ticker は属性であって同一性ではない。** Ticker が変わっても同じ銘柄、同じ Ticker でも別の銘柄になりうる。
- **正規化した名称は証拠であって同一性ではない。** 名称一致で発行体を統合しない。
- 同一性は **公的レジストリの識別子**から作る。作れないときは `PROVISIONAL` と明示し、黙って推測しない。
- 同一性が確定できない衝突は、**統合せず両方を PROVISIONAL に降格**し、run の警告として記録する。

## 1. 同一性キー

| 階層 | 条件 | キー | confidence |
|---|---|---|---|
| Issuer（US） | SEC CIK あり | `CIK:<cik>` | `STRONG` |
| Issuer（JP） | EDINET コードあり | `EDINET:<code>` | `STRONG` |
| Issuer（共通） | 上記なし | `NAME:<market>:<normalized name>` | `PROVISIONAL` |
| Security（JP） | 常に | `JP:JPX:<local code>` | `STRONG` |
| Security（US） | CIK あり | `US:CIK:<cik>:<security type>:<class token>` | `STRONG` |
| Security（US） | CIK なし | `US:<exchange>:SYMBOL:<symbol>` | `PROVISIONAL` |
| Listing | 常に | `<exchange_id>` + Security 同一性キー | Security に従う |

- `class token` は名称から取り出した株式種類（`Class A` → `A` 等）。同一 CIK・同一種別でも **クラス違いは別 Security**。
- `security_id` / `issuer_id` / `listing_id` は上記キーの決定的 UUID（`ref.deterministic_uuid`、SQL と Python で同一）。
- JP の EDINET 突合は、EDINET コード一覧の「証券コード」（5桁）と JPX の4桁コード + `0` を突き合わせる。

### 衝突（collision）

同じ `STRONG` キーに解決された2件以上が、別の Ticker・別の銘柄であるとき:

1. 双方を `PROVISIONAL` キー（`US:<exchange>:SYMBOL:<symbol>`）へ降格する。
2. `pipeline.run_errors` に `identity collision on <key>: fell back to symbol identity for <symbol>` を記録する。
3. 統合も破棄もしない。件数は summary の `identity.collisions` に出す。

## 2. 履歴（SCD2）

`ref.listing_states`（区分・状態）、`ref.listing_symbols`（Ticker）、`ref.security_names`（名称）、`ref.security_identifiers`（CIK / EDINET / 法人番号 / Ticker）は SCD2 で持つ。

- 値が変わったときだけ現行行を `effective_to` で閉じ、新しい行を開く。
- 値が変わっていない再観測では行を増やさず、`last_confirmed_at` だけ進める。
- 同一の subject に対して**開いている行は常に1行**（部分 unique index で強制）。
- 履歴テーブルに `DELETE` 権限を与えない（append-only）。

## 3. as-of 読み出し

`ref.listings_as_of(p_available_at)` は `ref.listing_states` を `available_at <= p_available_at` かつ `effective_to` が未設定または `p_available_at` より後の行で読む。

- 「その時点でシステムが知り得た姿」を返す。後から入った訂正・backfill は過去の as-of に混ざらない。
- Prediction・Replay・特徴量生成はすべてこの関数（と同じ規則）を通す。

## 4. 同一性が変わるときの手順（rebuild）

Ticker 由来の ID から registry 由来の ID への移行のように、in-place 移行ができない変更は次の手順に限る。

1. `ref.identity_migration_map` へ旧 `security_id` / `listing_id` / `issuer_id` と provider 座標（market / exchange / local_code / symbol）を記録する。
2. master・universe 判定・coverage・staging を削除する。`pipeline.runs` と `pipeline.source_fetches` は**残す**（何をいつ取得したかの記録）。
3. 公式ソースから再取得し、新しい同一性規則で再構築する。
4. `new_security_id` / `new_listing_id` / `new_issuer_id` を埋め、旧 → 新を再構成できる状態にする。
5. before / after の件数と、変わった Universe 判定の件数を報告する。

rebuild は versioned migration と worker code に残す。Dashboard の手作業で行わない。

## 5. 回帰テスト

| Fixture | 内容 | 実装 |
|---|---|---|
| A | 同一 CIK・複数証券 → 発行体は1つ、証券は別 | `workers/tests/test_identity_resolution.py` |
| B | 別 CIK・名称衝突 → 発行体を統合しない | 同上 |
| C | Ticker 変更 → 同じ security/listing、Ticker 履歴が2行 | 同上 + `workers/tests/test_db_master_semantics.py` |
| D | 名称変更のみ → 同一性は不変 | `test_identity_resolution.py` |
| E | 無変化の再取得 → 履歴が増えず `last_confirmed_at` が進む | `test_db_master_semantics.py` |
| F | as-of → T1 と T2 で別の状態を返す | 同上 |
| G | listing 未解決の判定 → 再適用で重複しない | 同上 |
| I | worker principal の最小権限 | 同上 |
