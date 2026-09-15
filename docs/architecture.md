# アーキテクチャ案（Phase 0 草案）

状態: **草案・ChatGPT監査待ち**（2026-09-15 作成）

## 1. 全体像

```
                ┌──────────────────────────────────────────────────────────┐
                │                    外部データソース                         │
                │ 価格(JP/US) FX 開示(TDnet/EDINET/EDGAR) ニュース 政府・商品    │
                └───────────────┬───────────────────────────┬──────────────┘
                                │ 日次バッチ                  │ 高頻度ポーリング
                                ▼                            ▼
┌───────────────────────────────────────────┐   ┌─────────────────────────────┐
│ Batch Workers (Python, GitHub Actions)     │   │ Collector Worker (Python)    │
│  universe → market data → FX → 3000円filter│   │  news/disclosure poller      │
│  → features → Stage1 technical/material    │   │  first_seen_at を記録         │
│  → candidates → Stage2 → Stage3 LLM        │   │  (常駐 or 短間隔ジョブ)        │
│  → ENTRY/WATCH → watch monitor             │   └──────────────┬──────────────┘
│  → outcome tracker → labels → export       │                  │
│  → training / walk-forward (Research)      │                  │
└───────────────┬───────────────────────────┘                  │
                │                                                │
                ▼                                                ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ Supabase（新規プロジェクト）                                                    │
│  Postgres: ref / market / pipeline / universe / features / materials /      │
│            analysis / prod(append-only) / outcomes / labels / research / ml │
│  Storage : raw documents, raw API responses, chart images, parquet archive, │
│            LLM input bundles, Excel exports                                  │
│  Auth    : 本人のみ（allowlist）                                              │
└───────────────────────────────┬────────────────────────────────────────────┘
                                │ 読み取り中心
                                ▼
┌────────────────────────────────────────────────────────────────────────────┐
│ Web (Next.js / TypeScript, 新規 Vercel Project)                              │
│  Dashboard / Universe / Materials / Stock Detail / Predictions / Watch /     │
│  Results / Model Lab / Pipeline — スマホ対応、認証必須                          │
└────────────────────────────────────────────────────────────────────────────┘
```

## 2. 処理パイプライン（日次）

| # | Step | 実装 | LLM |
|---|---|---|---|
| 1 | Security Master 更新（上場・廃止・銘柄種別） | Python | 使わない |
| 2 | 日足/必要な価格データ取得（全件） | Python | 使わない |
| 3 | USD/JPY 取得（同時点基準） | Python | 使わない |
| 4 | **3,000円 Hard Filter** → Eligible Universe | Python | 使わない |
| 5 | 全 Eligible 銘柄の Technical Feature 計算 | Python (pandas/polars) | 使わない |
| 6a | Stage 1 Technical Screening（v5.1 Route A〜H をコード化） | Python | 使わない |
| 6b | Material Candidates（材料ルート、Technicalとは独立） | Python + 材料DB | 使わない（紐付けは Step 10 の結果を利用） |
| 7 | 候補集合 = Technical ∪ Material | Python | 使わない |
| 8 | Stage 2（チャート・需給の詳細 Feature、知識ベース照合、チャート画像生成） | Python | 使わない |
| 9 | Stage 3 AI詳細分析 → ENTRY / WATCH_* / REJECT | Python + LLM | **使う（候補のみ）** |
| 10 | 状態保存: Prediction（ENTRYのみ, append-only）/ Watch / State Transition | Python + DB | — |
| 11 | Watch Monitor: 条件到達 → 再分析 → 新規 Snapshot | Python + LLM | 再分析時のみ |
| 12 | Outcome Tracking（Prediction と Eligible Universe 全件） | Python | 使わない |
| 13 | Teacher Label 付与（ルール + Research Mode） | Python (+LLM補助) | 補助のみ |
| 14 | Excel Export（View用） | Python (openpyxl) | 使わない |

材料系（常時）:

| # | Step | LLM |
|---|---|---|
| 10-1 | 収集（TDnet/EDINET/EDGAR/企業IR/ニュース/政府/商品…）→ raw保存・`first_seen_at` 記録 | 使わない |
| 10-2 | 重複排除・同一出来事クラスタリング → `material_event` | 埋め込み等（Decision Needed） |
| 10-3 | `market_relevance` 判定（ノイズ除去、キーワード単純除外は禁止） | 使う可能性あり |
| 10-4 | Entity Linking（`relation_type`、マクロは因果経路必須） | 使う可能性あり |
| 10-5 | 材料属性（新規性・サプライズ・直接性・インパクト・継続性・市場反応・未織り込み度） | 使う（Featureとして保存、真実扱いしない） |

## 3. 技術選定と根拠

2026-09-15 時点で各サービスの公式ドキュメントを確認した値に基づく。

### 3-1. Web: Next.js (TypeScript) on Vercel（新規 Project）
- スマホから閲覧できる認証付きWeb UIとして十分。ユーザーの既存運用経験がある。
- **制約**: Vercel Functions の最大実行時間は Hobby 300秒、Pro 800秒（beta で 1800秒）。Hobby の Cron は1日1回・±59分精度。
  → 全市場バッチ・LLMバッチ・学習は Vercel に載せない（指示書 §38 と一致）。Web は DB 読み取りと軽い操作のみ。
- Hobby プランは商用利用不可の規約があるが、本システムは非公開の個人研究用途。プラン選択は Decision Needed（D-04）。

