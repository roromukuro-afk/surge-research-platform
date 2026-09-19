# Jev 予測性能評価 設計

状態: **v1.3（ユーザー確定 2026-09-19: Phase B の経路は D-275、Phase B の規則は D-276 = D-272 の決定）** — Phase A 完了（§10）。Phase B は**実装と synthetic test まで完了、未開始**（§11）: S0 = 2026-09-24 以降、TypeSafe 直接 API で model を `jev-1.13.0` に固定、1 営業日最大 92 request、Phase B 全体の hard cap $2.50。**cohort の作成と 2026-09-24 分の送信はユーザーの開始指示まで行わない**
前提: [D-267](../unresolved-decisions.md) EOD Prediction 規則（S0 確定終値・目標 ×1.20・S1 から評価・T+1/3/5/10/20・期限 T+20）/ D-269（Vercel AI Gateway 経由の Jev は技術的に適合、production default ではない）/ D-268（+20% 到達の判定基準は未決）/ D-270（本設計の確定）

## 0. 目的と範囲

- **目的**: Jev の型付き判断（decision・`reaches_target` 確率・upside score・根拠 noul）と、実際の T+20 outcome の関係を測る。
- **基本は 1 銘柄 1 回**。
- **実装しないもの**: Jev の production default 化 / 同一銘柄の複数回推論（本番仕様としての平均化）/ confidence による自動 REJECT / 確率を売買確率として解釈すること。

## 1. retrospective と prospective の分離

Jev の training cutoff を信頼できる形で確定できず、過去の outcome をモデルが知っている可能性を排除できない。**過去データだけで採用判断はしない。**

| | Phase A: retrospective pilot | Phase B: prospective evaluation |
|---|---|---|
| S0 | 2025-01-01〜2026-06-30 から point-in-time に抽出 | **2026-09-24 以降**の営業日ごとに新しく発生する候補（shadow prediction。9/21〜23 は東証の休場日、D-276） |
| 件数 | **main 20（Primary 15 / Control 5）+ 匿名化 2 + drift 2 = 24 request**（正式な母集団 75 / 25 から固定 seed で縮小、D-274。当初は 100 件） | **1 営業日 main 最大 80（Primary 60 / Control 20）+ 匿名化 8 + drift 4 = 最大 92 request**。目標 25 営業日で Primary 1,500 / Control 500（main 2,000、約 2,300 request）（D-276） |
| 目的 | state 再構築・screening replay・Jev request・outcome 計算・report 生成・schema / hash / provenance・API cost accounting の検証**だけ** | **Jev の正式な性能判断** |
| 成績の扱い | **採用判断に使わない** | T+20 まで追跡して評価 |

**Phase A が正常に完了するまで Phase B の大量実行は始めない。**

## 2. 母集団

- **Primary cohort**: 現行 point-in-time screener を通過した銘柄。
- **Control cohort**: 同じ S0 の 3,000 円以下 Universe から、screener を**通過しなかった**銘柄を matched sampling（価格帯・流動性帯で対応付け）。
- **Primary : Control = 75 : 25**。
- **両群を混ぜて calibration を計算しない。**
  - Primary で評価: Brier score / Brier skill、calibration、PR-AUC、target hit rate、state 別実績、score と future return の関係。
  - Control は「screening → Jev」という pipeline 全体に追加価値があるかを見る **benchmark 専用**。
- 同じ銘柄の S0 は 20 営業日以上離す（outcome 窓の重複を避ける）。
- **第1版は JP のみ**（Yahoo EOD 経路が完成している市場）。US は Jev の有効性が確認されてから追加する。
- 3,000 円判定は **as-traded（raw）の S0 終値**（CLAUDE.md 1-16）。Yahoo の日足は分割調整済み（2026-09-18 実測）なので、split events から as-traded に戻してから判定する。

## 3. Point-in-time の入力（リーク防止）

- state = Canonical + addenda（全文、ハッシュを manifest に記録）+ 評価用 bundle（`eval-state-1`）。
- bundle には cutoff（S0 の夜）以前に利用可能だったものだけ: S0 までの日足（S0 の株数基準）、S0 時点の features と screening 結果、TDnet 開示の**タイトルのみ**（本文は取らない、D-126）。
- コードが決める値（S0 終値、目標 = ×1.20、3,000 円判定、評価窓）は state に事実として入れ、**Jev には聞かない**。

## 4. 匿名化 sensitivity test（main evaluation に混ぜない）

- Phase A / B それぞれ**約 10%** について、同じ state の匿名化版を**1回だけ**追加実行する。
- **匿名化する**: ticker / 企業名 / issuer 名 / 企業を直接特定する不要な文字列。
- **保持する**: 市場区分 / sector / numeric features / prices / volume / 開示内容の意味 / screening 結果 / Canonical・addenda。
- 通常版と匿名版を **paired comparison**: choice の変化、probability の差、score の差。

## 5. drift

