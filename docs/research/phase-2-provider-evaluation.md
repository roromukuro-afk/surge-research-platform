# Phase 2.0 Provider 評価記録（2026-09-16）

対象決定: **D-02a**（FX Provider）/ **D-03b**（Object Storage）/ **D-07a**（US EOD Market Data Provider）/ **D-40**（US security-level stable identifier）、および JP EOD Provider。

調査方針（[CLAUDE.md](../../CLAUDE.md) §2「Provider・サービスの比較と選定の根拠は**公式情報のみ**」）:

- 各候補を提供元の公式ドキュメント・料金ページ・利用規約・API リファレンスのみで評価した。第三者のブログ・比較サイト・LLM の記憶は根拠として採用していない。
- すべての候補について、別のエージェントが同じ公式ページを再取得して数値・条項・URL を照合した（29候補 × 検証）。照合で判明した誤りは本書に反映済み。
- **価格・規約ページは予告なく変わる。本書の数値はすべて 2026-09-16 時点の取得値である。**契約前に再確認すること。
- アカウント登録・ログイン・規約同意は一切行っていない。ログイン必須の情報は「公式に確認できず」として扱った。
- 本書は引用を最小限にし、根拠は URL で示す。条項の全文は各 URL を参照。

---

## 1. JP EOD

### 1-1. 候補の網羅性

JP（東証）の日足を供給する候補として調べた範囲では、**J-Quants 以外に該当する Provider が見つからなかった**。US 側候補の日本カバレッジを個別に確認した結果:

| Provider | 日本株 | 確認方法 |
|---|---|---|
| EODHD | **なし** | 公式の対応取引所一覧（<https://eodhd.com/list-of-stock-markets>）に日本の取引所が存在しない（韓国・台湾・中国・タイ等は存在）。`7203.TSE` / `7203.T` はいずれも 404。日本企業は独 Stuttgart・米 OTC 等の**外国上場**としてのみ出現 |
| Massive（旧 Polygon.io） | **なし** | Knowledge Base が米国市場のみと明記 |
| Tiingo | **なし** | 公式 symbology が US と中国のみと明記 |
| Sharadar (SEP) | **なし** | US public companies のみ |
| Financial Datasets | **なし** | US のみ |

### 1-2. J-Quants API v2（株式会社JPX総研 / JPX Market Innovation & Research）

公式: <https://jpx-jquants.com/ja>, `/ja/spec`, `/ja/spec/eq-bars-daily`, `/ja/spec/eq-master`, `/ja/spec/data-spec`, `/ja/spec/rate-limits`, `/ja/spec/data-update`, `/ja/help/data`, `/ja/help/usage`, `/ja/help/plan`, <https://jpx-jquants.com/termsofservice>

