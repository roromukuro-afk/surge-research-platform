# Groq HTTP 413 の切り分け（2026-09-18、Free tier の組織で実測）

**目的**: canonical Entry request が `groq/compound` で HTTP 413 になる理由を、推測ではなく測定で切り分ける。
413（body too large）と 429（rate limit）は Groq 公式でも別のエラーなので、413 を特定モデルの TPM に**断定で帰属させない**。有料化で解消するとも書かない。

**方法**: 1要素ずつ変えた request を送り、記録したのは body bytes / o200k token 数 / 使った field / HTTP status / Groq の error（type・code・message、org id は `org_<redacted>`）/ rate-limit header / 200 のときの usage と compound の `usage_breakdown` のモデル名だけ。API key・canonical 本文・addenda 本文・モデル出力は記録していない。本文は SHA-256 先頭16桁 `6afc8acf516c34dd` で識別。
スクリプト: セッションの scratchpad（リポジトリ外）。記録: `groq_413_results.jsonl` / `groq_413_followup.jsonl`（同）。

Entry prompt: 25,982 文字 / UTF-8 46,577 B / o200k_harmony 14,055 tokens（framing 込み。本文のみ 13,789）/ HTTP body 49,451 B。

---

## 1. compound: 境界（P1、`max_completion_tokens`=2048、production と同じ field）

| Entry の割合 | body | o200k tokens | 結果 |
|---|---:|---:|---|
| 1.0 | 49,451 B | 14,055 | **413** `invalid_request_error` / `request_too_large` / "Request Entity Too Large" |
| 0.5 | 30,201 B | 8,553 | 413（同上） |
| 0.375 | 23,517 B | 6,652 | 413（同上） |
| 0.3594 | 22,592 B | 6,368 | 200 |
| 0.3438 | 21,610 B | 6,094 | 200 |
| 0.3125 | 19,707 B | 5,471 | 200 |
| 0.25 | 16,008 B | 4,409 | 200 |

- 413 本文（107 B）は**モデル名も制限値も名指ししない**。
- 413 の時点で compound 自身の header は `x-ratelimit-limit-tokens: 70000`、残り 55,539〜63,188。**compound 自身の TPM 枠は空いていた**。

## 2. bytes か tokens か

| request | body | tokens | 結果 | Groq が数えた "Requested" |
|---|---:|---:|---|---:|
| gpt-oss-120b, Entry 全文 | 49,381 B | 14,055 | 413 | 14,523 |
| 同、JSON 構造の空白で +50,000 B（本文は同一） | 99,381 B | 14,055 | 413 | 14,529 |
| 同、+250,000 B | 299,381 B | 14,055 | 413 | 14,561 |
| gpt-oss-120b, ASCII 本文で同じ o200k 数 | 109,885 B | 14,055 | 413 | 14,542 |
| compound, ASCII で割合 0.375 と同じ token 数 | 51,791 B | 6,652 | 413 | — |
| compound, ASCII で 22,168 B | 22,168 B | 2,881 | 200 | — |

- **body を 6 倍（49→299 KB）にしても Groq の count は変わらない**（差は同一 request の再送でも出る ±50 の範囲。§4）。body 299 KB は gateway で止まらず token の検査まで届いた。
- 日本語と ASCII で o200k が同じなら、bytes が 2.2 倍違っても count は同じ。
- → **gpt-oss-120b の 413 は bytes ではなく tokens で決まる。** compound は本文が名指ししないため、bytes 仮説を直接否定する compound 単体の測定は未完（§5）。

## 3. gpt-oss-120b（直接）

413 本文（Groq 自身の文言、org id 伏せ字）:
> Request too large for model `openai/gpt-oss-120b` in organization `org_<redacted>` service tier `on_demand` on tokens per minute (TPM): Limit 8000, Requested 14523, please reduce your message size and try again.

（本文末尾に Dev Tier への upgrade 案内の一文があるが、Dev Tier での値は示されていない。）

| 割合 | tokens | 結果 |
|---|---:|---|
| 1.0 | 14,055 | 413（Requested 14,575 / 再送で 14,523） |
| 0.5 | 8,553 | 413（Requested 9,032） |
| 0.25 | 4,409 | 200（usage prompt_tokens 4,511） |
| 1.0、`response_format` と framing なし | 13,789 | 413（Requested 14,199） |

→ gpt-oss-120b については**本文そのものが**「1 request が TPM 8,000 を超える」と言っている。これは推測ではない。

## 4. Groq の "Requested" は prompt + max_completion_tokens ではない

| `max_completion_tokens` | Requested |
|---:|---:|
| 2048 | 14,523 |
| 1024 | 14,521 |
| 1 | 14,158 |