- 同じ入力の再実行は**約 5% だけ**。目的は Jev 自身の stochastic drift の測定のみ。
- 本番仕様を「3回平均」にはしない。

## 6. 保存（DB には保存しない）

評価専用の immutable files（上書き禁止）。`<評価ルート>/evaluation/jev/<run_id>/`:

| ファイル | 内容 |
|---|---|
| `manifest.json` | run_id / evaluation version / model / provider / model・version metadata / question schema hash / Canonical hash / addenda hashes / input-building code version / population definition / outcome definition / started_at |
| `predictions.parquet` / `.jsonl` | 1 件 1 回の予測（通常版） |
| `outcomes.parquet` | outcome（**Jev の答えを見る前に固定**） |
| `paired_anonymized.parquet` | 匿名化 paired comparison |
| `drift.parquet` | 同一入力の再実行 |
| `report.json` / `report.md` | 集計 |

- 全行 `teacher_admissible = false`。
- teacher / prediction / execution 等の production DB へは**一切書かない**。評価ルートは repository の外（git に入れない）。

## 7. Outcome

- 基準 = S0 の as-traded 確定終値。評価窓 = S1..S20。T+n = S0 の後の n 番目の取引セッション。
- D-268 が未決なので **intraday high で +20% 到達** と **close で +20% 到達** の両方を計算する。どちらを正式 label にするかは後で決める。
- 固定して保存: T+1 / T+3 / T+5 / T+10 / T+20 の終値リターン、max upside、max drawdown（S0 の株数基準、CLAUDE.md 1-9）。
- `success_label` とは統合しない。

## 8. API 予算

- **Phase A**: 24 request（main 20 + 匿名化 2 + drift 2、D-274）、hard budget **$0.05**、5 分間隔。（当初は 100 件 + 匿名化 約 10 件・drift 約 5 件、hard budget $0.20）
- **Phase B**（D-276）: 1 営業日最大 92 request、25 営業日で約 2,300 request ≈ $2.2（実測単価 約 $0.00095 / 件）。**TypeSafe 直接 API で model を `jev-1.13.0` に固定**（D-275 / D-276、1 request / 5 秒）。**Phase B 全体の TypeSafe direct 支出は $2.50 が hard cap**（記録済みの cost、cost の無い応答は見積もり額、それに次の request の見積もりを足して判定）、1 日の hard budget は $0.12 / 92 件。Vercel AI Gateway の回答には version が無く pin を確かめられないので、**Phase B の cohort には入れない**（D-275 の fallback の役割は Phase A 型の run・出力比較・adapter の regression test に限る）。
- 現在の実測 Gateway cost（1 件 ≈ $0.00092、D-269）を基準に、**run ごとに hard budget**（金額と件数）を設定し、超える前に止める。
- **どちらの経路でも無料 credit を超える実行はしない。** 有料 credit の購入・カード登録・auto-reload / auto-recharge はしない（Gateway の Auto-reload は Off、TypeSafe の auto-recharge は Off でカード未登録）。実行前に残高を確認する: Gateway は runner で残高を読み、TypeSafe は残高 API が無いので、**console の credit snapshot**（確認した残高・確認時刻・表示された失効日）から、この system がその後に記録した direct の支出を引いた額を使う（D-276。D-275 の「7 日以内」は廃止）。snapshot は表示された失効日（その日付の 00:00 UTC として扱う）まで有効で、それ以外の期限は設けない。失効日を跨ぐときだけ送信を止め、新しい console 残高を一度確認して snapshot を追加する。

---

## 9. Phase A 実装計画

API を送る直前まで（`preflight`）を実装・テストし、そこで一度報告する。

### 9-1. 構成（`workers/src/surge/evaluation/`、production 経路から import しない）

| module | 役割 |
|---|---|
| `store.py` | run ディレクトリ、write-once ファイル（JSON / JSONL / Parquet）、各ファイルの SHA-256 |
| `prices.py` | Yahoo 日足 + split events → as-traded の `CanonicalBar` と `CanonicalAction`、取引セッション暦 |
| `universe.py` | JPX 上場銘柄一覧（コード・名称・市場区分・33業種・規模区分）の読み込みと、内国普通株の抽出 |
| `population.py` | screening replay（`build_comparable_series` → `compute_features` → `evaluate_routes`、as-of = S0）と Primary / Control の抽出 |
| `state.py` | point-in-time の state（通常版・匿名化版）と request body |
| `outcome.py` | outcome の計算と freeze |
| `leakage.py` | リーク検査 |
| `cost.py` | hard budget と Gateway 残高の確認 |
| `report.py` | 指標の計算と `report.json` / `report.md` |
| `jobs/jev_eval.py` | CLI: `plan` → `build` → `freeze-outcomes` → `preflight` →（`run` → `report`） |

### 9-2. 手順

