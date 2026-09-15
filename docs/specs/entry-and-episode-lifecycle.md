# Entry / Watch / Episode ライフサイクル仕様

状態: **Phase 0.1 監査指摘 #6・#7・#8・#9・#12 を反映した仕様草案** — 2026-09-15
正本の優先順位: 監査原文（[audit-2026-09-15-phase-0.1.original.txt](../requirements/audit-2026-09-15-phase-0.1.original.txt)）> 実装指示書 v1.0 > v5.1。
本書のうち「Claude Code 解釈（要確認）」と明記した箇所は、監査・ユーザー承認前の暫定仕様である。

---

## 1. 時刻と価格の定義

| 名前 | 定義 |
|---|---|
| `signal_cutoff_at` | セットアップを検出した分析が使ったデータの時刻上限（EOD 分析なら当該セッションの価格データ上限） |
| `signal_reference_price` | セットアップ検出時点の参照価格（EOD なら当日終値・無調整）。**記録用。成績の基準には使わない** |
| `decision_cutoff_at` | ENTRY 判断（再分析）が使ったデータの時刻上限 |
| `entry_reference_price` | `decision_cutoff_at` 以前に観測された、ENTRY 判断時点の実際の価格。**+20% Threshold の唯一の基準** |
| `entry_price_observed_at` | `entry_reference_price` の観測時刻（`<= decision_cutoff_at`） |
| `entry_price_basis` | LAST_TRADE / MINUTE_CLOSE / ASK / MID 等（どれを使うかは D-01a） |
| `fx_observed_at` | 米国株の円換算に使った USD/JPY の観測時刻（**`<= decision_cutoff_at` 必須**） |
| Prediction の `data_cutoff` | = `decision_cutoff_at` |

不変条件:

- `threshold_20 = entry_reference_price × 1.20`（`signal_reference_price` や Watch の価格は使わない）
- `entry_price_observed_at <= decision_cutoff_at`
- `fx_observed_at <= decision_cutoff_at`（米国株の判定・Prediction すべて）
- `decision_cutoff_at` は対象市場の取引可能時間内（どの時間帯を取引可能とするかは D-01b）

## 2. 判定状態

| 状態 | 作成できる分析 | Prediction か |
|---|---|---|
| `SETUP_EOD` | EOD 分析 | いいえ（次の取引可能時点で再分析する対象） |
| `WATCH_BREAKOUT` / `WATCH_PULLBACK` / `WATCH_OTHER` | EOD 分析・場中分析 | いいえ |
| `REJECT` | すべて | いいえ |
| `ENTRY` | **場中の ENTRY 判断分析のみ**（`analysis_kind = ENTRY_DECISION` または `REANALYSIS`） | **はい（Episode を開く、または既存 Episode の再評価として記録）** |

- **EOD 分析は `ENTRY` を出力できない。** EOD で「今すぐ買える形」に見えても `SETUP_EOD` として保存する。
- DB 制約: `prod.predictions.analysis_id` は `analysis_kind IN ('ENTRY_DECISION','REANALYSIS')` の分析のみ参照可能。

## 3. フロー

```
[EOD batch]  Universe → Stage1 (Technical ∪ Material) → Stage2（分足取得）→ Stage3 (EOD分析)
                 │
                 ├─ SETUP_EOD ─────────────┐
                 ├─ WATCH_* ─────┐         │
                 └─ REJECT       │         │
                                 ▼         ▼
[場中]            watch_monitor（分足）   entry_decision（次の取引可能時点）
                    │ 条件到達                 │
                    ▼                          │
                 TRIGGER_HIT                   │
                    ▼                          ▼
                 REANALYSIS ──────────────▶ 判断
                                               ├─ ENTRY → Prediction（entry_reference_price を観測）→ Episode OPEN
                                               ├─ WATCH_*（条件付きへ変更）
                                               ├─ REJECT / FAILED_BREAKOUT
                                               └─ EXPIRED（有効期限切れ、D-20）
```

### 3-1. ENTRY 判断（再分析）で使う入力

- 当日セッション開始から `decision_cutoff_at` までの分足（寄り付きのギャップ、VWAP、上ヒゲ、出来高の進み具合）
- `first_seen_at <= decision_cutoff_at` の材料（EOD 以降に出た新規材料を含む）
- 市場地合い、支持抵抗、希薄化リスク
- `signal_reference_price` からの乖離（参考情報。基準価格ではない）

### 3-2. Watch 条件到達

- 条件（例: 1,050円突破）を分足で検出したら `TRIGGER_HIT`（`occurred_at` = 足の時刻、`detected_at` = 検出時刻）を記録する。
- **`TRIGGER_HIT` だけでは ENTRY にならない。** 必ず `REANALYSIS` を実行し、その判断が ENTRY のときだけ Prediction を作る。
- DB 制約: Watch 由来の Prediction は、`TRIGGER_HIT` 以後に作られた `REANALYSIS` 分析を参照していなければ挿入できない。

### 3-3. 材料と価格カットオフ（Claude Code 解釈・要確認 D-19）

EOD バッチは市場の引け後に動くため、「価格データの上限」と「材料データの上限」がずれる。

