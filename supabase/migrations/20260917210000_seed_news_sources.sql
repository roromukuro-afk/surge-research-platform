-- The news sources, and what each one's terms actually say.
--
-- Twelve candidates were researched against official pages only, and every
-- verdict was then re-read by a second pass told to refute it. Two verdicts
-- changed, one of them from PROHIBITED to ALLOWED - so the adversarial pass
-- earned its cost in both directions, not only by catching optimism.
--
-- The headline result is a loss. TDnet - the timely-disclosure service, the
-- single most market-moving Japanese source there is - cannot be collected. Its
-- robots.txt disallows the entire host, and every disclosure page carries an
-- express prohibition on 複製 (reproduction) without TSE's permission. The paid
-- API exists and is priced for institutions: 基本料金 70,000円/月 plus a data
-- tier from 100,000円/月.
--
-- EDINET is not a substitute for it. EDINET carries statutory periodic filings;
-- TDnet carries the announcement that moves the price on the day. Losing TDnet
-- means the Japanese material side is structurally weaker than the US one, and
-- that is a coverage gap rather than a bug.
--
-- Rejected sources are registered here anyway, with the clause that decided
-- them. A source rejected once should stay rejected with a reason a later reader
-- can check, rather than being rediscovered and re-argued.

-- ---------------------------------------------------------------------------
-- Japan
-- ---------------------------------------------------------------------------

insert into news.sources
  (source_key, name, scope, source_kind, access_mechanism, official_url, feed_url, docs_url,
   auth_requirement, required_env, documented_rate_limit, min_request_interval_seconds,
   discovery_role, verification_role, fetch_priority, enabled, notes)
values
  ('tdnet', 'TDnet 適時開示情報閲覧サービス', 'JP', 'OFFICIAL_DISCLOSURE', 'HTML_PAGE',
   'https://www.release.tdnet.info/inbs/I_main_00.html', null,
   'https://www.jpx.co.jp/markets/paid-info-listing/tdnet/',
   'NONE', '{}', 'none published for the free service', 5.0,
   true, true, 10, false,
   'REGISTERED BUT UNUSABLE. robots.txt disallows the whole host and the disclosure pages prohibit reproduction without TSE permission. The paid TDnet API is 70,000 JPY/month base plus a data tier from 100,000 JPY/month. Kept here so the prohibition is findable rather than rediscovered.'),

  ('jpx_news', 'JPX 上場・適時開示関連ニュース', 'JP', 'EXCHANGE', 'RSS',
   'https://www.jpx.co.jp/', 'https://www.jpx.co.jp/rss/index.html', 'https://www.jpx.co.jp/term-of-use/index.html',
   'NONE', '{}', 'no numeric limit; the terms ask that high-frequency automated retrieval be avoided', 5.0,
   true, true, 20, false,
   'REGISTERED BUT UNUSABLE. The Japanese terms prohibit secondary use and redistribution 如何なる用途に関わらず (regardless of purpose) without JPX permission. The English version narrows this to commercial purposes; the same page states the Japanese text prevails.'),

  ('edinet', 'EDINET API v2（金融庁 法定開示）', 'JP', 'REGULATOR', 'REST_API',
   'https://disclosure2.edinet-fsa.go.jp/weee0010.aspx',
   'https://api.edinet-fsa.go.jp/api/v2/documents.json',
   'https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/WZEK0110.html',
   'FREE_API_KEY', '{SURGE_EDINET_API_KEY}', 'not documented numerically; HTTP 429 is defined in the spec', 1.0,
   true, true, 30, false,
   'The strongest free Japanese source. Statutory filings only - periodic reports and 臨時報告書 - not timely disclosure. submitDateTime gives a per-filing publication time to the minute. Needs a free API key, which the account holder has to create.'),

  ('boj', '日本銀行 公表資料', 'JP', 'CENTRAL_BANK', 'RSS',
   'https://www.boj.or.jp/', 'https://www.boj.or.jp/rss/whatsnew.xml', 'https://www.boj.or.jp/about/copyright.htm',
   'NONE', '{}', 'no numeric limit; the statistics API manual asks that high-frequency access be avoided', 2.0,
   true, true, 40, false,
   'RSS pubDate carries RFC-822 with an explicit +0900 offset, so the publication minute is machine-readable. No robots.txt on any BOJ host (verified 404, not assumed).'),

  ('mof', '財務省 報道発表', 'JP', 'GOVERNMENT', 'HTML_PAGE',
   'https://www.mof.go.jp/', 'https://www.mof.go.jp/public_relations/whats_new/index.htm',
   'https://www.mof.go.jp/about_mof/notice/index.html',
   'NONE', '{}', 'not documented', 2.0,
   true, false, 60, false,
   'No RSS and no API: monthly HTML listings only, pattern /public_relations/whats_new/YYYYMM.html. No clean per-item publication timestamp, so items will carry DATE_ONLY precision.'),

  ('meti', '経済産業省 ニュースリリース', 'JP', 'GOVERNMENT', 'HTML_PAGE',
   'https://www.meti.go.jp/', 'https://www.meti.go.jp/press/index.html', 'https://www.meti.go.jp/main/rules.html',
   'NONE', '{}', 'not documented; the edge throttles aggressively in practice', 5.0,
   true, false, 60, false,
   'The Atom feed exists but has not advanced since 2026-06-19, so current items come from HTML with DATE_ONLY precision. The edge returns 403 to several clients and throttles after a short burst, hence the wide request interval.'),

  ('cao_kantei_egov', '内閣府 / 首相官邸 / e-Gov', 'JP', 'GOVERNMENT', 'RSS',
   'https://www.cao.go.jp/', 'https://www.esri.cao.go.jp/rss-jp.xml', 'https://www.cao.go.jp/notice/rule.html',
   'NONE', '{}', 'not documented', 2.0,
   true, false, 70, false,
   'Three separately operated estates sharing one licence. Timestamp quality is the weakest of the Japanese sources and varies by channel.')
