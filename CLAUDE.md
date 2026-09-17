# CLAUDE.md — 短期急騰AI研究プラットフォーム

Claude Code はこのプロジェクトの**主任開発エージェント**である。
ただし投資分析思想・教師データ設計・予測ロジックを**独断で簡略化・変更してはならない**。

## 0. 正本ドキュメントと優先順位

| 優先 | 文書 |
|---|---|
| 1 | 最新の ChatGPT 監査原文 — [Phase 1 開始条件（2026-09-16）](docs/requirements/audit-2026-09-16-phase-1-start-conditions.original.txt) > [Phase 0.2 最終パッチ](docs/requirements/audit-2026-09-15-phase-0.2-final-patch.original.txt) > [Phase 0.2](docs/requirements/audit-2026-09-15-phase-0.2.original.txt) > [Phase 0.1](docs/requirements/audit-2026-09-15-phase-0.1.original.txt) |
| 2 | 実装指示書 v1.0 原文 — [docs/requirements/implementation-instructions-v1.0.original.txt](docs/requirements/implementation-instructions-v1.0.original.txt) |
| 3 | v5.1 原文 — `docs/prompts/short-surge-v5.1.original.md`（**未受領。会話内でユーザーが確定させた全文を受領後に登録**） |
| 詳細仕様 | [docs/specs/](docs/specs/)（lifecycle / universe / security-identity / teacher-labels / regression-fixtures） |

`*.formatted.md` は閲覧用の整形版であり、原文として扱わない。

---

## 1. 絶対ルール（違反禁止）

### 1-1. 完全新規プロジェクト
- 既存のプロジェクト・リポジトリ・Vercel Project・Supabase Project・既存サイトを**使用・流用・改造しない**。
- 既存の似たプロジェクト（例: `jp_surge_radar`）のコードをコピー・統合しない。読みに行かない。変更もしない。

### 1-2. 原文保存
- v5.1 は、**会話内でユーザーが確定させた全文そのものを Canonical Source とする**（別の「元ファイル」を待たない）。受領したら `docs/prompts/short-surge-v5.1.original.md` に一字一句変更せず保存し、保存時点の SHA-256 を `docs/prompts/MANIFEST.md` に記録する。formatting / normalization / typo correction をしない。
- **Canonical v5.1（immutable original）と post-v5.1 decisions（versioned addenda）を混ぜない。** v5.1 ファイルに addendum の文言を書き込まない。LLM 入力でも別セクション・別ハッシュで持つ。
- 要約・短縮・リライト・条件削除・配点変更・整形をしない。整形したものは原文と呼ばない。
- 旧版は削除しない。addenda の優先順位は 新しい addendum > 古い addendum > v5.1。
- **v5.1 Canonical の登録前に Phase 1 へ進まない。** Phase 1 の終了時には必ず停止し、ChatGPT 監査を受けてから Phase 2 へ進む。

### 1-3. 投資ロジックを勝手に簡略化しない
- Route A〜H、スコア、閾値、ラベル定義、Entry 判断基準を「実装しやすいから」で削る・まとめる・固定値化しない。
- データ制約で実装できない場合は Decision Needed として報告する。自分の解釈で補った箇所は「Claude Code 解釈（要確認）」と明記する。

### 1-4. Setup・ENTRY・価格
- 判定状態: `TECHNICAL_SETUP_EOD` / `POST_CLOSE_CATALYST_SETUP` / `ENTRY` / `WATCH_BREAKOUT` / `WATCH_PULLBACK` / `WATCH_OTHER` / `REJECT`。
- **EOD 分析・引け後の材料分析は ENTRY を出さない。** 正式 Prediction は、場中の ENTRY 判断分析が「現在価格から ENTRY 可能」と判断した場合のみ。
- 引けまでのチャート・価格・出来高によるセットアップ（`TECHNICAL_SETUP_EOD`）と、引け後の新規材料によるセットアップ（`POST_CLOSE_CATALYST_SETUP`）を分ける。引け後の材料を EOD 価格の未織り込み評価に使わない。
- 価格は3つを分ける: `signal_reference_price`（記録用）、`decision_price` / `decision_price_observed_at`（AI が判断時に参照した価格）、`entry_reference_price` / `entry_price_observed_at`（判断後に現実に取引可能だったとみなす評価価格）。
- **成績・+20% Threshold は `entry_reference_price` だけを基準にする。** 算出方式は D-01a で決めるまで固定しない。
- **3,000円 Hard Filter を ENTRY 時に2回確認する。** `decision_price` で超えていれば ENTRY にしない。`decision_price` が3,000円以下でも `entry_reference_price` が超えていれば、Prediction・Episode を作らず `ENTRY_ABORTED_PRICE_LIMIT`（研究ログのみ、成績に含めない）。再び3,000円以下になったら再分析して新しい ENTRY 判断とする。
- Prediction は作成後に書き換えない（append-only）。
- 詳細: [docs/specs/entry-and-episode-lifecycle.md](docs/specs/entry-and-episode-lifecycle.md)

