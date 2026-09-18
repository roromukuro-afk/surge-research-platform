# Entry / Setup / Watch / Episode ライフサイクル仕様

状態: **v0.2.1（Phase 0.2 最終パッチ反映）** — 2026-09-15
根拠: [Phase 0.2 最終パッチ原文](../requirements/audit-2026-09-15-phase-0.2-final-patch.original.txt) > [Phase 0.2 監査原文](../requirements/audit-2026-09-15-phase-0.2.original.txt) > [Phase 0.1 監査原文](../requirements/audit-2026-09-15-phase-0.1.original.txt) > [実装指示書 v1.0 原文](../requirements/implementation-instructions-v1.0.original.txt) > v5.1
本書で「Claude Code 解釈（要確認）」と書いた箇所は、監査・ユーザー承認前の暫定仕様である。

> **2026-09-18 前提修正（[D-261](../unresolved-decisions.md)、ユーザー指示）**: Prediction は市場終了後に実行し、基準価格は対象セッションの**確定終値**（`signal_reference_price`、v5.1 の「分析基準価格」）。確定の条件は「セッション終了 + feed の遅延の後に取得したこと」（D-262）。本書の場中 ENTRY・`entry_reference_price`（判断後に取引可能だった価格）の規則は、将来の実売買検証（`execution_price` / `next_session_entry_price`、未実装）の設計として残す（名称は変えない、D-263）。EOD Prediction の規則は D-267 で確定し [eod-prediction.md](eod-prediction.md) に定めた（本書の場中の規則とは別の table `prod.eod_predictions`）。

---

## 1. 時刻・価格の定義

### 1-1. カットオフ

| 名前 | 定義 |
|---|---|
| `price_cutoff_at` | EOD 分析が使う価格・出来高データの上限（当該セッションの引け） |
| `decision_cutoff_at` | ENTRY 判断（再分析）が使うデータの上限。Prediction の `data_cutoff` はこれに等しい |
| `decision_completed_at` | ENTRY 判断の分析が完了した時刻（これ以降でなければ注文を出せない） |

### 1-2. 価格

| 名前 | 定義 | 用途 |
|---|---|---|
| `signal_reference_price` | セットアップ検出時点の参照価格（EOD なら引け値、無調整） | 記録のみ |
| `decision_price` / `decision_price_observed_at` | **AI が ENTRY 判断時に参照していた価格**と、その観測時刻（`<= decision_cutoff_at`） | 判断の再現、3,000円の再判定 |
| `entry_reference_price` / `entry_price_observed_at` | **ENTRY 判断後に、現実に取引可能だったとみなす評価価格**と、その観測時刻（`>= decision_completed_at`） | **成績・+20% Threshold の唯一の基準** |
| `entry_price_method` | `entry_reference_price` の算出方式（例: 判断完了後の最初の約定、判断完了後 N 分の VWAP、判断完了時の売気配）。**Provider の能力を確認してから D-01a で決める** | 監査・再現 |
| `actual_fill_price` / `actual_fill_at` | 実際の約定。自動売買の導入までは任意（null 可） | 将来 |

### 1-3. 通貨

| 用途 | JP | US |
|---|---|---|
| 3,000円 Eligibility（Universe・ENTRY 時の再判定） | 円建て株価 | USD 株価 × USD/JPY（`fx_observed_at <= 判定のカットオフ`） |
| +20% Threshold・Failure・MFE/MAE などの価格 Outcome | 円建て株価 | **USD 建て株価** |
| 補助 Outcome | — | 為替込みの JPY リターン（別列。Primary Outcome には使わない） |

### 1-4. 不変条件

- `target_price = entry_reference_price × 1.20`（銘柄の取引通貨建て）
- `decision_price_observed_at <= decision_cutoff_at <= decision_completed_at <= entry_price_observed_at`
- 米国株の円換算: `fx_observed_at <= decision_cutoff_at`（ENTRY 時）、`fx_observed_at <= price_cutoff_at`（EOD の Universe 判定時）
- 分析に使う材料: `available_to_model_at <= decision_cutoff_at`（EOD 分析では `<= price_cutoff_at`。§3）
- `signal_reference_price` と `decision_price` は、Threshold の計算に使わない
- Prediction が作られるのは `decision_price_jpy <= 3000` かつ `entry_reference_price_jpy <= 3000` の場合のみ（§6）

