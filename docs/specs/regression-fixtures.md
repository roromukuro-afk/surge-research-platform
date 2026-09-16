# 投資ロジック Regression Test 仕様（fixture）

状態: **v0.2.1（Phase 0.2 最終パッチ反映）。仕様のみ。コードは各 Phase で実装する。** — 2026-09-15

- すべて**合成データ**で作る（実銘柄・実ニュース本文を使わない）。
- 各 fixture は「期待する挙動」と「**禁止する挙動**」の両方を検査する。
- 規則を広げすぎていないことを確かめる**対照ケース（-C）**を置く。
- 実行可能になる Phase より前は `pending` として CI に登録し、削除しない。
- 価格の数値は、特記がなければ comparable path（[lifecycle §8](entry-and-episode-lifecycle.md)）で表す。
- `entry_price_method`（D-01a）が未確定の間は、`entry_reference_price` を fixture の入力値として与える。

Security Master の同一性・履歴・as-of・権限にかかわる fixture（Phase 1.1 監査の A〜G・I と Phase 1.1a の 1.1a-1〜8）は、投資ロジックの回帰とは別系統として [security-identity.md](security-identity.md) §7 に一覧があり、`workers/tests/test_identity_resolution.py` / `test_db_master_semantics.py` / `test_db_privileges_and_names.py` / `test_snapshot_width.py` で実装済み。

## 一覧

| ID | 内容 | 由来 | 実行可能 Phase | v0.2 での変更 |
|---|---|---|---|---|
| RF-01 | 急騰後崩壊銘柄の旧高値を上昇根拠にしない／障害として使う | 0.1 #13, **0.2 #1** | 3 / 7 | **全面修正** |
| RF-02 | 前兆なし TOB を ACTIONABLE_FALSE_NEGATIVE にしない | 0.1 #13 | 10 | 利用可能時刻の列名を更新 |
| RF-03 | WATCH_BREAKOUT 到達だけで ENTRY にしない | 0.1 #13 | 8 | **decision / entry 価格を分離** |
| RF-04 | 同一 Event の複数報道を独立材料として加点しない | 0.1 #13 | 5 | 時刻の列名を更新 |
| RF-05 | 引け後の材料を EOD 価格の未織り込み評価に使わない／Setup の分離 | 0.1 #13, **0.2 #5** | 5 / 7 | **全面修正** |
| RF-06 | 同一 Episode を複数成功として数えない | 0.1 #13 | 8 / 9 | 軽微 |
| RF-07 | signal / decision / entry 価格の分離 | 0.1 #6, **0.2 #3** | 8 | **修正** |
| RF-08 | EOD・引け後の分析から Prediction を作らない | 0.1 #6, 0.2 #5 | 8 | 引け後分析を追加 |
| RF-09 | USD/JPY の時点同期 | 0.1 #7 | 2 / 8 | ENTRY 時は decision_price を使う |
| RF-10 | パス解決と AMBIGUOUS_PATH | 0.1 #9, **0.2 #11** | 9 | **全面修正（約定データの段階を追加）** |
| RF-11 | 3,000円境界 | 指示書 §2 | 2 | 変更なし |
| RF-12 | Universe 定義 v1.0.0 | 0.1 #10 | 1 | 変更なし |
| RF-13 | Interpretive ラベルの採用ポリシーと整合性 | 0.1 #11 | 10 | initial_failure_line を明記 |
| RF-14 | 原文の改変検知／v5.1 と addenda を混ぜない | 0.1 #2, **0.2 #12** | 1 / 7 | **RF-14b を追加** |
| RF-15 | Research と Production の分離 | 指示書 §36 | 1 / 11 | 変更なし |
| RF-16 | 後日訂正データの as-of 読み取り | 0.1 #3 | 2 | 変更なし |
| **RF-17** | ニュース backfill で過去の判断が情報を知っていたことにしない | **0.2 #9** | 4 / 5 | **新規** |
| **RF-18** | 株式分割・併合・配当で Outcome を誤判定しない | **0.2 #10** | 9 | **新規** |
| **RF-19** | ENTRY 時の3,000円再判定 | **0.2 #4** | 8 | **新規** |
| **RF-20** | Horizon は ENTRY から20セッション、リセットしない | **0.2 #6** | 8 / 9 | **新規** |
| **RF-21** | Failure Line の二層 | **0.2 #7** | 8 / 9 | **新規** |
| **RF-22** | 米国株の Outcome は USD 建て | **0.2 #8** | 9 | **新規** |
| **RF-23** | False Negative の3分類（ACTIONABLE / PIPELINE_MISSED / OUT_OF_SCOPE_SHOCK） | **0.2 最終 #3** | 10 | **新規（最終パッチ）** |
| **RF-24** | THESIS_INVALIDATED 後の Outcome 二層 | **0.2 最終 #2** | 9 / 10 | **新規（最終パッチ）** |

