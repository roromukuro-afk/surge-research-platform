# Phase B Web shadow（Vercel）設計

状態: **v0.1（2026-09-19、D-279）** — Stage 1（synthetic fixture で動く Web UI）と Stage 2（artifact storage）を実装済み。Stage 3 以降は未着手。**正式な Phase B は何も変わらない。**

## 0. 前提（変えないもの）

- 正式な Phase B は、PC の Windows Task Scheduler が frozen worktree（commit `3289fdf`）から動かす cohort `jev-phase-b-jp-20260924-v1`（D-277、D-278）。Web 版は **parallel shadow** であり、正式系の cohort・task・worktree を変更・削除しない。Windows task を自動で無効化しない。**切替はユーザーが判断する。**
- shadow は凍結ファイル（`phase_b.FROZEN_FILES`）を変えずに import して使い、実行のたびに cohort の frozen fingerprint と一致することを確かめる（不一致なら止まる）。
- Vercel は Hobby のまま使う（有料プランへの変更はしない）。無料枠を超えそうなら、実行する前に止めて報告する。
- CLAUDE.md §2「Web（Vercel）に重い全市場処理を載せない」に対し、ユーザーは 2026-09-19 に、Phase B の shadow に限って全市場の EOD 処理を Vercel Workflows（Web のリクエスト処理ではなく、step に分けたバックグラウンド実行）で動かすと決めた（D-279）。

## 1. 調査結果（2026-09-19 時点。公式ドキュメント・PyPI・実測）

### 1-1. `apps/web`

- Next.js 15.5（App Router）。既存の画面は Postgres の `ui` contract を `surge_web` ロールで読む（`SURGE_WEB_DATABASE_URL`）。`vercel.json` も `.vercel` もなく、Vercel project は未作成。
- 今回の変更: `/` を Phase B shadow の Overview にし、既存の Dashboard を `/dashboard` に移した。

### 1-2. Vercel project

- チームは Hobby の 1 つ。project は 14 個あり、SURGE 用のものはない。CLAUDE.md 1-1 により既存の project は流用しないので、**新規作成が必要**（クラウドリソースの作成 = ユーザー確認事項、§7）。

### 1-3. Vercel Workflows（Hobby）