### 1-5. Watch は Prediction ではない
- Watch 条件に到達しても自動で ENTRY にしない。`TRIGGER_HIT → REANALYSIS` を必ず経る。状態遷移はすべて保存する。

### 1-6. Episode・Horizon・Failure Line
- 同一銘柄・同一仮説では、初回 ENTRY から Target / Failure / Thesis invalidation / Horizon end までを 1 Episode とし、成績は Episode 単位で数える。Episode 中の再評価は State Transition。
- **Horizon: S0 = ENTRY 成立セッション（ENTRY 時刻以降の S0 の値動きを含む）、S1 = 翌取引セッション、Primary Horizon は S20 close まで。** Watch/Setup の開始から数えない。REAFFIRMED などでリセットしない。新しい Episode のときだけ新しい Horizon。
- **`initial_failure_line` は Prediction 作成時に固定し、変更しない。** Primary Outcome・Teacher Label・`INITIAL_FAILURE_HIT` はこれで判定する。`current_risk_line` は State Transition として変更でき、研究用に別に評価する。
- **Outcome は二層。** `primary_episode_outcome`（正式評価、Episode 終了まで: TARGET_HIT / INITIAL_FAILURE_HIT / THESIS_INVALIDATED / HORIZON_EXPIRED 等）と `counterfactual_horizon_outcome`（研究用、当初 S20 close まで）。THESIS_INVALIDATED 後の +20% 到達を Primary の成功に戻さない。

### 1-7. 時刻と情報の利用可能性
- 材料は `source_published_at` / `system_first_seen_at` / `ingested_at` / `available_to_model_at` を持つ。
- **Production と Historical Replay は `available_to_model_at <= decision_cutoff_at` の情報だけを使う。** `source_published_at` を利用可能時刻の代わりにしない。
- backfill した情報を、過去の Prediction・Replay が知っていたことにしない。
- 米国株の円換算は `fx_observed_at <= decision_cutoff_at`（ENTRY）/ `<= price_cutoff_at`（EOD）。

### 1-8. データの範囲と用途
- 全 Universe は日足。分足は Stage 2・Setup・Watch・ENTRY 候補・Open Episode のみ。約定データはパス解決・entry 価格算出に必要な時間帯のみ。
- **J-Quants の分足・ティックは日次更新（16:30頃）なので、場中の ENTRY 判断・Watch 監視に使わない。** Historical research / EOD / Replay / Teacher data 用に限る。

### 1-9. Outcome
- **米国株: 3,000円 Eligibility は JPY 換算、+20% Threshold と価格 Outcome は USD 建て。** JPY リターンは補助 Outcome。
- raw（取引された無調整価格）を保存し、Outcome は分割・併合を ENTRY 時点の株数ベースに換算した比較可能な系列で計算する。**配当は +20% Target に加算しない。**
- パス解決: 始値で既に跨いでいれば始値のイベント → 日足 → より細かい分足 → 利用可能なら約定 → それでも順序不明なら `AMBIGUOUS_PATH`。より細かいデータが仕様上存在しないため順序不明なら `AMBIGUOUS_PATH`、本来あるはずの細かいデータが欠損していれば `UNRESOLVED_MISSING_DATA`。成功・失敗に恣意的に寄せない。

### 1-10. Universe 定義は版管理
- 現行は `universe-1.0.0`。変更は新しい版のファイルで行う。種別を判定できない銘柄を黙って含めない。
- **市場区分は「どこで売買されるか」であって「何であるか」ではない。** JPX のプライム/スタンダード/グロースには優先株式・社債型種類株式も載る。普通株かどうかは銘柄名（優先株式 / 種類株式 / 優先出資証券 等）で判定し、判定できない特殊証券を COMMON_STOCK と推測しない。