最終パッチでの変更: RF-01（DB 制約・単調性の緩和、RF-01-M の差し替え、RF-01-C2 追加）、RF-02（3分類、RF-02-P 追加）、RF-10（D-09b 確定）、RF-17（見逃し分類との関係）、RF-19（ケース d を ENTRY_ABORTED_PRICE_LIMIT に変更、ケース e・f 追加）、RF-20（S0 表記確定、ケース c 追加）、RF-21（close_reason 名）

---

## RF-01 旧高値を上昇根拠にしない／障害としては使う

**考え方（Phase 0.2 #1）**: 過去高値を**参照すること自体は禁止しない**。禁止するのは「過去高値まで戻ること」を上昇の根拠・Potential Upside にすること。Resistance・Supply Overhang・戻り売り候補・高値掴み保有者の存在・Reachable Zone までの障害としては、**積極的に**使う。

**入力（JP、合成）**
| 期間 | 価格 | 出来高 | 材料 |
|---|---|---|---|
| D1–D20 | 終値 495–505 | 約10万株/日 | なし |
| D21 | 510 → 急伸開始 | 60万株 | M1（企業固有材料） |
| D22–D30 | 高値 1,100（D30） | 50–100万株/日 | なし |
| D31–D40 | 650 まで下落 | 30–60万株/日 | なし |
| D41–D60 | 630–670 のレンジ | 約12万株/日 | **新材料なし** |

**操作**: `price_cutoff_at` = D60 引けで Feature・Stage 1〜3・Reachable Zone・価格障害の生成を実行。

**期待**
- 1,100円付近と D22–D35 の高出来高価格帯が `price_obstacles` に記録される（`obstacle_type` = `PRIOR_SURGE_HIGH` / `VOLUME_SHELF` 等、`role` = `RESISTANCE` / `SUPPLY_OVERHANG` / `HISTORICAL_OBSTACLE` / `TRAPPED_HOLDERS`、`status` = `ACTIVE` 等）。
- Feature `supply_overhang`（またはそれに相当する値）が 0 より大きい。
- Reachable Zone の上側境界は、現在の材料・需給・支持抵抗・出来高構造から導かれる。分析結果に「障害までの距離」と、障害をどう評価したか（有効 / 弱まった / 失効、と根拠）が記録される。
- 過去高値が存在するだけで上側境界を機械的に引き下げる DB 制約はない（最終パッチ #1）。障害の効き方は分析で評価する。

**禁止**
- `obstacle_type = PRIOR_SURGE_HIGH` を `role = BULLISH_BASIS`（上昇根拠）として保存する（出力検証器・DB 制約で拒否）。
- Potential Upside を `1100 / 650 − 1 ≒ +69%` と算出し、Reachable Zone・根拠・スコアのいずれかに使う。
- 根拠文で「旧高値まで戻る余地」を上昇の理由にする。

**RF-01-M（過去高値が遠いほど上昇余地を増やさない）**: D41–D60 の価格・出来高・材料が同一で、D21–D40 の急騰高値の水準だけが異なる3系列を作る（急騰の形は同じ比率で伸縮、崩壊後は 630–670 のレンジ）。
| 系列 | 過去急騰高値 | 現在値（約650）からの距離 |
|---|---|---|
| A1 | 900円 | 約 +38% |
| A2 | 1,100円 | 約 +69% |
| A3 | 1,500円 | 約 +131% |
- **期待**: 上昇余地を示す出力（Reachable Zone 上側境界、上昇余地のスコア、Potential Upside 相当の値）は、`A3 ≤ A2 ≤ A1`（等しくてもよい）。**過去高値が遠いほど上昇余地が増えてはならない。**
- **要求しないこと**: 「過去高値が存在すれば必ず Reachable Zone が低下する」（急騰のない系列と比べて低いこと）は要求しない。

**RF-01-C（対照・障害として使うこと）**: 系列 A2 で `price_obstacles` に 1,100円付近の障害が**何も記録されない**、または `supply_overhang` が 0 の場合は**失敗**とする（過去高値を無視しないことの検査）。

