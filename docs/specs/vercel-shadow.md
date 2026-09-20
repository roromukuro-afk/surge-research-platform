# Phase B Web shadow（Vercel）設計

状態: **v0.3（2026-09-19、D-279）** — Stage 1・2 を実装し、Vercel 上で動作を確認した（§9）。Cron → 保護された production → Workflow → 完了の経路も no-op で確認済み。Stage 3（実データ・Jev なし）を実装しテストした（§8）。Vercel 上の最初の probe（S0 = 2026-09-18）で、Workflow の step 内では Yahoo の取得が全件失敗することが分かり（SDK と C 拡張の問題、§8）、3750fc5 で直した。**修正はまだ deploy していない**（チームの Functions Storage が Hobby の上限を超えているため、ユーザー判断待ち）。**正式な Phase B は何も変わらない。**

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
| 3 | Workflow で universe・Yahoo・screening・selection まで実データ（Jev には送らない）。PC と universe・pass 数・Primary・Control・request hash を比較 | **実装済み**（§8。Vercel 上の実行は Blob 使用量の確認後。PC と同じ S0 での比較は S0 = 2026-09-24 から） |
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
- `CRON_SECRET`: 使わない。Vercel Cron の要求は Deployment Protection（All Deployments）をシークレットなしで通過する（2026-09-19 実測、§9）。受け口は保護の内側にあり、チームのメンバー・この project の OIDC・Cron のほかは届かない。

## 7. ユーザーの判断が要るもの

1. Vercel project の新規作成（名前、Services 構成）と private Blob store の作成（region は functions と同じ `iad1`）。**Hobby の Blob 枠は `mea-stock-screener` と共有になる。**
2. Deployment Protection（Vercel Authentication、All Deployments）の有効化。
3. shadow の TypeSafe 支出上限（提案 $0.30）。

## 8. 実装したもの（Stage 1–3）

### Stage 1–2


- `workers/src/surge/storage/vercel_blob.py` — `ObjectStore` の Vercel Blob 実装（private、上書き不可、操作数の記録）。`SURGE_OBJECT_STORE=vercel-blob`。
- `workers/src/surge/shadow/artifacts.py` — artifact の layout・write-once・integrity record・run record。
- `workers/src/surge/shadow/export.py` — PC の cohort を shadow の形式に変換する（`python -m surge.shadow.export`）。fixture と、Stage 3・5 の比較に使う。
- `scripts/make_shadow_fixture.py` → `apps/web/fixtures/shadow/` — 合成の cohort（40 銘柄、4 営業日、うち 1 日は coverage 92.5% で停止、2 日分の outcome、partial report 2 本）。実際の Phase B のコード（scheduled job）に偽の Yahoo・Yanoshin・TypeSafe と偽の時計を通して作る。
- `apps/web`: `/`（Overview）・`/predictions`・`/runs`・`/outcomes`・`/reports`・`/system`。`SURGE_SHADOW_SOURCE=fixture|local:<dir>|blob`（未設定なら開発時は fixture、本番は「未設定」と表示）。Jev の確率は「Jev 自身の回答、未校正」と明記して表示する（CLAUDE.md 1-17）。
- テスト: `workers/tests/test_shadow.py`（Blob の意味論、改ざん検出、PC と hash が一致する export、ローカルパスや Canonical 本文が出ないこと、fixture の完全性）、`apps/web/scripts/shadow-smoke.mjs`（本番ビルドで 6 画面を fixture から描画、CI の web-contracts に追加）。

### Stage 3（実データ、Jev なし）— 実装済み、Vercel 上は未実行