### 1-10b. 銘柄の同一性と履歴
- **Ticker は同一性ではない。** 正規化した名称も同一性ではない（fallback でも使わない）。同一性は公的レジストリの識別子（US = SEC CIK、JP = EDINET コード、Security の JP = JPX ローカルコード）から作る。
- 作れないときは `PROVISIONAL` として明示し、黙って推測しない。**発行体の fallback キーは security の座標に従う**（`ISSUER-OF:<security identity key>`）。名称は `ref.issuer_names` の `ALIAS`＝照合の証拠であり、統合の根拠にしない。
- **false merge より false split を優先する。** 分かれたものは後から統合できるが、統合したものは戻せない。
- **confidence を実態より高く言わない。** `STRONG`（そのものに対するレジストリ識別子）/ `REGISTRY_ANCHORED`（レジストリ識別子＋テキスト由来の属性。US の `US:CIK:<cik>:<type>:<class>` はこれ）/ `PROVISIONAL` の3段階。昇格は自動で行わず、`identity_version` を伴う記録された migration とする。
- 再利用可能なキー（`STRONG` と `REGISTRY_ANCHORED`）の衝突は統合せず両方を降格し、`error_type = 'IDENTITY_COLLISION'` として `context` 付きで記録する。降格判定を単一値比較にしない。
- **発行体名はレジストリの名称**（SEC registrant name / EDINET 提出者名）。商品名（"… - Common Stock"）を発行体名にしない。
- Ticker・名称・識別子・上場区分/状態・発行体名は SCD2 で持つ。変化したときだけ履歴行を開き、無変化の再観測は `last_confirmed_at` を進める。履歴を上書き・削除しない。
- 過去時点の姿は `ref.listings_as_of()` と同じ規則（`available_at <= cutoff`）でのみ読む。**`ref.listings.local_code` は現在値であり、過去 Ticker の正本ではない**（`ref.listing_symbols` が正本）。
- 同一性を変える再構築は [docs/specs/security-identity.md](docs/specs/security-identity.md) §5 の手順に従い、旧 → 新 ID を必ず残す。migration の適用だけで旧 DB の移行が終わったことにしない（reload 後に `ref.finalize_identity_rebuild` を実行する）。
- **`UNRESOLVED` は「除外」ではない。** 下流の価格・FX 取得対象は `INCLUDED` ∪ `UNRESOLVED`。Prediction は Eligibility が解決した `INCLUDED` のみ。
- coverage は provider の失敗とデータ品質警告と identity collision（レコード数と distinct キー数）を分けて記録する。
- **publish した run は immutable。** 属する artifact（runs / source_fetches / run_errors / master_snapshot / evaluations / coverage）は追記も更新も削除もできない。訂正は新しい run を publish して supersede する。
- **時刻は2種類を区別する。** `observed_at` = その値を供給した source を読んだ時刻、`available_at` = システムが知り得た時刻（identity 依存行は run の全 source の available_at 最大値）。`data_cutoff` は `max(source_fetches.available_at)`。
- **provenance は値を供給した source を指す。** CIK は SEC、EDINET コードは EDINET code list。primary provider で一律にしない。
- **どの run が Universe かは publication が決める**（`pipeline.run_publications`）。`finished_at` の新しさで決めない。下流は `universe.authoritative_run_at(market, version, knowledge_cutoff)` / `universe.eligibility_as_of(...)` を通して読み、**後から再構築した run を過去の時点へ逆流させない**。
- **Production run は再現可能でなければならない。** `git_sha` / `config_hash` / `job_version` / `universe_version` / `identity_version` / provider_bindings を必ず保存する（CLI と DB 制約で強制）。`idempotency_key` は論理的な invocation（market・as_of・source_data_version・各 version・config_hash）で決め、**run_id を含めない**。
- **知識時刻と有効時刻を混同しない。** as-of 読み出しは `available_at <= knowledge_cutoff` かつ `effective_from <= effective_at < effective_to` の両方で絞る。

### 1-11. 過去高値
- **過去の急騰高値まで戻ることを上昇根拠・Potential Upside にしない。**
- **過去高値の参照は禁止ではない。** Resistance・Supply Overhang・戻り売り候補・高値掴み保有者の存在・Reachable Zone までの障害として、必要に応じて積極的に使う。
- 過去高値は Resistance・Supply Overhang・Historical obstacle・高値掴み保有者の存在可能性として保存・評価する。
- Reachable Zone は現在の材料・需給・支持抵抗・出来高構造から作る。**過去高値が存在するだけで Reachable Zone の上限を機械的・単調に引き下げる制約は設けない**（新しい強材料・出来高・価格受容・高値突破で Supply Overhang の意味が弱まる・失効することがある）。ただし過去高値が遠いほど上昇余地が増える作りにしてはならない。

### 1-12. News と IR に固定序列を作らない
- 材料の強さは 新規性・サプライズ・直接性・経済的インパクト・継続性・市場反応・未織り込み度 で評価する。Discovery Source と Verification Source を分離し、同一出来事は 1 つの `material_event` に統合する。