**RF-01-C2（対照・障害の意味が弱まる場合）**: A2 の D55–D60 に新しい強材料（`available_to_model_at` = D55 10:00）が出て、出来高が20日平均の5倍、価格が 1,100円を上回って3セッション引けた場合、
- 1,100円付近の障害は記録されたうえで、`status = WEAKENED` または `INVALIDATED`（根拠付き）と評価されてよい。
- Reachable Zone 上側境界が 1,100円を下回らないことを理由に、テストを失敗にしない（機械的な引き下げを要求しない）。
- ただしこの場合も、上昇根拠は新材料・出来高・価格受容であり、「1,100円まで戻る」ことを根拠にしてはならない（RF-01 の禁止事項は引き続き有効）。

なお本 fixture 群は判定結果が `REJECT` であることを要求しない。


---

## RF-02 前兆なし TOB による急騰を ACTIONABLE_FALSE_NEGATIVE にしない

**入力（US、合成）**
| 期間 | 内容 |
|---|---|
| D1–D40 | 終値 $15.00 ± 1%、出来高は20日平均 ±20% 以内、材料なし、Stage 1 非該当 |
| D40 18:00 ET | 買収（tender offer）発表。`system_first_seen_at` = 18:02 ET、`available_to_model_at` = 18:05 ET |
| D41 | 始値 $19.50（+30%）、以後 $19.40–19.60 |

**期待**: Objective で `hit_20 = true`。Interpretive は `OUT_OF_SCOPE_SHOCK`。Prediction Engine の False Negative に数えず、Candidate Generation の学習データに「選ぶべきだった正例」として入らない。Pipeline 改善用データにも入らない（市場にも事前の情報がなかったため）。
**禁止**: `ACTIONABLE_FALSE_NEGATIVE` が付く。上昇前の判定入力に `available_to_model_at` が D40 18:05 ET 以降の情報を含める。
**RF-02-C（対照）**: D30–D39 に出来高が20日平均の4倍で価格が堅調、D35 10:00 ET に「買収検討」の報道（`available_to_model_at` = 10:04 ET）がある場合は、自動で `OUT_OF_SCOPE_SHOCK` にしない。Research 判定に回り、`ACTIONABLE_FALSE_NEGATIVE` が付くことを許可する。

**RF-02-P（対照・Pipeline の取りこぼし）**: RF-02-C と同じ報道（`source_published_at` = D35 10:00 ET）が、コレクタ障害のため `available_to_model_at` = D42 09:00 ET（急騰後）になった場合、`ACTIONABLE_FALSE_NEGATIVE` ではなく `PIPELINE_MISSED_ACTIONABLE_SIGNAL` とする（RF-23）。

---

## RF-03 WATCH_BREAKOUT 到達だけで ENTRY にしない

**入力（JP）**: D1 EOD に `WATCH_BREAKOUT`（終値 1,000円、条件「1,050円超え」）。D2 10:12 の1分足の高値 1,052円 → `TRIGGER_HIT`。

**RF-03a（再分析で ENTRY）**
| 項目 | 値 |
|---|---|
| `decision_cutoff_at` | 10:20:00 |
| `decision_price` / `decision_price_observed_at` | 1,068円 / 10:19:58 |
| `decision_completed_at` | 10:20:40 |
| `entry_reference_price` / `entry_price_observed_at` | 1,072円 / 10:21:00（fixture 入力値） |
- **期待**: Prediction 1件、`target_price = 1,286.4`、遷移 `WATCH_BREAKOUT → TRIGGER_HIT → REANALYSIS → ENTRY`。
- **禁止**: Target が 1,200（Watch 時の価格）、1,260（トリガー価格）、**1,281.6（`decision_price` 基準）**のいずれかになる。`entry_price_observed_at < decision_completed_at` の値を保存する（CHECK 制約で拒否）。

**RF-03b（再分析で REJECT）**: 10:20 時点で長い上ヒゲ、出来高が平均以下 → REJECT（または FAILED_BREAKOUT）。Prediction 0件。
**RF-03c**: REANALYSIS を参照しない Prediction の挿入 → DB 制約で拒否。

---

## RF-04 同一 Event の複数報道を独立材料として加点しない

**入力（JP 銘柄 X）**
| 文書 | ソース | source_published_at | system_first_seen_at | available_to_model_at | 内容 |
|---|---|---|---|---|---|
| d1 | 通信社 | 10:02 | 10:03:10 | 10:03:40 | X社が Y社と資本提携へ |
| d2 | 新聞 | 10:15 | 10:16:05 | 10:16:30 | 同上 |
| d3 | 企業 IR（TDnet） | 13:00 | 13:00:40 | 13:01:00 | 資本業務提携に関するお知らせ |