1. **plan**（API なし）: JPX 一覧から内国普通株（プライム / スタンダード / グロース）を取り、seed 付きで銘柄の部分集合を選ぶ → Yahoo 日足（2024-06-01〜現在、split events 付き）→ as-traded に復元 → 取引暦 → S0 候補日（月 2 日、seed 付き）ごとに screening replay → Primary 75 / Control 25（matched）→ 匿名化 10 / drift 5 を指定 → `manifest.json` と `population.*` を書く。
2. **build**（API なし）: 各 sample の state と request body（通常版・匿名化版）を作り、token 数を実測。
3. **freeze-outcomes**（API なし）: S1..S20 から outcome を計算して `outcomes.parquet` に固定。**これより前に予測を送らない。**
4. **preflight**（API なし・Gateway の残高照会のみ）: リーク検査、request 数、予算の見積もり、残高確認。**ここで止めて報告する。**
5. run / report: 承認後。

### 9-3. リーク検査

- state の日足がすべて S0 以前 / 開示タイトルの公表時刻が cutoff 以前
- state を直列化した中に cutoff より後の日付が無い
- outcome の足（S1..S20）がすべて S0 より後で、state に含まれない
- state の S0 終値 = outcome の基準価格（as-traded）
- features / screening の series の as-of = S0 で、最後の足が S0
- 匿名化版に ticker・企業名・issuer 名が残っていない
- request の Canonical / addenda のハッシュ = manifest のハッシュ
- S0 終値（as-traded）≤ 3,000 円、全行 `teacher_admissible = false`

### 9-3b. 実装上の規則（Claude Code 決定、D-270）

- **Canonical / addenda** は `docs/prompts/MANIFEST.md` と SHA-256 を照合し、addenda は**登録順**（古い → 新しい）で送る。ファイル名順だと `v5.1-addendum-2026-09-15.md` が、後から登録されそれを改める phase0.x の後ろに来る。登録されていない addendum があれば止める。plan の後に Canonical / addenda が変わった run は build / preflight / run を拒否する。
- **S0** は月 2 日（seed 付き）。取引暦は、抽出した銘柄の 30% 以上に足がある日（暦を推測しない、D-142）。S0 の後に 20 セッションが既にある日だけ。
- **Control** は、同じ S0 の非通過銘柄から、価格帯（0–500 / 500–1000 / 1000–2000 / 2000–3000 円）→ 流動性帯（その S0 の適格銘柄の 20 日平均売買代金の四分位）の順に近いもの。
- **Phase A の縮小（D-274）**: 正式な母集団（Primary 75 / Control 25 と匿名化 10・drift 5 の指定）を従来どおり引いて `population_full.jsonl` に残し、そこから `reduce_population` で Primary 15 / Control 5 を cohort ごとに固定 seed （`<seed>/phase-a-evaluated`）で選ぶ。読むのは cohort と sample id だけで、outcome・価格・回答は読まない。匿名化 2・drift 2 も同じ seed で選んだ 20 件から、互いに重ならないよう選び直す。評価するのは `population.jsonl` の 20 件。
- **開示タイトル**は Yanoshin の銘柄別 index（その銘柄の行だけ）から、cutoff（S0 の翌日 0:00 JST）の **30 分以上前**に公表された、60 日以内のもの。index の行は TDnet の公表時刻であって、システムが知り得た時刻ではない（CLAUDE.md 1-7）ので余裕を取る。
- **匿名化**: `security.code` / `security.name` を `[CODE]` / `[COMPANY]` に置き換え、タイトル中の企業名（株式会社・ホールディングス等を除いた形も含む）と、単独のトークンとしてのコード（半角・全角）を置き換える。数値の中の同じ数字列（例: 13,015 百万円の 1301）は置き換えない。
- **予算**: 1 件の見積もり = o200k tokens × 1.35（smoke の実測比は 1.23）× $0.042/M。支出は Gateway が報告した `cost`、報告の無い request（エラー等）は見積もり額で計上する。送る前に毎回「支出 + 次の見積もり ≤ 予算」と件数上限を確認する。予算は $5 以下かつ Gateway の残高以下。Jev の 32K context を見積もりで超える request は送らない。
- **outcome の固定**: `freeze-outcomes` は応答が 1 件でもあれば拒否する。`run` は freeze と、同じ予算で `ready_to_send` になった最新の preflight が無ければ拒否する。preflight は各 outcome を再計算して固定値と一致することも確かめる。
- **1 件でも次のどれかが出たら `run` はその時点で止まる**（ユーザー指示 2026-09-18、再試行しない）: Gateway error / schema incomplete / 予算の異常（報告されない cost、見積もりを超える cost、見積もりを超える input tokens、予算・件数の上限）/ request の hash 不一致（送る直前にファイルを照合）/ 想定外の model（`typesafe-ai/jev` 以外）・provider（`typesafe-ai` 以外）/ integrity violation（stage ファイルの照合失敗）。止まった run は `run-stopped-<n>.json` を残し、**再開しない**（見直してから新しい run にする）。
- 全件の後に Gateway の残高をもう一度読み、`stage-run.json` に送信前後の残高を残す。
- **送信の間隔（D-273 → D-274）**: Gateway の free tier は `typesafe-ai/jev` を rate limit する。5 件連続の後の 6 件目で 429 を 2 回再現し（約 2.2 秒間隔の `-03`、15 秒間隔かつ 60 秒に最大 4 件の `-04`）、窓の大きさは docs からも応答 header からも分からない。Phase A は**5 分間隔**で送り、guard は任意の 20 分に最大 4 件（`surge.evaluation.pacing.Pacer`。超えそうなら待ち、超えたら例外）。各応答に `pacing`（request 時刻・runner 側の開始時刻・直前の成功時刻・rolling 60 秒の件数・方針の窓の件数・待った秒数・方針）と `http`（status・error 名と type・Retry-After（無ければ null）・応答 header）を残す。runner は Gateway の error から応答側だけを写し、request 本文（`cause.requestBodyValues`）と API key は記録しない（`ops/jev-gateway-runner/describe-error.mjs`、`node check-describe.mjs` で offline に確認できる）。rate limit の境界はこれ以上探索しない。
- **Phase B の経路と送信（D-275）**: `surge.evaluation.providers` の TypeSafe direct（primary）と Vercel Gateway（fallback）。保存する request は従来どおり 1 つ（Gateway の wire 形式、preflight の検査対象）で、direct はそこから自分の wire 形式（model `jev-latest`、yes/no 型 `noul`）を作り、送ったバイトの hash を `wire_sha256` に残す。pacing は direct 5 秒間隔（60 秒に最大 12）、Gateway 5 分間隔。429 は direct なら provider の `retry_after_ms`（`retry-after-ms` → `Retry-After` → body）を優先して待って同じ request を再送（最大 3 回、1 回の待ちは 30 分まで、値が無ければ 60 秒から倍々）、超えたら停止。Gateway の 429 は Phase A どおり即停止。direct の served version（例 `jev-1.13.0`）は run の最初の回答で固定し、変われば停止（Phase B は §11: request で `jev-1.13.0` を指定し、別の版の回答はその日と cohort を止める）。raw の出力（加工前）を `responses/*.raw*.json`に保存し、予測行にも `answers_as_recorded` を残す。複数回の平均・confidence / probability のしきい値・decision ルールの変更は実装しない。