| 列 | 意味 |
|---|---|
| `price_cutoff_at` | EOD 分析が使う価格の上限（例: JP 15:30 JST の引け） |
| `material_cutoff_at` | EOD バッチが材料を読んだ時刻上限（バッチ開始時刻） |

- `first_seen_at > price_cutoff_at` の材料は、**EOD 価格に対して「未織り込み」と評価してはならない**（`priced_in_status = UNKNOWN_UNTIL_NEXT_SESSION`）。
- ただしその材料を理由に `SETUP_EOD` を作ること（翌セッションの再分析に回すこと）は禁止しない。
- その材料の織り込み度は、翌セッションの ENTRY 判断で `decision_cutoff_at` 以前の価格反応を使って評価する。

## 4. 分足の取得範囲（監査 #8）

| 対象 | 日足 | 分足 |
|---|---|---|
| 全 Universe | ○ | × |
| Stage 2 候補 | ○ | ○（分析用の直近期間。期間は D-08a） |
| SETUP_EOD / Watch 銘柄 | ○ | ○（場中監視） |
| ENTRY 判断対象 | ○ | ○ |
| Open Episode の銘柄 | ○ | ○（パス解決用） |

全銘柄の分足保存を前提にしない。

## 5. 3,000円 Hard Filter の再判定（Claude Code 解釈・要確認 D-18）

- Universe 段階（EOD）: 終値（無調整）と `fx_observed_at <= price_cutoff_at` の USD/JPY で判定。
- ENTRY 判断時: `entry_reference_price` と `fx_observed_at <= decision_cutoff_at` の USD/JPY で**再判定**し、3,000円を超えていれば ENTRY にしない（`REJECT`、理由 `HARD_FILTER_AT_ENTRY`）。

## 6. Prediction Episode（監査 #12）

### 6-1. 定義

- 同一銘柄・同一仮説（`thesis_key`）について、**初回 ENTRY から、Target / Failure / Thesis invalidation / Horizon end のいずれかまで**を 1 Episode とする。
- 成績（成功数・失敗数・的中率など）は **Episode 単位**で数える。Prediction 行の数では数えない。

### 6-2. テーブル（append-only）

| テーブル | 内容 |
|---|---|
| `prod.prediction_episodes` | episode_id, security_id, thesis_key, opening_prediction_id, opened_at, horizon_end_at |
| `prod.episode_closures` | episode_id, close_reason, closed_at, evidence, run_id（1 Episode につき 1 行、一意制約） |
| `prod.state_transitions` | Episode 中の再評価（`REAFFIRMED` / `DOWNGRADED` / `THESIS_WEAKENED` など）を含む全遷移 |

`close_reason`: `TARGET_HIT` / `FAILURE_HIT` / `THESIS_INVALIDATED` / `HORIZON_END` / `AMBIGUOUS_PATH` / `UNRESOLVED_MISSING_DATA`

### 6-3. ルール

1. Open Episode がある銘柄・同一 `thesis_key` について再分析が ENTRY 相当と判断しても、**新しい Prediction を作らず** `REAFFIRMED` の State Transition として保存する。
2. Episode の Objective 評価（Target / Failure 到達）は **開始 Prediction の `entry_reference_price` と `failure_line`** で行う。Episode 中に再分析が失敗ラインの見直しを提案した場合は State Transition として記録する（Claude Code 解釈・要確認 D-17c）。
3. `thesis_key` の定義、Open Episode 中に別仮説が出た場合、クローズ後の再 ENTRY の扱いは D-17a / D-17b。
4. Horizon は指示書 §28 の最大追跡期間 20 取引日を暫定値とする（D-17c）。
5. Outcome の 1/3/5/10/20D 追跡は Episode クローズ後も研究用に継続する（Episode の結果は変えない）。

## 7. パス解決と `AMBIGUOUS_PATH`（監査 #9）

Target（`threshold_20`）と Failure（`failure_line`）のどちらに先に触れたかを、次の順で判定する。

1. **エントリー当日**: `entry_price_observed_at` より後の分足のみを使う。
2. **翌日以降**: 各日の日足を見る。
   - 始値が Failure 以下 → `FAILURE_FIRST`（始値で確定）
   - 始値が Target 以上 → `TARGET_FIRST`（始値で確定）
   - 高値 ≥ Target かつ 安値 ≤ Failure → 当日の分足で判定（3へ）
   - 片方だけ → その方
3. **分足での判定**: 最初にどちらかに触れた分足を探す。
   - 1本の分足の中で両方に触れ、それより細かい足で順序を解決できない → **`AMBIGUOUS_PATH`**
   - 分足データが欠損して判定できない → `UNRESOLVED_MISSING_DATA`（D-09a）
4. `AMBIGUOUS_PATH` / `UNRESOLVED_MISSING_DATA` の Episode は**成功にも失敗にも数えない**。件数は別に集計する。

保存項目: `path_resolution`（TARGET_FIRST / FAILURE_FIRST / AMBIGUOUS_PATH / UNRESOLVED_MISSING_DATA / NEITHER_BY_HORIZON）、`resolution_granularity`（DAY_OPEN / DAY / MINUTE）、`resolved_at_bar_ts`。
