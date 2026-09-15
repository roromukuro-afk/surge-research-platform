# 教師ラベル仕様（Objective / Interpretive）

状態: **v0.2（Phase 0.2 監査是正後）。個々のラベル基準は Phase 10 前に確定（D-13a / D-13b）** — 2026-09-15

## 1. 2層構造

| 層 | 決め方 | 版 | 真実として扱うか |
|---|---|---|---|
| **Objective** | 価格パスからコードで確定 | `label_version`（コード版） | データ誤りがなければ確定値 |
| **Interpretive** | AI の Research 判定（adjudication）。必要に応じ人手レビュー | `labeler_model_version` + `prompt_version` | **仮説。** 信頼度とレビュー状態付き |

## 2. Objective ラベル

### 2-1. 計算の前提（[lifecycle](entry-and-episode-lifecycle.md) §7〜§9）

| 項目 | 定義 |
|---|---|
| 基準価格 | `entry_reference_price`（`decision_price` や `signal_reference_price` ではない） |
| 通貨 | 銘柄の取引通貨（JP = 円、US = USD） |
| 価格系列 | comparable path（分割・併合を ENTRY 時点の株数ベースに換算。配当は加算しない） |
| Failure | `initial_failure_line`（`current_risk_line` ではない） |
| Horizon | ENTRY 成立セッションを S0 として S20 の引けまで。REAFFIRMED 等でリセットしない |
| 打ち切り | `THESIS_INVALIDATED` で Episode を運用上クローズしても、Horizon 終了まで計算を続ける（D-17e 暫定） |

### 2-2. ラベル

| ラベル / 値 | 定義 |
|---|---|
| `path_resolution` | TARGET_FIRST / FAILURE_FIRST / AMBIGUOUS_PATH / UNRESOLVED_MISSING_DATA / NEITHER_BY_HORIZON / CORPORATE_ACTION_SUSPECTED（未確定） |
| `resolution_granularity` | SESSION_OPEN / DAY / INTRADAY_BAR / TRADE |
| `hit_10` / `hit_20` / `hit_30` | 各 horizon 内の comparable 高値が基準価格 ×1.10 / ×1.20 / ×1.30 以上 |
| `days_to_20` | 初めて +20% に達したセッション番号 |
| `mfe` / `mae` | comparable path 上の最大含み益率 / 最大含み損率（ENTRY 観測時刻より後のみ） |
| `failure_line_hit` | `initial_failure_line` に到達したか |
| `hit_20_before_failure` / `failure_before_20` | `path_resolution` から導出。AMBIGUOUS_PATH・UNRESOLVED_MISSING_DATA のときは両方 `null` |
| 補助（研究用） | `risk_line_path_resolution`（`current_risk_line` 基準）、米国株の `jpy_return`、配当込みリターン |

### 2-3. 実装指示書 §29 のラベルのうち Objective 側に分類する候補（暫定、D-13a）

| ラベル | 暫定定義 |
|---|---|
| `FAILED_BEFORE_TARGET` | `path_resolution = FAILURE_FIRST` |
| `FALSE_POSITIVE` | ENTRY Episode が `NEITHER_BY_HORIZON`。FAILURE_FIRST を含めるかは D-13a |

## 3. Interpretive ラベル

| ラベル | 前提となる Objective 条件（整合性制約、暫定） |
|---|---|
| `PREDICTIVE_SUCCESS` | TARGET_FIRST |
| `STATE_CONFIRMED_SUCCESS` | TARGET_FIRST |
| `PRICE_SUCCESS_EXOGENOUS` | TARGET_FIRST（または hit_20） |
| `PRICED_IN_ERROR` | TARGET_FIRST 以外 |
| `REACHABLE_ZONE_ERROR` | — |
| `DISTRIBUTION_ERROR` | — |
| `FALSE_PULLBACK` | — |
| `ACTIONABLE_FALSE_NEGATIVE` | ENTRY されなかった銘柄で hit_20 |
| `OUT_OF_SCOPE_SHOCK` | ENTRY されなかった銘柄で hit_20 |
| `OUT_OF_SCOPE_LATE` | ENTRY されなかった銘柄で hit_20 |

- 整合性制約に反する Interpretive ラベルは保存時に拒否する。
- `AMBIGUOUS_PATH` / `UNRESOLVED_MISSING_DATA` / `CORPORATE_ACTION_SUSPECTED` の Episode には成功系・失敗系の Interpretive ラベルを付けない。

### 3-1. 保存項目（必須）

| 列 | 内容 |
|---|---|
| `label` | ラベル |
| `labeler_model_version` | 判定に使ったモデル/ルールの版 |
| `confidence` | 判定の信頼度（初期段階では校正されていない値として扱う） |
| `evidence` | 根拠（参照した文書 ID・価格・Feature・時刻）。構造化 JSON |
| `human_review_status` | `UNREVIEWED` / `APPROVED` / `REJECTED` / `AMENDED` |
| `information_cutoff_at` | 判定で「当時利用可能だった」とみなした情報の上限。材料は `available_to_model_at <= information_cutoff_at` のみ |
| `input_sha256` | 判定入力のハッシュ |
| `objective_label_ref` | 前提にした Objective ラベル |
| `supersedes_label_id` | 再判定で置き換えた旧ラベル（上書きしない） |

### 3-2. Production ML 教師データへの採用

- 学習用データセットは版管理された `label_admission_policy` を通してのみ作る。
- 低 confidence の Interpretive ラベルを無条件で Production ML 教師データに入れない。
- ポリシーの内容は D-13b。データセットの manifest に `label_admission_policy_version` と採用/不採用件数を記録する。不採用ラベルも削除しない。

## 4. 見逃し（False Negative）の Research 判定

1. 対象: ENTRY されなかった Eligible 銘柄のうち、Objective で `hit_20` になったもの。
2. `information_cutoff_at` を上昇開始前（または上昇初期）に置く。定義は D-13a。
3. 判定者には `available_to_model_at <= information_cutoff_at` の情報だけを渡す。**backfill された情報（`available_to_model_at` が後日）は使わない。** 別扱いで使えるようにするかは D-23。
4. 出力: `ACTIONABLE_FALSE_NEGATIVE` / `OUT_OF_SCOPE_SHOCK` / `OUT_OF_SCOPE_LATE` と根拠。
5. `ACTIONABLE_FALSE_NEGATIVE` のみを見逃し学習に使う。事前の兆候がない突発急騰は `ACTIONABLE_FALSE_NEGATIVE` にしない。
6. 事象の種類（TOB など）だけで一律に除外しない。事前の兆候の有無で判定する（RF-02-C）。
