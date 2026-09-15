# 決定事項・未決事項（Decision Log）

行は削除しない。決定したら「決定済み」へ移し、日付・決定者・内容・根拠文書を書く。
種別: 投資ロジック（ユーザー/ChatGPT 判断必須） / 技術 / 費用 / 運用 / 規約

---

## A. 決定済み

| ID | 論点 | 決定内容 | 決定日 / 決定者 | 根拠 |
|---|---|---|---|---|
| D-01 | Entry Reference Price の定義 | `signal_reference_price` と `entry_reference_price` を分離。EOD のセットアップは `SETUP_EOD` / WATCH。次の取引可能時点の再分析で ENTRY 可能と判断した場合のみ Prediction。Threshold は `entry_reference_price` から計算 | 2026-09-15 / ChatGPT 監査（Phase 0.1 #6） | audit 原文 #6 |
| D-02 | USD/JPY の時点 | Entry/判定時点と同期。`fx_observed_at <= decision_cutoff_at` 必須 | 2026-09-15 / ChatGPT 監査 #7 | audit 原文 #7 |
| D-03 | ストレージ設計 | Postgres = 状態・索引・結果。大量の履歴 = Parquet + Object Storage。プロバイダは固定しない | 2026-09-15 / ChatGPT 監査 #3 | audit 原文 #3（プラン選択は D-03a、プロバイダは D-03b に分離） |
| D-05 | Worker 実行環境 | `JobRunner` / `Scheduler` interface で抽象化し、GitHub Actions に固定しない | 2026-09-15 / ChatGPT 監査 #4 | audit 原文 #4（実装の選択は D-05a） |
| D-06 | J-Quants の位置付け | Provider として抽象化。Free = 開発用、Production EOD は Light 以上を候補。Standard は信用データ等の有効性が教師データで確認されるまで必須にしない | 2026-09-15 / ChatGPT 監査 #15 | audit 原文 #15 |
| D-07 | 米国株データの選び方 | `MarketDataProvider` interface。EOD と Watch/ENTRY 用リアルタイムを別 Provider にできる。比較は公式仕様のみ | 2026-09-15 / ChatGPT 監査 #14 | audit 原文 #14 |
| D-08 | 分足の要否 | 全 Universe は日足、Stage 2・Watch・ENTRY 候補のみ分足 | 2026-09-15 / ChatGPT 監査 #8 | audit 原文 #8 |
| D-09 | 同一足で Target と Failure の両方に触れた場合 | 最小足で順序が解決不能なら `AMBIGUOUS_PATH`。成功にも失敗にも分類しない | 2026-09-15 / ChatGPT 監査 #9 | audit 原文 #9 |
| D-10 | Universe の範囲 | JP: Prime/Standard/Growth 普通株。US: NYSE/Nasdaq/NYSE American Common Stocks + 適格ADR。原則除外: ETF/REIT/Preferred/Warrant/Unit/Right/OTC/pre-merger SPAC/TOKYO PRO。版管理 | 2026-09-15 / ChatGPT 監査 #10 | audit 原文 #10（細部は D-10a〜e） |
| D-13 | 教師ラベルの構造 | Objective / Interpretive に分離。Interpretive に labeler_model_version / confidence / evidence / human_review_status。低 confidence を無条件で Production ML 教師データに入れない | 2026-09-15 / ChatGPT 監査 #11 | audit 原文 #11（詳細は D-13a / D-13b） |
| D-17 | 同一銘柄の重複 ENTRY | Prediction Episode を導入。途中の再評価は State Transition | 2026-09-15 / ChatGPT 監査 #12 | audit 原文 #12（詳細は D-17a〜c） |

---

## B. 未決（Remaining）

### B-1. Phase 1 の前に必要

| ID | 種別 | 論点 | 選択肢・事実 | Claude Code の意見 |
|---|---|---|---|---|
| **D-00** | 前提 | **v5.1 原文の受領** | — | 受領後、無加工で保存し SHA-256 を登録 |
| D-03a | 費用 | Supabase のプラン | Free: DB 500MB 超過で read-only、7日間低アクティビティで自動停止、Free プロジェクトは合計2つまで。Pro: 自動停止なし・ディスク 8GB 込み | 大量データを Parquet に出したので Postgres は小さくなる。ただし自動停止は日次運用と相性が悪い |
| D-03b | 費用/技術 | Object Storage プロバイダ | R2 / B2 / Supabase Storage / S3（[storage-architecture.md §6](storage-architecture.md)） | Phase 2〜3 の実測量で決める。Phase 1 はローカル実装で interface を固める |
| D-06a | 費用 | J-Quants の Production プラン | Light（5年・60回/分）/ Premium（前場・後場四本値）+ 分足アドオン | 判断材料: v5.1 が前場・後場の四本値を要求するか |
| D-07a | 費用 | 米国株の役割ごとの Provider（マスタ / EOD / 分足履歴 / リアルタイム） | [provider-comparison.md](provider-comparison.md) | 未確認項目（Ticker Types の値、Alpaca のマスタ API 等）を API キーで確認してから |
| D-10a | 投資ロジック | 「適格 ADR」の定義 | — | 判断しない |
| D-10b | 投資ロジック/技術 | JP の普通株の判定（J-Quants `ProdCat` の値）、東証上場の外国株式、出資証券・優先出資証券、`0109 その他` | `ProdCat` のコード値ページは未確認 | 値を確認してから提示 |
| D-10c | 投資ロジック/技術 | 米国 REIT の判定方法 | SEC SIC 6798 は確認済み。SIC だけで網羅できるかは未確認 | — |
| D-10d | 投資ロジック | NYSE Arca / Cboe BZX / IEX にのみ上場する普通株の扱い | 監査の取引所リストに含まれない | 判断しない |
| D-10e | 投資ロジック | 売買停止・監理/整理銘柄・Nasdaq Financial Status 異常の扱い | universe-1.0.0 では未規定 | 判断しない |
| D-14 | 運用 | 認証方式、障害通知、ステージング DB の有無 | — | Supabase Auth + 本人メールの allowlist、ステージングは持たない |
| D-16 | 運用 | プロジェクト名（仮 `surge-research-platform`） | — | 仮名のまま |
| D-22 | 規約/運用 | 実装指示書 v1.0 と監査結果の原文ファイル | 現在の `*.original.txt` は、チャットで受け取った本文を Claude Code が書き写したもの。元ファイルとバイト単位で一致するかは確認していない | 元ファイル（ChatGPT 出力など）があれば提供してもらい、差し替えて SHA-256 を更新する |