### 1-13. ノイズ除去・紐付け
- 単純なキーワード除外は禁止（`market_relevance`）。`relation_type` を必ず保存し、`WEAK_ASSOCIATION` 単独では強材料扱いしない。マクロ材料は因果経路が必須。

### 1-14. 教師ラベル
- Objective（価格パスからコードで確定）と Interpretive（AI の Research 判定）を分ける。Interpretive には `labeler_model_version` / `confidence` / `evidence` / `human_review_status` を保存し、版管理された採用ポリシーを通さずに Production ML 教師データへ入れない。
- 見逃しは3分類: `ACTIONABLE_FALSE_NEGATIVE`（cutoff 時点でシステムが実際に利用可能だった情報から拾えたのに落とした）/ `PIPELINE_MISSED_ACTIONABLE_SIGNAL`（市場には cutoff 前から情報があったが、収集障害・遅延で `available_to_model_at` が cutoff 後。Prediction Model の False Negative にせず Pipeline 改善用）/ `OUT_OF_SCOPE_SHOCK`（市場にも合理的な前兆がなかった）。
- backfill された情報を、当時 AI が知っていたことにしない。事象の種類だけで一律に除外もしない。

### 1-15. Production と Research を分離
- Historical Replay は `research` 側にのみ保存する。Research の結果を無検証で Production に入れない。Champion / Challenger は walk-forward で比較する。

### 1-16. データリーク禁止
- 特徴量・分析は `data_cutoff` 時点で利用可能だったデータのみ（`available_to_model_at` / `fetched_at` / manifest `created_at` で判定）。
- ランダムシャッフルのみの train/test split 禁止。3,000円判定は raw 価格。後日公表の分割係数・訂正データを過去の判断に混ぜない。上場廃止銘柄を残す。

### 1-17. 確率表示
- 十分な教師データと校正ができるまで確率の数値を表示しない。

### 1-18. 監査可能性
- 重要処理には `run_id`・timestamp・cutoff 類・source・各種 version・error log を保存する。LLM 入力はハッシュとともに保存する。

### 1-19. 回帰テスト
- [docs/specs/regression-fixtures.md](docs/specs/regression-fixtures.md)（RF-01〜RF-24）を該当 Phase で必ず実装する。未実装のものは `pending` として残し、削除しない。

---

## 2. アーキテクチャ規約

- **ストレージ**: PostgreSQL は状態・索引・監査・結果。OHLCV・分足・約定・履歴 Feature・チャート画像・Raw・研究成果物は Parquet + Object Storage。詳細: [docs/storage-architecture.md](docs/storage-architecture.md)
- **Object Storage はプロバイダ非依存**（`ObjectStore` interface、上書き禁止）。
- **Job 実行は `JobRunner` / `Scheduler` interface 経由。** GitHub Actions を Production の実行環境として固定しない。
- **市場データは `MarketDataProvider` interface 経由。** 用途ごとに Provider を割り当て、run ごとに記録する。
- Provider・サービスの比較と選定の根拠は**公式情報のみ**。
- Web（Vercel）に重い全市場処理・場中監視・学習を載せない。
- Supabase は Phase 1 開発を Free で始めてよいが、**Production Architecture を Free の上限に合わせて縮小しない。** Production 開始前にプランを再評価する。
- 詳細: [docs/interfaces.md](docs/interfaces.md)

---

## 3. 進め方

**2026-09-17 方針変更: Phase ごとの停止監査を廃止し、連続実装に移行した。**（それ以前の「Phase ごとに停止 → 報告 → ChatGPT 監査 → 承認 → 次へ」は無効）

- Claude Code は**主任開発エージェントとして連続実装する**。小さな不明点で停止しない。
- **可逆的・安全側・versioned に決められる事項は自分で決める。** 決めたら記録する。
  - 「可逆的」= 新しい version / 新しい migration で戻せる。
  - 「安全側」= 誤っていたときに黙って誤った値を作るのではなく、明示的に落ちる・`UNKNOWN` を返す・記録に残す。
  - 「versioned」= `rule_version` / `feature_version` / `identity_version` 等を伴い、過去の判断を書き換えない。
- unit / integration / regression test を追加しながら進む。
- ChatGPT へ戻るのは**大きな統合マイルストーンのみ**。

### 停止してユーザーに確認するのは、この6つだけ

1. ユーザー本人の**契約・支払い**が必要
2. **credential / account 操作**が必要
3. **法的・利用規約上、本人確認**が必要
4. **不可逆な削除**
5. **Canonical 仕様と真正面から衝突**
6. **プロジェクト目的そのものを変える**判断