## 2. 判定状態

| 状態 | 作成する分析 | 意味 | Prediction か |
|---|---|---|---|
| `TECHNICAL_SETUP_EOD` | EOD 分析 | 引けまでのチャート・価格・出来高（と、引けまでに利用可能だった材料）で形成されたセットアップ | いいえ |
| `POST_CLOSE_CATALYST_SETUP` | 引け後の材料分析 | 引け後に利用可能になった新規材料によって、翌セッションの監視対象になったもの | いいえ |
| `WATCH_BREAKOUT` / `WATCH_PULLBACK` / `WATCH_OTHER` | EOD 分析・場中分析 | 条件付きの監視 | いいえ |
| `REJECT` | すべて | — | いいえ |
| `ENTRY` | **場中の ENTRY 判断分析のみ**（`ENTRY_DECISION` / `REANALYSIS`） | 現在価格から Entry 可能 | **はい** |

- EOD 分析・引け後の材料分析は `ENTRY` を出力しない。
- 同じ銘柄が `TECHNICAL_SETUP_EOD` と `POST_CLOSE_CATALYST_SETUP` の両方に該当する場合は、**別々のレコードとして保存**し、翌セッションの ENTRY 判断は両方を入力として参照する（Prediction の `source_setup_ids` に両方を記録）。
- DB 制約: `prod.predictions.analysis_id` は `analysis_kind IN ('ENTRY_DECISION','REANALYSIS')` の分析だけを参照できる。

## 3. 材料の利用可能時刻と Setup の分離

### 3-1. 材料の時刻（すべての文書に保存）

| 列 | 定義 |
|---|---|
| `source_published_at` | ソースが記載している公開時刻 |
| `system_first_seen_at` | 本システムのコレクタがその文書を初めて観測した時刻 |
| `ingested_at` | DB に取り込まれた時刻 |
| `available_to_model_at` | ノイズ判定・Event 統合・銘柄紐付けなど、分析に使える状態になった時刻（`>= ingested_at >= system_first_seen_at`） |

- **Production と Historical Replay は `available_to_model_at <= decision_cutoff_at` の情報だけを使う。** `source_published_at` を利用可能時刻の代わりに使わない。
- **後から過去のニュースを backfill した場合**、その文書の `system_first_seen_at` / `ingested_at` / `available_to_model_at` は backfill を実行した時刻になる。過去の Prediction・Replay がその情報を知っていたことにはならない（[RF-17](regression-fixtures.md)）。
- `material_events` は `market_first_published_at`（所属文書の `source_published_at` の最小値、市場が認識し始めた時刻の推定）と `system_first_seen_at`（所属文書の最小値）を別々に持つ。分析で使えるかどうかは `available_to_model_at` で判定する。

### 3-2. Setup の分離（監査 Phase 0.2 #5）

| | `TECHNICAL_SETUP_EOD` | `POST_CLOSE_CATALYST_SETUP` |
|---|---|---|
| 入力できる価格 | `<= price_cutoff_at` | `<= price_cutoff_at`（参考のみ） |
| 入力できる材料 | `available_to_model_at <= price_cutoff_at` | 引け後に利用可能になった材料（`price_cutoff_at < available_to_model_at <= 分析のカットオフ`）を含む |
| 引け値に対する「未織り込み」評価 | 可（引けまでに利用可能だった材料のみ） | **しない**（`priced_in_status = NOT_EVALUATED_AGAINST_EOD`） |
| 織り込み度の評価 | — | 翌セッションの ENTRY 判断で、`decision_cutoff_at` までの価格反応を使って行う |

## 4. フロー