on conflict (source_key) do nothing;

-- ---------------------------------------------------------------------------
-- United States
-- ---------------------------------------------------------------------------

insert into news.sources
  (source_key, name, scope, source_kind, access_mechanism, official_url, feed_url, docs_url,
   auth_requirement, required_env, documented_rate_limit, min_request_interval_seconds,
   discovery_role, verification_role, fetch_priority, enabled, notes)
values
  ('sec_edgar', 'SEC EDGAR filings', 'US', 'REGULATOR', 'ATOM',
   'https://www.sec.gov/edgar/search/',
   'https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&owner=include&count=40&output=atom',
   'https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data',
   'NONE', '{SURGE_CONTACT_EMAIL}', '10 requests per second, stated in three official places', 0.15,
   true, true, 30, false,
   'US federal work: sec.gov content may be copied and redistributed without permission. A declared User-Agent carrying a contact address is enforced, not merely requested - anonymous clients are refused. The Atom entry updated field is the EDGAR acceptance time to the second, which is the right value for source_published_at.'),

  ('federal_reserve', 'Federal Reserve Board press releases', 'US', 'CENTRAL_BANK', 'RSS',
   'https://www.federalreserve.gov/', 'https://www.federalreserve.gov/feeds/press_all.xml',
   'https://www.federalreserve.gov/feeds/feeds.htm',
   'NONE', '{}', 'not documented', 2.0,
   true, true, 40, false,
   'RSS gives a real machine-readable publication time. A separate official page affirmatively sanctions automated retrieval, which the first research pass missed.'),

  ('us_treasury', 'U.S. Treasury press releases', 'US', 'GOVERNMENT', 'BULK_FILE',
   'https://home.treasury.gov/', 'https://home.treasury.gov/news-data/press-releases/manifest.json',
   'https://fiscaldata.treasury.gov/api-documentation/',
   'NONE', '{}', 'not documented', 2.0,
   true, false, 50, false,
   'A JSON index rather than RSS, with an ISO-8601 UTC datetime per item. The verification pass overturned the first reading here: it found site-wide Fiscal Service terms that the first pass had never opened, and storage survived them.'),

  ('whitehouse', 'White House', 'US', 'GOVERNMENT', 'RSS',
   'https://www.whitehouse.gov/', 'https://www.whitehouse.gov/presidential-actions/feed/',
   'https://www.whitehouse.gov/copyright/',
   'NONE', '{}', 'not documented', 2.0,
   true, false, 70, false,
   'WordPress RSS. Several category feeds exist; presidential actions and fact sheets are the ones that move markets.'),

  ('us_statistical_agencies', 'BLS / Census / BEA', 'US', 'GOVERNMENT', 'REST_API',
   'https://www.census.gov/data/developers.html', null,
   'https://www.bls.gov/developers/termsOfService.htm',
   'FREE_API_KEY', '{SURGE_BLS_API_KEY,SURGE_CENSUS_API_KEY,SURGE_BEA_API_KEY}',
   'per-agency; BLS documents a daily quota that differs with and without a key', 1.0,
   true, true, 80, false,
   'Three agencies with three different APIs and three different timestamp qualities, which is the main engineering cost here. Registration is free but per agency.'),

  ('company_ir_q4', 'Company IR feeds (Q4 Inc. hosted)', 'US', 'COMPANY_IR', 'RSS',
   'https://investor.nvidia.com/investor-resources/rss/default.aspx', null,
   'https://www.q4inc.com/terms-of-use',
   'NONE', '{}', 'no numeric limit; the platform robots.txt carries a crawl-delay', 10.0,
   true, false, 90, false,
   'The first research pass rejected all company IR feeds outright; the verification pass overturned that, having read terms the first pass never opened. Fixed feed URL patterns per issuer on a shared platform, so one adapter covers many companies.')