### 9-3c. run ディレクトリ

| ファイル | stage | 内容 |
|---|---|---|
| `manifest.json` | plan | §6 の項目（`input_building_code` は git HEAD・関係ファイルの SHA-256・各 version） |
| `inputs/universe.json` / `inputs/prices/<code>.json` | plan | JPX 一覧の SHA-256 と抽出銘柄、Yahoo の応答そのまま（as-traded への復元は毎回コードで行う） |
| `sessions.json` / `screening.parquet` | plan | 取引暦、S0 ごとの全銘柄の screening 結果 |
| `population.jsonl` / `.parquet` | plan | sample（cohort・matched_to・匿名化 / drift の指定・`teacher_admissible=false`） |
| `inputs/tdnet/<code>.json` / `requests/<sample>.<variant>.json` / `requests.jsonl` | build | 開示 index、送る request の正確なバイト列、token 数・見積もり・coverage |
| `outcomes.jsonl` / `.parquet` | freeze-outcomes | outcome（§7） |
| `preflight-<n>.json` / `credits-<n>.json` | preflight | リーク検査・予算・残高 |
| `responses/<sample>.<variant>(.raw).json` | run | 応答（request 本文は含まない） |
| `predictions.*` / `paired_anonymized.parquet` / `drift.parquet` | run | §6 |
| `report.json` / `report.md` | report | 下記 |
| `stage-<name>.json` | 各 stage | 書いたファイルの SHA-256。次の stage はこれで照合してから読む |

`report.json`（`eval-report-1.0.0`）: `banner`（Phase A は「採用判断に使わない」）/ `pipeline`（request 数・エラー・不完全、Gateway cost と見積もり、tokens、latency、served model / provider、outcome の解決状況）/ `primary`（高値・終値それぞれの positives・base rate・Brier・Brier skill・PR-AUC・10 bin calibration と ECE、decision 別の hit rate・T+20 リターン・max upside、upside score / reaches_target と将来リターンの Spearman）/ `control_benchmark`（件数・decision 分布・Primary と並べた hit rate のみ。calibration は計算しない）/ `anonymized_sensitivity` / `drift`（choice の変化・確率差・score 差）。

### 9-4. Phase A で明示する制約