これ以外は止まらない。未解決の外部依存（例: D-102 JPX / D-103 Alpaca）は**開発を止める理由にしない**。

### `IMPLEMENTED_NOT_LIVE_VERIFIED`

実 Provider が未確定・credential 未取得の領域は、実装を止めるのではなく **`IMPLEMENTED_NOT_LIVE_VERIFIED`** として扱う。

- interface と schema と pipeline は完成させる。
- 入力は合成 fixture / deterministic mock で満たす。
- 「実データで検証していない」ことを**報告と DB の両方に明示**する。実データで動いたことにしない。

- 投資ロジックに関わる曖昧な判断は Decision Needed に回す。
- 決定・未決事項は [docs/unresolved-decisions.md](docs/unresolved-decisions.md) に記録する（行は削除しない）。

### 報告フォーマット

Phase 完了報告（指示書 §49）:

```
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

監査ラウンドで別の形式が指定された場合はそれに従う。
「すべて完成しました」で終えない。未実装・妥協・データ制約を必ず書く。

---

## 4. 技術規約

- Web: `apps/web`（Next.js / TypeScript）。Worker: `workers/`（Python 3.12）。
- ジョブは冪等に作り、`run_id` / `idempotency_key` 単位で再実行できるようにする。
- DB は本プロジェクト専用の Cloud Supabase（Free）。`loop-vocabulary`（Pause 中、削除禁止）・`kaiji-radar` を流用しない。
- **`supabase/migrations/` が DB 設計の唯一の正本。** Dashboard の手作業を正本にしない。Cloud に適用した DDL と Git 上の migration を一致させる。
- ローカル Supabase（Docker）は migration 検証・integration test・オフライン開発の補助であり、Phase の blocker にしない。
- 接続情報は Supabase CLI・ローカル環境変数・GitHub Secrets に置く。hard-code・commit・チャット出力・service role key のログ出力を禁止。
- **DB から外部を取得する経路は、短命な署名 URL + host allowlist + https に限る。** DB に raw API key を渡さない。DB から任意 host へ Authorization ヘッダを送らない。**loader は SECURITY DEFINER**とし、runtime role に `extensions` スキーマの USAGE を与えない（allowlist を迂回して `extensions.http` を直接叩けないようにする）。
- **Object Storage の認証情報は worker secret store のみ。** bucket 単位に絞った credential を使い、**匿名（anon）ポリシーを作らない**。Git・チャット・ログ・ブラウザへ出さない。DB へ渡すのは署名 URL だけ。
- **新しい function に PUBLIC EXECUTE を残さない。** default privileges と event trigger で防ぎ、テストで「project schema に public 実行可能な function が 0 件」を検査する。
- **Production の worker は専用の最小権限 LOGIN role で接続する**（DB owner で接続しない）。パスワードは DB 内で生成し Vault に保管する。
- **runtime の worker は自分の設定を書き換えられない。** `pipeline.load_host_allowlist`・`pipeline.sources`・`ref.exchanges`・`ref.identity_migration_map`・`universe.definitions`・`universe.decision_reasons` は SELECT のみ。変更は migration / DB 管理者が行う。
- **新しいテーブルは既定で読み取り専用。** `ref` / `pipeline` / `universe` の default privileges は SELECT のみなので、runtime が書くテーブルを追加する migration は書き込み権限を明示的に grant する。**runtime worker（`surge_worker_prod`）には `DELETE` をどのスキーマでも与えない**（research スキーマの DELETE は research ロールのもの）。
- **このリポジトリは Public。** 公開してよいのはコード・設計文書・Prompt・Schema・Test 等のみ。
- **絶対に commit しない**: API key / secret / token、`.env` / `.env.local` 等、Supabase service role key、Provider の認証情報、利用規約上再配布できない Raw ニュース等のデータ、Raw market data の大量ダンプ、Production DB dump、Object Storage 内の研究データ、Prediction の実データ、その他の認証情報。
- 研究データ・Prediction 実データ・Raw 取得データは Private な Supabase / Object Storage 側にのみ保持する。テスト fixture は合成データのみ。
- `.gitignore` と GitHub の secret scanning / push protection を前提にし、commit 前に `git status` と差分で対象ファイルを確認する。ローカルパス・個人のメールアドレスも書かない。
- 各データソースの利用規約を確認してから取得コードを書く。
- テストは本番 DB・実データの保存先に書き込まない。
- 外部に影響する操作（リポジトリ作成・push・クラウドリソース作成・有料契約）はユーザー確認後に行う。
