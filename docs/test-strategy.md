# テスト戦略

## 1. レイヤー

| レイヤー | ツール（案） | 対象 |
|---|---|---|
| Python ユニット | pytest | Feature 計算、3,000円判定、ルール判定、ラベル判定、Outcome 計算 |
| Python 統合 | pytest + ローカル Supabase（Docker） | マイグレーション、as-of クエリ、append-only 制約、ジョブの冪等性 |
| TypeScript ユニット | Vitest | Web の表示ロジック・データ整形 |
| E2E | Playwright | 認証、主要画面、スマホ幅（375px） |
| 契約テスト | 記録済みレスポンス（fixture） | 外部API のスキーマ変化検知。CI では実APIを叩かない |
| データ品質 | 日次ジョブ内チェック | 件数急減、欠損率、価格異常値、重複 |

## 2. 本プロジェクト特有の必須テスト

### データリーク
- **未来データ挿入テスト**: `data_cutoff` 以降の行を追加しても、その cutoff で計算した Feature・候補・判定入力が変わらないこと。
- **取得時刻テスト**: `published_at` が cutoff 前でも `fetched_at` / `first_seen_at` が cutoff 後の文書は使われないこと。
- **分割調整テスト**: 後から判明した分割係数が、過去時点の3,000円判定・Feature に混入しないこと。
- **walk-forward テスト**: 学習データの最大日付 < 評価データの最小日付 を全 fold で検査。
- **知識ベース事例テスト**: 事例の期間より前の予測に、その事例を参照させないこと。

### 投資ロジック上の不変条件
- 3,000円境界値（2,999 / 3,000 / 3,001、米国株は FX 込み）。
- `threshold_20 == entry_reference_price × 1.20`（Watch 開始時価格ではない）。
- Watch 条件到達だけで ENTRY が生成されないこと（必ず REANALYSIS を経由）。
- 過去高値が Reachable Zone の算出入力に使われないこと（入力 Feature の許可リスト検査）。
- `WEAK_ASSOCIATION` 単独の紐付けが強材料フラグにならないこと。
- マクロ系 relation_type で `causal_path` が空なら保存できないこと。
- 突発急騰（事前兆候なし）に `ACTIONABLE_FALSE_NEGATIVE` が付かないこと。
- Prediction の UPDATE/DELETE が DB で拒否されること。
- Historical Replay の結果が `prod` テーブルに書き込めないこと。

### プロンプト原文
- `docs/prompts/MANIFEST.md` 登録ファイルの SHA-256 が一致すること（CI）。

## 3. テストデータの隔離

- テストは本番DB・実プロジェクトのデータディレクトリに**一時的にも書き込まない**。
- DB テストはローカル Supabase またはテスト専用スキーマ、ファイル系は一時ディレクトリを注入可能なパス引数で使う。
- 価格・ニュースの fixture は合成データまたは利用規約上保存可能な範囲に限る。

## 4. CI

- GitHub Actions: lint（ruff / eslint）、型検査（mypy or pyright / tsc）、pytest、vitest、プロンプトハッシュ検査。
- E2E は main マージ前にプレビュー環境で実行（Phase 1 以降に整備）。

## 5. カバレッジ方針

- 数値目標より「不変条件テストが全部ある」ことを優先。
- Feature 計算・ラベル判定・Outcome 計算は分岐網羅を目標にする。