- **screener は Routes A–H（route-1.0.0、features-1.0.0）の point-in-time replay**。material routes（M1–M6）と Stage 2 は replay しない（過去の材料の利用可能時刻を正確に再現できないため）。Phase B は日次で動く本番 screener を使う。
- Universe は**現在の** JPX 一覧（上場廃止銘柄を含まない＝生存者バイアス）。市場区分・33業種・規模区分も現在値（point-in-time ではない）。
- turnover は Yahoo に無いので **as-traded 終値 × 出来高**で近似する（route F が使う）。
- 開示タイトルは Yanoshin の銘柄別一覧（直近 300 件まで）。窓の始まりまで届かない銘柄は coverage 不足として記録する。
- Phase A の成績は採用判断に使わない（§1）。

### 9-5. Phase B の前に決めること（D-272 → **2026-09-19 にユーザーが決定、D-276・§11**）

Phase A の replay では、現行 Routes A–H が適格銘柄の約 61% を通した（Route D 単独で 35%）。Phase B の前に、1 日あたりの最大 shadow prediction 数、上限を超えたときの sampling / selection、Route D の候補生成への寄与の扱いを決める。Phase A では投資ロジックと route 閾値を変えない。


## 10. Phase A 結果（2026-09-18、**成功として完了**）

**Phase A の性能値（予測精度・Brier・PR-AUC・calibration・hit rate 等）は Jev の採用判断に使わない。** ここで確定したのは「Jev と evaluation pipeline が技術的に成立する」ことまでで、予測性能は prospective の Phase B で判定する。Jev は production provider に接続しない（production default にしない）。

| 項目 | 結果 |
|---|---|
| run | `jev-a-20260918-05`（commit 3dcb292、CI green）。正式な母集団（`-04` と同一の 100 件）から固定 seed で縮小（D-274） |
| 送信 | **24 / 24 成功**（main 20 = Primary 15 / Control 5、匿名化 2、drift 2）、HTTP 200、停止条件 0 |
| schema completeness | **24 / 24** |
| main と outcome の join | **20 / 20**（Primary 15 / Control 5、混ぜていない） |
| 匿名化 paired comparison | **2 組** |
| drift comparison | **2 組** |
| leakage / integrity | **違反 0**（preflight のリーク検査、stage ごとの hash 照合、送信直前の request hash 照合、応答と request の対応） |
| 実 Gateway cost | **$0.02269008**（Gateway の報告額の合計 = 残高差）、**hard budget $0.05 以内** |
| pacing | **5 分間隔、429 なし**（14:36:20〜16:31:20 UTC、任意の 20 分に最大 4 件） |
| report | **report.json / report.md の生成に成功** |

- 入力 22,222〜22,785 tokens（中央値 22,535、o200k の 1.175 倍）、latency 中央値 1.17 秒。
- この 20 件は +20% 到達が高値・終値とも 0 件だったため、Brier skill と PR-AUC は未定義（null）として出力された。少数で未定義になる統計を未定義のまま扱えることの確認であり、性能の値ではない。
- 送信間隔は Windows の monotonic clock の分解能（約 15.6 ms）の範囲で 300 秒（壁時計で 299.988〜300.030 秒）。
- 停止済みの `-03`（約 2.2 秒間隔、6 件目で 429）と `-04`（15 秒間隔、6 件目で 429）は監査記録として残す（D-273 / D-274）。


## 11. Phase B（prospective shadow evaluation）の規則と実装（D-276、2026-09-19）

**状態: 実装と synthetic test まで完了。cohort は未作成、API 送信は未開始**（ユーザーの開始指示まで行わない）。

### 11-1. ユーザー決定（D-272 の解決）

| 項目 | 規則 |
|---|---|
| 1 日の上限 | main 最大 80（Primary 60 + Control 20）、匿名化 = main の 10%（8）、drift = 5%（4）→ **最大 92 request / 営業日**。TypeSafe direct、1 request / 5 秒。目標 25 営業日（Primary 1,500 / Control 500 / main 2,000、約 2,300 request ≈ $2.2）。有料 credit は買わない |
| screener | Routes A–H は変えない。営業日ごとに、その日の 3,000 円以下 universe 全体へ現行 screener を point-in-time で適用し、**通過・非通過の全銘柄と各 route の membership を保存してから** Jev に送る最大 80 件を抽出する |
| Primary 60 | 通過銘柄から、outcome も Jev の出力も見ずに決定論的な一様抽出。seed は evaluation version・trading date・固定 experiment seed を含む。同じ date / version / seed なら同じ 60。60 以下なら全件。未選択も candidate pool として残し、各候補に selected / not・selection probability・route membership・D-only を記録 |
| Route D | 閾値・定義を変えない。D-only を任意に除外・減量しない。事前登録の subgroup（D-only / D+other / non-D）ごとに reaches_target の calibration、+20% 到達率、decision 分布、score、将来の最大上昇、最大 drawdown を別集計。変更の検討は Phase B の後 |
| Control 20 | 同日の 3,000 円以下で route を 1 つも通過しなかった銘柄から、Primary と価格帯・流動性帯で match、可能なら sector も（sector の一致で sample を大きく失わない範囲）。同点は決定論的 hash。outcome は使わない |
| 開始 | **S0 = 2026-09-24**（9/21〜23 は東証の休場日）。それより前の S0 は拒否 |
| model | TypeSafe direct。alias は `jev-latest` だが served version を保存。version の固定指定が公式に可能なら `jev-1.13.0` に pin、不可なら `jev-latest` で版の変化時に自動停止。**別の版を黙って cohort に混ぜない** |
| credit | 残高 API が無いので console の credit snapshot（confirmed balance・confirmed_at・表示された expiry）と local の支出 ledger。任意の 7 日再確認は設けない。表示された expiry までは「確認した残高 − local で記録した支出」で管理し、expiry を跨ぐときだけ Phase B を止めて新しい console 残高を一度確認する。run ごとの hard budget は維持。**Phase B 全体の TypeSafe direct は $2.50 が hard cap** |
| 固定 | Phase B 中は Canonical・addenda・Jev question schema・screening route 定義・sampling algorithm・outcome 定義・Jev の decision の解釈を変えない |
| 導入しない | probability threshold・confidence threshold・3 回平均・コード側の decision override・Jev の production 利用・teacher dataset への登録。**Raw output をそのまま評価する** |