**期待**: `material_event` は1件。`market_first_published_at = 10:02`、`system_first_seen_at = 10:03:10`。d1 = `DISCOVERY`、d3 = `VERIFICATION`、d2 = `COVERAGE`。材料属性は Event 単位で1回だけ評価。材料件数の Feature は1。
**禁止**: Event が3件になる、スコアが3回加算される、IR が出たことで Discovery を d3 に付け替える。
**RF-04-C（対照）**: 同じ日の「大型受注」（10:30）と「自社株買い」（15:00）は別の Event（2件）になる。

---

## RF-05 引け後の材料を EOD 価格の未織り込み評価に使わない／Setup を分離する

**入力（JP 銘柄 Z）**: D10 の `price_cutoff_at` = 15:30 JST
| 材料 | source_published_at | system_first_seen_at | available_to_model_at |
|---|---|---|---|
| Ma | 14:05 | 14:10 | 14:12 |
| Mb | 15:00 | **15:45** | 15:47 |
| Mc | 16:00 | 16:01 | 16:03 |

Z は D10 引けまでのチャートでもセットアップ条件を満たしている。

**期待**
- D10 の EOD 分析（`TECHNICAL_SETUP_EOD` の判定）の入力は Ma のみ。引け値に対する織り込み評価の対象も Ma のみ。
- Mb・Mc による引け後の材料分析で `POST_CLOSE_CATALYST_SETUP` が作られ、`priced_in_status = NOT_EVALUATED_AGAINST_EOD`。
- Z には `TECHNICAL_SETUP_EOD` と `POST_CLOSE_CATALYST_SETUP` の**2件が別レコード**で保存される。
- D11 の ENTRY 判断（`decision_cutoff_at` = 09:20）は両方の Setup と Mb・Mc を入力に含み、09:20 までの価格反応で織り込み度を評価する。Prediction の `source_setup_ids` に2件とも記録される。

**禁止**
- Mb または Mc が `TECHNICAL_SETUP_EOD` の分析入力に含まれる。
- `source_published_at` が 15:30 より前であることを理由に、Mb を EOD の織り込み評価に含める。
- 引け後の材料で作られた Setup が `TECHNICAL_SETUP_EOD` として保存される。
- D10 引け値に対して Mb・Mc を「未織り込み」と評価する。

**RF-05-C（対照）**: チャート条件を満たさず、Mc だけがある銘柄 W → `POST_CLOSE_CATALYST_SETUP` のみが作られ、チャート条件を満たさないことを理由に落とされない（指示書 §16）。

---

## RF-06 同一 Prediction Episode を複数成功として数えない

**入力（JP）**: S0 に ENTRY（`entry_reference_price` = 1,000円、`initial_failure_line` = 920円）→ Episode E1。S1〜S4 の各セッションで再分析が同じ仮説で ENTRY 相当と判断。S7 に高値 1,210円。
**期待**: Prediction 1件、Episode 1件、`REAFFIRMED` 4件。クローズ理由 `TARGET_HIT`。成功数の集計は1。
**禁止**: S1〜S4 に Prediction が作られる。成功数が2以上。S1〜S4 の価格で Target を再計算する。
**RF-06-C（対照）**: E1 クローズ後に別の仮説で ENTRY → 新しい Episode E2（`thesis_key` の定義 D-17a が決まるまで pending）。

---

## RF-07 signal / decision / entry 価格の分離

| 項目 | 値 |
|---|---|
| D1 EOD `TECHNICAL_SETUP_EOD` の `signal_reference_price` | 1,000円 |
| D2 09:15 `decision_price` | 1,060円 |
| `entry_reference_price` | 1,063円 |
- **期待**: `target_price = 1,275.6`。3つの価格が別々の列に保存される。
- **禁止**: Target が 1,200（signal 基準）または 1,272（decision 基準）。

## RF-08 EOD・引け後の分析から Prediction を作らない

- `analysis_kind = STAGE3_EOD` または `POST_CLOSE_MATERIAL` の分析が「ENTRY 相当」の内容を出力した場合、Setup として保存される。
- **禁止**: `prod.predictions` への挿入（DB 制約で拒否）。

## RF-09 USD/JPY の時点同期

