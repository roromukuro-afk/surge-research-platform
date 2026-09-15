# Interface 設計（Phase 0.1）

状態: **設計草案**。コードは Phase 1 以降。ここに書くシグネチャは仕様記述のための擬似コードであり、実装言語の最終形ではない。

目的: 実行環境（GitHub Actions / 常駐 Worker 等）、市場データ Provider、Object Storage を**交換可能**にし、どれを選んでも投資ロジック・監査・リーク防止の挙動が変わらないようにする。

---

## 1. JobRunner / Scheduler

### 1-1. 責務の分離

| コンポーネント | 責務 | 持たないもの |
|---|---|---|
| **Job** | 1つの処理（例: `jp_eod_universe`, `watch_monitor`, `entry_decision`）。入力は `JobRequest` のみ | 実行環境の知識 |
| **Scheduler** | いつ何を実行するかを決め、`JobRequest` を発行する（取引カレンダー・市場セッションに基づく） | 実行 |
| **JobRunner** | `JobRequest` を受け取り、どこかで Job を実行する | スケジュール判断 |
| **Job Store（Postgres）** | リクエスト・リース・状態・ログの正本 | — |

### 1-2. JobRequest

```
JobRequest {
  request_id
  job_name               # 例: "jp_eod_universe"
  job_version            # Job 実装の版（git_sha と併記）
  run_mode               # PRODUCTION | RESEARCH | DEV
  market                 # JP | US | GLOBAL
  params                 # data_cutoff / decision_cutoff_at / replay_run_id など
  idempotency_key        # 例: "jp_eod_universe:JP:2026-09-15T06:30:00Z"
  requested_by           # scheduler_id | manual | replay
  not_before, deadline
  max_attempts, timeout_seconds
}
```

- `idempotency_key` は Postgres で一意制約。複数の Scheduler が同じリクエストを発行しても**二重実行しない**。
- Job は常に同じ入口で起動する（例: `python -m workers.jobs run --request-id <id>`）。どの Runner でも同じコードが同じ入力で動く。

### 1-3. JobRunner interface

```
interface JobRunner:
    runner_id: str
    capabilities() -> RunnerCapabilities   # max_duration, persistent, concurrency, has_network, ...
    submit(request: JobRequest) -> SubmissionHandle
    status(handle) -> RunStatus
    cancel(handle) -> None
```

実装候補（**どれも固定しない**）:

| 実装 | 方式 | 向く用途 | 制約（確認済みの事実） |
|---|---|---|---|
| `LocalRunner` | ローカルでプロセス起動 | 開発・テスト | 可用性なし |
| `GitHubActionsRunner` | `workflow_dispatch` で request_id を渡して起動 | 日次バッチ、学習 | 1ジョブ最大6時間。非公開リポジトリ無料枠は月2,000分（GitHub Free） |
| `QueueWorkerRunner` | 常駐 Worker が `pipeline.job_requests` を `FOR UPDATE SKIP LOCKED` で取得 | 場中の Watch 監視・ENTRY 判断・ニュース収集など、短間隔・低遅延の処理 | 常駐環境の費用・運用が必要 |
| `CloudJobRunner`（将来） | マネージドなジョブ実行サービス | バッチの置き換え | 未調査 |

### 1-4. Scheduler interface

```
interface Scheduler:
    scheduler_id: str
    tick(now) -> list[JobRequest]    # 発行したリクエストを Job Store に登録
```

- スケジュール定義は実行環境から独立したデータ（例: `schedules.yaml`）として持つ。
  - `on: EOD_DATA_AVAILABLE(market=JP)` → `jp_eod_universe`
  - `on: SESSION_OPEN(market=US) + offset` → `entry_decision`（Setup 対象）
  - `every: N minutes during SESSION(market)` → `watch_monitor`
- トリガーの実体（GitHub Actions の cron、常駐 Worker 内のループ等）は `tick()` を呼ぶだけ。
- 休場日は `tick()` がリクエストを出さず、スキップ理由を記録する。