- `workers/src/surge/shadow/day.py` — PC の `plan_day` と `build` を step に分けたもの。**分けるだけ**で、判定・選定・request の組み立ては凍結コードの関数そのもの（`jev_eval._fetch_one`・`screen`・`_screening_row`・`select_day`・`build`）を呼ぶ。
  - `day_window`: `plan_day` と同じ拒否（2026-09-24 より前、JPX の休業日、S0 の終値確定前、S1 が開き得る時刻以降）。確定の 15 分前以内に起動されたら Workflow の `sleep` で待つ。
  - `read_chunk`: 150 銘柄/step、PC と同じ順序・同じ 0.3 s の間隔、180 s の締切を過ぎたら残りを次の step へ（1 step で最低 1 銘柄は進む）。step の出力は screen 行・route 根拠・bar のある日付・digest 2 種（読んだ chart 行の SHA-256 と、as-traded の履歴 = bars と分割の SHA-256）だけで、価格は Workflow の storage に残さない。
  - `plan`: coverage 95%、30% のセッション規則、`select_day`。plan のファイル（manifest・sessions・universe・screening・candidates・population）は RunStore と同じ直列化で bytes にする。
  - `reread_selected`: 選定銘柄（最大 80）を同じ `now` で読み直し、**履歴の digest** が screen したときと同じことを確かめる（違えばその日は停止）。
  - `build`: 一時ディレクトリの RunStore に plan のファイルと選定銘柄の価格だけを置き、**凍結された `build()` をそのまま実行**する（Yanoshin の開示タイトル、request の bytes と SHA-256、o200k のトークン数）。request 本文は保存しない。
  - `write_day` / `write_stopped_day`: `surge/phase-b-shadow/<cohort>/<S0>/` に artifact を write-once で書き、integrity record（`system: vercel-shadow`、`status: built`、`jev: not called`）を最後に書く。停止した日は PC と同じ `day_stopped` の形。run 記録は `runs/<日付>/prediction-<n>.json`。
  - probe: 過去の営業日を読んで screen するだけ（選定も書き込みもしない）。見込みの日より前に、Yahoo への到達・step の所要時間・CPU・イベント数を Vercel 上で測るためのもの。