- 米国株、`decision_cutoff_at` = 10:00:00 ET、`decision_price` = $19.90
- FX 観測: 150.40（09:59:30 ET）、150.90（10:00:30 ET）
- **期待**: 150.40 を使い、19.90 × 150.40 = 2,992.96円 → 3,000円以下。
- **禁止**: 150.90（19.90 × 150.90 = 3,002.91円）を使う。
- `decision_cutoff_at` 以前の FX が許容遅延内にない場合は `FX_MISSING`（許容遅延は D-02a）。
- Universe 判定では `price_cutoff_at` に対して同じ検査を行う。

## RF-10 パス解決

共通: `entry_reference_price` 1,000円、`target_price` 1,200円、`initial_failure_line` 920円。S0 に ENTRY。対象は S3。

| ケース | S3 のデータ | 期待 `path_resolution` / `resolution_granularity` |
|---|---|---|
| a | 始値 900（その後の高値 1,250） | `FAILURE_FIRST` / `SESSION_OPEN` |
| b | 始値 1,210（その後の安値 910） | `TARGET_FIRST` / `SESSION_OPEN` |
| c | 日足 高値 1,230・安値 910。分足: 09:05 に安値 915、13:40 に高値 1,210 | `FAILURE_FIRST` / `INTRADAY_BAR` |
| d | 日足は c と同じ。10:30 の分足の終値 1,150、10:31 の分足が始値 1,202・安値 915 | `TARGET_FIRST` / `INTRADAY_BAR`（足の始値で既に Target を跨いでいる） |
| e | 10:31 の分足が 高値 1,205・安値 915（始値 1,100）。約定: 10:31:12 に 916、10:31:40 に 1,201 | `FAILURE_FIRST` / `TRADE` |
| f | e と同じ分足。約定: 10:31:12.000 に 915 と 1,205 が同一時刻で順序不明 | `AMBIGUOUS_PATH` / `TRADE` |
| g | e と同じ分足。この市場は構成上約定データを提供していない | `AMBIGUOUS_PATH` / `INTRADAY_BAR` |
| h | e と同じ分足。約定データは通常提供されているが、10:31 の時間帯が欠損 | `UNRESOLVED_MISSING_DATA` |
| i | 日足は c と同じで、S3 の分足がすべて欠損 | `UNRESOLVED_MISSING_DATA` |
| j | S0: 10:00:30 に ENTRY。10:00 の分足に安値 915 があるが、約定で見るとそれは 10:00:05（ENTRY 前） | ENTRY 前の値動きとして無視し、`FAILURE_FIRST` にしない |

- **禁止**: f・g・h・i を `TARGET_FIRST` / `FAILURE_FIRST` に分類する。a で「高値 1,250 に触れた」ことを理由に Target 側にする。
- g と h の区別は D-09b で確定（最終パッチ #6）: g（細かいデータが仕様上存在しない）= `AMBIGUOUS_PATH`、h（本来あるはずのデータが欠損）= `UNRESOLVED_MISSING_DATA`。両方とも必須。

## RF-11 3,000円境界

- JP: 2,999円 → 含める、3,000円 → 含める、3,001円 → 除外（`PRICE_ABOVE_3000`）。判定は無調整価格。後日公表の分割係数を過去日の判定に使わない。

## RF-12 Universe 定義 v1.0.0

（v0.1 から変更なし）JP プライム/スタンダード/グロースの普通株を含める。TOKYO PRO（`Mkt = 0105`）・ETF・REIT は除外。US NYSE/Nasdaq/NYSE American の普通株を含め、ETF・Preferred・Warrant・Unit・Right・OTC・SIC 6770 の SPAC・テスト銘柄は除外。種別不明は `TYPE_UNKNOWN` で除外しカバレッジに計上。ADR（D-10a）と NYSE Arca のみの上場銘柄（D-10d）は pending。

## RF-13 Interpretive ラベルの採用ポリシーと整合性

- a: confidence がポリシー下限未満で `UNREVIEWED` → Production 学習データに入らない。manifest に不採用件数を記録。
- b: `FAILURE_FIRST`（`initial_failure_line` 基準）の Episode に `PREDICTIVE_SUCCESS` → 拒否。
- c: `AMBIGUOUS_PATH` / `UNRESOLVED_MISSING_DATA` の Episode に成功系・失敗系ラベル → 拒否。
- d: `current_risk_line` に先に触れたが `initial_failure_line` 基準では `TARGET_FIRST` の Episode → Objective の Primary Outcome は `TARGET_FIRST` のまま。

## RF-14 原文の改変検知

