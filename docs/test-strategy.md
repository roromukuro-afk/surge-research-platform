# テスト戦略

状態: **v0.2（Phase 0.2 監査是正後）** — 2026-09-15

## 1. レイヤー

| レイヤー | ツール（案） | 対象 |
|---|---|---|
| Python ユニット | pytest | Feature 計算、Universe 判定、3,000円判定、ルール判定、パス解決、ラベル判定 |
| Python 統合 | pytest + ローカル Postgres + ローカル ObjectStore | マイグレーション、append-only / CHECK / 一意制約、as-of 読み取り、manifest、Job の冪等性とリース |
| Interface 適合テスト | pytest（同じテストを全実装に対して実行） | `JobRunner` / `MarketDataProvider` / `ObjectStore` の各実装が共通の保証を満たすか |
| 契約テスト | 記録済みレスポンス（fixture） | 外部 API の形式変化の検知。CI では実 API を呼ばない |
| TypeScript ユニット | Vitest | Web の表示ロジック |
| E2E | Playwright | 認証、主要画面、スマホ幅 |
| データ品質 | 日次 Job 内のチェック | 件数の急減、欠損率、異常値、重複、`TYPE_UNKNOWN` 件数 |

## 2. 投資ロジック回帰テスト

仕様: **[specs/regression-fixtures.md](specs/regression-fixtures.md)**（RF-01〜RF-22）

- 監査で必須とされた6件（RF-01〜RF-06）を含む。
- 実行可能になる Phase より前は `pending` として CI に登録し、削除しない。
- 規則を広げすぎていないこと・必要な参照をしていることを確かめる対照ケース（RF-01-C / RF-01-M / RF-02-C / RF-04-C / RF-05-C / RF-06-C / RF-17-C / RF-19c / RF-20-C）も必ず実装する。

## 3. Interface の共通保証テスト

| Interface | 検査内容 |
|---|---|
| JobRunner / Scheduler | 同じ `idempotency_key` の二重投入で1回だけ実行される。リース切れで再取得できる。Runner を替えても Job の出力が同じ |
| MarketDataProvider | `get_price_observation(at_or_before=T)` が T より後の観測を返さない。provenance と raw の保存。無調整価格の保持 |
| FxProvider | `at_or_before` を超える観測を返さない（RF-09） |
| ObjectStore | 既存 key への上書きを拒否。sha256 の一致 |

## 4. データリーク検査

- **未来データ挿入**: cutoff 以降の行・manifest を追加しても、その cutoff で計算した Feature・候補・分析入力が変わらない。
- **利用可能時刻**: `source_published_at` が cutoff 前でも `available_to_model_at` が後なら使われない（RF-05）。
- **backfill**: 後から取り込んだ過去ニュースが、既存の Production 分析・Historical Replay の入力に入らない（RF-17）。
- **Corporate action**: 分割・併合で Target / Failure / MFE / MAE を誤判定しない。配当を Target に加算しない（RF-18）。
- **後日訂正**: 訂正版 manifest は、作成時刻より前の as-of では読まれない（RF-16）。
- **分割係数**: 後日公表の係数が過去の判定に混入しない（RF-11）。
- **walk-forward**: 全 fold で 学習データの最大日付 < 評価データの最小日付。
- **知識ベース事例**: `available_from` より前の判断で事例を参照しない。
- **Research 判定の入力**: `information_cutoff_at` より後の情報が含まれない（RF-02）。

## 5. 権限・分離

- Research ロールで `prod.*` に書き込めない（RF-15）。
- Web 用のキーで `prod.*` に書き込めない。

## 6. 原文の改変検知

- `docs/prompts/MANIFEST.md` に登録されたファイルの SHA-256 を CI で検査する（RF-14）。

## 7. テストデータの隔離

- 本番 DB・本番バケット・実データの保存先に**一時的にも書き込まない**。ローカル DB・一時ディレクトリ・テスト用バケットを注入して使う。
- fixture は合成データ、または利用規約上保存可能な範囲に限る。

## 8. CI

- lint（ruff / eslint）、型検査、pytest、vitest、原文ハッシュ検査、回帰 fixture（pending 含む一覧表示）。