```
[引けまで]   Universe → Stage1 (Technical ∪ Material) → Stage2（分足）→ EOD 分析
                                                            ├─ TECHNICAL_SETUP_EOD ─┐
                                                            ├─ WATCH_* ───────┐     │
                                                            └─ REJECT         │     │
[引け後〜翌寄り前] 新規材料（available_to_model_at > price_cutoff_at）→ 材料分析        │     │
                                                            ├─ POST_CLOSE_CATALYST_SETUP ─┤
                                                            └─（該当なし）      │     │
                                                                              ▼     ▼
[場中]                                          watch_monitor（分足）   entry_decision（次の取引可能時点）
                                                  │ 条件到達                     │
                                                  ▼                              │
                                               TRIGGER_HIT → REANALYSIS ────────▶ 判断
                                                                                 ├─ 3,000円再判定（§6）で超過 → REJECT
                                                                                 ├─ ENTRY → decision_price 記録 → entry_reference_price 観測
                                                                                 │          → entry_reference_price で3,000円再確認
                                                                                 │              ├─ 3,000円以下 → Prediction → Episode OPEN
                                                                                 │              └─ 超過 → ENTRY_ABORTED_PRICE_LIMIT（研究ログのみ）
                                                                                 ├─ WATCH_* / REJECT / FAILED_BREAKOUT
                                                                                 └─ EXPIRED（D-20）
```

- Watch 条件に到達しても `TRIGGER_HIT` だけでは ENTRY にならない。必ず `REANALYSIS` を経る（DB 制約で保証）。
- 「次の取引可能時点」の定義は D-01b。

## 5. 分足・約定データの取得範囲

| 対象 | 日足 | 分足 | 約定（trade/tick） |
|---|---|---|---|
| 全 Universe | ○ | × | × |
| Stage 2 候補 | ○ | ○（期間は D-08a） | × |
| Setup / Watch 銘柄 | ○ | ○（場中監視） | × |
| ENTRY 判断対象 | ○ | ○ | 利用可能なら（`entry_reference_price` の算出方式による） |
| Open Episode | ○ | ○ | パス解決で最小足が曖昧になった時間帯のみ、利用可能なら |

全銘柄の分足・約定の保存を前提にしない。

**日本株の場中データの注意**: J-Quants の分足・ティックは日次更新でリアルタイム配信ではない（公式ヘルプ）。**場中の ENTRY 判断・Watch 監視には使わない。** 用途は Historical research / EOD / Replay / Teacher data（パス解決を含む）に限る。場中用の Provider は別に選ぶ（D-06b）。

## 6. 3,000円 Hard Filter の ENTRY 時再判定（D-18・D-26 確定）

- Universe（EOD）: 引け値（無調整）で判定。米国株は `fx_observed_at <= price_cutoff_at` の USD/JPY で円換算。
- **ENTRY 判断時: `decision_price`（米国株は `fx_observed_at <= decision_cutoff_at` の USD/JPY で円換算）で再判定する。3,000円を超えていれば正式 Prediction を作らない**（`REJECT`、理由 `HARD_FILTER_AT_ENTRY`）。Setup/Watch 時点で3,000円以下だったかは関係ない。
- **`entry_reference_price` でも再確認する**（監査 0.2 最終パッチ #4）。`decision_price` が3,000円以下で ENTRY と判断しても、`entry_reference_price`（米国株は `entry_price_observed_at` 以前の USD/JPY で円換算）が3,000円を超えた場合は、**正式な Prediction・Episode を作らない。**
  - `prod.entry_attempts` に `status = ENTRY_ABORTED_PRICE_LIMIT` として研究ログを残す（decision / entry の両価格、観測時刻、FX を含む）。
  - 正式な +20% Threshold・Outcome・Prediction 成績には含めない。
  - 後に再び3,000円以下になった場合、自動で復活させない。その時点で再分析し、新しい ENTRY 判断として扱う。
- ENTRY 判断は、Prediction を作ったかどうかにかかわらず `prod.entry_attempts` に1件ずつ記録する（`PREDICTION_CREATED` / `ENTRY_ABORTED_PRICE_LIMIT` / その他）。`entry_reference_price` を観測できなかった場合の扱いは D-31。

## 7. Prediction Episode

### 7-1. 定義

