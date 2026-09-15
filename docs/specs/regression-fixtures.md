# 投資ロジック Regression Test 仕様（fixture）

状態: **仕様のみ（Phase 0.1 監査指摘 #13）。コードは各 Phase で実装する。** — 2026-09-15

- すべて**合成データ**で作る（実銘柄・実ニュース本文を使わない）。
- 各 fixture は「期待する挙動」と「**禁止する挙動**」の両方を検査する。
- 規則を広げすぎないために、必要なものには**対照ケース（-C）**を置き、正当な挙動まで禁止していないことも検査する。
- 実行可能になる Phase を記す。それより前の Phase では `pending` として CI に登録し、仕様の存在を可視化する。

| ID | 監査 #13 の対応項目 | 実行可能 Phase |
|---|---|---|
| RF-01 | 500→1100→650、旧高値を上値余地にしない | 7（Reachable Zone 検証器）/ 3（Feature） |
| RF-02 | 前兆なし TOB +30% を ACTIONABLE_FALSE_NEGATIVE にしない | 10 |
| RF-03 | WATCH_BREAKOUT 到達だけで ENTRY にしない | 8 |
| RF-04 | 同一 Event の複数報道を独立材料として加点しない | 5 |
| RF-05 | EOD cutoff 後の材料を EOD 価格の未織り込み評価に使わない | 5 / 7 |
| RF-06 | 同一 Episode を複数成功として数えない | 8 / 9 |
| RF-07〜RF-16 | 追加（本ラウンドの他の指摘に対応） | 各行参照 |

---

## RF-01 急騰後崩壊銘柄の旧高値を上値余地にしない

**入力（JP、合成）**
| 期間 | 価格 | 出来高 | 材料 |
|---|---|---|---|
| D1–D20 | 終値 495–505 | 約10万株/日 | なし |
| D21 | 510 → 急伸開始 | 60万株 | M1（企業固有材料）`first_seen_at` = D21 12:00 |
| D22–D30 | 高値 1,100（D30） | 50–100万株/日 | なし |
| D31–D40 | 650 まで下落 | 30–60万株/日 | なし |
| D41–D60 | 630–670 のレンジ | 約12万株/日 | **新材料なし** |

**操作**: `data_cutoff` = D60 引けで Stage 1〜3 と Reachable Zone 生成を実行。

**期待**
- Reachable Zone の各境界に `anchor_type`（根拠の種類）が付く。`anchor_type` は現在の材料・需給・支持抵抗・出来高構造に由来する許可リストの値のみ。
- 1,100円付近や D22–D35 の高出来高価格帯は、`supply_overhang` 等の**需給悪化の根拠**としてのみ現れる。
- Feature `supply_overhang`（またはそれに相当する値）が 0 より大きい。

**禁止**
- `anchor_type = PRIOR_SURGE_HIGH` を上値側の境界に使う。
- 上値余地を `1100 / 650 − 1 ≒ +69%` と算出し、Reachable Zone・根拠・スコアのいずれかに使う。
- LLM 出力の根拠文に「旧高値まで戻る余地」を上値の理由として含む（出力検証器が拒否する）。

**対照（RF-01-C）**: 本 fixture は**判定結果が REJECT であることを要求しない**。現在の材料・需給から別の根拠で ENTRY と判断すること自体は許可する。検査対象は Reachable Zone の根拠である。

---

## RF-02 前兆なし TOB による急騰を ACTIONABLE_FALSE_NEGATIVE にしない

**入力（US、合成。日本株の値幅制限を避けるため米国株で作る）**
| 期間 | 内容 |
|---|---|
| D1–D40 | 終値 $15.00 ± 1%、出来高は20日平均 ±20% 以内、材料なし、Stage 1 非該当 |
| D40 18:00 ET | 買収（tender offer）発表。`first_seen_at` = D40 18:02 ET |
| D41 | 始値 $19.50（+30%）、以後 $19.40–19.60 |

**操作**: D41 以降の Objective 判定 → 見逃し Research 判定。

**期待**
- Objective: `hit_20 = true`。
- Interpretive: `OUT_OF_SCOPE_SHOCK`（または同等の「予測不能」ラベル）。
- Candidate Generation 学習用データに「選ぶべきだった正例」として入らない。

**禁止**
- `ACTIONABLE_FALSE_NEGATIVE` が付く。
- 判定入力に D40 18:02 ET 以降の情報を「上昇前に取得可能だった情報」として含める。

**対照（RF-02-C）**: 同じ価格パスで、D30–D39 に出来高が20日平均の4倍・価格が堅調、D35 に「買収検討」の報道（`first_seen_at` = D35 10:00 ET）がある場合、**自動で OUT_OF_SCOPE_SHOCK にしない**。Research 判定に回り、`ACTIONABLE_FALSE_NEGATIVE` が付くことを許可する（事象の種類だけで除外しないことの検査）。

---

## RF-03 WATCH_BREAKOUT 到達だけで ENTRY にしない

