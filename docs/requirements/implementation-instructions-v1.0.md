<!--
ユーザー提供の原文（2026-09-15 受領）。
文言は変更していない。Markdown として表示するため、節番号の行を見出し(##)にし、箇条書きを "- " に揃えた整形のみ行っている。
-->

# 短期急騰AI研究プラットフォーム
# Claude Code 共同開発・実装指示書 v1.0

あなたはこのプロジェクトの主任開発エージェントである。
ただし、投資分析思想・教師データ設計・予測ロジックについて独断で簡略化・変更してはいけない。
本プロジェクトは、

- ユーザー
- Claude Code
- ChatGPT

による共同開発で進める。
役割は以下とする。
Claude Code

- システム設計
- コーディング
- データベース
- API連携
- Webアプリ
- データパイプライン
- ML
- テスト
- デプロイ
- 技術的改善

ChatGPT

- 要件監査
- 投資分析ロジック監査
- データリーク監査
- 教師データ監査
- スクリーニング監査
- 材料分析監査
- チャート分析監査
- Claude Codeへの修正指示

ユーザー

- 最終方針
- 投資思想
- 優先順位
- 仕様承認

Claude Codeは大きなPhaseを完了するたびに、

- 実装内容
- 変更ファイル
- テスト結果
- 未実装項目
- 仕様上迷った点
- 妥協した点
- データ制約

を報告し、一度停止する。
その報告をChatGPTが監査し、次の修正・開発指示を与える。
一気に最後まで作り切ろうとしないこと。

## 1. 最重要ルール：完全新規プロジェクト
既存のプロジェクト、既存のVercel Project、既存のSupabase Project、既存のリポジトリ、既存サイトを流用してはいけない。
今回のシステムは、
完全な新規プロジェクト
としてゼロから作成する。
既存プロジェクトを検索して「これを使えそう」と判断してはいけない。
既存システムとの統合も、ユーザーから後から明示されない限り行わない。

## 2. プロジェクトの目的
日本株・米国株について、
対象となる全上場普通株を毎営業日可能な限り全件取得
し、
最初に絶対条件として、
日本株
株価 ≤ 3,000円
米国株
株価 × 同時点USD/JPY ≤ 3,000円
を適用する。
そのEligible Universe全体に対して、

1. 数値スクリーニング
2. チャート・需給スクリーニング
3. 材料・ニューススクリーニング
4. AI詳細分析
5. 現在価格からEntryできるかの判断
6. Prediction保存
7. その後の株価・材料変化追跡
8. 結果検証
9. 教師データ化
10. システム・モデル改善

を継続的に行う。
目的は、
「結果的に上がった株を説明するAI」ではない。
目的は、
現在地点からEntryでき、約1か月以内に+20%以上の上昇を狙える銘柄を、上昇前または上昇初期で発見するAIを育てること
である。

## 3. v5.1を必ず原文保存する
別途ユーザーから渡される、
「短期急騰専門・3000円以下限定 完全版プロンプト v5.1」
をプロジェクト内部へ原文のまま保存する。
例：
`docs/prompts/short-surge-v5.1.md`
勝手に、

- 要約
- 短縮
- リライト
- 条件削除
- 配点変更

してはいけない。
Git管理する。
将来v5.2、v5.3等へ変更した場合も旧版を残す。
Predictionには必ず、
`prompt_version`
を保存する。

## 4. v5.1後に確定した追加仕様
v5.1原文は保存するが、その後の共同議論によって以下が追加仕様として確定している。
これらは実装時にv5.1より優先する。

### 4-1. 正式Predictionは「現在Entry可能」の場合のみ
最終的な正式Predictionは、
現在の価格・現在の市場状態からEntry可能
とAIが判断した場合のみ作成する。
「面白いがブレイク待ち」
「押し目待ち」
「もう少し下がれば買いたい」
は正式Predictionではない。

### 4-2. WatchとPredictionを分離
判定状態を最低限、

- `ENTRY`
- `WATCH_BREAKOUT`
- `WATCH_PULLBACK`
- `WATCH_OTHER`
- `REJECT`

に分ける。
Predictionとして成績計測するのは、
ENTRYのみ。
WATCHは監視対象。

### 4-3. Watch条件達成後は必ず再分析
例えば、
現在1,000円
1,050円突破待ち
だった場合、
1,050円を超えた瞬間に自動的にEntry扱いしてはいけない。
その時点で再度、

- 出来高
- VWAP
- 上ヒゲ
- 材料
- 新規ニュース
- 市場地合い
- 支持抵抗
- 希薄化

を分析する。
その時点でEntry可なら、
その時の価格
をEntry Reference Priceとして新しいPrediction Snapshotを作成する。

## 5. Predictionは現時点価格基準
Predictionの評価基準価格は、
Prediction作成時の実際のEntry Reference Price
とする。
過去に安かった価格を基準にしない。
例：
9月15日：1,000円でWATCH
9月17日：1,080円でENTRY
なら、
+20% Thresholdは、
1,080 × 1.20 = 1,296円
である。
1,000円基準にしてはいけない。

## 6. 過去高値を「上値余地」にしない
このルールは非常に重要。
例えば、
500円
→ 材料
→ 1,100円
→ 暴落
→ 650円
となった銘柄について、
「1,100円まで戻れば+69%」
とは絶対に評価しない。
過去急騰高値は、
Potential Upsideではない。
むしろ、

- Supply Overhang
- 戻り売り
- 高値掴み保有者
- Distribution

の可能性を示す。
Reachable Zoneは必ず、
現在の材料・現在の需給・現在の支持抵抗・現在の出来高構造
から作る。

## 7. ニュースとIRに固定序列を作らない
以下は禁止。
`IR > News`
と固定すること。
短期急騰予測では、
ニュース・外部イベントの方が重要になる場合が多い。
理由：
企業IRには、

- 決算日
- 月次
- 既知イベント
- 既存計画

など市場が事前に予想している情報も多い。
一方、

- 政府方針
- 戦争
- 制裁
- 関税
- 商品価格急変
- 規制変更
- 競合の突発ニュース
- 業界の構造変化
- 新しい報道

などは、
市場がその瞬間から織り込み始める可能性がある。
したがって材料の強さは情報源ではなく、

- 新規性
- サプライズ
- 直接性
- 経済的インパクト
- 継続性
- 市場反応
- 未織り込み度

で評価する。

## 8. Discovery SourceとVerification Sourceを分離
例えばReutersが最初に報道し、
数時間後に政府が公式発表する場合、
最初に市場へ情報が入った時刻
はReuters報道時点かもしれない。
そのため、
Discovery Source
市場へ最初に情報を伝えた可能性がある情報源。
Verification Source
事実確認に使う一次情報等。
を分離する。
一次情報だから予測上必ず上位とは限らない。

## 9. Material Eventのfirst_seen_atを保存
ニュースには、

- published_at
- fetched_at
- first_seen_at

を保存する。
同じ出来事について、
Reuters
Yahoo
日経
株探
フィスコ
企業IR
など複数記事が存在しても、
複数独立材料として扱わない。
1つの、
`material_event`
へ統合する。
そのEventの市場認識開始時刻として、
`first_seen_at`
を重要Featureとして保存する。

## 10. ニュース・材料は大量に収集する
開示レーダーのように、
複数ソースから大量に材料候補を集める。
日本：

- TDnet
- EDINET
- 企業IR
- 株探
- フィスコ
- Reuters
- 日経
- Yahoo!ファイナンス掲載ニュース
- みんかぶ
- アイフィス
- トレーダーズ・ウェブ
- 政府・省庁
- 日銀
- その他有用ソース

米国：

- SEC EDGAR
- Company IR
- Reuters
- 金融ニュース
- 規制当局
- Fed
- Treasury
- White House
- DOE
- DoD
- FDA
- その他有用ソース

国際：

- 原油
- 天然ガス
- 金属
- 金利
- 為替
- 関税
- 戦争
- 制裁
- AI
- 半導体
- 防衛
- 電力
- 原子力
- データセンター
- サプライチェーン
- 暗号資産
- その他株価へ影響し得るイベント

## 11. 内部では取得データを保持する
ニュース・材料・開示等をWebサイト上で転載することが目的ではない。
本システムはまず、
非公開の研究システム
として運用する。
取得可能なデータについては、
内部Raw Storageへ可能な限り保持する。
ただし取得方法・保存可否は各ソース/APIの利用条件を確認すること。
利用条件上全文保存不可の場合は、

- metadata
- URL
- snippet
- hash
- extracted features

等を保存する。

## 12. ニュースノイズを除去
一般的な、

- 殺人事件
- 一般犯罪
- 芸能
- スポーツ
- ゴシップ
- 一般生活情報
- 通常の天気
- 一般的な台風ニュース

など、
市場・企業への実質的接続がないものは除外する。
ただし単純キーワード除外は禁止。
例えば台風でも、

- 工場停止
- 港湾停止
- 航空
- 保険
- 電力設備
- 農産物
- 原材料価格

など企業利益へ接続する場合は材料になり得る。
判定基準は、
`market_relevance`
とする。

## 13. ニュース→銘柄のEntity Linkingを重視
AIに、
「AIニュースだからAI関連株」
のような雑な紐付けをさせてはいけない。
relation_typeを保存する。
例：

- DIRECT_COMPANY
- SUBSIDIARY
- PRODUCT
- CUSTOMER
- SUPPLIER
- COMPETITOR
- INDUSTRY
- POLICY_EXPOSURE
- COMMODITY_EXPOSURE
- FX_EXPOSURE
- RATE_EXPOSURE
- GEOPOLITICAL_EXPOSURE
- WEAK_ASSOCIATION

`WEAK_ASSOCIATION`
だけでは強材料扱いしない。

## 14. マクロ材料は因果経路を必須にする
例えば、
ホルムズ海峡問題
なら、
ホルムズ海峡
→ 原油供給
→ 原油価格
→ 企業の売上/コスト
→ 利益
→ 株価
まで接続する。
「原油関連だから」
だけでは不十分。

## 15. Stage 1ではLLMを原則使わない
Eligible Universe全件について、
コードでFeatureを計算する。
最低限：

- Return 1/3/5/10/20D
- High/Low distance
- ATR
- ATR%
- realized volatility
- Volume
- Relative Volume
- turnover
- Float turnover
- SMA
- EMA
- MA slope
- RSI
- MACD
- ADX
- Bollinger width
- gap
- candle body
- upper wick
- lower wick
- close position
- breakout distance
- support/resistance candidate
- volatility contraction

など。
v5.1 Route A〜Hを可能な限りコード化する。

## 16. 材料ルートを独立させる
チャート条件を満たさないという理由だけで、
新しい強材料銘柄を落とさない。
Candidate Generationは、
Technical candidates
と、
Material candidates
を独立生成する。
最終的に、
`Technical ∪ Material`
をAI分析候補へ送る。

## 17. AIは候補生成後に使う
全3,000円以下銘柄へLLM詳細分析を行わない。
流れ：
全市場
↓
3,000円Hard Filter
↓
Technical / Material Screening
↓
候補集合
↓
ここからAI詳細分析
とする。

## 18. AIにはチャートの「意味」を分析させる
AIに単なるパターン判定だけをさせない。
例えば、
`lower_wick = true`
ではなく、

- seller exhaustion
- absorption
- accumulation
- distribution
- breakout acceptance
- failed breakout
- supply overhang
- demand vacuum
- dead-cat bounce
- healthy pullback

など、
市場心理・需給の意味
を分析する。
必ず数値Featureを根拠にする。

## 19. チャート知識ベースを作る
システムに、
「下ヒゲとは何か」
「なぜ発生するか」
「どの文脈で有効か」
「どんな場合はFalse Positiveか」
まで教える。
Knowledge Baseには、

- Definition
- Mechanism
- Positive Context
- Negative Context
- Counterexample
- Numerical Features
- Chart Examples
- Failed Examples

を持たせる。
対象例：

- 下ヒゲ
- 上ヒゲ
- 包み足
- 三角持ち合い
- フラッグ
- ボックス
- GC
- MA Reclaim
- VWAP
- Anchor VWAP
- 出来高
- 売り枯れ
- Distribution
- Failed Breakout
- 急騰後崩壊
- 健全な押し目

など。

## 20. チャート画像も教材にする
OHLCVから生成したチャート画像を保存可能にする。
AIへ、

- 数値Feature
- OHLCV
- チャート画像
- 材料時系列

を組み合わせて渡せる構造にする。
ただし画像だけで判断させない。

## 21. 初期知識と実績学習を分離
最初は、
v5.1
＋
Technical Knowledge Base
を初期Priorとして使う。
その後、
実市場の教師データから、
「本当に有効だったか」
を検証する。
例えば、
長い下ヒゲ＋支持線＋出来高増
を初期的に高評価しても、
実データ上有効性が低ければ将来的にWeightを下げる。

## 22. 教師データで重みを学習する
固定で、
材料30%
チャート30%
出来高20%
などとしない。
Driver Typeによって有効なFeatureは異なる。
例：

- 決算型
- 企業固有材料型
- 政策型
- マクロ型
- 需給ブレイク型
- Event Driven型
- 複合型

教師データから、
条件付きWeight
を学習する。

## 23. Feature interactionを学習
単独Featureだけでなく組み合わせを重視する。
例えば、
長い下ヒゲ単独
より、
長い下ヒゲ
× 支持線
× 出来高増
× 翌日安値非更新
の方が意味を持つ可能性がある。
最初のML候補として、

- LightGBM
- XGBoost
- CatBoost

等のtabular modelを検討する。
巨大なDeep Learningから始めない。

## 24. LLM判定も教師データのFeature
LLMの分析も真実として扱わない。
例えば、

- novelty_score
- material_duration
- market_psychology
- priced_in
- supply_overhang
- reachable_zone_quality

などをFeatureとして保存する。
後から、
「LLMの継続性High判定は実際に有効だったか」
を検証できるようにする。

## 25. 正式Prediction
AIが、
現在の価格からEntry可能
と判断した場合のみPredictionを作る。
保存：

- prediction_id
- run_id
- timestamp
- ticker
- entry_reference_price
- currency
- FX
- driver_type
- technical_state
- material_event_ids
- 20% threshold
- reachable_zone
- failure_line
- failure_distance
- rationale
- falsifiers
- prompt_version
- rule_version
- ml_version
- llm_provider
- llm_model
- feature_version
- data_cutoff

Predictionは後から書き換えない。

## 26. Watch状態
Entry不可だが監視価値がある場合、
PredictionではなくWatchへ保存する。
例：
`WATCH_BREAKOUT`
`WATCH_PULLBACK`
Watchの条件が発生したら再分析。
Entry可になった場合だけ、
その時点で正式Predictionを作成する。

## 27. State Transitionを保存
例：
WATCH_BREAKOUT
↓
TRIGGER_HIT
↓
REANALYSIS
↓
ENTRY
または、
WATCH_BREAKOUT
↓
FAILED_BREAKOUT
↓
REJECT
など。
この状態遷移自体を教師データにする。

## 28. Outcome Tracking
Prediction作成後、
1D
3D
5D
10D
20D
で追跡する。
最低限：

- close return
- MFE
- MAE
- +10% hit
- +20% hit
- +30% hit
- days_to_20
- failure_line_hit
- hit_20_before_failure
- failure_before_20

を保存する。

## 29. 「+20%になった株」を全部成功例にしない
非常に重要。
単純に、
20日以内+20%=Success
としてはいけない。
教師ラベルを細分化する。
例：

- PREDICTIVE_SUCCESS
- STATE_CONFIRMED_SUCCESS
- FALSE_POSITIVE
- FAILED_BEFORE_TARGET
- PRICED_IN_ERROR
- REACHABLE_ZONE_ERROR
- DISTRIBUTION_ERROR
- FALSE_PULLBACK
- ACTIONABLE_FALSE_NEGATIVE
- PRICE_SUCCESS_EXOGENOUS
- OUT_OF_SCOPE_SHOCK
- OUT_OF_SCOPE_LATE

## 30. 突発急騰をFalse Negativeにしない
例えば、
材料前兆なし
↓
突然TOB
↓
PTS +30%
のようなケースを、
AIが拾えなかったFalse Negative
として学習してはいけない。
本システムの目的は、
予測可能・Entry可能だった急騰
を見つけることである。

## 31. Actionable False Negativeだけを学習する
AIが選ばなかった銘柄が+20%以上上昇した場合、
Research Modeで、
「上昇前または上昇初期に取得可能だった情報から合理的に拾えたか」
を判定する。
拾えた場合のみ、
`ACTIONABLE_FALSE_NEGATIVE`
とする。

## 32. システム全体を学習対象にする
学習対象はLLMだけではない。
最低限4層：
Candidate Generation Learning
どの株をAIへ送るべきか。
Interpretation Learning
材料・チャートをどう解釈するか。
Entry Prediction Learning
現在価格からEntryするべきか。
State Transition Learning
どんな確認・否定シグナルで昇格・失効するか。

## 33. 全Eligible Universeの結果は内部保存
正式PredictionはENTRYだけでよい。
ただし、
3,000円以下だった全銘柄について、
最低限Featureと将来Outcomeは保存する。
理由：
見逃し研究に必要だから。

## 34. Excelの位置付け
Excelはデータベースの正本ではない。
Excelは、
人間が予測・結果を確認しやすいExport/View
とする。
正式データは内部DBへ保存。
Excelには主として、

- Prediction
- Entry
- Outcome
- Model version
- 成績

などを出力する。

## 35. Raw Dataを残す
可能なものはRawデータを保持する。
理由：
将来Entity LinkingやFeature Engineが改善した際、
過去データを新ロジックで再処理するため。
ただし、
Production当時のPrediction
と、
後から新モデルで再計算した、
Historical Replay
を絶対に混同しない。

## 36. ProductionとResearchを分離
Production
その時点で採用済みのモデル・ルールのみ使用。
Research
過去データ、
成功例、
失敗例、
見逃し例、
Feature Importance、
Weight改善
などを分析。
Research結果を無検証でProductionへ入れない。

## 37. Champion / Challenger
Production ModelをChampion。
新モデルをChallenger。
Walk-forward validationで比較する。
未来データリークは禁止。
Random Shuffleだけのtrain/test splitは禁止。

## 38. Webサイト
外出先からスマホでも確認可能なWebアプリにする。
Vercel等を利用可能。
ただし、
重い全市場バッチ処理やML学習を無理にVercel Functionへ押し込めない。
Web UI/APIと、
batch worker / scheduler / training worker
を分離してよい。

## 39. 推奨画面
Dashboard

- Universe count
- Eligible count
- Stage 1
- Stage 2
- AI analyzed
- Entry Predictions
- Watch
- Coverage
- Pipeline status

Universe
全銘柄の、

- eligible
- current stage
- route
- exclusion reason

Materials
開示レーダー型。

- 時刻
- ソース
- タイトル
- Material Event
- linked stocks
- relevance
- novelty
- impact

Stock Detail

- Chart
- Technical Features
- Materials
- Material Timeline
- AI Analysis
- Entry status
- Prediction history
- State history
- Outcome

Predictions
正式ENTRY予測のみ。
Watch
Breakout / Pullback等。
Results
予測結果。
Model Lab

- Champion
- Challenger
- Route別成績
- Feature別成績
- Driver別成績
- Failure分析
- False Negative
- LLM評価精度
- Version比較

Pipeline
データ取得状況・エラー・Coverage。

## 40. プライベート運用から開始
当初は一般公開しない。
ユーザー本人が外出先から確認できるよう、
Authenticationを実装する。
データ、ニュース、研究結果、予測履歴を外部へ不用意に公開しない。

## 41. 推奨技術思想
特定技術を盲目的に固定しないが、
候補として、
Web
Next.js / TypeScript
Hosting
Vercel
DB
新規Supabase PostgreSQL等
Object/Raw storage
適切なオブジェクトストレージ
Worker
長時間ジョブに向いた環境
ML
Python
を検討する。
ただしClaude Codeは、
現在の各サービス制約・無料枠・実行時間・データ量を確認し、
理由を示して選択する。

## 42. 既存プロジェクト禁止を再確認
新規Supabase。
新規Vercel Project。
新規Repository。
既存サイトを改造してはいけない。

## 43. 監査可能性を最優先
重要処理には、

- run_id
- timestamp
- data_cutoff
- source
- version
- error log

を保存する。
後から、
「なぜこの銘柄が選ばれたか」
「なぜ除外されたか」
「どのデータを見ていたか」
を再現可能にする。

## 44. 最初から完璧なMLを作らない
教師データが少ない初期段階では、
v5.1 Rule Engine
＋
LLM Detailed Analysis
を中心にする。
教師データが十分に蓄積してから、
ML Weightを本格導入する。
データが少ないのに精密な確率を表示してはいけない。

## 45. 確率表示について
十分な教師データとCalibrationができるまで、
「+20%確率8.2%」
のような見せ方をしない。
初期は、

- ENTRY
- WATCH
- REJECT

と、
分析根拠を中心にする。
将来確率を表示する場合は、
過去の類似状態によって校正された数字であること。

## 46. 開発フェーズ
一気に完成させない。
Phase 0
Requirements / architecture / repository setup
Phase 1
Security Master + Universe
Phase 2
Market Data + 3000円 Hard Filter
Phase 3
Stage 1 Technical Screening
Phase 4
News / Disclosure Collection
Phase 5
Noise Filter + Entity Linking + Material Event
Phase 6
Chart Knowledge Base + Stage 2
Phase 7
LLM Stage 3 Analysis
Phase 8
ENTRY / WATCH / Prediction Snapshot
Phase 9
Outcome Tracking + Excel Export
Phase 10
Teacher Dataset
Phase 11
ML / Weight Learning
Phase 12
Model Lab / Continuous Improvement

## 47. Claude Codeの最初のタスク
最初からPhase 12まで作らない。
まず、
Phase 0
を実施する。
以下を作成する。

- 新規リポジトリ
- README
- CLAUDE.md
- docs/
- architecture
- data model draft
- source strategy
- prompt preservation
- versioning policy
- development phases
- test strategy
- deployment strategy
- unresolved decisions

そして、
実装前のアーキテクチャ案を提示して停止する。
ChatGPT監査後にPhase 1へ進む。

## 48. CLAUDE.mdに必ず書くこと

- 既存プロジェクトを使用しない
- v5.1を原文保存
- 勝手に投資ロジックを簡略化しない
- Prediction = 現在Entry可能のみ
- WatchはPredictionではない
- 過去高値をUpsideにしない
- NewsとIRに固定序列なし
- 突発急騰をFalse Negativeにしない
- ProductionとResearchを分離
- データリーク禁止
- 大きなPhaseごとにChatGPT監査
- 曖昧な判断は勝手に決めず「Decision Needed」として報告

## 49. Phase完了報告形式
Claude CodeはPhase完了時に、
Completed
Files changed
Tests
Coverage
Known limitations
Decisions made
Decisions needed
Risks
Next proposed phase
の形式で報告する。
「すべて完成しました」で終えない。

## 50. このプロジェクトの最終像
最終的に作るのは、
日米全市場を毎日監視し、3,000円以下の銘柄を全件スクリーニングし、チャート・需給・ニュース・材料を統合し、現在価格からEntry可能な+20%短期急騰候補だけを正式予測として出し、その後の結果を細かく教師データ化し、成功・失敗・見逃し・状態遷移からシステム全体が継続的に強くなっていくAI研究プラットフォーム
である。
LLM単体を賢くすることが目的ではない。
データ取得、スクリーニング、材料紐付け、チャート解釈、Entry判断、教師データ、Weight、モデル、状態遷移を含むシステム全体を強くすること
が目的である。