on conflict (source_key) do nothing;

-- ---------------------------------------------------------------------------
-- What each one permits. Quad-state: silence is NOT_SPECIFIED, never ALLOWED.
-- ---------------------------------------------------------------------------

insert into news.source_policies
  (source_key, policy_version, license_mode,
   full_text_storage_allowed, metadata_storage_allowed, derived_output_sharing_allowed,
   raw_redistribution_allowed, commercial_use_allowed, attribution_required, delete_on_cancel,
   robots_allows_path, robots_checked_at, crawl_delay_seconds, deciding_clause, terms_url, terms_checked_at, notes)
values
  ('tdnet', 'tdnet-2026-09-17', 'PRIVATE_PERSONAL_RESEARCH_ONLY',
   'PROHIBITED', 'PROHIBITED', 'PROHIBITED', 'PROHIBITED', 'PROHIBITED', 'REQUIRED', 'NOT_SPECIFIED',
   'PROHIBITED', now(), null,
   '適時開示情報閲覧サービスに記載されている内容は、著作物として著作権法により保護されており、株式会社東京証券取引所に無断で転用、複製又は販売等を行うことは固く禁じます。 — reproduction without TSE permission is strictly prohibited. Compounded by robots.txt on release.tdnet.info, which is "User-agent: * / Disallow: /" for the entire host, unconditional across user agents.',
   'https://www.jpx.co.jp/term-of-use/index.html', now(),
   'Both the licence and the access rule refuse, independently. Either alone would be enough.'),

  ('jpx_news', 'jpx-2026-09-17', 'PRIVATE_PERSONAL_RESEARCH_ONLY',
   'PROHIBITED', 'PROHIBITED', 'PROHIBITED', 'PROHIBITED', 'PROHIBITED', 'REQUIRED', 'NOT_SPECIFIED',
   'ALLOWED', now(), null,
   '当サイトに掲載されている情報について、有料・無料を問わずJPXからの許諾を得ている場合を除き、商用目的によるデータ収集のほか如何なる用途に関わらず二次利用及び再配信はできません。 — secondary use is refused 如何なる用途に関わらず, regardless of purpose. The English translation narrows this to commercial purposes, and the same page states the Japanese text prevails.',
   'https://www.jpx.co.jp/term-of-use/index.html', now(),
   'robots.txt permits crawling; the terms do not permit keeping what is crawled. The two are different questions and this source answers them differently.'),

  ('edinet', 'edinet-2026-09-17', 'PUBLIC_REDISTRIBUTABLE',
   'ALLOWED', 'ALLOWED', 'ALLOWED', 'ALLOWED', 'ALLOWED', 'REQUIRED', 'NOT_SPECIFIED',
   'NOT_SPECIFIED', now(), null,
   'EDINET adopts 公共データ利用規約（第1.0版）, whose §1 grants 複製、公衆送信、翻訳・変形等の翻案等 freely, 商用利用も可能. Reproduction is expressly granted, so the permission rests on a grant rather than on silence.',
   'https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/WZEK0030.html', now(),
   'The terms page has three sections; the API terms in sections II and III carry conditions the first research pass never read. Attribution is required. No robots.txt exists on the API hosts (verified, not assumed).'),

  ('boj', 'boj-2026-09-17', 'PUBLIC_REDISTRIBUTABLE',
   'ALLOWED', 'ALLOWED', 'ALLOWED', 'ALLOWED', 'PROHIBITED', 'REQUIRED', 'NOT_SPECIFIED',
   'NOT_SPECIFIED', now(), null,
   '当サイトの内容については、以下の場合を除き、転載・複製を行うことが出来ます。転載・複製を行う場合は、出所を明記してください。 i．商用目的で転載・複製を行う場合 … — reproduction is permitted except for commercial purposes, except where a part is marked as reserved, and except for photographs and images.',
   'https://www.boj.or.jp/about/copyright.htm', now(),
   'Commercial use is the named exception, which this project does not make. Images are excluded, so a collector must keep text and not figures. The statistics API has its own separate terms.'),

  ('mof', 'mof-2026-09-17', 'PUBLIC_REDISTRIBUTABLE',
   'ALLOWED', 'ALLOWED', 'ALLOWED', 'ALLOWED', 'ALLOWED', 'REQUIRED', 'NOT_SPECIFIED',
   'NOT_SPECIFIED', now(), null,
   'Adopts 公共データ利用規約（第1.0版）, §1 of which grants 複製 freely and permits commercial use.',
   'https://www.mof.go.jp/about_mof/notice/index.html', now(),
   'The verification pass added the caveat that PDL 1.0 has restrictive halves the first pass did not quote - third-party rights and content carved out by separate rules.'),

  ('meti', 'meti-2026-09-17', 'PUBLIC_REDISTRIBUTABLE',
   'ALLOWED', 'ALLOWED', 'ALLOWED', 'ALLOWED', 'ALLOWED', 'REQUIRED', 'NOT_SPECIFIED',
   'NOT_SPECIFIED', now(), null,
   '経済産業省ウェブサイトで掲載・発信している情報の著作権は、特記されていない限り経済産業省に帰属し、権利表記の記載がない限り「公共データ利用規約（第1.0版）」（PDL1.0）に準拠した利用条件の下で、利用することができます。',
   'https://www.meti.go.jp/main/rules.html', now(),
   'No robots.txt exists (the root returns a branded 403 from S3, not a policy). The edge throttles hard after a short burst, so the request interval is deliberately wide.'),

  ('cao_kantei_egov', 'cao-2026-09-17', 'PUBLIC_REDISTRIBUTABLE',
   'ALLOWED', 'ALLOWED', 'ALLOWED', 'ALLOWED', 'ALLOWED', 'REQUIRED', 'NOT_SPECIFIED',
   'NOT_SPECIFIED', now(), null,
   'The three estates share 公共データ利用規約（第1.0版）, whose §1 grants 複製 freely.',
   'https://www.cao.go.jp/notice/rule.html', now(),
   'The verification pass found an express restriction on the specific feed the first pass had recommended, and an access claim that did not reproduce. Storage still survives; the channel choice needs care.'),

  ('sec_edgar', 'sec-2026-09-17', 'PUBLIC_DOMAIN',
   'ALLOWED', 'ALLOWED', 'ALLOWED', 'ALLOWED', 'ALLOWED', 'NOT_REQUIRED', 'NOT_SPECIFIED',
   'ALLOWED', now(), 0.1,
   'Information presented on sec.gov is considered public information and may be copied or further distributed by users of the web site without the SEC''s permission.',
   'https://www.sec.gov/about/privacy-information#dissemination', now(),
   'The cleanest licence of the whole set. The binding constraint is technical rather than legal: 10 requests per second, and a User-Agent carrying a contact address that is enforced rather than requested.'),

  ('federal_reserve', 'frb-2026-09-17', 'PUBLIC_DOMAIN',
   'ALLOWED', 'ALLOWED', 'ALLOWED', 'ALLOWED', 'ALLOWED', 'NOT_REQUIRED', 'NOT_SPECIFIED',
   'NOT_SPECIFIED', now(), null,
   'Board materials are US government works in the public domain; a separate official page affirmatively sanctions automated retrieval of the feeds.',
   'https://www.federalreserve.gov/disclaimer.htm', now(),
   'No rate limit published anywhere, which is a reason to self-throttle rather than a licence to hammer.'),

  ('us_treasury', 'treasury-2026-09-17', 'PUBLIC_DOMAIN',
   'ALLOWED', 'ALLOWED', 'ALLOWED', 'ALLOWED', 'ALLOWED', 'NOT_REQUIRED', 'NOT_SPECIFIED',
   'ALLOWED', now(), null,
   'US federal work. The verification pass read the Fiscal Service site-wide terms that the first pass never opened, and found nothing that prohibits storage.',
   'https://fiscaldata.treasury.gov/api-documentation/', now(),
   'One of the two verdicts the adversarial pass changed. It changed toward permitted, on evidence the first pass had not looked for.'),

  ('whitehouse', 'wh-2026-09-17', 'PUBLIC_DOMAIN',
   'ALLOWED', 'ALLOWED', 'ALLOWED', 'ALLOWED', 'ALLOWED', 'NOT_REQUIRED', 'NOT_SPECIFIED',
   'NOT_SPECIFIED', now(), null,
   'US federal work under 17 U.S.C. 105; the site adds no restriction on reuse of its own material.',
   'https://www.whitehouse.gov/copyright/', now(), null),

  ('us_statistical_agencies', 'stats-2026-09-17', 'PUBLIC_DOMAIN',
   'ALLOWED', 'ALLOWED', 'ALLOWED', 'ALLOWED', 'ALLOWED', 'NOT_REQUIRED', 'NOT_SPECIFIED',
   'NOT_SPECIFIED', now(), null,
   'US federal statistical output is public domain; each agency''s developer terms permit use and none prohibits storage.',
   'https://www.bls.gov/developers/termsOfService.htm', now(),
   'The verification pass found four material errors in the first pass''s access details, one of them flatly false. Storage survived all four.'),

  ('company_ir_q4', 'q4-2026-09-17', 'PRIVATE_PERSONAL_RESEARCH_ONLY',
   'NOT_SPECIFIED', 'ALLOWED', 'NOT_SPECIFIED', 'PROHIBITED', 'PROHIBITED', 'REQUIRED', 'NOT_SPECIFIED',
   'ALLOWED', now(), 10.0,
   'The platform terms permit personal, non-commercial access to the syndication feeds and prohibit redistribution; they do not address keeping the full text.',
   'https://www.q4inc.com/terms-of-use', now(),
   'full_text_storage_allowed is NOT_SPECIFIED, so the collector will keep metadata only for this source and record why. That is the quad-state doing its job: the terms are silent, and silence is not consent.')
on conflict (source_key, policy_version) do nothing;
