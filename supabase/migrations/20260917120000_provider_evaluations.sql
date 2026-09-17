-- Phase 2: every source evaluated for the zero-cost core, and why it was rejected.
--
-- Recorded as data rather than prose because the same dead ends will otherwise
-- be re-proposed every few months. Most of these fail on one clause, and the
-- clause is what is worth keeping: "it was too expensive" and "its terms forbid
-- retaining the data" lead to very different next moves.
--
-- Two entries were reversed by adversarial verification, which is the reason
-- this table exists in the form it does. The first pass concluded that
-- Tachibana Securities' free API had no storage prohibition and that Massive's
-- free tier permitted retention. Re-reading the same official pages found an
-- express storage ban on Tachibana's own API page, and a display-only default
-- in Massive's section 2 that the first pass never quoted. Both are recorded
-- with the clause that settles them.

do $$
begin
  if not exists (select 1 from pg_type t join pg_namespace n on n.oid = t.typnamespace
                 where t.typname = 'evaluation_verdict' and n.nspname = 'market') then
    create type market.evaluation_verdict as enum (
      'QUALIFIES',              -- free, storable, automatable, sufficient
      'QUALIFIES_WITH_CAVEAT',  -- usable, but something material is unresolved
      'REJECTED_LICENCE',       -- the terms forbid what this project must do
      'REJECTED_COVERAGE',      -- the free tier cannot cover the universe
      'REJECTED_COST',          -- there is no genuinely free tier
      'REJECTED_ACCESS',        -- automated access is blocked or prohibited
      'UNRESOLVED'              -- the governing document could not be read
    );
  end if;
end;
$$;

create table if not exists market.provider_evaluations (
  evaluation_id bigint generated always as identity primary key,
  evaluated_at date not null,
  market_scope text not null,
  role market.provider_role,
  candidate text not null,
  official_url text,
  verdict market.evaluation_verdict not null,
  monthly_cost text,
  deciding_clause text,
  clause_url text,
  notes text,
  verified_by_second_pass boolean not null default false,
  reversed_by_verification boolean not null default false
);

comment on table market.provider_evaluations is
  'Every data source considered for the zero-cost core, with the clause that decided it. Kept so a rejected source is rejected once, with a reason a later reader can check rather than repeat.';
comment on column market.provider_evaluations.reversed_by_verification is
  'True where an adversarial re-read of the same official page overturned the first conclusion. Both such entries here were reversed from "usable" to "rejected", which is the direction that matters.';
comment on column market.provider_evaluations.deciding_clause is
  'The sentence the verdict turns on, verbatim where short enough to quote. A verdict without one is an opinion.';

insert into market.provider_evaluations
  (evaluated_at, market_scope, role, candidate, official_url, verdict, monthly_cost,
   deciding_clause, clause_url, notes, verified_by_second_pass, reversed_by_verification)