- `shadow_service/flows.py` の `shadow_day` workflow（`day_open` → `day_universe` → `day_chunk` × 約 25 → `day_plan` → `day_reread` → `day_build` → `day_write`、止まるときは `day_stop`）。起動は `POST /api/shadow/stage3/probe?s0=…&confirm=stage3-probe` と `POST /api/shadow/stage3/day?[s0=…&]confirm=stage3-day`（どちらも保護の内側、手動のみで cron には入れていない）。`GET /api/shadow/runs/<id>/events` で run のイベント数を種類別に数える。`/api/shadow/health` は入力構築コードの SHA-256（PC の manifest の `code_sha256` と比べる値）も返し、`?tokenizer=1` で o200k_harmony が読み込めるかを確かめる。
- テスト: `workers/tests/test_shadow_day.py` — PC の `plan_day` + `build` と同じ合成データで、分割版の manifest・sessions・universe・screening・candidates・population・requests・TDnet・stage 記録が **byte 単位で一致**。再取得でキー順や調整後終値が違っても履歴が同じなら続行、履歴が違えば停止。締切、coverage 不足、窓の判定。`workers/tests/test_shadow_service.py` — SDK のローカル world で workflow ごと: 160 銘柄を 150 + 10 の 2 step で読み、fake Blob に commit された artifact が PC と一致して検証も通る。probe は何も書かない。coverage 不足は停止日として記録される。
- `workers/src/surge/shadow/compare.py` — 同じ S0 の PC の日と shadow の日を比べる（`python -m surge.shadow.compare --s0 2026-09-24`）。universe・3,000 円以下の母集団・screening の pass・route membership・Primary・Control・request hash を項目ごとに一致／不一致で示し、食い違った銘柄を列挙する。request hash は (a) shadow が記録した入力から凍結 `build` で組み直した bytes との一致（コード）と、(b) PC の hash との一致（データ）の 2 段で、(b) の不一致は価格履歴か開示タイトルかに分解する。PC の日は読むだけで、何も書かない。テストで、同じデータなら全項目一致、PC の読み取り後に出た開示は「開示タイトル」、価格の食い違いは「価格履歴」と示されることを確認。
- **実測で分かったこと（2026-09-19、PC から Yahoo へ 7 銘柄・計 14 回）**: 同じ `period2` で同じ chart を 2 回取ると、JSON のキー順が変わることがあり、調整後終値（`adjclose`）の値も変わることがある。一方、凍結コードが使う素の OHLCV と分割から作る as-traded の履歴と、screen の判定は一致した。→ §2 の「再取得して digest と照合」は **履歴の digest** で照合する（chart 行の digest も記録するが、一致は求めない）。
- **比較の時期**: `select_day` は 2026-09-24 より前の S0 を拒否する（凍結）。したがって PC と同じ S0 で universe・pass・Primary・Control・request hash を比べられるのは **S0 = 2026-09-24 が最初**で、shadow の day はその送信窓（9/24 16:10 JST 〜 9/25 09:00 JST）の中で動かす。開示タイトルは読んだ時刻で変わり得るので、request hash は (a) 同じ入力（PC が記録した開示と価格）から作った bytes と、(b) 独立に読んだ入力から作った bytes の両方で比べる（§4）。それまでに Vercel 上で測れるのは probe（S0 = 2026-09-18 なら、PC のリハーサルの件数と比べられる）。
- **Workflow SDK と C 拡張（2026-09-19 の probe で判明、3750fc5 で修正、未 deploy）**: SDK は最初の run で `sys.modules` を独自の mapping に差し替え、その後に**初めて**読み込まれる C 拡張は step 内で `SystemError: … dictobject.c … bad argument to internal function` になる（SDK のローカル world で再現。Vercel 固有ではない）。probe では Yahoo の取得（curl_cffi）が全件これで失敗し、build のトークン数（tiktoken）も同じ理由で失敗するはずだった。workflow モジュールの先頭でホスト側に読み込み、sandbox の `passthrough_modules` に指定して解消した（ローカル world で Yahoo 取得と o200k 計数が 1 回目・2 回目とも成功、回帰テストあり）。`GET /api/shadow/selftest/extensions` が Vercel の step から Yahoo 1 件（crumb の取得を含む）と o200k 計数を確かめ、`GET /api/shadow/selftest/yahoo` が読み取り経路そのもの（cookie・crumb・chart・as-traded の履歴・分割）を、手で選んだ数銘柄（`?codes=`、detail）または本番と同じ 1 step 分（`?mode=chunk&universe=150`）で確かめる。どちらも Blob へは書かず、リクエストも作らない。Yanoshin（TDnet の索引）を Vercel から読む経路は、まだ一度も通っていない。
- **未実装**: 月の使用量の予算ガード（§5）。cron で自動運用する（Stage 5）前に入れる。

## 9. Vercel 上での実施記録（2026-09-19、ユーザー承認後）