- **RF-14a**: `docs/prompts/MANIFEST.md` に登録済みのファイルを1バイト変更 → CI 失敗。
- **RF-14b（v5.1 と addenda を混ぜない）**:
  - addendum を追加しても、`short-surge-v5.1.original.md` の SHA-256 は MANIFEST に記録した保存時点の値から変わらない。
  - LLM への入力バンドルは、v5.1 原文と各 addendum を**別セクション**として持ち、それぞれの SHA-256 を個別に記録する（v5.1 本文の中に addendum の文言を差し込まない）。
  - v5.1 原文ファイルに addendum の見出し・文言が含まれていたら CI 失敗。

## RF-15 Research と Production の分離

- `run_mode = RESEARCH` のジョブが `prod.*` に書き込もうとする → 権限エラー。Historical Replay の結果は `research.*` にのみ保存される。

## RF-16 後日訂正データの as-of 読み取り

- D5 の日足を D7 に訂正版として再保存（`supersedes_manifest_id` 付き）。`as_of = D6` で読むと旧版、`as_of = D8` で読むと訂正版。D6 時点の判断の再現に訂正版を使わない。

---

## RF-17 ニュース backfill（新規、Phase 0.2 #9）

**入力**
| 文書 | source_published_at | system_first_seen_at | ingested_at | available_to_model_at | 取得経路 |
|---|---|---|---|---|---|
| A | 2026-09-20 10:00 JST | **2026-10-01 03:00** | 03:01 | 03:05 | 2026-10-01 の backfill |
| B | 2026-09-20 10:00 JST | 2026-09-20 10:02 | 10:02 | 10:05 | 通常の収集 |

既存: 2026-09-21 09:30 を `decision_cutoff_at` とする Production の分析・Prediction（A の backfill 前に作成済み）。

**期待**
- A の `system_first_seen_at` / `ingested_at` / `available_to_model_at` は backfill 実行時刻になる。
- backfill 後も、既存 Production 分析の入力バンドルとその SHA-256 は変わらない。既存分析から A への参照は作られない。
- `decision_cutoff_at` = 2026-09-21 09:30 の Historical Replay は A を使わない。
- A だけで構成される `material_event` の `system_first_seen_at` は 2026-10-01 03:00。`market_first_published_at` は 2026-09-20 10:00 と記録してよいが、分析で使えるかの判定には使わない。
- 2026-10-01 より前の日付の Feature（材料件数・新規性など）を再計算しても、A は含まれない。

**禁止**
- backfill 時に `system_first_seen_at` や `available_to_model_at` を `source_published_at` で埋める。
- A を 2026-09-21 の Replay・既存 Prediction の根拠に加える。

**RF-17-C（対照）**: B は `decision_cutoff_at` = 2026-09-20 10:30 の判断では使える。`decision_cutoff_at` = 2026-09-20 10:03 の判断では使えない（`available_to_model_at` = 10:05 のため）。

見逃しの判定との関係（D-23 確定、最終パッチ #3）: A のような backfill 情報は、「市場には cutoff 前から情報が存在した」ことの確認には使えるが、AI が当時知っていた情報としては扱わない。A が上昇を合理的に拾える情報だった場合、ラベルは `PIPELINE_MISSED_ACTIONABLE_SIGNAL` であり `ACTIONABLE_FALSE_NEGATIVE` ではない（RF-23）。

---

## RF-18 株式分割・併合・配当（新規、Phase 0.2 #10）

**RF-18a 2-for-1 分割（JP）**
- S0 ENTRY: `entry_reference_price` 1,000円、Target 1,200円、`initial_failure_line` 920円
- S4 が権利落ち（`r = 2`）。raw の値: S4 は 510〜525円（comparable 1,020〜1,050）、S7 に raw 高値 610円（comparable 1,220）
- **期待**: `TARGET_FIRST`（S7）。MFE = +22.0%。raw の日足はそのまま保存されている。`corporate_action_ids_applied` に分割が記録される。
- **禁止**: S4 の raw 510円を 920円と比べて `FAILURE_FIRST` にする。MAE を約 −49% と算出する。

**RF-18b 1-for-10 併合（US）**
- S0 ENTRY: `entry_reference_price` $2.00、Target $2.40、`initial_failure_line` $1.80
- S3 が権利落ち（`r = 0.1`）。S3 の raw $19.50（comparable $1.95）、S5 の raw 安値 $17.50（comparable $1.75）
- **期待**: `FAILURE_FIRST`（S5）。
- **禁止**: S3 の raw $19.50 を Target $2.40 と比べて `TARGET_FIRST` にする。MFE を +875% と算出する。

