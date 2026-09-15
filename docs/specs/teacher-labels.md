# 教師ラベル仕様（Objective / Interpretive）

状態: **Phase 0.1 監査指摘 #11 による構造定義。個々のラベル基準は Phase 10 前に確定（D-13a / D-13b）** — 2026-09-15

## 1. 2層構造

| 層 | 決め方 | 版 | 真実として扱うか |
|---|---|---|---|
| **Objective** | 価格パスからコードで確定 | `label_version`（コード版） | データ誤りがなければ確定値 |
| **Interpretive** | AI の Research 判定（adjudication）。必要に応じ人手レビュー | `labeler_model_version` + `prompt_version` | **仮説。** 信頼度とレビュー状態付き |

## 2. Objective ラベル

価格パスだけで決まるもの。

| ラベル / 値 | 定義 |
|---|---|
| `path_resolution` | TARGET_FIRST / FAILURE_FIRST / AMBIGUOUS_PATH / UNRESOLVED_MISSING_DATA / NEITHER_BY_HORIZON（[lifecycle §7](entry-and-episode-lifecycle.md)） |
| `hit_10` / `hit_20` / `hit_30` | 各 horizon 内で高値が基準価格 ×1.10 / ×1.20 / ×1.30 以上 |
| `days_to_20` | 初めて +20% に達した取引日数 |
| `mfe` / `mae` | 最大含み益率 / 最大含み損率 |
| `failure_line_hit` | 失敗ラインに到達したか |
| `hit_20_before_failure` / `failure_before_20` | `path_resolution` から導出。AMBIGUOUS_PATH のときはどちらも `null` |

### 実装指示書 §29 のラベルのうち Objective 側に分類する候補（暫定、D-13a で確定）

| ラベル | 暫定定義 |
|---|---|
| `FAILED_BEFORE_TARGET` | `path_resolution = FAILURE_FIRST` |
| `FALSE_POSITIVE` | ENTRY Episode が `NEITHER_BY_HORIZON`（Target にも Failure にも届かず期限切れ）。※ FAILURE_FIRST を含めるかは D-13a |

## 3. Interpretive ラベル

「なぜそうなったか」「予測可能だったか」の判断を含むもの。

| ラベル | 前提となる Objective 条件（整合性制約） |
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

- 整合性制約に反する Interpretive ラベル（例: FAILURE_FIRST の Episode に `PREDICTIVE_SUCCESS`）は**保存時に拒否**する。
- `AMBIGUOUS_PATH` / `UNRESOLVED_MISSING_DATA` の Episode には、成功系・失敗系の Interpretive ラベルを付けない。
- 各ラベルの判定基準の詳細、および上の「前提条件」表そのものは D-13a で確定する（上表は暫定）。

### 3-1. 保存項目（必須）

| 列 | 内容 |
|---|---|
| `label` | ラベル |
| `labeler_model_version` | 判定に使ったモデル/ルールの版（LLM なら provider + model + prompt_version） |
| `confidence` | 判定の信頼度 |
| `evidence` | 根拠（参照した文書 ID・価格・Feature・時刻）。構造化 JSON |
| `human_review_status` | `UNREVIEWED` / `APPROVED` / `REJECTED` / `AMENDED` |
| `information_cutoff_at` | 判定で「当時取得可能だった」とみなした情報の時刻上限 |
| `input_sha256` | 判定入力のハッシュ |
| `objective_label_ref` | 前提にした Objective ラベル |
| `supersedes_label_id` | 再判定で置き換えた旧ラベル（上書きしない） |

- `confidence` は初期段階では**校正されていない値**として扱う（数値の大小を確率とみなさない）。

### 3-2. Production ML 教師データへの採用

- 学習用データセットは **`label_admission_policy`（版管理）** を通してのみ作る。
- 低 confidence の Interpretive ラベルを**無条件で Production ML 教師データに入れない**。
- ポリシーの内容（confidence の下限、人手レビュー必須のラベル種別、`UNREVIEWED` の扱い）は D-13b。
- データセットの manifest に `label_admission_policy_version` と採用/不採用件数を記録する。
- 不採用ラベルも削除せず Research 用に残す。

## 4. 見逃し（False Negative）の Research 判定

1. 対象: ENTRY されなかった Eligible 銘柄のうち、Objective で `hit_20` になったもの。
2. `information_cutoff_at` を、上昇開始前（または上昇初期）の時点に設定する。定義は D-13a。
3. 判定者（LLM / ルール / 人手）には `fetched_at` / `first_seen_at <= information_cutoff_at` の情報だけを渡す。
4. 出力: `ACTIONABLE_FALSE_NEGATIVE` / `OUT_OF_SCOPE_SHOCK` / `OUT_OF_SCOPE_LATE` のいずれかと根拠。
5. `ACTIONABLE_FALSE_NEGATIVE` のみを見逃し学習（Candidate Generation / Entry 層）に使う。事前の兆候がない突発急騰は `ACTIONABLE_FALSE_NEGATIVE` にしない。
6. 「TOB だから一律に対象外」のような事象種別による自動除外はしない。事前の兆候（異常出来高・報道など）の有無で判定する（[RF-02-C](regression-fixtures.md)）。