| 項目 | 内容 |
|---|---|
| 料金（月額・税込） | Free ¥0 / Light ¥1,650 / Standard ¥3,300 / Premium ¥16,500。アドオン（Light 以上）: 分足・ティック ¥5,500、TDnet ¥11,000 |
| 支払 | クレジットカードのみ、月額。日割り返金なし。ダウングレードは月1回まで。無料プランは1年で自動解約 |
| カバレッジ | **東証上場銘柄のみ**。地方取引所単独上場・PTS は対象外（公式明記）。銘柄数の公式値は公開されていない |
| 日足の取得可能範囲 | Free 12週間前〜2年12週間前 / Light 5年 / Standard 10年 / Premium 20年。アーカイブ自体の開始は 2008-05-07 |
| Raw / Adjusted | **両方**。O/H/L/C/Vo は調整前、AdjO/AdjH/AdjL/AdjC/AdjVo が調整済み。調整済み値は分割発生時に**遡って再計算**される（遡及期間に上限なし） |
| Corporate action | 日足行に `AdjFactor`（権利落ち日の調整係数）と `ExRT`（1=分割, 2=併合, 3=ライツイシュー）。対応は分割・併合・ライツイシューのみ。ライツイシューでは出来高は調整されない。外株・TOKYO PRO MARKET のライツイシューは調整対象外。配当は別エンドポイント（**Premium 限定**） |
| 上場廃止銘柄 | 価格は取得可能（上場期間内の日付指定）。ただし**上場日・上場廃止日の項目なし、上場廃止銘柄一覧なし**。日付のみ指定で全銘柄が返るため、取得済み期間については survivorship bias は構造的に発生しない |
| 銘柄コード変更 | **対応表・変更履歴は提供されない**。日次スナップショットの差分で自分で検出する必要がある |
| 外部識別子 | **ISIN / FIGI とも提供なし**（公式明記）。銘柄コードは5桁（末尾は株券種類の予備コード、普通株=0）。4桁で問い合わせると普通株のみが返り、優先株等が黙って落ちる |
| 更新時刻 | 日足 16:30頃、上場銘柄一覧 17:30頃 + 翌営業日8:00頃。更新タイミングは無通知で変更されうると明記 |
| 訂正の扱い | **既存データへの上書き**。旧データの保持も差分提供もなし。更新完了通知 API もデータの版番号も ETag も**なし** |
| Rate limit | Free 5 / Light 60 / Standard 120 / Premium 500 req/min。日次上限は公開されていない |
| 認証・実装 | `x-api-key` ヘッダのみ。`GET /v2/equities/bars/daily?date=...` で**全上場銘柄の1日分が1リクエスト**（JPX 自身が銘柄ごとのループを避けるよう明記） |
| SLA | なし（規約でシステム稼働率保証なしと明記） |

**ライセンス（決定的な制約）** — 規約第8条および <https://jpx-jquants.com/ja/help/usage>:

- 利用目的は**登録ユーザー本人の私的使用に限る**。第三者が使用できる状態にすること、**商用**または**学術**目的での利用は私的使用に該当しない。
- **法人による利用は営利・非営利を問わず不可**（社内限定・非営利でも不可）。法人・外部配信は別製品 J-Quants Pro（<https://pro.jpx-jquants.com/>、料金未調査）。
- 学術利用は**学生本人の卒業論文執筆に限る**。授業・ゼミでの集団利用、教職員の指導目的利用、研究者の論文執筆・学会発表は禁止。
- **自分が管理する外部クラウドへの保存は可**（本人のみが閲覧可能な状態を維持し、アクセス制御・暗号化は自己責任）。→ 本プロジェクトの Private Supabase / Object Storage 構成は規約の範囲内。
- **解約またはプランのダウングレード後は、保存データ・複製物・元データを復元できる派生物を削除**する義務。元データを復元できない派生物（学習済みモデル等）は、外部に配信・公開しない限り削除不要。
- 分析結果（チャート・グラフ・レポート）の共有は可だが、生データの配布は不可。分析結果を「継続反復的に」公開・共有する場合は私的利用と認められない。

---

## 2. US EOD

