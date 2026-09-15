# プロンプト原文保存・バージョニング方針

## 1. プロンプト原文の保存

| 項目 | 方針 |
|---|---|
| 保存場所 | `docs/prompts/short-surge-v5.1.md`（以後 `short-surge-v5.2.md` …） |
| 内容 | ユーザーから渡された原文を**一字一句そのまま**。要約・短縮・リライト・条件削除・配点変更・整形変更をしない |
| 改変検知 | `docs/prompts/MANIFEST.md` に各ファイルの SHA-256 を記録し、CI で一致を検査する（登録済みファイルのハッシュが変われば CI 失敗） |
| 旧版 | 削除しない。新版は必ず新ファイル |
| Git | すべてコミット管理 |

**現状: v5.1 原文は未受領**。受領後、無加工で保存し、ハッシュを MANIFEST に登録する（Phase 1 着手前の前提条件）。

## 2. 追加仕様（addenda）

- v5.1 以後に共同議論で確定した仕様は `docs/prompts/addenda/` に別ファイルで保存する。
  - 例: `addenda/v5.1-addendum-2026-09-15.md`（実装指示書 v1.0 の §4〜§14、§25〜§31 等に由来する追加仕様）
- **優先順位**: 最新の addendum > 古い addendum > v5.1 原文。
- 実行時に LLM に渡すプロンプトは「原文 + 適用 addenda + 実行時データ」を組み立てたものとし、組み立て結果の内容ハッシュを `analysis.llm_analyses.input_hash` に保存する。
- addendum の内容も原文を改変しない方針で管理（訂正は新しい addendum で行う）。

## 3. バージョン識別子

| 識別子 | 対象 | 形式（案） | 変更タイミング |
|---|---|---|---|
| `prompt_version` | プロンプト原文 + 適用addenda の組 | `short-surge-v5.1+add-2026-09-15` | 原文または addendum の追加 |
| `rule_version` | コード化したスクリーニング/判定ルール（Route A〜H 等） | SemVer `rules-1.0.0` | ルールの閾値・条件の変更 |
| `feature_version` | 特徴量定義一式 | `feat-1.0.0` | 計算式・窓の変更 |
| `linker_version` | ノイズ判定・クラスタリング・Entity Linking | `link-1.0.0` | 判定ロジック変更 |
| `label_version` | 教師ラベル判定基準 | `label-1.0.0` | 基準変更 |
| `ml_version` | 学習済みモデル | `ml-<layer>-<yyyymmdd>-<shortsha>` | 学習ごと |
| `render_version` | チャート画像生成 | `render-1.0.0` | 描画仕様変更 |
| `schema` | DB | マイグレーションファイル連番 | スキーマ変更 |
| `git_sha` | 実行コード | コミットSHA | 毎 run に記録 |

## 4. ルール

1. Production Prediction には上記 version をすべて保存する（該当しないものは `null` を明示）。
2. version を上げずに投資ロジック・特徴量・ラベルの挙動を変えてはならない。
3. 投資ロジックに関わる version 変更（`prompt_version` / `rule_version` / `label_version`）は ChatGPT 監査とユーザー承認を経る。
4. 過去データを新しい version で再計算した結果は `research` スキーマに保存し、Production 結果を上書きしない。
5. 各 version の変更内容は `docs/changelog/` に記録する（Phase 1 以降に作成）。