- 同一銘柄・同一仮説（`thesis_key`、D-17a）について、初回 ENTRY から Target / Failure / Thesis invalidation / Horizon end までを 1 Episode とする。成績は Episode 単位で数える。

### 7-2. Horizon（監査 Phase 0.2 #6）

- **Primary prediction horizon = ENTRY 成立時点（`entry_price_observed_at`）から 20 trading sessions。**
- WATCH・Setup の開始日から数えない。
- `REAFFIRMED` などの State Update では**リセットしない**。
- 新しい Episode が正式に開始されたときだけ、新しい Horizon を持つ。
- **表記（D-17d 確定、監査 0.2 最終パッチ #5）**:
  - **S0 = ENTRY 成立セッション。** ENTRY 時刻（`entry_price_observed_at`）以降の S0 の値動きも Outcome に含める。
  - **S1 = ENTRY の翌取引セッション。** 以後 S2, S3, … と取引セッションだけを数える（休場日は数えない）。
  - **Primary Horizon は S20 の close まで。**
  - 指示書 §28 の 1D/3D/5D/10D/20D は S1/S3/S5/S10/S20 の close に対応する。

### 7-3. Failure Line の二層（監査 Phase 0.2 #7）

| 列 | 置き場所 | 変更 | 用途 |
|---|---|---|---|
| `initial_failure_line` | `prod.predictions` | **Prediction 作成時に固定。変更禁止** | Primary Outcome・Teacher Label・Episode の `INITIAL_FAILURE_HIT` |
| `current_risk_line` | `prod.risk_line_updates`（append-only、State Transition と対応） | 再分析で変更可（初期値 = `initial_failure_line`） | 運用上のリスク管理の研究 |

- `current_risk_line` に触れても Episode はクローズしない（`RISK_LINE_HIT` の State Transition として記録）。
- `current_risk_line` のパス解決は研究用の別レコードとして計算する。

### 7-4. クローズ

`close_reason`: `TARGET_HIT` / `INITIAL_FAILURE_HIT` / `THESIS_INVALIDATED` / `HORIZON_EXPIRED`（S20 close）/ `AMBIGUOUS_PATH` / `UNRESOLVED_MISSING_DATA` / `CORPORATE_ACTION_SUSPECTED`（未確定のため保留）

### 7-4-1. Outcome の二層（D-17e 確定、監査 0.2 最終パッチ #2）

| 層 | 名前 | 範囲 | 用途 |
|---|---|---|---|
| 正式 | `primary_episode_outcome` | ENTRY から **Episode の終了まで**（TARGET_HIT / INITIAL_FAILURE_HIT / THESIS_INVALIDATED / HORIZON_EXPIRED 等） | Prediction 評価・成績・Teacher Label |
| 研究 | `counterfactual_horizon_outcome` | ENTRY から**当初の S20 close まで**（Episode の終了に関係なく追跡） | counterfactual MFE / MAE、later target hit など |

- **`THESIS_INVALIDATED` 後に株価が +20% に到達しても、Primary の成功に戻さない。** その事実は `counterfactual_horizon_outcome.later_target_hit` にだけ記録する。
- `TARGET_HIT` / `INITIAL_FAILURE_HIT` / `HORIZON_EXPIRED` で終わった Episode は、両層の到達判定が一致する（counterfactual 側は S20 まで MFE/MAE を追い続ける）。
- counterfactual の値を Primary の成績・Production ML の教師データの正解として使わない。研究で使う場合は、そうと分かる列名・データセットに分ける。

### 7-5. Episode 中の再評価

- Open Episode がある銘柄・同一 `thesis_key` で再分析が ENTRY 相当と判断しても、新しい Prediction は作らず `REAFFIRMED` として記録する。
- 別仮説・クローズ後の再 ENTRY の扱いは D-17b。

## 8. Outcome の価格系列（監査 Phase 0.2 #8・#10）

### 8-1. 保存と比較可能な系列

