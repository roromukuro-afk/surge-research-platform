# 未決事項（Decision Needed）

決定したら「決定」欄に日付・決定者・内容を追記する。行は削除しない。

凡例: 種別 = 投資ロジック（ユーザー/ChatGPT 判断必須） / 技術 / 費用 / 運用

| ID | 種別 | 論点 | 選択肢 | Claude Code 推奨 | 必要な時期 | 決定 |
|---|---|---|---|---|---|---|
| D-00 | 前提 | **v5.1 原文の受領** | — | 受領後、無加工保存 + SHA-256 登録 | Phase 1 前（必須） | 未 |
| D-01 | 投資ロジック | **Entry Reference Price の定義**。日足バッチは引け後に分析するため、実際に買えるのは翌営業日以降 | (a) data_cutoff 時点の終値 (b) 翌営業日始値 (c) 分析完了時点の実勢価格（場中データが必要） (d) 指値価格を AI が提示し約定判定 | 判断しない（投資ロジック）。記録上は (b) の方が「実際に Entry 可能だった価格」に近く、(a) は楽観バイアスが入る点のみ指摘 | Phase 8 前 | 未 |
| D-02 | 投資ロジック | 米国株3,000円判定の **USD/JPY の時点** | (a) 米国終値時点の為替 (b) 日本時間の基準時刻の為替 (c) 仲値 | (a) が「同時点」に最も近い | Phase 2 前 | 未 |
| D-03 | 費用/技術 | **Supabase プラン・容量設計**。Free は DB 500MB で Phase 2〜3 に超過見込み。Free プロジェクト上限は2つ（既存プロジェクトで枠が埋まっている可能性） | (a) 最初から Pro（月額発生） (b) Phase 1 は Free、超過前に Pro (c) 長期履歴を Parquet で Storage に退避し Postgres を小さく保つ | (b) + 必要なら (c) 併用 | Phase 1 前 | 未 |
| D-04 | 費用/運用 | Vercel プラン（Hobby は商用不可規約・Cron 1日1回、Pro は有料） | Hobby / Pro | 非公開個人研究なので Hobby で開始、Web の cron は使わない | Phase 1 後半 | 未 |
| D-05 | 費用/技術 | **ニュース高頻度収集の実行環境**（`first_seen_at` 精度に直結） | (a) 小型常駐VM/コンテナ（Fly.io 等） (b) GitHub Actions 短間隔 (c) 自宅PC | (a) | Phase 4 前 | 未 |
| D-06 | 費用 | **J-Quants プラン** | Light ¥1,650（5年） / Standard ¥3,300（10年・信用取引データ） / Premium ¥16,500（20年・前場・分足/TDnetアドオン） | 需給分析に信用残を使うなら Standard 以上。分足/TDnet を J-Quants で賄うなら Premium | Phase 1 前 | 未 |
| D-07 | 費用 | **米国株の価格・浮動株データ源** | Massive(旧Polygon) / Tiingo / EODHD / その他 | 全銘柄一括日足・上場廃止銘柄履歴・分割情報の有無で比較表を作ってから提示 | Phase 1 前 | 未 |
| D-08 | 投資ロジック/費用 | **分足（場中データ）の要否**。§4-3「突破した瞬間に再分析」、VWAP、場中の上ヒゲは日足のみでは厳密に扱えない | (a) 初期は日足のみ・Watch判定は引け後 (b) 初期から分足 | 判断しない。(a) の場合「瞬間」ではなく「引け後判定」になる制約を明記 | Phase 8 前 | 未 |
| D-09 | 投資ロジック | **同一足で +20% と失敗ラインの両方に触れた場合** の `hit_20_before_failure` 判定 | (a) 保守的に失敗先行とみなす (b) 分足で判定 (c) 曖昧フラグを立てて学習から除外 | 判断しない | Phase 9 前 | 未 |
| D-10 | 投資ロジック | **「全上場普通株」の範囲** | JP: TOKYO PRO Market の扱い。US: ADR、OTC、SPAC、Financial Status 異常銘柄の扱い | 判断しない | Phase 1 前 | 未 |
| D-11 | 費用/技術 | LLM プロバイダ・モデル・月額上限 | Claude 等 | 候補数の実測後に費用試算を提示 | Phase 5〜7 前 | 未 |
| D-12 | 費用/規約 | ニュース・開示ソースの取得手段（有料ライセンス/RSS見出しのみ/取得しない）、TDnet 取得手段 | ソース別 | Phase 4 冒頭で規約調査表を提出 | Phase 4 | 未 |
| D-13 | 投資ロジック | 教師ラベル12種の判定基準（特に `PREDICTIVE_SUCCESS` vs `STATE_CONFIRMED_SUCCESS`、`OUT_OF_SCOPE_*`、`ACTIONABLE_FALSE_NEGATIVE` の判定者） | ルール / LLM補助 / 人手 | 判断しない | Phase 10 前 | 未 |
| D-14 | 運用 | 障害通知手段、ステージングDBの有無、認証方式（メールOTP等） | — | Supabase Auth + allowlist、ステージングは Phase 1 では持たない | Phase 1 | 未 |
| D-15 | 運用 | Excel の出力形式・受け取り方法（Web からダウンロード / メール / Drive） | — | Web から署名付きURLでダウンロード | Phase 9 | 未 |
| D-16 | 運用 | プロジェクト名・リポジトリ名（仮: `surge-research-platform`） | — | 仮名のまま進め、必要なら Phase 1 前に変更 | Phase 1 前 | 未 |
| D-17 | 投資ロジック | 同一銘柄に ENTRY 済み Prediction がある間の再 ENTRY・複数 Prediction の扱い（成績の重複カウント防止） | — | 判断しない | Phase 8 前 | 未 |
