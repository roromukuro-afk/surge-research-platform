# 決定事項・未決事項（Decision Log）

行は削除しない。決定したら「決定済み」へ移し、日付・決定者・内容・根拠文書を書く。
種別: 投資ロジック（ユーザー/ChatGPT 判断必須） / 技術 / 費用 / 運用 / 規約

---

## A. 決定済み

| ID | 論点 | 決定内容 | 決定日 / 決定者 | 根拠 |
|---|---|---|---|---|
| D-01 | Entry の基準価格 | signal / decision / entry を分離。成績・Threshold は `entry_reference_price`（判断後に現実に取引可能だったとみなす評価価格）。`actual_fill` は任意。算出方式は D-01a | 2026-09-15 / ChatGPT 監査 0.1 #6 → **0.2 #3 で更新** | audit 0.1 #6, 0.2 #3 |
| D-02 | USD/JPY の時点 | `fx_observed_at <= decision_cutoff_at` 必須 | 2026-09-15 / 監査 0.1 #7 | audit 0.1 #7 |
| D-03 | ストレージ設計 | Postgres = 状態・索引・結果、大量の履歴 = Parquet + Object Storage | 2026-09-15 / 監査 0.1 #3 | audit 0.1 #3 |
| D-05 | Worker 実行環境 | `JobRunner` / `Scheduler` で抽象化 | 2026-09-15 / 監査 0.1 #4 | audit 0.1 #4 |
| D-06 | J-Quants の位置付け | Free = 開発用、Production EOD は Light 以上を候補、Standard は必須にしない。**分足・ティックは日次16:30頃更新のため場中 ENTRY 判断に使わず、Historical research / EOD / Replay / Teacher data 用に限る** | 2026-09-15 / 監査 0.1 #15、**0.2 #2** | audit 0.2 #2。公式の更新スケジュールで 16:30頃（確約ではない）を Claude Code も確認 |
| D-07 | 米国株データの選び方 | `MarketDataProvider`、用途別 Provider、公式仕様のみで比較 | 2026-09-15 / 監査 0.1 #14 | audit 0.1 #14 |
| D-08 | 分足の要否 | 全 Universe は日足、Stage 2・Watch・ENTRY 候補のみ分足 | 2026-09-15 / 監査 0.1 #8 | audit 0.1 #8 |
| D-09 | 同一足で両方に触れた場合 | `AMBIGUOUS_PATH` | 2026-09-15 / 監査 0.1 #9 | audit 0.1 #9 |
| D-09a | データ欠損の扱い | 降りられないデータ不足は `UNRESOLVED_MISSING_DATA`。判定手順は 始値 → 日足 → 分足 → 約定 → AMBIGUOUS_PATH | 2026-09-15 / **監査 0.2 #11** | audit 0.2 #11 |
| D-10 | Universe の範囲 | `universe-1.0.0` | 2026-09-15 / 監査 0.1 #10 | audit 0.1 #10 |
| D-13 | 教師ラベルの構造 | Objective / Interpretive | 2026-09-15 / 監査 0.1 #11 | audit 0.1 #11 |
| D-17 | 同一銘柄の重複 ENTRY | Prediction Episode | 2026-09-15 / 監査 0.1 #12 | audit 0.1 #12 |
| D-17c | Horizon と Failure Line | Horizon = ENTRY 成立から 20 trading sessions（Watch 開始から数えない、State Update でリセットしない、新 Episode のみ新 Horizon）。`initial_failure_line`（固定・Primary）と `current_risk_line`（変更可・研究用） | 2026-09-15 / **監査 0.2 #6・#7** | audit 0.2 #6, #7 |
| D-18 | ENTRY 時の3,000円再判定 | 再判定する。Setup/Watch 時に3,000円以下でも、ENTRY 判断時に超えていれば Prediction を作らない | 2026-09-15 / **監査 0.2 #4** | audit 0.2 #4 |
| D-19 | 引け後に取得した材料 | `TECHNICAL_SETUP_EOD` と `POST_CLOSE_CATALYST_SETUP` を分離。引け後の材料を EOD 価格の未織り込み評価に使わない | 2026-09-15 / **監査 0.2 #5** | audit 0.2 #5 |
| D-25 | 米国株の Outcome 通貨 | Eligibility = JPY 換算、Threshold・価格 Outcome = USD、JPY リターンは補助 | 2026-09-15 / **監査 0.2 #8** | audit 0.2 #8 |
| D-27 | 材料の利用可能時刻 | `source_published_at` / `system_first_seen_at` / `ingested_at` / `available_to_model_at`。Production・Replay は `available_to_model_at <= decision_cutoff_at` のみ。backfill で過去に遡らない | 2026-09-15 / **監査 0.2 #9** | audit 0.2 #9 |
| D-28 | 分割・併合・配当 | raw を保存、Outcome は比較可能な系列、配当は Target に加算しない | 2026-09-15 / **監査 0.2 #10** | audit 0.2 #10 |
| D-29 | 過去急騰高値の役割 | 上昇根拠・Potential Upside への使用は禁止。Resistance / Supply Overhang / 戻り売り候補 / 高値掴み保有者 / 障害としては積極的に使う | 2026-09-15 / **監査 0.2 #1** | audit 0.2 #1 |
| D-30 | v5.1 の管理 | 原文ファイルとして受領後に登録。Canonical v5.1（immutable original）と post-v5.1 decisions（versioned addenda）を混ぜない | 2026-09-15 / **監査 0.2 #12** | audit 0.2 #12 |