**入力（JP、合成）**
- D1 EOD: `WATCH_BREAKOUT`、終値 1,000円、トリガー条件「1,050円超え」
- D2 10:12 の1分足: 高値 1,052円 → `TRIGGER_HIT`

**RF-03a（再分析で ENTRY）**
- 10:20 に REANALYSIS を実行。出来高・VWAP・上ヒゲ等が良好 → ENTRY。
- 観測価格: 10:20 の `LAST_TRADE` = 1,068円（`entry_price_observed_at` = 10:20:00）
- **期待**: Prediction が1件作られ、`entry_reference_price = 1,068`、`threshold_20 = 1,281.6`。State Transition は `WATCH_BREAKOUT → TRIGGER_HIT → REANALYSIS → ENTRY`。
- **禁止**: `threshold_20` が 1,200（Watch 時の価格基準）や 1,260（トリガー価格基準）になる。

**RF-03b（再分析で REJECT）**
- 10:20 時点で長い上ヒゲ、出来高は平均以下 → REJECT（または FAILED_BREAKOUT）。
- **期待**: Prediction は0件。遷移は `TRIGGER_HIT → REANALYSIS → REJECT`。

**RF-03c（再分析を経ない挿入）**
- `TRIGGER_HIT` の後、REANALYSIS 分析を参照せずに Prediction を挿入しようとする。
- **期待**: DB 制約で挿入が拒否される。

---

## RF-04 同一 Event の複数報道を独立材料として加点しない

**入力（JP 銘柄 X、合成）**
| 文書 | ソース | published_at | first_seen_at | 内容 |
|---|---|---|---|---|
| d1 | 通信社（例: Reuters） | 10:02 | 10:03:10 | X社が Y社と資本提携へ |
| d2 | 新聞（例: 日経） | 10:15 | 10:16:05 | 同上 |
| d3 | 企業 IR（TDnet） | 13:00 | 13:00:40 | 資本業務提携に関するお知らせ |

**期待**
- `material_event` は **1件**。`first_seen_at = 10:03:10`。
- Event↔文書の役割: d1 = `DISCOVERY`、d3 = `VERIFICATION`、d2 = `COVERAGE`。
- 材料属性（新規性・インパクト等）は Event 単位で1回だけ評価される。
- 材料件数の Feature は 1。Material 候補への寄与も1件分。

**禁止**
- Event が3件になる、または材料スコアが3回加算される。
- IR（d3）が出たことで、Discovery を d3 に付け替える（情報源による固定序列の禁止）。

**対照（RF-04-C）**: 同じ日に X社の「大型受注」（10:30）と「自社株買い」（15:00）という**別の出来事**がある場合、Event は **2件**になる（統合しすぎないことの検査）。

---

## RF-05 EOD cutoff 後の材料を EOD 価格の未織り込み評価に使わない

**入力（JP 銘柄 Z、合成）**
- D10 の `price_cutoff_at` = 15:30 JST（引け）、EOD バッチ開始 = 17:00
| 材料 | published_at | first_seen_at |
|---|---|---|
| Ma | 14:05 | 14:10 |
| Mb | 15:00 | **15:45**（システムが取得したのは引け後） |
| Mc | 16:00 | 16:01 |

**期待**
- D10 の EOD 分析で、Ma のみが D10 引け値に対する織り込み評価の対象になる。
- Mb・Mc は `priced_in_status = UNKNOWN_UNTIL_NEXT_SESSION`。
- Mb・Mc を理由に `SETUP_EOD` を作ることは許可される。
- D11 の ENTRY 判断（例: `decision_cutoff_at` = 09:20）では Mb・Mc を入力に含め、09:20 までの価格反応で織り込みを評価する。

**禁止**
- D10 の EOD 分析で、Mb または Mc を D10 引け値に対して「未織り込み」と評価する。
- `published_at` が 15:30 より前であることを理由に、Mb を EOD の織り込み評価に含める（取得時刻で判定する）。

---

## RF-06 同一 Prediction Episode を複数成功として数えない

**入力（JP、合成）**
- D1 09:30 ENTRY（`entry_reference_price` = 1,000円、failure_line = 920円）→ Episode E1 OPEN
- D2〜D5 の各日、再分析が同じ仮説で ENTRY 相当と判断
- D8 に高値 1,210円（1,200円に到達）

**期待**
- Prediction: **1件**。Episode: **1件**。`REAFFIRMED` の State Transition: **4件**。
- Episode クローズ理由は `TARGET_HIT`。成功数の集計値は **1**。

**禁止**
- D2〜D5 に Prediction が作られる。
- 成功数が 2 以上になる。
- D2〜D5 の価格を基準に Target を再計算する。

**対照（RF-06-C）**: E1 クローズ後の D15 に、新しい材料による**別の仮説**で ENTRY と判断された場合、新しい Episode E2 が作られる（`thesis_key` の定義は D-17a。確定までは pending）。

---

## RF-07 signal_reference_price と entry_reference_price の分離