### 11-2. version 固定の確認（→ `jev-1.13.0` に pin）

- TypeSafe の Models 文書（`docs.typesafe.ai/models.md`）: `model` には alias（`jev-latest` / `jev-preview`）のほか版 ID（例 `jev-1.13.0`）を指定でき、models 一覧に載っていなくても受け付ける。`jev-latest` は現在 `jev-1.13.0`。`GET /v1/models`（2026-09-19、推論なし）の一覧は `jev-latest`・`jev-preview` の 2 つ。
- 実測（2026-09-19 09:44:52Z、1 件だけ。Canonical も銘柄も含まない数語の state で、評価 dataset ではない。`direct-check-pin-*`）: `model: "jev-1.13.0"` → **HTTP 200、応答の `model` = `jev-1.13.0`**、338 input tokens、$0.0000142。
- よって Phase B は request の `model` を `jev-1.13.0` にし、回答の `model` が `jev-1.13.0` でなければ**その日の run を止め、cohort 全体も止める**（`cohort-stopped-<n>.json`。以後どの日も plan / preflight / run されない）。Gateway の回答には版が無いので Phase B の cohort に入れない。

### 11-3. 実装

| module | 役割 |
|---|---|
| `evaluation/selection.py`（**cohort が凍結**） | 1 日の抽出（`phase-b-selection-1.0.0`）: 上限 60 / 20 / 8 / 4 / 92、開始日、Route D subgroup、hash key、Control の match |
| `evaluation/phase_b.py` | cohort の凍結（`frozen_protocol`・fingerprint）、送信窓、cohort の支出と $2.50 cap、cohort の停止 |
| `evaluation/cost.py` | credit snapshot（`credit/typesafe-<n>.json`、write-once）と local の支出（全 run の `responses/*.json`、深さを問わない） |
| `evaluation/providers.py` | TypeSafe direct の `model` に版 ID を指定できる（`jev-1.13.0`） |
| `evaluation/leakage.py` | `check_prospective_sample`: outcome が無い時点で行える検査すべて |
| `evaluation/report.py` | `DECISION_INTERPRETATION`（回答の読み方。凍結）、Route D subgroup 別集計、Phase B banner |
| `jobs/jev_eval.py` | `phase-b-init` / `record-credit` / `plan-day` / `phase-b-day` / `phase-b-status` / `phase-b-report`、Phase B の build / preflight / run / freeze-outcomes |

**1 営業日の流れ**（`phase-b-day --cohort-id C --s0 D|auto [--send]`。`--send` が無ければ preflight まで）:

1. **plan-day**（S0 の全足が確定する S0 15:30 + Yahoo の遅延 20 分 × 2 = **16:10 JST 以降**、S1 が開き得る**翌平日 09:00 JST より前**）: JPX 一覧の内国普通株すべて → Yahoo 日足（S0 − 400 日〜）→ as-traded → screener（Routes A–H）を S0 で適用 → 全銘柄の結果（`screening.parquet`）と適格候補すべて（`candidates.*`: pool・route membership（route_A〜H）・Route D subgroup・D-only・価格帯・流動性帯・sector・key・順位・selected・selection probability・match）を保存 → 抽出（`population.*`）。universe の 95% 未満しか読めない日、データ上 session でない日（30% 規則）、通過銘柄が 1 つも無い日、cohort が全件送信済みの日を 25 日持っている場合は、何も書かずに拒否。1 銘柄の取得失敗（Yahoo の拒否・通信エラー。新しい session で 1 回だけ再試行）はその銘柄の失敗として記録し、日全体は止めない。
2. **build**: Yanoshin の開示タイトルと state・request（Phase A と同じ `eval-state-1`）。
3. **preflight**（outcome はまだ無い）: cohort の凍結が保たれている、保存した候補から抽出し直して同じ sample になる、上限（60 / 20 / 8 / 4 / 92）、`check_prospective_sample`（日足・開示・日付が cutoff 以前、S0 終値 = sample の基準、series の as-of、Canonical / addenda の hash、匿名化、3,000 円、teacher_admissible = false）、送信窓、credit snapshot、1 日の budget、**cohort の支出 + この日の見積もり ≤ $2.50**。
4. **run**: TypeSafe direct に `jev-1.13.0` で、5 秒間隔。**各 request の直前**（pacing の待ちの後）に送信窓と credit の失効を確かめ、送る前に cohort cap を確かめる。版の違う回答で日と cohort を停止。各応答に `ledger_usd`（cost、無ければ見積もり）。
5. **freeze-outcomes**（T+20 の足が確定した後）: その日の銘柄の日足を改めて読み、凍結された outcome コードで計算。回答は読まない。
6. **phase-b-report**: 全日の送信を完了し（S1 が開き得る時刻より前の送信のみ）、outcome が凍結された日だけを集計。Primary / Control を混ぜない。Route D の 3 subgroup を別集計。