| 項目 | 結果 |
|---|---|
| project | `surge-research-platform`（新規、Hobby、region iad1、OIDC 有効）。MCP では作成権限がなく（403）、ログイン済みの Vercel CLI で作成 |
| Blob store | `surge-shadow`（新規、**private**、iad1）を project に接続。`BLOB_READ_WRITE_TOKEN` は Vercel が設定し、手元には残さない（接続時に CLI が書き出した `.env.local` は中身を表示せずに削除） |
| Deployment Protection | Vercel Authentication = **All Deployments**（production を含む）。project 作成直後、最初のデプロイより前に設定 |
| Trusted Sources | この project 自身の既定（同じ環境同士、development → preview）に **development → production** を足した（CLI が発行する短命の開発用 OIDC トークンで production を検証するため）。長期のバイパス用シークレットは作っていない（`vercel curl` は無ければ自動で作る実装なので使わない） |
| 未認証アクセス | 3 つのホスト名 × 8 パス、Services 化の後は API を含む 7 パスすべてが Vercel のログインへ 302。本文にデータなし。ログインしていないブラウザでも「Login – Vercel」で止まる |
| 認証済みアクセス | 開発用 OIDC（ユーザーの CLI 認証から発行）で 6 画面すべて 200・期待する内容 |
| Services | `vercel.json` の `services`: `web`（`apps/web`、Next.js）と `shadow`（リポジトリのルート、entrypoint = `pyproject.toml`）。Python の関数と Workflows のキュー関数が別々にビルドされる（Python 3.12.14、uv） |
| Python Workflow SDK | `vercel-workflow` 0.10.2 が Services の中で動く。**workflow 本体は決定性の sandbox で再実行され、import 時のファイル操作（`Path.resolve()` 等）も拒否される** → パッケージの import を副作用なしにし、パス設定は step と ASGI アプリで行う（SDK のローカル world で発見・テスト化） |
| 凍結コードのクラウド実行 | `/api/shadow/health` がクラウドで frozen protocol の fingerprint を再計算し **`73cceb0d…`（公式 cohort と一致）**。curl_cffi 0.16.3 は import 可（Yahoo への実通信は Stage 3） |
| Cron → Workflow | `/api/shadow/cron/noop`（`0 14 * * *`）を `vercel crons run` で起動: Cron の要求（`x-vercel-cron-schedule` 付き）は**シークレットなしで保護を通過**し、Workflow run が起動して `completed`（preview での手動起動も 4.4 秒で完了） |
| Stage 2（クラウド） | 自己検証 workflow が合成 fixture の 28 object を private Blob に write-once で書き込み、全日を integrity 記録と照合して読み戻し、同じ内容の再書き込みは再実行扱い・別内容は拒否・元の内容は不変を確認。Blob 操作は advanced 31・simple 26 |
| Web 画面の Blob 読み出し | production を `SURGE_SHADOW_SOURCE=blob`・`SURGE_SHADOW_COHORT=synthetic-phase-b-fixture` にして再デプロイ（CLI から、push 済みの commit と同じ tree。project は Git 未連携）。開発用 OIDC で 7 画面（6 画面と停止日の predictions）すべて 200、integrity 照合を通って描画。直前の fixture 版と本文を比べ、違いは Blob にコピーした範囲（9/24 全体・9/28 の停止日・report-1・typesafe-1）と出典の表示だけ |
| 入力構築コードの同一性 | `/api/shadow/health` の `input_building_code_sha256` は当初 `18a8d21b…` で、PC の凍結 worktree の `8158344c…`（9/24 の PC の manifest に入る値）と違った。ファイル別に比べると（`?files=1`）30 本中 `ops/jev-gateway-runner/package-lock.json` だけが Python の関数バンドルに入っていなかった（アップロードはされていた）。`vercel.json` の shadow service に `includeFiles: ops/jev-gateway-runner/**` を足して、30 本すべて一致（`8158344c…`）。Workflow の step 側（別バンドル）は no-op step の出力で確認する |
| アップロードの範囲 | deployment のソース一覧に `bw.html`・CLAUDE.md・README.md と、作業ツリーの未追跡ファイル（`apps/web/.impeccable` など）が入っていた（どのサービスも使っていない）。`.vercelignore` を許可リスト（vercel.json・pyproject.toml・shadow_service・workers/src・docs/prompts・ops/jev-gateway-runner・apps/web）にし、deploy は push 済み commit の `git archive` から行う |
| 使用量（Chrome で確認、probe 前） | ユーザー指示で私が接続済み Chrome の Usage 画面を読んだ（30 日の移動窓、チーム全体）。Blob: Storage 5.25 MB / 1 GB、Simple 811 / 10K、Advanced 125 / 2K、Transfer 363.9 MB / 10 GB。Functions: Active CPU 1h 44m / 4h（43%）、Provisioned Memory 23.4 / 360 GB-Hrs。Workflows の Events と Data Written は**表示なし / 不明**（Hobby の画面には出ない。Workflows が内部で使う Queues は出る: Sends 90）。判断基準（表示項目がすべて 50% 未満）を満たしたので probe を実行。ほかに **Functions Storage 10.98 GB / 10 GB（上限超過）**、Deployment Storage 9.56 / 10 GB |
| Stage 3 probe（S0 = 2026-09-18、Blob への書き込みなし） | 50 step・153 events・Workflow storage 約 160 KB。universe 3,700（JPX workbook の SHA-256 は PC のリハーサルと同一）。**Yahoo は 3,700 件すべて失敗**（上記 SDK の問題。cookie/crumb の取得までも到達していない）。0 件取得のため、3,000 円以下の件数・pass 数は比較できない。読み取り step 47 本（1 本 79 銘柄、約 182 秒: 失敗後の 2 秒待ちと再試行で 180 秒の締切に達する）、読み取り step の CPU 合計 18.1 秒、最大メモリ 103 MB |
| 使用量（probe 後） | Active CPU 1h 46m / 4h、Provisioned Memory 28.3 / 360 GB-Hrs（+4.9）、Queue Sends 249（+159）、Blob は変化なし、**Functions Storage 11.36 GB / 10 GB**（この日の deploy 分が遅れて反映され、超過が拡大） |