| 候補 | 月額 | 全市場一括 | Raw 価格 | Corporate action | 上場廃止 | 保存可否（規約） |
|---|---|---|---|---|---|---|
| **EODHD** All World | **$19.99**（年 $199 = $16.58/月） | **1リクエスト**（`eod-bulk-last-day/US`） | OHLC は raw。**出来高は分割調整済み** | splits / dividends 別エンドポイント。配当は調整前後の両方 | `delisted=1` で一覧、EOD 取得可。**2018年より前の廃止銘柄は splits/dividends なし** | **Non-Professional は保存・加工・分析を明示的に許諾**。ただし「displaying」も禁止列挙に含まれる。解約時削除条項は**なし** |
| Massive（旧 Polygon.io） | $29 / $79 / $199（個人）、$2,499（Business） | 1リクエスト（grouped daily） | `adjusted=false` で raw（**既定は調整済み**） | splits / dividends / ticker events（後者は実験的 `vX`） | `active=false` + `date=` で時点復元、`delisted_utc` あり | **個人プランは "display use only"、non-display use と Derived Works を禁止**。保存を明示的に許諾するのは $2,499/月 Business のみ |
| Sharadar Direct | $39（Prices Full History、年 $299） | バルクダウンロード（購入した履歴年数に対応） | **不可に近い**。OHLCV は分割調整済みで、unadjusted は `closeunadj` のみ。公式が「(1)と(3)は full OHLCV の imputation が必要」と明記 | ACTIONS テーブルが最良（分割・配当・スピンオフ・ティッカー変更・上場廃止理由） | 最良（`isdelisted`、廃止理由、1998年〜） | 個人・非職業利用に限る明示ライセンス。保存禁止条項なし。解約後30日以内に削除義務（研究成果物は保持可） |
| Nasdaq Data Link (SEP) | **公開価格なし**（ログイン必須） | Tables API + `qopts.export` | 同上（Sharadar と同一データ） | 同上 | 同上 | Order Form ベース。1.4(e)「cloud or other technology service」が本構成に対して曖昧。2026-11-01 に規約改定予定 |
| Tiingo | $30（年 $300） | **全市場エンドポイントなし**（5,442 リクエスト/日） | raw と adjusted を同一行で両方（最良） | 日足行に divCash / splitFactor。専用 API は beta | **自認で不完全**（「まだ recycle されていない ticker」に限る）→ survivorship bias 要件に抵触 | **無料プランは DB・object store への永続化を明示的に禁止**。有料プランも解約・**ダウングレード**時に全システムからの削除義務 |
| Financial Datasets | $200 + 超過（本ワークロードで実質 ~$340） | **なし**（日次 5,442 コール） | **文書化されていない**（`adjusted` パラメータも記載もなし） | **splits も dividends も存在しない**（OpenAPI 61パス全数確認） | プラットフォーム全体としては主張、価格側には廃止フラグも日付もなし | **全プランで商用・内部利用を明示的に許諾**（再配布のみ Scale 限定）— ライセンスは最も寛容 |

---

## 3. FX（USD/JPY）

| 候補 | 費用 | 観測時刻 | 当日取得 | 保存可否 | 判定 |
|---|---|---|---|---|---|
| **ECB euro reference rates** | 無料・キー不要 | 14:10 CET 前後に決定、**16:00 CET 公表** | ○ | 出典明記と改変の明示が条件。保存・再配布の禁止条項なし | **採用候補（一次）**。USD/JPY の直接系列はなく `JPY/EUR ÷ USD/EUR` で導出。翌営業日の公表後は**訂正・再公表されない**（規則で明記）ため再現性が最も高い |
| BOJ 時系列データ検索 FM08'FXERD04（17:00 JST 東京市場スポット） | 無料・キー不要 | **17:00 JST** | **×（API は2営業日遅れ）**。当日値は 17:50頃の PDF のみ（PDF アーカイブは70営業日） | 出典明記で可、商用は事前許可。API 利用サービスを公開する場合は日銀への通知義務 | 参考系列。日次運用には使えない |
| US Fed H.10 / FRED DEXJPUS | 無料（FRED は要 API キー・アカウント） | 12:00 ET（noon buying rate） | **×（週次公表・月曜16:15 ET）** | 制限なし。FRED は表示義務あり | 除外（最大6日の遅延）。ただし ALFRED の vintage 取得は再現性検証に有用（1971年〜） |
| OANDA Exchange Rates API | **$840/月**（Close を含む最低プラン Premium）。Lite $450 には Open/Close なし | 日次キャンドル（既定 UTC 境界） | ○ | ドキュメントは DB 保存を明示的に許諾。**ただし拘束力のある Rate Subscription Agreement が公式 URL で読めない**（301→マーケ頁、別 URL は Cloudflare 523） | 除外（費用が2桁違い。契約条項が未確認） |
| EODHD Forex（`USDJPY.FOREX`） | US EOD プランに**含まれる**（追加費用 $0） | **公式に記載なし**（日付のみ、タイムゾーンも瞬時も不明） | ○ | Non-Professional の保存許諾は FX にも及ぶ | **採用候補（二次・相互確認用）**。ただし公式が「indicative であり取引目的には不適」と明記 |
| Massive Currencies | $49/月（年額 $39/月） | `t` は**ウィンドウ開始**時刻のみ。close 時刻のフィールドなし | ○ | 個人プランは display use only / non-display use 禁止 | 除外 |
| Tiingo FX | $30/月 | 日足の確定時刻は非公開 | ○ | 無料プランは永続化禁止。有料もダウングレードで削除義務 | 除外（履歴が2020年〜と浅い） |
| TraderMade | £0〜£799/月（2つの公式料金ページが1桁食い違う） | 非公開 | ○ | **「保存・表示・配布を目的として一定間隔で系統的にクエリすること」を規約で明示的に禁止** | **除外**（本プロジェクトのアクセスパターンそのものを禁止している） |

