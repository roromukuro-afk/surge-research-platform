# 原文保存・バージョニング方針

状態: **v0.1（Phase 0.1 監査是正後）** — 2026-09-15

## 1. 原文の保存

| 項目 | 方針 |
|---|---|
| 対象 | v5.1 プロンプト、実装指示書、ChatGPT 監査結果など「後から判断の根拠になる文書」 |
| 保存場所 | プロンプト: `docs/prompts/short-surge-v5.1.original.md`（以後 v5.2 …も `*.original.md`）。要件・監査: `docs/requirements/*.original.txt` |
| 内容 | 受け取った本文を**一字一句そのまま**。要約・短縮・リライト・条件削除・配点変更・**整形**をしない |
| 整形版 | **原文と呼ばない。** 必要な場合のみ `*.formatted.md` として別ファイルにし、冒頭に「整形版・原文ではない」と明記する |
| 改変検知 | `docs/prompts/MANIFEST.md` に SHA-256 を記録。CI で一致を検査（登録済みファイルのハッシュが変われば失敗） |
| 改行変換の防止 | `.gitattributes` で原文ファイルを `-text` に指定し、Git による改行変換でハッシュが変わらないようにする |
| 旧版 | 削除しない。新版は必ず新ファイル |
| 由来の記録 | MANIFEST に「受領日・受領経路（ファイル / チャット本文の転記）」を書く。原文ファイル自体には注記を書き込まない |

**現状**
- v5.1 原文: **未受領**（Phase 1 の前提条件）。**会話内でユーザーが確定させた全文そのものを Canonical Source とし、別の「元ファイル」は待たない**（監査 0.2 最終パッチ #8）。受領時に `short-surge-v5.1.original.md` へ一字一句変更せず保存し、保存時点の SHA-256 を MANIFEST に記録する。formatting / normalization / typo correction をしない
- 実装指示書 v1.0 / 監査 Phase 0.1 / 監査 Phase 0.2: チャット本文を転記した `.original.txt` を保存済み。元ファイルとのバイト一致は未確認（D-22）

## 2. 追加仕様（addenda）

- **Canonical v5.1 と post-v5.1 decisions を混ぜない**（監査 0.2 #12）。
  - v5.1 本体: immutable original（`short-surge-v5.1.original.md`。会話内で確定した全文をそのまま）
  - post-v5.1 decisions: versioned addenda（`addenda/` 配下の別ファイル）
  - v5.1 ファイルに addendum の見出し・文言・注記を書き込まない。
- v5.1 以後に確定した仕様は `docs/prompts/addenda/` に別ファイルで置く。
- **優先順位**: 新しい addendum > 古い addendum > v5.1 原文。
- addendum は根拠となる原文（指示書・監査）の該当箇所への**所在一覧**とし、言い換えで上書きしない。詳細仕様は `docs/specs/` に置く。
- LLM に渡すプロンプトは「v5.1 原文」「適用する各 addendum」「実行時データ」を**別セクション**として組み立てる。v5.1 原文の SHA-256（`prompt_original_sha256`）、各 addendum の SHA-256（`prompt_addenda_sha256s`）、組み立て結果全体の SHA-256（`input_sha256`）を別々に保存する（RF-14b）。
- 訂正は新しい addendum で行う。

## 3. バージョン識別子

| 識別子 | 対象 | 形式（案） | 変更タイミング |
|---|---|---|---|
| `prompt_version` | プロンプト原文 + 適用 addenda の組 | `short-surge-v5.1+add-2026-09-15.2` | 原文または addendum の追加 |
| `rule_version` | コード化したスクリーニング・判定ルール | `rules-1.0.0` | 閾値・条件の変更 |
| `feature_version` | Feature 定義一式 | `feat-1.0.0` | 計算式・窓の変更 |
| `universe_version` | Universe 定義 | `universe-1.0.0` | 含める/除外条件の変更（新しい仕様ファイル） |
| `linker_version` | ノイズ判定・統合・Entity Linking | `link-1.0.0` | ロジック変更 |
| `label_version` | Objective ラベルの計算 | `label-1.0.0` | 計算方法の変更 |
| `labeler_model_version` | Interpretive ラベルの判定器 | `<provider>/<model>/<prompt_version>` | 判定器の変更 |
| `label_admission_policy_version` | 教師データへの採用ポリシー | `admit-1.0.0` | ポリシー変更 |
| `ml_version` | 学習済みモデル | `ml-<layer>-<yyyymmdd>-<shortsha>` | 学習ごと |
| `render_version` | チャート画像生成 | `render-1.0.0` | 描画仕様変更 |
| `schema_version` | DB / Parquet スキーマ | マイグレーション連番 / データセット版 | スキーマ変更 |
| `git_sha` | 実行コード | コミット SHA | 毎 run |
| `provider_bindings` | 役割ごとの Provider とプラン | JSON | 変更ごと（run に記録） |

## 4. ルール

1. Production の Prediction・分析には上記のうち該当する version をすべて保存する（該当しないものは `null` を明示）。
2. version を上げずに投資ロジック・Feature・Universe・ラベルの挙動を変えない。
3. 投資ロジックに関わる version（`prompt_version` / `rule_version` / `universe_version` / `label_version` / `label_admission_policy_version`）の変更は ChatGPT 監査とユーザー承認を経る。
4. 過去データを新しい version で再計算した結果は `research` 側に保存し、Production の結果を上書きしない。
5. 各 version の変更内容は `docs/changelog/` に記録する（Phase 1 以降に作成）。