- **raw（実際に取引された無調整価格）を保存する。** 上書きしない。
- Outcome エンジンは、ENTRY 時点の株数ベースに揃えた**経済的に比較可能な価格系列**（comparable path）を使う。
  - 分割比率 `r` = 旧1株あたりの新株数（2-for-1 分割なら `r = 2`、1-for-10 併合なら `r = 0.1`）
  - ENTRY から対象の足までに権利落ちした分割・併合の `r` の積を `R` とすると、`comparable_price = raw_price × R`
  - Target・`initial_failure_line` は ENTRY 時点の株数ベースのまま変えない
- 適用した corporate action の ID を Outcome に記録する。
- 記録された corporate action がないのに、分割・併合と整合する価格の不連続がある場合は `CORPORATE_ACTION_SUSPECTED` とし、Outcome を確定しない。
- **配当は「株価 +20%」の Target に加算しない。** 配当込みのリターンは必要なら補助 Outcome とする。
- 分割・併合以外（スピンオフ、株主割当、合併・TOB による上場廃止など）の扱いは D-24。

### 8-2. 米国株

- Primary Outcome は USD 建ての comparable path。
- 補助 Outcome: `jpy_return = (comparable_price × fx_at_evaluation) / (entry_reference_price × fx_at_entry) − 1`。FX の観測時刻は各価格の観測時刻以前とする。

## 9. パス解決（監査 Phase 0.2 #11）

対象: `target_price` と `initial_failure_line`（研究用に `current_risk_line` でも同じ手順を別に実行）。すべて comparable path で判定する。

**ENTRY セッション（S0）**: `entry_price_observed_at` より後のデータだけを使う。約定データがあればそれを、なければ `entry_price_observed_at` 以降に始まる分足から判定する（ENTRY より前の値動きを含む足の部分は使わない）。

**S1 以降の各セッション**を時系列順に次の手順で判定する。

| 手順 | 条件 | 結果 | `resolution_granularity` |
|---|---|---|---|
| 1 | 始値が Target 以上 | `TARGET_FIRST` | `SESSION_OPEN` |
| 1 | 始値が Failure 以下 | `FAILURE_FIRST` | `SESSION_OPEN` |
| 2 | 日足の高値 ≥ Target かつ 安値 ≤ Failure | 手順 3 へ | — |
| 2 | どちらか一方のみ | その側 | `DAY` |
| 2 | どちらにも触れない | 次のセッション | — |
| 3 | 利用可能な最も細かい分足を時系列に見る。各足で、始値が既に一方を跨いでいればその側、足の中で一方だけに触れればその側 | 確定 | `INTRADAY_BAR` |
| 3 | 1本の最小足の中で両方に触れた | 手順 4 へ | — |
| 3 | 必要な分足が欠損している | `UNRESOLVED_MISSING_DATA` | — |
| 4 | その足の時間帯の約定データを時系列に見て、最初に一方の価格に達した約定の側 | 確定 | `TRADE` |
| 4 | 同一時刻の約定で両方に達し、順序が分からない | `AMBIGUOUS_PATH` | `TRADE` |
| 4 | 市場・Provider の構成上、約定データを提供していない | `AMBIGUOUS_PATH` | `INTRADAY_BAR` |
| 4 | 約定データは通常提供されているが、その時間帯が欠損している | `UNRESOLVED_MISSING_DATA` | — |
| 5 | S20 の引けまでどちらにも触れない | `NEITHER_BY_HORIZON` | — |

- 手順 3・4 の区別（D-09b 確定、監査 0.2 最終パッチ #6）: **より細かいデータが仕様上存在しないため順序不明 → `AMBIGUOUS_PATH`**。**本来利用できるはずの細かいデータが欠損 → `UNRESOLVED_MISSING_DATA`**。
- Primary（`primary_episode_outcome`）のパス解決は Episode の終了時点で止める。counterfactual は S20 close まで同じ手順で続ける（§7-4-1）。
- `AMBIGUOUS_PATH` / `UNRESOLVED_MISSING_DATA` を成功・失敗のどちらにも寄せない。件数は別に集計する。

保存: `path_resolution`、`resolution_granularity`、`resolved_session_index`、`resolved_at_ts`、`corporate_action_ids_applied`、`label_version`。
