# EOD Prediction 仕様

状態: **eod-prediction-1.0.0** — 2026-09-18
根拠: [ユーザー決定: EOD Prediction 規則（原文）](../requirements/user-decision-2026-09-18-eod-prediction-rules.original.txt) > [ユーザー決定: 前提修正（原文）](../requirements/user-decision-2026-09-18-eod-premise.original.txt) > 旧 addenda > v5.1
LLM 向けの要約: [v5.1-addendum-2026-09-18-eod-prediction.md](../prompts/addenda/v5.1-addendum-2026-09-18-eod-prediction.md)
決定: [D-261 / D-262 / D-263 / D-267 / D-268](../unresolved-decisions.md)

## 1. いつ、何を基準に予測するか

- **S0** = 予測対象日（その取引セッション）。
- Prediction は **S0 の終値が確定した後**に実行する。
- 基準価格は **S0 の確定終値** = `signal_reference_price`（v5.1 の「分析基準価格」）。判断（`decision_completed_at`）より前の market event でよい。
- 「確定」の条件（D-262）: **S0 のセッション終了 + feed の公表遅延の後に取得した値**であること。JP は Yahoo 日足（東証 15:30 終了 + 20 分）、US は Alpaca SIP 日足（16:00 ET + 15 分 + 1 分）。取得の provider / feed / basis / セッション終了時刻 / 取得時刻 / 遅延を価格と一緒に保存する。
- 時刻の順序: `S0 のセッション終了 <= data_cutoff <= decision_completed_at`、かつ `セッション終了 + 遅延 <= close_fetched_at <= decision_completed_at`。

## 2. 3,000円 Hard Filter と目標価格

- **3,000円 Hard Filter は S0 の確定終値で判定**する。超えていれば Prediction を作らない。
  - 米国株は円換算して判定する。レートは **S0 の終値以前に観測した** USD/JPY（CLAUDE.md 1-7: EOD は `fx_observed_at <= price_cutoff_at`）。+20% と価格 Outcome は USD 建てのまま（CLAUDE.md 1-9）。
- **目標価格 = S0 確定終値 × 1.20**。入力ではなく計算値。

## 3. 評価の窓

| 項目 | 値 |
|---|---|
| 評価開始 | **S1**（翌営業日）。S0 は終値で終わっているので、S0 の値動きは評価に入らない |
| outcome checkpoint | **T+1 / T+3 / T+5 / T+10 / T+20**（T+n = Sn） |
| 最終評価期限 | **T+20 営業日**（= S20 の終値まで） |

- セッションは実際に立ったものを数える（D-142: 検証済み取引カレンダーが無いので、休場日を推測して境界を作らない）。

## 4. Outcome 側で未決のもの（Prediction 生成の blocker ではない）

- **+20% 到達を intraday high と close のどちらで判定するか（D-268）。** 参考: 現行の teacher-labels 仕様の Objective `hit_20` は「各 horizon 内の comparable 高値が基準価格 ×1.20 以上」で定義されている。EOD Prediction の Outcome でどちらを採るかはユーザー判断で、ここでは決めない。
- **`success_label` と +20% 到達（`hit_20`、指示文では `hit_20_percent`）は同義にしない。** 既存の定義（Interpretive の成功ラベル群と Objective の到達指標）をそのまま保つ。
- `initial_failure_line` を EOD Prediction で必須にするか（CLAUDE.md 1-6 は作成時に固定する）。DB では与えられた場合だけ検査し、NULL を許す（あとで NOT NULL にできる、D-267）。
- checkpoint ごとの Outcome を記録する table と engine は Outcome 側の実装で作る。

## 5. 場中 ENTRY との関係

- 場中 ENTRY と `entry_reference_price`（判断後に取引可能だった価格）の規則・table（`prod.entry_attempts` / `prod.predictions` / `prod.episodes`）は、将来の実売買検証（`execution_price` / `next_session_entry_price`、翌営業日の寄り付き後など、未実装）の設計として**そのまま残す**。名称も制約も変えない（D-263）。
- EOD Prediction は別の table（`prod.eod_predictions`）に入れる。場中の table の制約（判断より後に観測した価格、attempt / episode との一致）を緩めずに済ませるため。

## 6. 実装

| 層 | 場所 |
|---|---|
| DB 制約 | `supabase/migrations/20260918030000_eod_predictions.sql`（`prod.eod_predictions`、append-only） |
| 規則（Python） | `workers/src/surge/entry/eod_prediction.py`（`eod_prediction_terms()`） |
| 確定終値 | `workers/src/surge/entry/session_close.py` / `providers/yahoo_finance.py` / `alpaca_historical.session_close` |