**RF-18c 配当**
- S0 ENTRY 1,000円、S5 に1株 30円の配当落ち。Horizon 中の comparable 高値 1,180円。
- **期待**: `hit_20 = false`（1,180 < 1,200）。
- **禁止**: 配当 30円を加えて 1,210円相当とし、`hit_20 = true` にする。

**RF-18d 記録のない不連続**
- S6 に raw 価格が前日の約半分で寄り付き、corporate action の記録がない。
- **期待**: `CORPORATE_ACTION_SUSPECTED` とし、Outcome を確定しない。
- **禁止**: `FAILURE_FIRST` に確定する。

**RF-18e 3,000円判定との関係**: 分割の前後とも、Universe・ENTRY 時の3,000円判定は raw 価格で行う（RF-11）。

---

## RF-19 ENTRY 時の3,000円再判定（新規、Phase 0.2 #4）

| ケース | Setup 時 | ENTRY 判断時 | 期待 |
|---|---|---|---|
| a（JP） | `TECHNICAL_SETUP_EOD` 引け値 2,950円 | `decision_price` 3,020円 | `REJECT`（`HARD_FILTER_AT_ENTRY`）、Prediction 0件 |
| b（US） | $19.80 × 150.00 = 2,970円 | `decision_price` $19.95 × 151.00（`fx_observed_at <= decision_cutoff_at`）= 3,012.45円 | 同上 |
| c（対照） | 2,950円 | `decision_price` 2,990円、分析は ENTRY | Prediction を作ってよい |
| d（最終パッチ #4） | 2,950円 | `decision_price` 2,995円で ENTRY、`entry_reference_price` 3,005円 | **Prediction・Episode は作らない。** `prod.entry_attempts` に `ENTRY_ABORTED_PRICE_LIMIT`。Threshold・Outcome・成績に含めない |
| e（d の続き） | — | 同じセッションの後刻に 2,980円まで下落 | d の試行を自動で復活させない。新しい再分析が ENTRY と判断し、その `decision_price` / `entry_reference_price` がともに3,000円以下の場合のみ、新しい Prediction を作る |
| f（US、entry 側） | — | `decision_price` $19.80 × 151.00 = 2,989.80円で ENTRY、`entry_reference_price` $19.90 × 151.00 = 3,004.90円 | d と同じく `ENTRY_ABORTED_PRICE_LIMIT` |

**禁止**: a・b で Prediction を作る。Setup 時点で3,000円以下だったことを理由に再判定を省略する。

---

## RF-20 Horizon（新規、Phase 0.2 #6）

**入力（JP）**: S−3 に `WATCH_BREAKOUT` 開始、S0（10:00）に ENTRY。Target 1,200円。S6 に `REAFFIRMED`。S9 と S10 の間に休場日がある。
| ケース | 価格 | 期待 |
|---|---|---|
| a | S20 に高値 1,205 | `TARGET_FIRST`（Horizon 内） |
| b | S21 に初めて高値 1,205 | `NEITHER_BY_HORIZON`、close_reason `HORIZON_EXPIRED` |
| c | S0 の 10:00 に ENTRY（1,000円）、S0 の 14:20 に高値 1,205 | `TARGET_FIRST`（S0。ENTRY 時刻以降の S0 の値動きを含む） |

- **期待**: `horizon_end` は S20 の引け。休場日は数えない。
- **禁止**: Watch 開始（S−3）から数えて S17 で終える。`REAFFIRMED`（S6）で数え直して S26 まで延ばす。休場日を1セッションとして数える。
- 表記は D-17d で確定（最終パッチ #5）: S0 = ENTRY 成立セッション、S1 = 翌取引セッション、Primary Horizon は S20 close まで。a〜c すべて必須。
- **禁止（追加）**: c で S0 の値動きを除外して S1 から判定を始める。

**RF-20-C（対照）**: Episode E1 クローズ後、S12 に正式に新しい Episode E2 が開始 → E2 は S12 を S0 とする独立した Horizon を持つ。

---

## RF-21 Failure Line の二層（新規、Phase 0.2 #7）

**入力（JP）**: S0 ENTRY 1,000円、Target 1,200円、`initial_failure_line` 920円。S5 の再分析で `current_risk_line` を 980円に引き上げ（State Transition）。S6 に安値 950円。S11 に高値 1,210円。
- **期待**
  - Primary: `TARGET_FIRST`（S11）。Episode のクローズ理由 `TARGET_HIT`（`INITIAL_FAILURE_HIT` ではない）。
  - 研究用の risk line パス解決: `RISK_LINE_FIRST`（S6）。`RISK_LINE_HIT` の State Transition が記録される。
  - `prod.predictions.initial_failure_line` は 920 のまま。
