# Jev 予測性能評価 設計

状態: **v1.1（ユーザー確定 2026-09-18、Phase A の規模と送信間隔は D-274 で変更）** — Phase A は縮小 run（24 request、5 分間隔）で end-to-end 検証中
前提: [D-267](../unresolved-decisions.md) EOD Prediction 規則（S0 確定終値・目標 ×1.20・S1 から評価・T+1/3/5/10/20・期限 T+20）/ D-269（Vercel AI Gateway 経由の Jev は技術的に適合、production default ではない）/ D-268（+20% 到達の判定基準は未決）/ D-270（本設計の確定）

## 0. 目的と範囲

- **目的**: Jev の型付き判断（decision・`reaches_target` 確率・upside score・根拠 noul）と、実際の T+20 outcome の関係を測る。
- **基本は 1 銘柄 1 回**。
- **実装しないもの**: Jev の production default 化 / 同一銘柄の複数回推論（本番仕様としての平均化）/ confidence による自動 REJECT / 確率を売買確率として解釈すること。

## 1. retrospective と prospective の分離

Jev の training cutoff を信頼できる形で確定できず、過去の outcome をモデルが知っている可能性を排除できない。**過去データだけで採用判断はしない。**

| | Phase A: retrospective pilot | Phase B: prospective evaluation |
|---|---|---|
| S0 | 2025-01-01〜2026-06-30 から point-in-time に抽出 | **2026-09-21 以降**に新しく発生する候補（shadow prediction） |
| 件数 | **main 20（Primary 15 / Control 5）+ 匿名化 2 + drift 2 = 24 request**（正式な母集団 75 / 25 から固定 seed で縮小、D-274。当初は 100 件） | **1,500〜2,000 件** |
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
- **Phase B**: 1,500〜2,000 件。
- 現在の実測 Gateway cost（1 件 ≈ $0.00092、D-269）を基準に、**run ごとに hard budget**（金額と件数）を設定し、超える前に止める。
- **Vercel AI Gateway の無料 $5 credit を超える実行はしない。** paid credit の購入・auto-reload はしない（Auto-reload は Off を確認済み）。実行前に Gateway の残高を確認する。

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

### 9-5. Phase B の前に決めること（D-272）

Phase A の replay では、現行 Routes A–H が適格銘柄の約 61% を通した（Route D 単独で 35%）。Phase B の前に、1 日あたりの最大 shadow prediction 数、上限を超えたときの sampling / selection、Route D の候補生成への寄与の扱いを決める。Phase A では投資ロジックと route 閾値を変えない。
