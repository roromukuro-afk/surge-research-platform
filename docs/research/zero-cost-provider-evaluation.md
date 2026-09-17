# Zero-Cost Provider 再調査（2026-09-17）

前提変更: **投資元本を継続的な Market Data 購読料へ流出させない。** Core Production System は incremental recurring cost = 原則 0円 で動作すること。J-Quants Standard と EODHD は `OPTIONAL_PAID` へ格下げ（削除はしない）。

調査方針: 公式情報のみ。**別エージェントが同じ公式ページを再取得して逐語照合**（6トピック × 検証）。**6件すべてで検証側が誤りを検出**したため、以下は訂正後の内容。

判定基準（4条件すべてを満たすこと）:
1. 月額0円（有料プランの無料枠は、それ単体で足り、かつ期限が無い場合のみ可）
2. **自分のDBへの保存が規約上許される**（「一時表示のみ」「蓄積禁止」は不可）
3. 自動取得が規約上許される（robots.txt と規約の両方）
4. 再配布・第三者表示は不要（本プロジェクトは行わない）

> **検証で2件の結論が逆転した。** 一次調査は「立花証券APIは蓄積禁止条項が無い」「Massive無料枠は保存可」と結論したが、再読で**立花のAPIページ自身に明示の蓄積禁止**が、**Massiveの規約§2に display use only の既定**が見つかった。どちらも「使える→使えない」方向の逆転で、これが本調査で最も重要な成果。

---

## 1. Zero-cost JP EOD options

### 1-1. 証券会社API — **全滅（規約）**

| 候補 | 判定 | 決め手 |
|---|---|---|
| **立花証券 e支店API** | **REJECTED（規約）** | APIページ「４．ご利用にあたって」7項: 「**当社提供情報は、蓄積、編集および加工等は禁止しております。**」（<https://www.e-shiten.jp/e_api/mfds_json_api_menu.html>） |
| 三菱UFJ eスマート証券 kabuステーションAPI | REJECTED（規約） | ご利用規約 第4条「**蓄積**、編集加工、二次利用」「情報を閲覧している端末以外への転載」を禁止 |
| 楽天証券 MARKETSPEED II RSS | REJECTED（規約） | 利用規定 第5条「転用、販売及び**蓄積**は固く禁じます」。加えて Excel 依存でサーバ常駐不可 |
| SBI証券 / 松井 / GMOクリック / マネックス | REJECTED | 株式の公開APIが存在しない |
| 岡三オンライン | REJECTED | 有料。かつ 2026-10-09 サービス終了 |
| SBIネオトレード | UNRESOLVED | 利用同意書が非公開で判定不能 |
| IBKR | REJECTED（有料） | 東証リアルタイムは月額300円 |

立花証券は**それ以外のすべてが理想的だった**（0円、HTTP GET/POSTでサーバ常駐可、日足20年+分割係数、上場日・上場廃止日付きの全銘柄マスタ、当日夕方18:00〜翌3:30更新）。一次調査は口座規程PDFだけを読んで「蓄積禁止条項なし」と結論したが、**APIページ本体に禁止が書かれていた**。加えて流量制限「秒10件」も公開されており（一次調査は「非公開」と誤記）、同ページは「すべての要求をこの上限で可能とするキャパシティーを当社は用意できておりません」とも述べている。

### 1-2. 公的・無料ソース

| 候補 | 判定 | 決め手 |
|---|---|---|
| **JPX 統計ファイル**（日報 stq PDF / 月間相場表 / data_j.xlsx） | **UNRESOLVED** | 唯一の「無料で当日（T+1）・全銘柄」のJP EOD。robots.txt は `Disallow:` 空（全許可）。規約は「**二次利用**及び再配信はできません」だが**二次利用が未定義**。一方で同ページは「必要に応じてファイルをダウンロード、保存することをお勧めします」とも書く |
| J-Quants **Free** | REJECTED（カバレッジ） | 保存は明示的に可。しかし**日足が12週間遅延**（当日スクリーニング不可）、かつ**無料プランは1年で自動解約**され、解約時に保存データの削除義務が発生する。保存した履歴が1年を超えられない |
| EDINET | QUALIFIES（価格ではない） | 公共データ利用規約 PDL1.0。「どなたでも…複製…自由に利用できます…商用利用も可能です」。スクレイピング禁止だが **API利用は明示的に推奨**。開示書類のみで価格は無い |