- **禁止**: S6 で Primary を `FAILURE_FIRST` にする。`initial_failure_line` の UPDATE（DB で拒否）。`current_risk_line` への接触で Episode をクローズする。

---

## RF-22 米国株の Outcome 通貨（新規、Phase 0.2 #8）

**入力（US）**: S0 ENTRY `entry_reference_price` $10.00、Target $12.00、ENTRY 時の FX 150.00（円換算 1,500円、3,000円以下）。
| ケース | 価格 | 評価時の FX | Primary（USD） | 補助 JPY リターン |
|---|---|---|---|---|
| a | S9 に高値 $12.10 | 140.00 | `hit_20 = true` | 12.10 × 140 / 1,500 − 1 = +12.9% |
| b | Horizon 中の高値 $11.50 | 160.00 | `hit_20 = false` | 11.50 × 160 / 1,500 − 1 = +22.7% |

- **期待**: Primary の Target 判定・MFE/MAE は USD 建て。JPY リターンは補助 Outcome の別列。
- **禁止**: a で円高を理由に `hit_20 = false` にする。b で円安を理由に `hit_20 = true` にする。

---

## RF-23 False Negative の3分類（新規、最終パッチ #3）

共通: ENTRY されなかった Eligible 銘柄。Prediction cutoff（その日の判断のカットオフ）= D35 15:00 ET。D41 以降に +20% 以上（Objective `hit_20 = true`）。

| ケース | cutoff 前の状況 | 期待ラベル | 使い道 |
|---|---|---|---|
| a | 「買収検討」報道: `source_published_at` = D35 10:00、`available_to_model_at` = D35 10:04。出来高も異常。システムは Stage 1 で落とした | `ACTIONABLE_FALSE_NEGATIVE` | Prediction Model（候補生成・Entry 層）の見逃し学習 |
| b | 同じ報道: `source_published_at` = D35 10:00、コレクタ障害で `available_to_model_at` = D42 09:00（後日の backfill） | `PIPELINE_MISSED_ACTIONABLE_SIGNAL` | Data / News Pipeline 改善用。**Prediction Model の False Negative にしない** |
| c | 市場にも事前の報道・異常出来高・その他の合理的な前兆なし。D40 18:00 に突然の買収発表 | `OUT_OF_SCOPE_SHOCK` | Prediction Engine の False Negative にしない |

**期待**
- b の判定では、報道の存在確認に backfill 文書を使ってよい（`source_published_at` と遅延の記録）。ただし判定記録に「cutoff 時点で AI は未取得」と残る。
- b の `pipeline_miss_records` に、`source_published_at`、`available_to_model_at`、遅延時間、原因（例: `COLLECTOR_OUTAGE`）が記録される。
- a〜c のいずれも、D35 の Production 分析の入力バンドルとハッシュは変わらない。

**禁止**
- b に `ACTIONABLE_FALSE_NEGATIVE` を付ける（backfill 情報を当時 AI が知っていたことにする）。
- b・c を Prediction Model の見逃し学習データに入れる。
- a の判定入力に `available_to_model_at > cutoff` の情報を含める。

---

## RF-24 THESIS_INVALIDATED 後の Outcome 二層（新規、最終パッチ #2）

**入力（JP）**: S0 ENTRY 1,000円、Target 1,200円、`initial_failure_line` 920円。S5 の再分析で `THESIS_INVALIDATED`（それまで Target・Failure に未到達、S0〜S5 の高値 1,080・安値 950）。S12 に高値 1,230円、S15 に安値 900円。

**期待**
| 層 | 値 |
|---|---|
| `primary_episode_outcome` | close_reason `THESIS_INVALIDATED`（S5）。`hit_20 = false`。MFE / MAE は S5 までの +8.0% / −5.0% |
| `counterfactual_horizon_outcome` | S20 close まで追跡。`later_target_hit = true`（S12）、counterfactual MFE +23.0%、counterfactual MAE −10.0%（S15） |
| 成績集計 | 成功数に含めない |

**禁止**
- S12 の +23% 到達を理由に Primary を `TARGET_HIT` / 成功に変える。
- counterfactual の `later_target_hit` を Production ML の正解ラベルとして使う。
- Episode を S5 でクローズせずに S20 まで Primary を延長する。

**対照（RF-24-C）**: `THESIS_INVALIDATED` がなく S12 に 1,230円に達した場合は、Primary が `TARGET_HIT`（S12）になり、counterfactual も同じ到達を記録する。