### 3-2. DB / Storage / Auth: Supabase（新規 Project）
- Postgres・オブジェクトストレージ・認証を1サービスで賄え、監査用の外部キー・トリガー（append-only 強制）・RLS が使える。
- **制約**: Free は DB 500MB で超過時 read-only、Storage 1GB、7日間低アクティビティで自動停止、Free プロジェクトは2つまで。Pro はディスク 8GB 込み・Storage 100GB 込み・自動停止なし。
- **容量見積もり（概算）**: JP 約3,800 + US 約5,000〜6,000 普通株 ≒ 1万銘柄/日。日足だけで年約250万行、全 Eligible 銘柄の特徴量（数十列）を毎日保存すると年 数GB 規模。
  → **Phase 2〜3 で Free の 500MB を超える見込みが高い**。Pro への移行時期は Decision Needed（D-03）。
- 大容量・低頻度参照データ（生APIレスポンス、長期の全銘柄特徴量履歴）は Storage に Parquet で退避し、Postgres にはマニフェストと直近期間を置く構成も選べる（D-03 と合わせて決定）。

### 3-3. Batch Worker: Python 3.12 on GitHub Actions（新規プライベートリポジトリ）
- 1ジョブ最大6時間、スケジュール実行・手動再実行・ログ保存が無料で揃う。ML（LightGBM 等）とデータ処理のエコシステムが Python に集中している。
- **制約**: プライベートリポジトリの無料枠は月2,000分。JP/US 日次バッチ（各20〜40分想定）× 約21営業日 × 2市場 ≒ 月 840〜1,680分で無料枠に近い。cron は定刻どおりに起動しない場合がある。
  → 日次バッチには適するが、**ニュースの高頻度ポーリングには不向き**。

### 3-4. Collector Worker（ニュース・開示の高頻度収集）
- `first_seen_at` の精度はポーリング間隔そのもの。15分間隔なら最大15分の誤差になる。
- 候補: (a) 小型常駐VM/コンテナ（Fly.io 等）、(b) GitHub Actions の短間隔ジョブ（分数消費大・起動遅延あり）、(c) 自宅PC常駐（可用性低）。
- **推奨は (a)**。ただし費用が発生するため Decision Needed（D-05）。Phase 4 までに決めればよい。

### 3-5. LLM
- Stage 3 と材料解釈で使用。プロバイダ・モデルは抽象化し、`llm_provider` / `llm_model` / `prompt_version` を毎回保存。
- 候補は Claude（Anthropic API）など。選定と月額上限は Decision Needed（D-11）。Phase 7 までに決めればよい。

### 3-6. ML
- Phase 11 以降。LightGBM / XGBoost / CatBoost 等の tabular model から開始。Deep Learning からは始めない。
- 学習は GitHub Actions またはローカルで実行し、モデル成果物は Storage、メタデータは `ml.model_registry` に保存。

## 4. 環境分離

| 区分 | 内容 | 物理分離 |
|---|---|---|
| Production | 採用済みルール/モデルでの日次運用。`prod` スキーマ、Prediction は append-only | Supabase 本番プロジェクト |
| Research | Historical Replay、特徴量重要度、Challenger 学習、見逃し分析。`research` スキーマ（`replay_run_id` 必須） | 同一DB内の別スキーマ（初期）。容量次第で分離 |
| Development | ローカル Supabase（Docker）+ テスト用データ | ローカル |

Production Prediction と Historical Replay は**テーブルを分ける**。Web UI でも別区分で表示する。

## 5. データリーク防止の設計原則

1. **二時間軸**: すべての取得データに「イベント時刻（published_at / trade_date）」と「システム取得時刻（fetched_at / first_seen_at）」を持たせる。
2. **as-of 取得関数**: 特徴量・分析は `as_of(data_cutoff)` 経由でのみデータを読む。`fetched_at <= data_cutoff` を強制する共通関数を用意し、直接クエリを禁止する。
3. **価格の二系統**: 実取引価格（無調整）と分割調整係数（`known_at` 付き）を分けて保存。3,000円判定は無調整価格。
4. **LLM入力の固定**: LLMに渡した入力バンドル（Feature・OHLCV・材料時系列・画像）を内容ハッシュ付きで保存し、再現可能にする。
5. **生存者バイアス**: 上場廃止銘柄も Security Master に残し、Outcome 追跡を廃止時点まで行う。
6. **walk-forward**: 学習・評価は時系列分割のみ。

## 6. 認証・非公開

- Supabase Auth（メールOTP/マジックリンク等）+ 本人メールアドレスの allowlist。
- 全テーブルで RLS を有効化し、匿名アクセスは拒否。Worker は service role キーを GitHub Secrets 経由で使用。
- Vercel 側でも Deployment Protection を併用可能（D-04）。
- Excel・ニュース・研究結果を公開URLに置かない（Storage は private バケットのみ、署名付きURLで一時取得）。

## 7. 監査ログ

- `pipeline.runs`: run_id / job名 / started_at / finished_at / data_cutoff / git commit SHA / 設定ハッシュ / 各version / status
- `pipeline.run_errors`、`pipeline.source_fetch_log`（ソース別の取得件数・HTTPステータス・所要時間）
- `pipeline.coverage_snapshots`（Universe件数・取得成功率・欠損銘柄）
- 各結果テーブルは `run_id` を外部キーで持つ。