### 1-5. リースと冪等性（Runner 非依存の保証）

| 列（`pipeline.job_requests`） | 用途 |
|---|---|
| status | QUEUED / CLAIMED / RUNNING / SUCCEEDED / FAILED / CANCELLED |
| claimed_by, lease_expires_at, heartbeat_at | 二重実行防止。リースが切れたら再取得可 |
| attempt | 再試行回数 |
| run_id | `pipeline.runs` への参照 |

- Production の Prediction 作成など「1回しか起きてはいけない」書き込みは、Runner ではなく**DB の一意制約**で守る（例: `(security_id, decision_window_id)`）。

---

## 2. MarketDataProvider

### 2-1. Provider の役割バインディング

同じ Provider を全用途に使う前提にしない。用途ごとに Provider を割り当て、run ごとに記録する。

| 役割（role） | 用途 | 必要な性質 |
|---|---|---|
| `SECURITY_MASTER_JP` / `_US` | 銘柄一覧・種別・取引所・上場廃止 | 上場廃止銘柄の履歴、種別コード |
| `EOD_UNIVERSE_JP` / `_US` | Stage 1 用の全銘柄日足 | 全銘柄一括取得、無調整価格 + 調整情報 |
| `INTRADAY_HISTORY_JP` / `_US` | Stage 2・パス解決・Replay 用の分足履歴 | 対象銘柄の過去分足 |
| `TRADES_HISTORY_JP` / `_US` | パス解決（最小足が曖昧な時間帯）・`entry_reference_price` の事後算出 | 約定の時刻・順序（提供される場合） |
| `REALTIME_DECISION_JP` / `_US` | Watch 監視・ENTRY 判断時の `decision_price`・当日分足、判断後の `entry_reference_price` の観測 | 遅延区分 `REALTIME`（D-21）。**日次更新の Provider（J-Quants の分足・ティック等）は割り当て不可** |
| `FX_USDJPY` | USD/JPY | 観測時刻付き |
| `CORPORATE_ACTIONS_*` | 分割・併合等 | 公表時刻（known_at） |

設定例（値は未決定）:

```
provider_bindings:
  EOD_UNIVERSE_JP: jquants
  EOD_UNIVERSE_US: <D-07a>
  REALTIME_DECISION_JP: <D-06b>
  REALTIME_DECISION_US: <D-07a>
  FX_USDJPY: <D-02a>
```

### 2-2. interface

```
interface MarketDataProvider:
    provider_id: str
    capabilities() -> ProviderCapabilities
        # markets, roles_supported, history_depth, latency_class (REALTIME | DELAYED_15M | EOD | DELAYED_WEEKS),
        # adjusted_and_raw, intraday_granularity, rate_limit, license_scope (personal/internal/commercial)

    list_securities(market, as_of_date, include_inactive: bool) -> Result[list[SecurityRecord]]
    get_daily_bars_all(market, trade_date) -> Result[list[DailyBar]]          # 一括が無い Provider は内部で分割取得
    get_daily_bars(symbols, start_date, end_date) -> Result[list[DailyBar]]
    get_intraday_bars(symbols, start_ts, end_ts, granularity) -> Result[list[IntradayBar]]
    get_trades(symbols, start_ts, end_ts) -> Result[list[Trade]]              # 任意の能力。capabilities().trades で宣言
    get_price_observation(symbols, at_or_before: timestamptz) -> Result[list[PriceObservation]]
    get_corporate_actions(market, start_date, end_date) -> Result[list[CorporateAction]]

interface FxProvider:
    get_fx(pair, at_or_before: timestamptz) -> Result[FxObservation]

Result[T] {
  data: T
  provenance: { provider_id, endpoint, request_params, requested_at, received_at,
                raw_object_key, http_status, provider_plan, latency_class }
}

DailyBar        { security_ref, trade_date, open, high, low, close, volume, turnover?, vwap?,
                  is_adjusted: false, adjustment_factor?, source_fetched_at }
PriceObservation{ security_ref, price, basis (LAST_TRADE | MINUTE_CLOSE | BID | ASK | MID),
                  observed_at, latency_class }
FxObservation   { pair, rate, observed_at, basis, source }
```