---

## B. 未決（Remaining）

### B-1. Phase 1 の前に必要

| ID | 種別 | 論点 | 事実・選択肢 | Claude Code の意見 |
|---|---|---|---|---|
| **D-00** | 前提 | **v5.1 原文ファイルの受領** | チャットへの貼り付けではなく、原文ファイルとして受け取る（監査 0.2 #12） | 受領後、無加工で保存し SHA-256 を登録 |
| D-03a | 費用 | Supabase のプラン | Free: DB 500MB 超過で read-only、7日間低アクティビティで自動停止、Free は合計2プロジェクトまで。Pro: 自動停止なし | 日次運用では自動停止が問題になりうる |
| D-10a | 投資ロジック | 「適格 ADR」の定義 | — | 判断しない |
| D-10b | 投資ロジック/技術 | JP の普通株判定（J-Quants `ProdCat` の値）、東証上場の外国株式、出資証券・優先出資証券、`0109 その他` | `ProdCat` のコード値ページは未確認 | Phase 1 の最初に値を確認して提示 |
| D-10c | 投資ロジック/技術 | 米国 REIT の判定方法 | SEC SIC 6798 は確認済み。網羅性は未確認 | — |
| D-10d | 投資ロジック | NYSE Arca / Cboe BZX / IEX にのみ上場する普通株 | 監査の取引所リスト外 | 判断しない |
| D-10e | 投資ロジック | 売買停止・監理/整理・Nasdaq Financial Status 異常 | universe-1.0.0 では未規定 | 判断しない |
| D-14 | 運用 | 認証方式、障害通知、ステージング DB | — | Supabase Auth + 本人メールの allowlist |
| D-16 | 運用 | プロジェクト名（仮 `surge-research-platform`） | — | 仮名のまま |
| D-22 | 規約/運用 | 指示書・監査の原文ファイル | `*.original.txt` はチャット本文の転記。元ファイルとのバイト一致は未確認 | 元ファイルがあれば差し替え |

Phase 1 で使う Provider（JP マスタ = J-Quants、US マスタ = D-07a）は、開発用の無料枠で interface の実装を始められる。

### B-2. Phase 2〜8 の前に必要