**凍結（`cohort.json` の `frozen` と fingerprint）**: Canonical / addenda の hash、question schema hash、state version、screener（route / feature version、3,000 円、lookback 400 日）、selection（version・開始日・1 日の上限）、outcome（version・定義）、回答の読み方（`DECISION_INTERPRETATION` と `prediction_row` のソースの hash）、ファイル hash（`jev_questions.py`・`state.py`・`universe.py`・`population.py`・`selection.py`・`outcome.py`・`market/series.py`・`features/engine.py`・`features/indicators.py`・`routes/engine.py`）。どれかが cohort と違えば、その cohort の日は plan / build / preflight / run されない（変えるなら新しい cohort）。取得・pacing・予算・report の集計コードは凍結しない（各日が自分の code version を記録する）。

**事前登録（`cohort.json` の `preregistered`）**: Route D の 3 subgroup の定義と、subgroup ごとの指標（高値・終値それぞれの reaches_target calibration（10 bin・ECE）と Brier、+20% 到達率、decision 分布、upside score の平均・中央値、最大上昇（高値・終値）、最大 drawdown（安値・終値））。

### 11-4. Claude Code の決定（可逆・安全側、D-276）

- **Primary の抽出**: key = SHA-256（`evaluation version|experiment seed|S0|PRIMARY|code`）の小さい順に 60（bottom-k の一様抽出）。key に入るのはこれだけ（route・価格・outcome・回答は入らない）。selection probability = min(1, 60 / 通過数)。
- **Control の数**: Primary が 60 未満の日は Primary 3 件に 1 件（切り上げ、最大 20）で 75 : 25 を保つ。対応させる Primary は hash（`ANCHOR`）の順。
- **Control の match**: 価格帯（0–500 / 500–1000 / 1000–2000 / 2000–3000）と流動性帯（その日の適格銘柄の 20 日平均売買代金の四分位）が同じ中で sector も同じもの → 価格帯と流動性帯 → 価格帯 → 流動性帯 → その他、の順。同じ段では hash（`CONTROL`）の小さい順。sector は価格・流動性の一致の中での優先にすぎないので、sector の不一致で sample は減らない。
- **匿名化 / drift**: その日の main の 10% / 5%（切り捨て、最大 8 / 4）、互いに重ならない、hash（`SENSITIVITY`）の順。
- **送信窓**: S0 の足が確定する時刻（session 終了 + 遅延 20 分の 2 倍 = 16:10 JST。薄い銘柄の最終約定も終値とみなせる、D-262）から、翌平日 09:00 JST（祝日なら実際の S1 はもっと後なので、早い側に倒す）まで。窓の外の plan・送信はしない。
- **universe の読み込み**: 95% 未満なら、その日は抽出しない（偏った部分集合から選ばない）。1 銘柄の失敗（Yahoo の拒否、通信の timeout・切断）はその銘柄だけの失敗として記録する。履歴は S0 − 400 日から（75 本の warm-up と 60 セッションの窓、平滑化指標の立ち上がりを十分に過ぎる）。
- **目標 25 営業日**: 全件を送った日（`stage-run.json`）が 25 に達したら新しい日を plan しない（$2.50 cap より先に止まる。止まった日・部分的な日は数えない）。
- **停止**: 版の違う回答 → その日と cohort を停止。それ以外のエラー・予算・窓・失効 → その日だけ停止（再開しない。部分的に送った日は集計に含めない）。
- **credit の失効日**: 表示された日付の 00:00 UTC として扱う（早い側）。
- **1 日の budget**: $0.12 / 92 件（1 日 ≈ 見積もり $0.10、実費 ≈ $0.087）。
- **Phase B の outcome**: T+20 が確定した後、その日の銘柄の日足を改めて読んで計算する（読み直した S0 終値が基準と合わない銘柄があれば、その日の outcome は凍結しない）。