---

## 4. Object Storage

| 候補 | 保存 | 転送（egress） | 無料枠 | Versioning / Object Lock | 日本リージョン | 資格情報のスコープ |
|---|---|---|---|---|---|---|
| **Cloudflare R2** | $0.015/GB-月 | **無料**（S3 API 経由の直接 egress） | 10 GB-月、Class A 100万/月、Class B 1,000万/月 | **Versioning なし**。Bucket Lock（期間・日付・無期限）で上書き/削除を防止 | **なし**（`apac` は best-effort の location hint） | **バケット単位に絞れる**（Object Read & Write トークン） |
| Backblaze B2 | $0.00695/GB-月 | **平均保存量の3倍まで無料**、超過 $0.01/GB | 10 GB | **S3 標準の Versioning + Object Lock（governance / compliance）あり** | **なし**（US West/East, EU Central, CA East のみ。**リージョンはアカウント単位**） | アプリケーションキーで prefix 単位に絞れる |
| Amazon S3（Tokyo） | $0.025/GB-月 | **$0.114/GB** | 既存アカウントには**なし**（新規のみ $200 クレジット/12ヶ月） | **完全対応**（Versioning / Object Lock / Glacier） | **あり**（ap-northeast-1 / ap-northeast-3） | IAM で最小権限（リソース ARN・prefix・条件） |
| Supabase Storage（既存 Free） | Free 1 GB / Pro 100 GB | Free **5 GB/月** | 1 GB | **なし**（削除は復元不可）。ライフサイクルも未実装 | あり（Tokyo） | **不可**。S3 アクセスキーは全バケットに対する全操作権限を持ち RLS を迂回する |

Supabase Storage の追加リスク: 無料枠超過時の制裁が**プロジェクト単位**（プロジェクト停止、DB の read-only 化、全 API への 402）であり、**Phase 1 の security master ごと止まる**。Free プランは 1週間の無活動でプロジェクトが一時停止し、最大ファイルサイズは 50 MB。

サイズ見積（本プロジェクト独自の試算、ベンダー文書の値ではない）: 9,149銘柄 × 250営業日 = 約229万行/年。Parquet（辞書 + ZSTD）で1行 50〜100 バイトとして **114〜229 MB/年**、10年で **1.1〜2.3 GB**。Raw 取得物のアーカイブを含めても数十 GB の規模。

---

## 5. D-40: US security-level stable identifier