| ID | 種別 | 論点 | 必要な時期 | Claude Code の意見 |
|---|---|---|---|---|
| D-01a | 投資ロジック | `entry_price_method`（判断完了後の最初の約定 / 判断完了後 N 分の VWAP / 判断完了時の売気配 など） | Phase 8 前 | **場中 Provider の能力（約定・気配の提供有無）を確認してから決める**（監査 0.2 #3） |
| D-01b | 投資ロジック | 「次の取引可能時点」の定義（寄り直後か寄り後 N 分か。PTS・米国プレマーケットを含めるか） | Phase 8 前 | 判断しない |
| D-02a | 技術/投資ロジック | FX の Provider と許容遅延 | Phase 2 前 | — |
| D-03b | 費用/技術 | Object Storage プロバイダ | Phase 2 前 | Phase 1 はローカル実装 |
| D-05a | 費用/技術 | Production の Runner・Scheduler（日次／場中監視／ニュース収集） | Phase 4・8 前 | 場中とニュース収集は常駐型が必要な見込み |
| D-06a | 費用 | J-Quants の Production プラン（Light / Premium）と分足・ティックアドオン（研究・パス解決用） | Phase 2 前 | v5.1 が前場・後場の四本値を要求するかで判断 |
| **D-06b** | 費用/技術 | **日本株の場中 ENTRY 判断・Watch 監視用のリアルタイム Provider**（J-Quants は対象外で確定） | Phase 8 前（調査は早めに） | 公式情報のみで候補を調査する。約定・気配の提供有無は D-01a にも影響する |
| D-07a | 費用 | 米国株の役割ごとの Provider | Phase 1〜2 | 未確認項目を API キーで確認 |
| D-08a | 投資ロジック | Stage 2 で取得する分足の期間 | Phase 6 前 | v5.1 を確認してから |
| D-09b | 投資ロジック | パス解決の手順 4 で、約定データを「構成上提供していない」場合（→ AMBIGUOUS_PATH）と「欠損」（→ UNRESOLVED_MISSING_DATA）を区別する解釈 | Phase 9 前 | Claude Code 解釈。確認を依頼 |
| D-11 | 費用/技術 | LLM プロバイダ・モデル・月額上限 | Phase 5〜7 前 | — |
| D-12 | 費用/規約 | ニュース・開示ソースの取得手段 | Phase 4 | Phase 4 冒頭で規約調査表を提出 |
| D-17a | 投資ロジック | `thesis_key`（「同一仮説」）の定義 | Phase 8 前 | 判断しない |
| D-17b | 投資ロジック | Open Episode 中の別仮説、クローズ後の再 ENTRY | Phase 8 前 | 判断しない |
| D-17d | 投資ロジック | 20 sessions の数え方（暫定: ENTRY 成立セッションを S0、S20 の引けで終了） | Phase 8 前 | Claude Code 解釈。指示書 §28 の 1D〜20D と揃えるため。確認を依頼 |
| D-17e | 投資ロジック | `THESIS_INVALIDATED` で Episode をクローズしても Primary Outcome は Horizon 終了まで計算するか（暫定: 計算する） | Phase 9 前 | Claude Code 解釈。判断で計測を打ち切ると教師データが偏るため。確認を依頼 |
| D-20 | 投資ロジック | Setup（両種別）の有効期間 | Phase 8 前 | 判断しない |
| D-21 | 投資ロジック | 15分遅延データを `decision_price` / `entry_reference_price` に使うことを許すか | Phase 8 前 | 許す場合も `latency_class` を保存して区別すべき |
| D-23 | 投資ロジック | 見逃しの Research 判定で、backfill した情報（`available_to_model_at` が後日）を「当時市場で入手可能だった情報」として別扱いで使えるか（暫定: 使わない） | Phase 10 前 | 監査 0.2 #9 は Production と Replay を対象としており、見逃し判定への適用は明記されていないため確認を依頼 |
| D-24 | 投資ロジック | 分割・併合・配当以外の corporate action（スピンオフ、株主割当、合併・TOB による上場廃止など）の Outcome での扱い | Phase 9 前 | 判断しない |
| D-26 | 投資ロジック | 判断時は3,000円以下だったが、判断後の `entry_reference_price` が3,000円を超えた場合（暫定: Prediction は有効、`entry_reference_price_jpy` を記録） | Phase 8 前 | Claude Code 解釈。監査 0.2 #4 は「ENTRY 判断時」の再判定と読んだ。確認を依頼 |

### B-3. Phase 9 以降

| ID | 種別 | 論点 | 必要な時期 |
|---|---|---|---|
| D-04 | 費用/運用 | Vercel のプラン | Web 公開時 |
| D-13a | 投資ロジック | 12ラベルの分類と判定基準、`information_cutoff_at` の定義 | Phase 10 前 |
| D-13b | 投資ロジック | `label_admission_policy` の内容 | Phase 10 前 |
| D-15 | 運用 | Excel の受け取り方法 | Phase 9 |