### 11-5. テスト（synthetic・offline、`workers/tests/test_evaluation_phase_b.py` 19 件 + `test_evaluation.py` 56 件）

- 1 日 = Primary 60 / Control 20 / 匿名化 8 / drift 4 / **92 request**、同じ date・version・seed なら入力の順序によらず同じ 80、seed・date・version が違えば違う抽出。key の値、下位 60 が選ばれること、全候補（通過 300・非通過 200）と route membership の保存。
- 60 以下なら全件（probability 1.0）、Control は 3 : 1。
- **D-only を除外しない**: route を付け替えても同じ銘柄が選ばれる（抽出は route を読まない）、D-only の probability は他と同じ、D-only が実際に選ばれる。
- Control: 価格帯・流動性帯の一致、sector の優先、sector が合わなくても 20 件、同点は hash の小さい方。
- **2026-09-24 より前の S0・週末を拒否**、16:10 JST 前と翌 09:00 JST 以降の plan を拒否。
- end to end（170 銘柄）: plan → build → preflight → run で **92 件すべて `jev-1.13.0` を指定・`jev-1.13.0` が回答**、5 秒間隔、network 接続なし（socket を遮断）、repository に何も書かない、評価ルートの外に何も書かない、key を書かない、`teacher_admissible = false`。
- `--send` なしでは送らない。窓が開いていなければ何もしない。
- **版の変化**（4 件目が `jev-1.14.0`）でその日と cohort が止まり、以後の plan / preflight / run を拒否。
- Gateway は Phase B の cohort に入れない。
- **$2.50 cap**: cap を超える日は preflight で ready にならず送らない、preflight の後に増えた支出も送信前に止める、ちょうど $2.50 は可。
- 09:00 JST を跨ぐ run は 08:59:55 までの 4 件で止まる。credit の失効を跨ぐ run は失効前の 3 件で止まり、新しい snapshot まで ready にならない。snapshot は失効日まで有効（29 日後でも有効）。
- 凍結: question schema や `routes/engine.py` が変われば拒否。
- 1 銘柄の通信エラーはその銘柄の失敗として記録して日は続く。95% 未満なら何も書かずに拒否。全件送信済みの日が 25 ある cohort は新しい日を plan しない（止まった日は数えない）。
- T+20 前は outcome を凍結しない、後は凍結して Route D subgroup 別の report を作る。
- **DB に書けない**: 評価のコードは DB の module を import しない（間接の import も含む）。

### 11-6. 実データでの予行（2026-09-19、cohort なし・Jev への送信なし）

plan-day と同じ読み込み・screening・抽出を、直近の session **S0 = 2026-09-18** で全 universe に対して行った（開始日の制約だけこの process 内で外した予行で、Phase B の sample ではない。記録は評価ルートの `phase-b-rehearsal-*`、要約のみ）。

| 項目 | 結果 |
|---|---|
| universe | JPX 一覧の内国普通株 **3,700**、Yahoo の履歴 **3,700 / 3,700**（失敗 0、coverage 100%）、S0 はデータ上 session |
| 所要時間 | **28.4 分**（1 銘柄 約 0.46 秒。0.3 秒の間隔を含む）。16:10 JST に始めれば 16:40 ごろに抽出まで終わる |
| 価格の保存量 | 圧縮前 60 MB / 日（gzip で保存） |
| 適格 | **2,867**（3,000 円超 830、S0 に足なし 3） |
| 通過 | **2,066（適格の 72.1%）**。route 別: D 1,235・B 838・A 616・H 221・C 157・G 100・F 97・E 6。**D-only 713**（通過の 34.5%）、D+other 522、non-D 831 |
| 抽出 | Primary **60**（probability 0.029）: D-only 23 / D+other 13 / non-D 24（通過の構成比どおり）。Control **20**: 価格帯+流動性帯+sector 18、価格帯+流動性帯 2（sector で sample は減っていない）。匿名化 8・drift 4 → **92 request** |

- Phase A の replay（61.2%）より通過率が高い日だったが、上限 92 の設計はそのまま成り立つ。
- peak memory の計測は失敗（0 を返した）ので未計測。履歴は 1 銘柄ずつ読んで screening し、保持しない作り。

### 11-7. まだしていないこと

- cohort の作成（`phase-b-init`）と 2026-09-24 分の送信。OS の scheduler への登録もしていない（登録すると 9/24 に自動送信され得るため）。
- 開始するときの手順: `phase-b-init --cohort-id <id> --experiment-seed <seed>` → 9/24 16:10 JST 以降に `phase-b-day --cohort-id <id> --s0 2026-09-24`（preflight まで）→ 確認して `--send`。
- credit snapshot は記録済み（2026-09-19 08:43:13Z の console 読み取り: Credit Balance $5.00、Monthly credit、Expires (UTC) Oct 19, 2026）。10-19 00:00 UTC を跨ぐ前に新しい残高の確認が一度必要（Phase B の 17 営業日目ごろ）。