**JPX が唯一の道であり、その可否は JPX 自身に書面で問い合わせるしかない。** 問い合わせ先は統計ファイルのページに公開されている（東京証券取引所 株式部データサービス室）。聞くべきことは一つ: **「公表された統計ファイルとその値を、本人だけが閲覧する私的データベースに保持し、自分の投資判断にのみ使うことは『二次利用』に当たるか」**。

---

## 2. Zero-cost US EOD options

| 候補 | 判定 | 決め手 |
|---|---|---|
| **Alpaca Markets Basic（無料）** | **QUALIFIES_WITH_CAVEAT** | 最有力。$0・非期限（トライアルではなく口座の既定プラン）、履歴API 200 req/分。**永続化禁止条項が一切ない**。複製禁止は「publication or distribution or for any commercial enterprise」に限定され、私的DBはどれにも当たらない |
| Massive（旧 Polygon.io）Stocks Basic | **REJECTED（規約）** | Market Data Terms **§2**: 「**any and all Market Data is strictly for display use only**」。§5(d) が non-display use をライセンス無しに禁止。無人スクリーナーは典型的な non-display use |
| Tiingo Free | REJECTED（規約） | §1.6(a) が「databases, object stores」を名指しして永続化を全面禁止 |
| Stooq | REJECTED（アクセス） | 全URLが JavaScript proof-of-work のBot検証を返す。規約ページ自体が読めない。robots.txt も無い |
| Alpha Vantage Free | REJECTED（カバレッジ） | 25 req/日。**ライセンスは全候補中最も本用途に好意的**なので、スポット検証用に保持する価値あり |
| Finnhub Free | REJECTED（カバレッジ） | 日足エンドポイント自体が Premium |
| Twelve Data Free | REJECTED（カバレッジ） | 800 credits/日（対象5,400銘柄）。保存可能期間を定めた文書が存在しない |
| Marketstack Free | REJECTED（カバレッジ） | 100 req/**月** |
| Financial Modeling Prep Free | REJECTED（規約） | 保存禁止 |
| Tradier Lite | UNRESOLVED | $0・120 req/分・日足エンドポイントあり。しかし API規約にも顧客規約にも**市場データのライセンス条項が一切無い**。書面照会で解決可能 |

**Alpaca の残り2点**（口座保有者本人しか確認できない）:
1. 規約に「The Content and the Service are intended for United States residents only.」とある一方、Alpaca は非米国居住者向け口座を公式に販売している。**自社文書内の矛盾**であり、日本居住者として使う前に書面確認が要る。
2. 無料プランが **full SIP** 履歴を返すか **IEX のみ**（出来高の約2.5%）かがドキュメント内で矛盾。**無料APIキーで `feed=sip` を1回叩けば確定する**。IEX のみなら OHLCV の正本には使えない。

---

## 3. Broker API options（まとめ）

サーバ常駐で無人運用できる日本の証券会社APIは **立花証券のみ**だったが、そこが規約で落ちた。kabuステーションと楽天RSSは規約に加えて**デスクトップアプリ常駐が必要**で、そもそも無人運用の前提を満たさない。米国側では **Alpaca** と **Tradier** が該当し、Alpaca が先行。

---

## 4. Coverage achievable at ¥0/month

| 役割 | 無料で可能か | 供給源 |
|---|---|---|
| JP security master | ✅ | JPX 上場銘柄一覧（Phase 1 で稼働中） |
| US security master | ✅ | Nasdaq Trader symbol directory（Phase 1 で稼働中） |
| US delisting / ticker change | ✅ | Nasdaq Trader 日次スナップショットの差分 + **SEC EDGAR Form 25 / 25-NSE / 8-K** |
| JP delisting / 商号変更 / 権利落ち | ⚠️ | JPX の各データセット（robots.txt 全許可・「二次利用」解釈が未解決） |
| **FX USD/JPY** | ✅ | **ECB（実装済み・稼働確認済み）** |
| US identifier enrichment | ✅ | **OpenFIGI（実装済み・稼働確認済み）** |
| **US EOD 価格** | ⚠️ | **Alpaca Basic**（口座開設と2点確認が必要） |
| **JP EOD 価格** | ❌ | **無料で明確に適法な供給源が存在しない** |
| JP 履歴バックフィル | ⚠️ | J-Quants Free（12週遅延・1年で削除義務） |

**Nasdaq Trader の規約**は一見「storage 禁止」に読めるが、例外が本用途を覆う: 「one unaltered permanent copy to be used by the viewer for **personal and non-commercial use** only」。

---

## 5. Unavoidable coverage gaps

1. **JP の当日EOD価格**。0円で明確に適法な経路が無い。JPX への書面照会が唯一の解決路で、否定回答なら選択肢は (a) J-Quants Standard ¥3,300/月を有料で入れる、(b) JP を当面スコープ外にする、(c) 別の無料源を探し続ける、の3つしかない。
2. **US の履歴の深さ**。Alpaca 無料枠の履歴範囲は要確認。Massive 無料枠は2年だったが規約で落ちた。10年バックフィルは無料では成立しない見込み。
3. **JP のコーポレートアクション**。JPX データセットに依存し、同じ「二次利用」解釈リスクを共有する。
4. **US 配当・分割の構造化データ**。SEC は上場廃止は出すが、配当・順分割・ティッカー変更の構造化データは出さない。Alpaca が出すかは要確認。

---

## 6. Core recurring cost

**現在の Core recurring cost = ¥0/月**（`market.recurring_cost` が 0 を返すことを Cloud で確認済み）。

ただし**インフラ側に3つの条件**がある。

| | 無料枠 | 先に効く制約 |
|---|---|---|
| Cloudflare R2 | 10 GB-月 / Class A 100万 / Class B 1,000万 / egress 無料 | **バケット作成前に subscription checkout が必須**でカード登録を伴う。超過は自動停止せず課金される（$0.015/GB-月）。**支払い失敗時はバケットにアクセスできなくなり、30日でデータ削除の可能性**。GB-月は**日次ピークの平均**で計算される |
| Supabase Free | DB 500 MB / egress 5 GB | **500 MB が最初に効く。**10年分の日足をPostgresに入れると約3.5 GBで7倍超過 → **Postgres は index/state のみ、履歴は Parquet on R2** という現行設計が必須条件になる。1週間無活動でプロジェクト一時停止（公式に「1日数リクエストで回避できる」と明記）、停止後の復元期限は1年 |
| **GitHub Actions** | public repo は分数無制限 | **⚠️ 規約リスク。** Actions の追加規約は、GitHub-hosted runner の利用を「**production, testing, deployment, or publication of the software project associated with the repository**」に限定し、「Any activity that places a burden on our servers... disproportionate to the benefits」を禁じる。**日次の外部クロールはこれに当たらない読み方が自然**で、違反時の措置は課金ではなく**リポジトリ無効化またはアカウント停止**。scheduled workflow は高負荷時に**遅延ではなく破棄される**ことがあり、60日無活動で自動停止する |

**GitHub Actions を日次取得の実行環境にしない。** 代替は (a) ローカル/自前マシンでの実行、(b) Oracle Cloud Always Free（A1: 2 OCPU / 12 GB。回収リスクあり）、(c) GitHub へ書面確認。CI（テスト実行）としての Actions 利用は規約どおりの用途なので問題ない。

---

## 7. Optional paid-provider improvements

`OPTIONAL_PAID` として保持し、Production Core を置き換えず **Research Challenger** として評価する。

| | 月額 | 無料枠に対する改善 |
|---|---|---|
| J-Quants Standard | ¥3,300 | **JP EOD の唯一確実な適法経路。**日足10年、当日16:30更新、調整係数と権利落種別つき。無料では埋まらないギャップを唯一埋める |
| EODHD All World | $19.99 | US を1リクエストで全市場、raw OHLC、上場廃止銘柄、splits/dividends。Alpaca が SIP を返さない場合や履歴が浅い場合の代替 |

測るもの: coverage improvement / missing price reduction / corporate action accuracy / revision detection / Stage 1 recall / actionable FN reduction / Prediction performance。**改善幅が購読料を正当化できる場合のみ**継続契約を検討する。

---

## 8. 本書の限界

- アカウント登録・ログイン・規約同意は一切行っていない。無料枠の中身で公開ページから確認できない部分（Alpaca の SIP/IEX、履歴の深さ、Tradier の実データ）は**未検証**。
- Stooq は Bot 検証により規約すら読めておらず、「読めない」以上の判定はしていない。
- 規約・料金ページは予告なく変わる。本書は 2026-09-17 時点。
- 判定はすべて `market.provider_evaluations` にも行として記録してある（`market.zero_cost_status` で役割別の集計が見られる）。