- D1 EOD `SETUP_EOD`（`signal_reference_price` = 1,000円）
- D2 09:15 ENTRY 判断で観測価格 1,060円 → ENTRY
- **期待**: `entry_reference_price = 1,060`、`threshold_20 = 1,272`、`signal_reference_price = 1,000` は別列に保存。
- **禁止**: `threshold_20 = 1,200`。
- 実行可能 Phase: 8

## RF-08 EOD 分析から Prediction を作らない

- EOD 分析（`analysis_kind = STAGE3_EOD`）が「ENTRY 相当」の内容を出力した場合、`SETUP_EOD` として保存される。
- **禁止**: `prod.predictions` への挿入（DB 制約で拒否）。
- 実行可能 Phase: 8

## RF-09 USD/JPY の時点同期

- 米国株 $19.90、`decision_cutoff_at` = 10:00:00 ET
- FX 観測: 150.40（09:59:30 ET）、150.90（10:00:30 ET）
- **期待**: 150.40 を使い、19.90 × 150.40 = 2,992.96円 → 3,000円以下。
- **禁止**: 150.90 を使う（19.90 × 150.90 = 3,002.91円 となり判定が変わる）。
- `decision_cutoff_at` 以前の FX が許容遅延内に存在しない場合は判定不能（`FX_MISSING`）。許容遅延は D-02a。
- 実行可能 Phase: 2（Universe）/ 8（ENTRY）

## RF-10 AMBIGUOUS_PATH

共通: `entry_reference_price` 1,000円、Target 1,200円、Failure 920円、D1 に ENTRY。
| ケース | D3 の足 | 分足 | 期待 |
|---|---|---|---|
| a | 高値 1,230 / 安値 910 | 10:31 の1本が 高値 1,205・安値 915、それより細かい足なし | `AMBIGUOUS_PATH`、成功・失敗のどちらにも数えない |
| b | 高値 1,230 / 安値 910 | 09:05 に安値 915、13:40 に高値 1,210 | `FAILURE_FIRST` |
| c | 始値 900 | — | `FAILURE_FIRST`（`resolution_granularity = DAY_OPEN`） |
| d | 高値 1,230 / 安値 910 | 当日分足が欠損 | `UNRESOLVED_MISSING_DATA` |
- **禁止**: ケース a・d を TARGET_FIRST / FAILURE_FIRST に分類する。
- 実行可能 Phase: 9

## RF-11 3,000円境界

- JP: 2,999円 → 含める、3,000円 → 含める、3,001円 → 除外（`PRICE_ABOVE_3000`）
- 判定は無調整価格。後日公表された分割係数を過去日の判定に使わない。
- 実行可能 Phase: 2

## RF-12 Universe 定義 v1.0.0

合成の銘柄マスタで以下を検査。
| 銘柄 | 期待 |
|---|---|
| JP プライム普通株 / スタンダード普通株 / グロース普通株 | 含める |
| JP TOKYO PRO Market（`Mkt = 0105`） | 除外 `TOKYO_PRO` |
| JP ETF / REIT | 除外 |
| US NYSE 普通株 / Nasdaq 普通株 / NYSE American 普通株 | 含める |
| US ETF（ETF フラグ Y） | 除外 `ETF` |
| US Preferred / Warrant / Unit / Right | 除外 |
| US OTC | 除外 `OTC` |
| US SIC 6770（BLANK CHECKS）の SPAC | 除外 `SPAC_PRE_MERGER` |
| US テスト銘柄（Test Issue = Y） | 除外 `TEST_ISSUE` |
| 種別不明 | 除外 `TYPE_UNKNOWN`、カバレッジに件数計上 |
| US ADR | **pending（D-10a）** |
| NYSE Arca にのみ上場する普通株 | **pending（D-10d）** |
- 実行可能 Phase: 1

## RF-13 Interpretive ラベルの採用ポリシーと整合性

- a: confidence がポリシー下限未満・`UNREVIEWED` のラベル → Production 学習データセットに入らない。manifest に不採用件数が記録される。
- b: `FAILURE_FIRST` の Episode に `PREDICTIVE_SUCCESS` を保存 → 拒否。
- c: `AMBIGUOUS_PATH` の Episode に成功系ラベル → 拒否。
- ポリシーの数値は D-13b。確定までは閾値をパラメータ化したテストにする。
- 実行可能 Phase: 10

## RF-14 プロンプト原文の改変検知

- `docs/prompts/MANIFEST.md` に登録済みのファイルを1バイト変更 → CI 失敗。
- 実行可能 Phase: 1（CI 整備時）

## RF-15 Research と Production の分離

- `run_mode = RESEARCH` のジョブが `prod.*` に書き込もうとする → 権限エラー（DB ロールで拒否）。
- Historical Replay の結果は `research.*` にのみ保存される。
- 実行可能 Phase: 1（ロール設計）/ 11

## RF-16 後日訂正データの as-of 読み取り

- D5 の日足を D7 に訂正版として再保存（新 manifest、`supersedes_manifest_id` 付き）。
- `as_of = D6` で読むと旧版、`as_of = D8` で読むと訂正版が返る。
- **禁止**: D6 時点の判断の再現に訂正版が使われる。
- 実行可能 Phase: 2
