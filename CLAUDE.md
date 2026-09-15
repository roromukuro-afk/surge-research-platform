# CLAUDE.md — 短期急騰AI研究プラットフォーム

Claude Code はこのプロジェクトの**主任開発エージェント**である。
ただし投資分析思想・教師データ設計・予測ロジックを**独断で簡略化・変更してはならない**。
正本となる要件は [docs/requirements/implementation-instructions-v1.0.md](docs/requirements/implementation-instructions-v1.0.md)。

---

## 1. 絶対ルール（違反禁止）

### 1-1. 完全新規プロジェクト
- 既存のプロジェクト・リポジトリ・Vercel Project・Supabase Project・既存サイトを**使用・流用・改造しない**。
- 「既存の似たプロジェクト（例: `C:\Users\rorom\jp_surge_radar`）が使えそう」と判断して統合しない。コードのコピーもしない。
- 既存システムとの統合はユーザーが後から明示した場合のみ。

### 1-2. v5.1 は原文保存
- 「短期急騰専門・3000円以下限定 完全版プロンプト v5.1」は `docs/prompts/short-surge-v5.1.md` に**一字一句そのまま**保存する。
- 要約・短縮・リライト・条件削除・配点変更をしない。整形（改行・全角半角・見出し記号）も変えない。
- 旧版は削除しない。新版は別ファイル（v5.2 等）で追加する。
- Prediction には必ず `prompt_version` を保存する。
- v5.1 以後に確定した追加仕様は `docs/prompts/addenda/` に**別ファイル**で置き、v5.1原文には手を入れない。追加仕様は v5.1 より優先する。

### 1-3. 投資ロジックを勝手に簡略化しない
- Route A〜H、スコア、閾値、ラベル定義、Entry判断基準を「実装しやすいから」で削る・まとめる・固定値化しない。
- データ制約で実装できない場合は、黙って妥協せず **Decision Needed** として報告する。

### 1-4. Prediction = 現在Entry可能のみ
- 正式Predictionは、AIが**現在の価格・現在の市場状態からEntry可能**と判断した `ENTRY` のみ。
- 判定状態は最低限 `ENTRY` / `WATCH_BREAKOUT` / `WATCH_PULLBACK` / `WATCH_OTHER` / `REJECT`。
- 評価基準価格は Prediction 作成時の実際の **Entry Reference Price**。過去の安値やWatch開始時の価格を基準にしない。
- Prediction は**作成後に書き換えない**（append-only。DBレベルで UPDATE/DELETE を禁止する）。

### 1-5. Watch は Prediction ではない
- `WATCH_*` は監視対象であり成績計測しない。
- Watch条件（ブレイク価格到達など）を満たしても**自動でENTRYにしない**。必ず再分析（出来高・VWAP・上ヒゲ・材料・新規ニュース・地合い・支持抵抗・希薄化）を行い、Entry可ならその時点の価格で新しい Prediction Snapshot を作る。
- 状態遷移（WATCH → TRIGGER_HIT → REANALYSIS → ENTRY / REJECT など）はすべて保存し、教師データにする。

### 1-6. 過去高値を上値余地にしない
- 過去の急騰高値までの距離を Potential Upside / Reachable Zone として扱わない。
- 過去高値はむしろ Supply Overhang・戻り売り・Distribution の兆候として扱う。
- Reachable Zone は**現在の**材料・需給・支持抵抗・出来高構造からのみ作る。

### 1-7. News と IR に固定序列を作らない
- `IR > News` のような情報源による固定序列を実装しない。
- 材料の強さは 新規性・サプライズ・直接性・経済的インパクト・継続性・市場反応・未織り込み度 で評価する。
- Discovery Source（市場に最初に伝えた情報源）と Verification Source（事実確認用の一次情報など）を分離する。
- 同一出来事の複数記事は1つの `material_event` に統合し、`first_seen_at` を保存する。