| 候補 | 費用 | 粒度 | 保存・DB 化の可否 | 判定 |
|---|---|---|---|---|
| **FIGI**（Bloomberg / OMG、OpenFIGI 経由） | **無料** | `shareClassFIGI`（share class）/ `compositeFIGI`（国 composite）/ `figi`（取引所）の3層 | **パブリックドメイン献呈**。商用・非商用を問わず利用・改変・再配布可。FAQ が DB 保存の可否を明示的に肯定。ライセンス供与部分は撤回しないと規約に明記 | **採用候補** |
| CUSIP / CGS | 5,442銘柄は「Up to 10,000」帯 = **$46,825/年**（非金融研究割引75%で $11,706）。500未満は免除だがライセンス契約は必要 | share class | **規約が「CUSIP の master file / database の作成・維持」を目的としない利用に限ると明記**。ベンダー経由の間接取得でも自社ライセンスが必要 | **除外**（費用と規約の双方） |
| ISIN（CGS as US NNA）+ GLEIF ISIN-to-LEI | GLEIF ファイルは無料・CC0（2026-09-16 版は931万行、うち US ISIN 229万件） | share class | GLEIF 側は CC0。ただし **US ISIN は内部に CUSIP を埋め込んでおり、CGS はその部分に権利を主張**。両者の公表文書に整合の説明がない | **除外**（法的に未解決。加えて GLEIF ファイルには ticker も証券種別もなく、普通株を特定できない） |
| SEC（CIK / company_tickers / 13F list） | 無料 | **証券レベルの識別子は存在しない**（CIK は提出者。`company_tickers.json` は 10,422行・8,022 CIK で、1,435 CIK が複数 ticker を持つ） | 13F 四半期リストには CUSIP が載るが、SEC の PDF 版自体に CGS の再配布禁止表示がある（TXT 版には表示がない） | 補助（現状の REGISTRY_ANCHORED の根拠のまま。昇格材料にならない） |
| Massive `/v3/reference/tickers` | **$0 プランで取得可**（5 calls/min、1,000行/ページ） | `composite_figi` + `share_class_figi` + `cik` + `primary_exchange` が**1行に揃う** | FIGI 自体はパブリックドメイン。ただし無料プランの `date=` 時点指定は**2年**までで、それ以前に廃止された銘柄は時点復元できない | 併用候補（CIK↔FIGI の橋渡し） |
| EODHD ID Mapping API | US EOD プランに含まれる（1リクエスト1コール、1,000行/ページ） | `symbol` / `isin` / `figi` / `lei` / `cusip` / `cik` を1行で返す。**`filter[cik]` が使える** | CUSIP を返すため、**取り込み時に cusip/isin フィールドを破棄する**運用が必要（CGS の間接エンドユーザー条項） | 併用候補（CIK→FIGI の直接橋渡し） |
| Tiingo `permaTicker` | Fundamentals アドオン（**価格非公開・メール交渉**） | security | ベンダー固有。規約未確認 | 除外（単一ベンダーロックイン） |

FIGI の既知の欠点:

- **上場廃止銘柄は `includeUnlistedEquities: true` が必須**、かつ FIGI が保持している最終 ticker でしか引けない（例: `FRC`/`SIVB`/`LEH` は不可、`FRCB`/`SIVBQ`/`LEHMQ` は可）。
- **CIK は入力 idType として受け付けられない**。したがって現在の CIK アンカーからの橋渡しは TICKER+exchCode 経由になり、最初のマッピングは ticker の曖昧さを引き継ぐ（`FRCB` は**2つの異なる発行体**を返す）。
- Rate limit: キーなし 25 req/min・10 jobs/req、キーあり 25 req/6秒・100 jobs/req。バルクファイルの配布はない。
- ticker の記法差: SEC は `BRK-B`、OpenFIGI は `BRK/B`。

---

## 6. 本書の限界

- ログインが必要な情報は取得していない（Nasdaq Data Link の価格、J-Quants のダッシュボード、各社の実レスポンスサイズ・実銘柄数）。
- 実際の API レスポンスは**一切取得していない**（無料・無認証の ECB / BOJ / GLEIF / OpenFIGI / SEC を除く）。したがってカバレッジ実数・EOD 確定時刻・フィールドの実値は未検証である。
- 料金・規約は変わる。契約直前に再取得し、差分を本書に追記すること（本書は削除・上書きせず追記する）。