### 2-3. 共通の保証（Provider 実装に関係なく強制）

1. すべての応答の raw を `raw/api_responses` に保存し、`provenance.raw_object_key` を持つ。
2. 価格は**無調整**を正本として保存。調整済み値のみを返す Provider は、そのままでは `EOD_UNIVERSE` に使えない（調整係数から無調整を復元できる場合のみ可）。
3. `get_price_observation` / `get_fx` は `at_or_before` を超える観測を返さない。呼び出し側でも `observed_at <= decision_cutoff_at` を再検査する。
4. `latency_class` を Prediction に保存する。ENTRY 判断ジョブは、許可された遅延区分以外の Provider を拒否する（許可範囲は D-21）。`capabilities().update_schedule` が日次の Provider は `REALTIME_DECISION_*` に割り当てられない（設定検証で拒否）。
5. 利用規約上の用途（個人・非プロ・内部利用など）を capabilities に持ち、設定時に確認できるようにする。
6. `decision_price` と `entry_reference_price` は別の呼び出しで観測する。前者は `at_or_before = decision_cutoff_at`、後者は判断完了後の観測（算出方式 `entry_price_method` は D-01a）。
7. `capabilities().trades` は「構成上提供しない」（`NOT_SUPPORTED`）と「提供するが当該時間帯が欠損」（結果の欠損）を区別して返す（パス解決の `AMBIGUOUS_PATH` / `UNRESOLVED_MISSING_DATA` の判定に使う、D-09b）。

### 2-4. J-Quants Provider（抽象化の方針）

| プラン | 本プロジェクトでの位置付け |
|---|---|
| Free | **開発用**（公式: 直近12週間を除く2年分 = 遅延データ）。Production では使わない |
| Light 以上 | **Production EOD の候補** |
| Standard | 信用取引週末残高等を含む。**教師データで有効性が確認されるまで必須にしない** |
| Premium / アドオン | 前場・後場四本値（Premium）、分足・ティック（アドオン） |

**分足・ティックの用途（監査 0.2 #2、公式の更新スケジュールで確認済み）**: 株価四本値・分足・ティックの更新は日次 16:30頃（確約ではない）で、リアルタイム配信ではない。したがって `JQuantsProvider` は `INTRADAY_HISTORY_JP` / `TRADES_HISTORY_JP`（Historical research / EOD / Replay / Teacher data）にのみ割り当て、**`REALTIME_DECISION_JP` には割り当てない**。場中用は別 Provider（D-06b）。

`JQuantsProvider` はプランを設定値として持ち、`capabilities()` がプランに応じた `history_depth`・`latency_class`・利用可能 API を返す。

---

## 3. ObjectStore

```
interface ObjectStore:
    store_id: str
    put(key, bytes, content_type, sha256) -> ObjectRef     # 既存 key への上書きは拒否（immutable）
    get(key) -> bytes
    head(key) -> ObjectMeta
    list(prefix) -> iterator[ObjectMeta]
    presign_get(key, expires_seconds) -> url               # Web からの Excel 取得等
```

- 実装候補: S3 互換（R2 / B2 / S3）、Supabase Storage、ローカルファイルシステム（開発）。
- 上書き禁止をインターフェースで保証する（訂正は新 key + manifest の `supersedes_manifest_id`）。

---

## 4. LLMProvider（参考・Phase 7 で詳細化）

```
interface LLMProvider:
    provider_id, model_id
    analyze(input_bundle_ref, prompt_bundle_ref, output_schema) -> LLMResult
        # LLMResult: structured_output, raw_output_ref, input_sha256, usage, latency
```

`prompt_version` / `llm_provider` / `llm_model` / `input_sha256` を必ず保存する。