- prompt 部分 ≈ 14,157（o200k 14,055 + template 約 100）→ **Groq は実 tokenizer で数えている**。
- completion 部分は max_completion_tokens ではなく**見積り**（ここでは約 365）。1024 と 2048 で差は 2。
- 同じ request の再送で 14,575 → 14,523 と揺れる（見積りが一定でない）。
- 帰結: 「prompt + 予約 > TPM」の preflight は、**prompt 単独で超える場合だけ**「送れない」と断定できる。予約分で超えるだけなら Groq は受理し得る（コードの文言をこの区別に直した）。

## 5. compound の内部

compound の 200 応答の `usage_breakdown`（割合 0.25、`max_completion_tokens` 8192）:

| 内部モデル | prompt | completion |
|---|---:|---:|
| meta-llama/llama-4-scout-17b-16e-instruct | 3,931 | 20 |
| meta-llama/llama-4-scout-17b-16e-instruct | 4,334 | 139 |
| openai/gpt-oss-120b | 4,783 | 379 |

- compound は**内部で gpt-oss-120b と llama-4-scout に prompt 全体を渡している**（実測）。これらの制限は compound の header に出ない。
- compound の境界（6,368〜6,652 o200k）は、内部の gpt-oss-120b 呼び出しが 8,000 に届く位置と**矛盾しない**が、それには内部呼び出しの completion 見積りが ~0.9〜1.25K である必要があり、確かめる手段が無い。**compound の 413 の原因は Groq が述べておらず、ここでも断定しない。**
- `max_completion_tokens` を 8192 にしても割合 0.25 は 200（compound でも予約は count をほとんど動かさない）。

### field の除去（P3）
`response_format` を外しても 413。`tool_choice` / `compound_custom` / tool field 全部 / `max_completion_tokens` 256 / `temperature` / 全部外した bare の各 request は、**llama-4-scout の TPD 超過で 429** になり測定できなかった（§6）。429 は size の所見として数えていない。

## 6. 付随して分かったこと: compound は llama-4-scout の 1日枠を消費する

429 本文: `Rate limit reached for model meta-llama/llama-4-scout-17b-16e-instruct ... on tokens per day (TPD): Limit 500000, Used 499785, Requested 6129`。
- 本日の実験で llama-4-scout の TPD 500,000 をほぼ使い切った。回復は約 347 tokens/分（500,000/1,440）で、"try again in" の値と一致。
- TPD が尽きている間、compound は full size の request にも 413 ではなく 429 を返した → compound では **429（内部モデルの TPD）の検査が 413 より先**。このため TPD 枯渇中の padding 試験（compound, 60 KB, 4,409 tokens）は 429 で、bytes 仮説の compound 単体での否定は未完。
- 割合 0.25 の compound 1 回で llama-4-scout 8,424 tokens・gpt-oss-120b 5,162 tokens を消費した（実測）。Entry 全文なら約 3.2 倍（**推計**）。

## 7. Entry prompt を削らずに収める余地

| 構成 | tokens | 変更可否 |
|---|---:|---|
| Canonical v5.1 | 9,836 | 不変（CLAUDE.md 1-2） |
| addenda | 3,458 | 不変 |
| bundle | 185 | 入力 |
| output contract | 291 | project の文言 |
| JSON framing（system） | 266 | project の文言（Groq の "json" 規則に必要） |

不変部分が 94.7%。project 側の文言を**全部**消しても ~13.3K tokens で、gpt-oss-120b の 8,000 を超える。**正規化で収める余地は無い。** canonical を短くすることは禁止（別の method への答えになる）。

## 8. 直接の llama-4-scout（同日、最後の候補）

`groq_probe --model meta-llama/llama-4-scout-17b-16e-instruct --contract-smoke --single-attempt` で、Entry request を1回だけ送る設定で試した。事前の小さな quota probe が **HTTP 404 `model_not_found`** で、Entry request は送られていない。`/v1/models`（metadata のみ）でこのアカウントが直接呼べる chat モデルは compound / compound-mini / gpt-oss-120b / gpt-oss-20b / gpt-oss-safeguard-20b / qwen3.8-27b / allam-2-7b。llama-4-scout は compound の内部でのみ使われている。→ 無料 Groq で約 14K tokens の Entry request を送れるモデルは無い（D-266）。

## 9. 言えないこと

- Free と Developer で**最大 body size が違う**という公式記述は見つからない（rate limit の表の違いのみ）。
- Dev Tier での gpt-oss-120b / compound 内部モデルの TPM 値は未確認。**「有料化で 413 が解消する」とは記録しない。**
- compound の 413 の原因（§5）。