### 1-8. ノイズ除去・紐付け
- 単純なキーワード除外は禁止。判定基準は `market_relevance`。
- ニュース→銘柄の紐付けは `relation_type` を必ず保存。`WEAK_ASSOCIATION` 単独では強材料扱いしない。
- マクロ材料は因果経路（事象→供給/価格→売上/コスト→利益→株価）を必須とする。

### 1-9. 突発急騰を False Negative にしない
- 事前に取得可能な兆候がない突発急騰（例: 前兆なしのTOB発表）を「見逃し」として学習しない。
- 見逃しとして学習するのは、上昇前/上昇初期に取得可能だった情報から合理的に拾えたと Research Mode で判定された `ACTIONABLE_FALSE_NEGATIVE` のみ。
- 「20日以内に+20%」を一律に成功ラベルにしない。ラベルは細分化する（docs/data-model-draft.md 参照）。

### 1-10. Production と Research を分離
- Production は採用済みのモデル・ルールのみ使用。
- 後から新ロジックで再計算した Historical Replay を Production Prediction と**同じテーブル・同じ画面区分に混ぜない**。
- Research の結果を無検証で Production に入れない。Champion / Challenger を walk-forward で比較してから昇格する。

### 1-11. データリーク禁止
- すべての特徴量・分析は `data_cutoff` 時点で**取得済みだった**データのみを使う（イベント時刻ではなく取得時刻 `fetched_at` / `first_seen_at` で判定）。
- ランダムシャッフルのみの train/test split 禁止。時系列順の walk-forward を使う。
- 分割調整済み価格など、後から判明する情報を過去時点の判断に混ぜない（3,000円フィルタはその時点の実際の取引価格で判定）。
- 生存者バイアスを避けるため、上場廃止銘柄も Security Master と履歴に残す。

### 1-12. 確率表示
- 十分な教師データと校正（Calibration）ができるまで「+20%確率 8.2%」のような数値を表示しない。初期は ENTRY / WATCH / REJECT と根拠を表示する。

### 1-13. 監査可能性
- 重要処理には `run_id`・timestamp・`data_cutoff`・source・各種version・error log を保存する。
- 「なぜ選ばれたか / 除外されたか / どのデータを見ていたか」を後から再現できるようにする。LLM入力は内容ハッシュとともに保存する。

---

## 2. 進め方

- **一気に最後まで作らない。** Phase 単位で実装し、Phase 完了ごとに停止して報告する。ChatGPT 監査を経てから次へ進む。
- 曖昧な判断・投資ロジックに関わる判断は勝手に決めず、報告の **Decisions needed** に挙げる。技術的な判断は理由を添えて **Decisions made** に記録する。
- 未決事項は [docs/unresolved-decisions.md](docs/unresolved-decisions.md) に集約し、決まったら決定日と決定者を追記する（削除しない）。

### Phase 完了報告フォーマット（必須）

```
## Phase N 完了報告
### Completed
### Files changed
### Tests
### Coverage
### Known limitations
### Decisions made
### Decisions needed
### Risks
### Next proposed phase
```

「すべて完成しました」で終えない。未実装・妥協・データ制約を必ず書く。

---

## 3. 技術規約

- Web: `apps/web`（Next.js / TypeScript）。重い処理（全市場取得・特徴量計算・LLMバッチ・学習）を Vercel Function に入れない。
- Worker: `workers/`（Python 3.12）。ジョブは冪等に作り、`run_id` 単位で再実行できるようにする。
- DB スキーマ変更は必ず `supabase/migrations/` のマイグレーションで行う。本番DBを手作業で変更しない。
- 秘密情報（APIキー・DB接続文字列）はコミットしない。`.env.example` にキー名のみ記載。
- 各データソースの利用規約を確認してから取得コードを書く。全文保存不可のソースは metadata / URL / snippet / hash / 抽出特徴量のみ保存する。
- テストは実プロジェクトのデータ・本番DBに書き込まない（一時ディレクトリ・ローカルDB・テスト用スキーマで隔離）。
- 外部に影響する操作（リポジトリ公開・push・クラウドリソース作成・課金プラン変更）はユーザー確認後に行う。