### B-2. Phase 2〜8 の前に必要

| ID | 種別 | 論点 | 必要な時期 | Claude Code の意見 |
|---|---|---|---|---|
| D-01a | 投資ロジック | `entry_price_basis`（直近約定 / 1分足終値 / 売気配 / 仲値） | Phase 8 前 | 判断しない。売気配は「実際に買える価格」に近いが、取得できる Provider が限られる |
| D-01b | 投資ロジック | 「次の取引可能時点」の定義（寄り付き直後か、寄り後 N 分か。PTS・米国のプレマーケットを含めるか） | Phase 8 前 | 判断しない |
| D-02a | 技術/投資ロジック | FX の Provider と許容遅延（`decision_cutoff_at` から何分前までの観測を有効とするか） | Phase 2 前 | — |
| D-05a | 費用/技術 | Production の Runner・Scheduler の実装（日次バッチ／場中監視／ニュース収集） | Phase 4・8 前 | 場中監視とニュース収集は常駐型（`QueueWorkerRunner`）が必要になる見込み |
| **D-06b** | 費用/技術 | **日本株のリアルタイム価格の入手手段**（場中の ENTRY 判断・Watch 監視用） | Phase 8 前（調査は早めに） | J-Quants 分足の提供タイミングは公式ページで確認できず。未調査の候補（証券会社 API、データベンダー等）を公式情報で調べる |
| D-08a | 投資ロジック | Stage 2 で取得する分足の期間（直近何日分か） | Phase 6 前 | v5.1 の記述を確認してから |
| D-09a | 投資ロジック | 分足欠損でパスを解決できない場合を `UNRESOLVED_MISSING_DATA` として別扱いにするか | Phase 9 前 | 監査の `AMBIGUOUS_PATH`（順序が解決不能）とは原因が違うため分けることを提案 |
| D-11 | 費用/技術 | LLM プロバイダ・モデル・月額上限 | Phase 5〜7 前 | 候補数を実測してから費用を試算 |
| D-12 | 費用/規約 | ニュース・開示ソースの取得手段、TDnet の取得手段 | Phase 4 | Phase 4 冒頭で規約調査表を提出 |
| D-17a | 投資ロジック | `thesis_key`（「同一仮説」）の定義 | Phase 8 前 | 判断しない |
| D-17b | 投資ロジック | Open Episode 中に別の仮説が出た場合、クローズ後の再 ENTRY | Phase 8 前 | 判断しない |
| D-17c | 投資ロジック | Horizon の長さ（暫定: 20取引日）、Episode 中に失敗ラインを見直す場合の扱い（暫定: 開始 Prediction の値で評価し、見直しは State Transition に記録） | Phase 8 前 | 暫定案の確認を依頼 |
| D-18 | 投資ロジック | ENTRY 判断時に `entry_reference_price` で3,000円 Hard Filter を再判定するか（暫定: 再判定する） | Phase 8 前 | Claude Code 解釈。確認を依頼 |
| D-19 | 投資ロジック | 価格カットオフ後に取得した材料の扱い（暫定: EOD の未織り込み評価には使わず `UNKNOWN_UNTIL_NEXT_SESSION`、ただし `SETUP_EOD` 作成の理由にはできる） | Phase 5・7 前 | Claude Code 解釈。確認を依頼 |
| D-20 | 投資ロジック | `SETUP_EOD` の有効期間（何セッションで失効するか） | Phase 8 前 | 判断しない |
| D-21 | 投資ロジック | 15分遅延データを `entry_reference_price` に使うことを許すか | Phase 8 前 | 遅延データは判断時点の価格ではないため、許す場合も `latency_class` を保存して区別すべき |

### B-3. Phase 9 以降

| ID | 種別 | 論点 | 必要な時期 |
|---|---|---|---|
| D-04 | 費用/運用 | Vercel のプラン | Web 公開時 |
| D-13a | 投資ロジック | 12ラベルの Objective / Interpretive への分類と各判定基準、`information_cutoff_at` の定義（暫定表は [teacher-labels.md](specs/teacher-labels.md)） | Phase 10 前 |
| D-13b | 投資ロジック | `label_admission_policy`（confidence の下限、人手レビュー必須のラベル、UNREVIEWED の扱い） | Phase 10 前 |
| D-15 | 運用 | Excel の受け取り方法 | Phase 9 |