- Blob の操作数（SURGE 分）は上の通り。チーム全体の月間使用量はダッシュボードでしか見えない（Stage 3 の前に確認する）。
- 検証用の no-op cron は毎日 1 回（14:00–14:59 UTC）残している: Stage 3 の cron を入れるときに置き換える。
- **Functions Storage の超過**: deploy のたびに Python の関数 2 本（約 44 MB ずつ）などが加わる。上限を超えたときの扱いは画面にも公式ドキュメントにも見当たらない。**2026-09-20 にチーム全体を棚卸しした**（15 project・177 deployment、`vercel list --all` と `/v13/deployments/<id>/builds`）: 関数バンドルの合計は 2.741 GB で、画面の 11.36 GB とは一致しない（画面はバンドルのバイト数より多くを数えている。比は約 4.1 倍）。現行 production・alias の指す deployment（alias の解決結果で判定する。deployment 側の `alias` / `aliasAssigned` は過去に割り当てられた記録で、現在の宛先ではない）・各 project の直前 production（rollback 用）を残すと、削除できるのは 138 deployment・1.247 GB（全体の 45.5%）。これを消せば画面は約 6.2 GB になる見込み（比が一定なら）。SURGE の古い deployment はユーザーの指示でこの回では消さない。**第 1 段階（2026-09-20 20:33–20:40 JST）**: 容量の大きい 4 project（math_mastery_srs・mea-stock-screener・online-juku-meme・loop-vocabulary）の 46 件を、削除直前に 1 件ずつ現況と照合して削除（39 件成功・0.986 GB、7 件は `vercel remove --safe` がブランチ alias 付きとして自動 skip）。**`vercel api ... -X DELETE` は確認フラグを要求して 1 件も消えない**ので `vercel remove <deployment id> --safe --yes` を使う。削除後のバンドル合計は 1.481 GB（削除前 2.741 GB）。画面の Functions Storage は削除の約 25 分後も 11.25 GB のままで、**この数値は日次の系列らしく即座には下がらない**（Deployment Storage は同じ画面で 1.68 GB まで落ちている）。次の deploy は、画面で 9 GB 以下を確認してから行う。
- 9/20 の cron の確認と 9/24 の shadow day・比較は、リポジトリ外の運用スクリプトを Claude の一時 task（各 1 回限り）が実行する。**deploy 後の確認は A→B→C→D の順**（`pre_deploy_checks.py`）: A = step 内の C 拡張、B = 1 銘柄を day と同じ経路で読む、C = 種類の異なる 10 銘柄（4 桁・新形式の英数字・出来高の大小・分割の有無）、D = 本番と同じ 150 銘柄の読み取り step 1 回。**3,700 銘柄の probe はやり直さない**（universe 全体を読むのは 9/24 の shadow day だけ）。9/24 は、production が期待する commit を動かし、A〜D が通り、Functions Storage の見積りが 10 GB 未満で、step 内で Yahoo が読めることを確かめてからでないと起動しない。
- **定時の起動は 2026-09-19 には記録されなかった**（14:00–15:00 UTC のランタイムログに `/api/shadow/cron/noop` は手動の `vercel crons run` の 1 件だけ。cron は登録済み: `vercel crons ls`）。この日は 14:21 UTC に production を入れ替えている。翌日の枠（2026-09-20 14:00–14:59 UTC）で再確認する。Cron の要求が保護を通過して Workflow が完了すること自体は、Vercel の cron 起動（`x-vercel-cron-schedule` 付き）で確認済み。