| 項目 | Hobby | 出典 |
|---|---|---|
| Workflow Events | 50,000 / 月 | [Workflow Pricing and Limits](https://vercel.com/docs/workflows/pricing) |
| Workflow Data Written | 1 GB / 月 | 同上 |
| Workflow Data Retained | Hobby では使えない。run 完了後の保持は 1 日 | 同上 |
| 1 run あたり | steps 10,000、events 25,000（2,000 を超えると replay が遅くなる）、入出力 50 MB、replay 240 s、run の長さは無制限 | 同上 |
| 1 step の最長実行 | Function の上限（Hobby 300 s） | 同上 |
| Queues（Workflow が内部で使う） | 1,000,000 operations / 月 | [Queues Pricing and Limits](https://vercel.com/docs/queues/pricing) |

### 1-4. Python Workflow SDK と既存 worker の再利用

- `vercel-workflow` 0.10.2（PyPI、2026-09-15）。公式に **beta**。`@wf.workflow`、`@wf.step(max_retries=...)`、`FatalError`、`RetryableError(retry_after=...)`、`sleep()`（絶対時刻も可）、`pyproject.toml` の `[[tool.vercel.workflows]] entrypoint = "module:attr"` を Vercel の Python builder が読む。ローカル world（`WORKFLOW_TARGET_WORLD=local`）で手元でも動かせる。出典: [Python Workflows](https://workflow-sdk.dev/docs/getting-started/python)、PyPI の sdist。
- Next.js と同じ project に Python を置くには **Services（beta）** を使う（`vercel.json` の `services` に Next.js と Python を並べる、[Services](https://vercel.com/docs/services)）。**Services の中で Python Workflows を動かす組み合わせは、公式ドキュメントに例も記述もない** → Stage 3 の最初の deploy で最小の canary workflow を流して確かめる（**未検証**）。
- 既存 worker: Phase B の日次処理が使う外部パッケージは `curl_cffi`・`openpyxl`・`tiktoken`・`pyarrow`（PC の parquet 出力のみ）の 4 つで、残りは標準ライブラリ。凍結された判定（`screen`、`select_day`、`build_state`、`request_body`、`compute_outcome` ほか）は関数としてそのまま呼べる。ただし `plan_day` は 3,700 銘柄を 1 プロセスで回す作りなので、**step に分けるための orchestration は新しく書く**（凍結ファイルには触れない）。

### 1-5. curl_cffi

- 0.16.3 に `cp310-abi3-manylinux2014_x86_64` の wheel があり、Python 3.12 で入る。**Vercel 上で実際に動くか、Yahoo がクラウドの IP を拒否しないかは未検証**（Stage 3 の canary で測る）。

### 1-6. 1 step あたりの銘柄数

- PC の実測（2026-09-19 のリハーサル、S0 = 2026-09-18）: 3,700 銘柄を 1,703.8 s（0.46 s/銘柄、うち 0.3 s は意図的な pause）、失敗 0、価格 archive は非圧縮 60 MB。`screen()` の CPU はこの PC で 6.2 ms/銘柄（3,700 銘柄で約 23 s）。
- Hobby の上限 300 s に対し、**初期値は 150 銘柄/step**（見込み約 70 s = 上限の 1/4）。加えて step の中に 180 s の締切を持たせ、過ぎたら残りの銘柄を次の step に回す（最悪の 1 銘柄が重なっても 300 s を超えない）。並列度の既定は 1（Yahoo への頻度を PC と同じにする）。**確定値は Stage 3 で Vercel 上の p50 / p95 / 最大を測ってから決める。**

### 1-7. Vercel Blob

- 現在チームにある store は 1 つ（`mea-investment-ai`、4.88 MB・18 files、project `mea-stock-screener`）。
- Hobby: storage 1 GB/月、simple operations 10,000、**advanced operations（put・copy・list）2,000/月**、転送 10 GB。**超えると 30 日間 Blob が使えなくなる**。枠はチーム共有なので、SURGE と `mea-stock-screener` は互いの枠を消費し、互いを止め得る。ダッシュボードで store を閲覧する操作も advanced operations に数えられる。出典: [Vercel Blob Pricing](https://vercel.com/docs/vercel-blob/usage-and-pricing)
- 月の操作数は CLI / API からは取れない（`vercel usage` は "Costs not found (404)"、billing API は "Plan not found"）。**ダッシュボードの Usage でしか見えない。**

### 1-8. その他の Hobby の制約

- Cron: 1 日 1 回まで、精度は ±59 分（指定した時間帯のどこか）、100 本/project。[Cron Jobs Usage & Pricing](https://vercel.com/docs/cron-jobs/usage-and-pricing)
- Functions（Fluid compute）: 最長 300 s（既定 = 上限）、2 GB / 1 vCPU、Python の bundle は 500 MB まで。[Functions Limits](https://vercel.com/docs/functions/limitations)
- 月間の枠（チーム共有）: Active CPU 4 時間、Provisioned Memory 360 GB-hrs、Invocations 1,000,000。[Fair Use Guidelines](https://vercel.com/docs/limits/fair-use-guidelines)
- Deployment Protection: Vercel Authentication で production を含む全 deployment を保護でき、有料 add-on は不要（Password Protection は Hobby では使えない）。[Deployment Protection](https://vercel.com/docs/deployment-protection)
- Hobby は非商用に限る。

## 2. 構成（予定）

- Vercel project を 1 つ新規作成し、Services で `web`（`apps/web`、Next.js）と `shadow`（Python。`workers` の `surge` を import し、Workflows と cron の受け口を持つ）を並べる。Deployment Protection は Vercel Authentication（All Deployments）。
- Cron（UTC）: prediction `10 7 * * 1-5`（平日 16:10 JST）、outcome `0 9 * * 1-5`（平日 18:00 JST）。Hobby の cron はその時間帯のどこかで起動するため、workflow が `sleep(close_confirmed_at)` で 16:10 JST まで待つ。休業日（JPX calendar）は no-op、送信窓の外なら送らない（PC と同じ規則）。
- prediction workflow の step:
  1. calendar guard と frozen fingerprint の確認
  2. JPX の銘柄一覧（universe）
  3. Yahoo の履歴取得と `screen()`（150 銘柄/step、締切 180 s）
  4. sessions と candidate pool
  5. Primary / Control（`select_day`）
  6. anonymized / drift（同じ `select_day` が決める）
  7. 選定銘柄の価格の再取得（手順 3 の digest と照合）と Yanoshin
  8. request の組み立て（bytes の SHA-256）
  9. TypeSafe direct（**Stage 4 以降のみ**。1 request = 1 step、自動 retry なし）
  10. artifact の保存、integrity record（commit point）と run record
- step の出力は小さく保つ（銘柄ごとの screen 行と価格の digest）。価格そのものは Workflow の storage に残さない（Data Written 1 GB/月のため）。

## 3. Artifact（Stage 2 で実装）

`surge/phase-b/<cohort_id>/` の下に置く（`workers/src/surge/shadow/artifacts.py`）。

| key | 中身 |
|---|---|
| `cohort.json` / `calendar.json` | cohort（method は hash のみ）／ job が使う JPX 休業日 |
| `credit/typesafe-<n>.json` / `stops/cohort-stopped-<n>.json` | console の読み取り記録／cohort の停止 |
| `reports/report-<n>.json`、`reports/report-final.json` | rolling report と final report |
| `runs/<JST 日付>/<job>-<n>.json` | scheduled job の起動 1 回ごとの記録 |
| `<S0>/manifest.json`・`sessions.json`・`inputs/universe.json.gz`・`screening.jsonl.gz`・`candidates.jsonl.gz`・`population.jsonl`・`inputs/prices-selected.jsonl.gz`・`inputs/price-digests.json`・`inputs/tdnet.jsonl.gz`・`requests.jsonl`・`responses.jsonl.gz`・`predictions.jsonl`・`predictions-variants.jsonl`・`run.json` | 1 営業日分 |
| `<S0>/integrity.json` | その日の **commit point**（最後に書く。各 artifact の SHA-256 とサイズ） |
| `<S0>/outcomes.jsonl`、`<S0>/outcomes-integrity.json` | T+20 後の outcome と、その commit point |

- **write-once**: どの object も一度だけ書く（Blob は `allowOverwrite` を切って PUT し、拒否されたら読み戻して SHA-256 を比べる: 同じなら再実行、違えば conflict）。上書きはしない。
- 読む側は integrity record にある object だけを、記録された SHA-256 と照合して読む（Python・Web とも）。
- PC の `RunStore` と同じ JSON 直列化なので、中身が同じ artifact は PC のファイルと同じ SHA-256 になる（gzip の場合は `content_sha256`）。→ PC と cloud の比較は hash で行える。
- **request body は保存しない**（Canonical v5.1 と addenda の全文を含むため）。body の SHA-256 は `requests.jsonl` にあり、body は保存した入力と、記録された hash の method から作り直せる。
- PC の記録にあるマシン固有の情報（絶対パス・pid・host・traceback）は落とす。byte 単位でコピーするファイルにローカルパスが含まれていたら、書き換えずに export を止める。
- Blob 操作の見込み: 書き込みは 1 営業日あたり prediction 約 15 object + run 記録 2、outcome のある日はさらに 2〜3 → 月 22 営業日で約 400 advanced operations（2,000 の約 2 割）。Web の読み取りは list を使わない（key はカレンダーと cohort から導く）。見つかった object は永久にキャッシュし、未作成の key は 15 分（過去の日は 1 日）キャッシュする。まだ作られ得ない key（未来の日、T+20 前の outcome）は読みにいかない。

## 4. 段階と切替条件

| Stage | 内容 | 状態 |
|---|---|---|
| 1 | Web UI を synthetic fixture で作る | **実装済み** |
| 2 | Cloud artifact storage（API 送信なし） | **実装済み**（Blob の実接続は store 作成後。それまでは Blob と同じ規則の fake と local store で検証） |
| 3 | Workflow で universe・Yahoo・screening・selection まで実データ（Jev には送らない）。PC と universe・pass 数・Primary・Control・request hash を比較 | 未着手 |
| 4 | Jev を cloud から 1 件だけ smoke | 未着手 |
| 5 | shadow を 1 営業日実行し、Windows 版と selected cohort・request bytes・prediction・artifact を比較 | 未着手 |

- request hash の比較は 2 段で行う。(a) **同じ入力**（PC の artifact）から cloud のコードで作った request bytes が PC と完全に一致すること（コードの同一性）。(b) **独立に取得した入力**でも一致すること（データの同一性）。(b) で差が出たら、価格 bar・features・開示タイトル・questions のどれかに分解して示す。開示タイトルは取得時刻が違えば正当に変わり得る（S0 の 16:10 JST 以降に出た開示）。
- 切替の条件（ユーザーの 2026-09-19 の指示）: 同じ S0 で universe・screening・Primary / Control・request hash・Jev schema・outcome 計算・artifact integrity がすべて一致。判断はユーザー。

## 5. お金と枠

- **TypeSafe**: shadow の送信は正式系と同じ TypeSafe アカウントの credit（$5.00、2026-10-19 に失効）を使う。PC の credit 計算（console の snapshot − PC が記録した支出）は cloud の支出を知らない。そこで shadow の支出上限を **$0.30**（Stage 4 の 1 件 + Stage 5 の 1 営業日 ≈ $0.12）に固定し、Phase B の $2.50 cap が credit で必ず賄えるようにする（提案、§7）。25 営業日の並走（さらに $2〜3）は credit を圧迫するため、しない。
- **Vercel**: run ごとに Blob の操作数・step 数・関数の実行時間を記録し、月の累計が予算（Blob advanced 1,200、Workflow events 30,000 など）を超えそうなら実行前に止める（Stage 3 で実装）。他の project の分はダッシュボードでしか見えない。

## 6. Secrets

- `TYPESAFE_API_KEY`: Vercel の Environment Variables に dashboard から入力する（Sensitive）。Git・CLI の引数・ログ・artifact に出さない。Web では値を表示しない（設定されているかどうかだけ）。Stage 4 まで要らない。
- `BLOB_READ_WRITE_TOKEN`: store を project に接続すると Vercel が設定する。
- `CRON_SECRET`: cron の受け口の認証（Stage 3）。

## 7. ユーザーの判断が要るもの

1. Vercel project の新規作成（名前、Services 構成）と private Blob store の作成（region は functions と同じ `iad1`）。**Hobby の Blob 枠は `mea-stock-screener` と共有になる。**
2. Deployment Protection（Vercel Authentication、All Deployments）の有効化。
3. shadow の TypeSafe 支出上限（提案 $0.30）。

## 8. 実装したもの（Stage 1–2）

- `workers/src/surge/storage/vercel_blob.py` — `ObjectStore` の Vercel Blob 実装（private、上書き不可、操作数の記録）。`SURGE_OBJECT_STORE=vercel-blob`。
- `workers/src/surge/shadow/artifacts.py` — artifact の layout・write-once・integrity record・run record。
- `workers/src/surge/shadow/export.py` — PC の cohort を shadow の形式に変換する（`python -m surge.shadow.export`）。fixture と、Stage 3・5 の比較に使う。
- `scripts/make_shadow_fixture.py` → `apps/web/fixtures/shadow/` — 合成の cohort（40 銘柄、4 営業日、うち 1 日は coverage 92.5% で停止、2 日分の outcome、partial report 2 本）。実際の Phase B のコード（scheduled job）に偽の Yahoo・Yanoshin・TypeSafe と偽の時計を通して作る。
- `apps/web`: `/`（Overview）・`/predictions`・`/runs`・`/outcomes`・`/reports`・`/system`。`SURGE_SHADOW_SOURCE=fixture|local:<dir>|blob`（未設定なら開発時は fixture、本番は「未設定」と表示）。Jev の確率は「Jev 自身の回答、未校正」と明記して表示する（CLAUDE.md 1-17）。
- テスト: `workers/tests/test_shadow.py`（Blob の意味論、改ざん検出、PC と hash が一致する export、ローカルパスや Canonical 本文が出ないこと、fixture の完全性）、`apps/web/scripts/shadow-smoke.mjs`（本番ビルドで 6 画面を fixture から描画、CI の web-contracts に追加）。