values
  -- ---------------------------------------------------------------- JP EOD
  (date '2026-09-17', 'JP', 'EOD_CURRENT_JP', 'Tachibana Securities e-shiten API',
   'https://www.e-shiten.jp/api/', 'REJECTED_LICENCE', '0 JPY',
   'Section 4 item 7 of the API page: the information provided is prohibited from being accumulated, edited or processed.',
   'https://www.e-shiten.jp/e_api/mfds_json_api_menu.html',
   'Everything else fitted: free, server-side HTTP, daily OHLCV with split factors and roughly twenty years of history, a whole-universe master with listing and delisting dates, refreshed the evening of the trading day. The first research pass concluded the terms were silent on storage after reading only the account regulations PDF; the API page itself carries the prohibition. Also publishes a rate limit of 10 requests/second and states the capacity to sustain it does not exist.',
   true, true),

  (date '2026-09-17', 'JP', 'EOD_CURRENT_JP', 'Mitsubishi UFJ eSmart kabu station API',
   'https://kabu.com/item/kabustation_api/default.html', 'REJECTED_LICENCE', '0 JPY with conditions',
   'Terms of use article 4 prohibits accumulation, editing and secondary use, and transfer to any terminal other than the one displaying the information.',
   'https://kabu.com/pdf/Gmkpdf/service/kabustationuserpolicy.pdf',
   'Independently unusable unattended: the API is served by a Windows desktop terminal that must be running. The free tier also requires a margin or futures account and at least one execution in a rolling three-month window.',
   true, false),

  (date '2026-09-17', 'JP', 'EOD_CURRENT_JP', 'Rakuten Securities MARKETSPEED II RSS',
   'https://marketspeed.jp/ms2/', 'REJECTED_LICENCE', '0 JPY',
   'Usage rules article 5 firmly prohibits diversion, sale and accumulation.',
   'https://marketspeed.jp/', 'Also Excel-bound, so not a server-side path.', true, false),

  (date '2026-09-17', 'JP', 'EOD_CURRENT_JP', 'JPX statistics files (daily stq PDF, monthly tables, data_j.xlsx)',
   'https://www.jpx.co.jp/markets/statistics-equities/daily/', 'UNRESOLVED', '0 JPY',
   'Site terms prohibit secondary use and redistribution for any purpose absent JPX permission, without defining secondary use; the same pages recommend downloading and keeping the files.',
   'https://www.jpx.co.jp/termsofuse/',
   'The only free source of CURRENT full-universe Japanese end-of-day prices found. robots.txt permits every path. Resolution needs a written enquiry to the file owner (TSE data service section) asking whether keeping the published values in a private, non-redistributed database for one person''s own analysis is secondary use.',
   true, false),

  (date '2026-09-17', 'JP', 'EOD_HISTORY_JP', 'J-Quants API Free plan',
   'https://jpx-jquants.com/ja', 'REJECTED_COVERAGE', '0 JPY, expires annually',
   'Storage is expressly permitted where only the account holder can view it; but the free subscription is automatically cancelled after one year, and cancellation obliges deletion of the stored data and its copies.',
   'https://jpx-jquants.com/ja/help/usage',
   'Daily bars are also delayed twelve weeks on the free plan, so it cannot drive a daily screen. A stored corpus could never legitimately outlive one annual cycle.',
   true, false),

  -- ---------------------------------------------------------------- US EOD
  (date '2026-09-17', 'US', 'EOD_CURRENT_US', 'Alpaca Markets Basic (free) market data',
   'https://docs.alpaca.markets/docs/about-market-data-api', 'QUALIFIES_WITH_CAVEAT', '0 USD',
   'Copying is prohibited only for publication, distribution or a commercial enterprise; use is granted for personal, non-commercial purposes. No persistence ban of any kind.',
   'https://files.alpaca.markets/disclosures/library/TermsAndConditions.pdf',
   'The strongest zero-cost US candidate. 200 historical requests a minute, the default plan on any account rather than a trial. Two things need settling first, both by the account holder: the terms say the content is intended for United States residents only while Alpaca separately markets non-US accounts, and whether the free plan serves full SIP history or only IEX is contradictory in its own docs - one authenticated call settles it.',
   true, false),

  (date '2026-09-17', 'US', 'EOD_CURRENT_US', 'Massive (formerly Polygon.io) Stocks Basic free plan',
   'https://massive.com/pricing', 'REJECTED_LICENCE', '0 USD',
   'Market Data Terms section 2: unless otherwise stated in a subsequent agreement, any and all Market Data is strictly for display use only. Section 5(d) separately bars non-display use unless licensed.',
   'https://massive.com/legal/market-data-terms-of-service',
   'Technically ideal - one grouped request returns every US stock for a date, well inside five calls a minute. The first research pass recommended it and filed the non-display question as unanswerable; the clause answering it is in section 2, which that pass never read. An unattended screener with no human viewing the quotes is the paradigm non-display use.',
   true, true),

  (date '2026-09-17', 'US', 'EOD_CURRENT_US', 'Tiingo free (Starter) plan',
   'https://www.tiingo.com/pricing', 'REJECTED_LICENCE', '0 USD',
   'Terms 1.6(a): on a Starter or trial plan you may not write, save, archive, back up or otherwise retain Tiingo Data in any persistent or durable storage, including databases and object stores.',
   'https://app.tiingo.com/tos/', 'The project''s entire design is named in that sentence.', true, false),

  (date '2026-09-17', 'US', 'EOD_CURRENT_US', 'Stooq bulk daily files',
   'https://stooq.com/db/', 'REJECTED_ACCESS', '0 USD',
   'Every endpoint served a JavaScript proof-of-work browser challenge instead of content; the terms page sits behind the same gate and there is no robots.txt.',
   'https://stooq.com/', 'Automated retrieval is actively blocked by the operator, and the terms cannot be read to establish anything.',
   true, false),

  (date '2026-09-17', 'US', 'EOD_CURRENT_US', 'Alpha Vantage free tier',
   'https://www.alphavantage.co/premium/', 'REJECTED_COVERAGE', '0 USD',
   'Twenty-five API requests per day, one symbol per daily-series call.',
   'https://www.alphavantage.co/support/',
   'The friendliest licence of the whole set for exactly this use, and the throttle is the only thing in the way. Worth keeping for spot checks and cross-verification.',
   true, false),

  (date '2026-09-17', 'US', 'EOD_CURRENT_US', 'Finnhub free tier',
   'https://finnhub.io/pricing', 'REJECTED_COVERAGE', '0 USD',
   'The stock-candles operation is marked premium in the official API spec; the free tier returns no daily bars at all.',
   'https://finnhub.io/docs/api/stock-candles', null, true, false),

  (date '2026-09-17', 'US', 'EOD_CURRENT_US', 'Twelve Data free Basic',
   'https://twelvedata.com/pricing', 'REJECTED_COVERAGE', '0 USD',
   'Eight credits a minute and 800 a day against a universe of roughly 5,400; and storage is permitted only within timeframes specified in documentation that could not be found.',
   'https://twelvedata.com/terms', null, true, false),

  (date '2026-09-17', 'US', 'EOD_CURRENT_US', 'Marketstack free tier',
   'https://marketstack.com/product', 'REJECTED_COVERAGE', '0 USD',
   'One hundred requests a month - roughly five a trading day.',
   'https://marketstack.com/product', 'Storage terms also could not be established.', true, false),

  (date '2026-09-17', 'US', 'EOD_CURRENT_US', 'Tradier Lite',
   'https://documentation.tradier.com/', 'UNRESOLVED', '0 USD',
   'Neither the API agreement nor the customer agreement contains any market-data licence clause, so retention is neither permitted nor forbidden in writing.',
   'https://documentation.tradier.com/',
   'Free to brokerage account holders, 120 requests a minute, a daily OHLCV endpoint. Fails only on evidence; one written enquiry would settle it.',
   true, false),

  -- ------------------------------------------------ corporate actions, free
  (date '2026-09-17', 'US', 'DELISTING_HISTORY_US', 'SEC EDGAR (Form 25, 25-NSE, 8-K)',
   'https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data', 'QUALIFIES', '0 USD',
   'Information presented on sec.gov is considered public information and may be copied or further distributed by users of the web site without the SEC''s permission.',
   'https://www.sec.gov/privacy',
   'The cleanest source in the whole review: permits more than this project needs. Ten requests a second with a declared User-Agent carrying a contact address. Gives delisting dates with a legal paper trail; does not give structured dividends, forward splits or a ticker-change table.',
   true, false),

  (date '2026-09-17', 'JP', 'CORPORATE_ACTIONS_JP', 'JPX datasets (delisted issues, ex-rights information, name changes, data_j.xlsx)',
   'https://www.jpx.co.jp/listing/stocks/delisted/', 'QUALIFIES_WITH_CAVEAT', '0 JPY',
   'robots.txt disallows nothing; the terms restrict secondary use and redistribution without defining secondary use.',
   'https://www.jpx.co.jp/robots.txt',
   'Carries the same interpretive risk as the JPX price files, and would be settled by the same written enquiry. This is the free answer to the delisting dates and code changes J-Quants does not publish.',
   true, false),

  (date '2026-09-17', 'US', 'SECURITY_MASTER_US', 'Nasdaq Trader symbol directory',
   'https://www.nasdaqtrader.com/trader.aspx?id=symboldirdefs', 'QUALIFIES_WITH_CAVEAT', '0 USD',
   'Content may not be stored for subsequent use without prior written consent, except one temporary copy in memory and one unaltered permanent copy for the viewer''s personal and non-commercial use.',
   'https://www.nasdaqtrader.com/Trader.aspx?id=DisclaimerPolicies',
   'Already the Phase 1 US security master. The personal non-commercial exception covers this project''s use, and is worth recording because the headline clause reads restrictive. Publishes no prices.',
   true, false)
on conflict do nothing;

create index if not exists provider_evaluations_verdict_idx
  on market.provider_evaluations (verdict, market_scope);

create or replace view market.zero_cost_status as
  select role,
         count(*) filter (where verdict = 'QUALIFIES') as qualifies,
         count(*) filter (where verdict = 'QUALIFIES_WITH_CAVEAT') as qualifies_with_caveat,
         count(*) filter (where verdict = 'UNRESOLVED') as unresolved,
         count(*) filter (where verdict like 'REJECTED%') as rejected,
         count(*) as evaluated
  from market.provider_evaluations
  where role is not null
  group by role
  order by role;

comment on view market.zero_cost_status is
  'Per role: how the search for a zero-cost source is going. A role with only rejections is a role the platform cannot fill for free today, and that should be visible rather than inferred.';

revoke all on table market.provider_evaluations from public;
grant select on table market.provider_evaluations, market.zero_cost_status
  to surge_worker_prod, surge_worker_research, surge_readonly, surge_purge;
