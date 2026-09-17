# Phase 2.1 Runbook — Provider Smoke Test と Licence Purge

Phase 2.1 は**大量取得をしない**。各 Provider に数リクエストだけ投げて、公式ドキュメントと実際のレスポンスの差を洗い出す。

現在の状態:

| Check | 実行済みか | 必要なもの |
|---|---|---|
| `storage` | ✅（local store で実行済み） | なし（R2 で再実行するには R2 の資格情報） |
| `ecb` | ✅（実 API で実行済み） | なし（無料・キー不要） |
| `openfigi` | ✅（実 API で実行済み） | なし（無料・キー不要） |
| `jquants` | ❌ SKIPPED | **J-Quants Standard の契約と API キー** |
| `eodhd` | ❌ SKIPPED | **EODHD All World の契約と API トークン** |
| R2 への書き込み・purge 実証 | ❌ | **Cloudflare アカウントと R2 バケット** |

---

## 1. ユーザー作業：J-Quants（Standard ¥3,300/月・税込）

**重要（D-76）**: J-Quants は**登録者本人の私的利用に限る**。法人利用は社内限定・非営利でも不可、学術利用も学生本人の卒業論文を除き不可。解約・**ダウングレード時には保存データの削除義務**がある。契約は「この研究基盤が一人の私的研究であり続ける」という前提の下でのみ成立する。

1. <https://jpx-jquants.com/ja> を開き、右上から**新規登録**（メールアドレスとパスワード）。
2. ログイン後、**プラン選択**で **Standard（¥3,300/月・税込）**を選ぶ。支払いはクレジットカードのみ。
3. ダッシュボードの **API キー**（`x-api-key` に載せる文字列）を発行し、**コピーする**。
4. リポジトリ直下に `.env.local` を作り（`.env.example` をコピー）、次の2行を埋める:

   ```
   SURGE_JQUANTS_API_KEY=<コピーしたキー>
   SURGE_JQUANTS_PLAN=Standard
   ```

5. `.env.local` が Git 管理外であることを確認する:

   ```bash
   git check-ignore -v .env.local
   ```

   `.gitignore:5:.env.*	.env.local` のように**出力があれば成功**。無言なら **止めて**、`.gitignore` を直すまで先へ進まない。

**キーをチャット・Issue・ログに貼らないこと。** 私（Claude）はキーの値を見る必要がない。`.env.local` に置いてあれば worker が読む。

---

## 2. ユーザー作業：EODHD（EOD Historical Data — All World、$19.99/月 または $199/年）

1. <https://eodhd.com/pricing> で **EOD Historical Data — All World** を選ぶ（年額 $199 = 実質 $16.58/月）。
2. 登録後、<https://eodhd.com/cp/dashboard> の **API token** をコピー。
3. `.env.local` に追記:

   ```
   SURGE_EODHD_API_TOKEN=<コピーしたトークン>
   SURGE_EODHD_PLAN=EOD Historical Data - All World
   ```

   `SURGE_EODHD_PLAN` の文字列は `market.provider_plans` の値と**一字一句同じ**である必要がある（保存オブジェクトの外部キーになる）。

---

## 3. ユーザー作業：Cloudflare R2

1. <https://dash.cloudflare.com/sign-up> でアカウントを作る（R2 は無料枠 10 GB-月 / Class A 100万 / Class B 1,000万）。
2. 左メニュー **R2 Object Storage** → **Create bucket**。
   - 名前: `surge-market-raw`（任意、`.env.local` の値と合わせる）
   - Location: **Asia-Pacific (APAC)** を選ぶ（best-effort のヒントであり、日本国内保持の保証ではない）
   - **Object versioning は R2 には無い。** 本プロジェクトは content-addressed な write-once キーと条件付き書き込みで代替する。
3. R2 の **Manage API tokens** → **Create API token**。
   - Permissions: **Object Read & Write**
   - **Specify bucket(s)** で先ほどのバケット**だけ**を選ぶ（アカウント全体のトークンを作らない）
   - TTL は必要に応じて設定
4. 表示される **Access Key ID** / **Secret Access Key** / **Account ID** を `.env.local` に:

   ```
   SURGE_OBJECT_STORE=r2
   SURGE_R2_ACCOUNT_ID=<Account ID>
   SURGE_R2_BUCKET=surge-market-raw
   SURGE_R2_ACCESS_KEY_ID=<Access Key ID>
   SURGE_R2_SECRET_ACCESS_KEY=<Secret Access Key>
   ```

   Secret は**この画面を閉じると二度と表示されない**。
5. **J-Quants の raw object に無期限 Bucket Lock を設定しない**（監査指示 2）。削除義務があるデータを削除できなくする設定は、それ自体が規約違反になる。

---

## 4. 実行

```bash
cd workers
python -m pip install -e ".[dev,r2]"
set -a && . ../.env.local && set +a
python -m surge.jobs.phase21_smoke --out ../.local/phase21-smoke.json
```

`--checks storage,ecb,openfigi` のように一部だけ走らせることもできる。

**成功の判別**: 終了コード 0、かつ出力 JSON の `"failed": 0`。資格情報が無い check は `SKIPPED` として出る（`PASSED` にはならない）。

出力 JSON には各 Provider が**実際に返したフィールド名**が `*_field_names_returned` として入る。ドキュメントとの差はここで見る。

---

## 5. Licence Purge のリハーサルと実行

Purge は **`surge_purge` ロール**で接続する。`surge_worker_prod` にはどのスキーマにも DELETE が無く、purge 関数の EXECUTE も無い。

```sql
-- 1. 対象を数える（dry run）。削除はしない。
select market.open_purge_request('jquants', null, 'Light', 'Standard から Light へダウングレード', 'runbook', true);
select * from market.purge_targets where purge_request_id = <返ってきた id>;

-- 2. 何が残る予定かを見る
select * from market.outstanding_purge_obligations;
```

実際に削除するときは `dry_run := false` で開き、`workers` 側の `PurgeRunner` を使う（object storage・ローカルキャッシュ・Postgres の派生行をまとめて処理し、1件ずつ結果を記録する）。

**dry run は決して完了扱いにならない。** `market.complete_purge_request` は `dry_run = false` でかつ pending / skipped / failed が 0 のときだけ `completed_at` を立てる。

### 契約終了・ダウングレード時のチェックリスト

| 事象 | 実行する purge |
|---|---|
| J-Quants を解約 | `open_purge_request('jquants', null, null, ...)` — 全オブジェクト |
| J-Quants を Standard → Light | `open_purge_request('jquants', null, 'Light', ...)` — Standard 以上でしか取れなかったものだけ |
| EODHD を解約 | 規約に削除条項が**無い**（`NOT_SPECIFIED`）。義務としては発生しないが、判断のためにまず dry run で対象を見る |

---

## 6. 現時点で確認できていないこと

- **J-Quants v2 の実際の JSON フィールド名**。アダプタは v2 の短縮名（`O`/`H`/`L`/`C`/`Vo`/`AdjO`…）と v1 の長い名前の両方を受け付け、**どちらでもない場合はエラーにする**（黙って null にしない）。smoke test が実レスポンスのキー一覧を出力するので、契約後に確定させる。
- **EODHD の EOD 公表時刻**。公式に記載が無いため、`available_at` は自分の受信時刻（`OBSERVED_NOW`）を使う（D-78）。
- **R2 の `If-None-Match: *` の実挙動**。ローカルストアでは上書き拒否を実証済み。R2 での実証はバケット作成後。
