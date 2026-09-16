-- Phase 1: reference seed data (exchanges, sources, universe definition 1.0.0).
-- Seed rows are reference data, not market data, so they belong in version control.

insert into ref.exchanges (exchange_id, market_code, mic, name, country, timezone, is_target, notes) values
  ('XTKS', 'JP', 'XTKS', 'Tokyo Stock Exchange',       'JP', 'Asia/Tokyo',     true,  'Prime / Standard / Growth are in scope; TOKYO PRO Market is not.'),
  ('XNYS', 'US', 'XNYS', 'New York Stock Exchange',    'US', 'America/New_York', true,  null),
  ('XNAS', 'US', 'XNAS', 'Nasdaq Stock Market',        'US', 'America/New_York', true,  null),
  ('XASE', 'US', 'XASE', 'NYSE American',              'US', 'America/New_York', true,  null),
  ('ARCX', 'US', 'ARCX', 'NYSE Arca',                  'US', 'America/New_York', false, 'Out of scope in universe-1.0.0 (D-10d).'),
  ('BATS', 'US', 'BATS', 'Cboe BZX Exchange',          'US', 'America/New_York', false, 'Out of scope in universe-1.0.0 (D-10d).'),
  ('IEXG', 'US', 'IEXG', 'Investors Exchange',         'US', 'America/New_York', false, 'Out of scope in universe-1.0.0 (D-10d).'),
  ('OTCM', 'US', null,   'Over the counter',           'US', 'America/New_York', false, 'Excluded by universe-1.0.0.')
on conflict (exchange_id) do nothing;

insert into pipeline.sources (source_id, name, market_code, source_kind, base_url, terms_url, terms_checked_at, full_text_allowed, notes) values
  ('jpx_listed_issues', 'JPX listed issue list (data_j.xlsx)', 'JP', 'EXCHANGE_OFFICIAL',
   'https://www.jpx.co.jp/markets/statistics-equities/misc/01.html', 'https://www.jpx.co.jp/termsofuse/', '2026-09-16', true,
   'Official monthly listed issue workbook; provides market / product classification.'),
  ('nasdaq_trader_symbol_directory', 'Nasdaq Trader Symbol Directory', 'US', 'EXCHANGE_OFFICIAL',
   'https://www.nasdaqtrader.com/dynamic/SymDir/', 'https://www.nasdaqtrader.com/Trader.aspx?id=DisclaimerPage', '2026-09-16', true,
   'nasdaqlisted.txt and otherlisted.txt: exchange, ETF flag, test issue flag, financial status.'),
  ('sec_company_tickers', 'SEC company tickers (exchange mapping)', 'US', 'REGULATOR',
   'https://www.sec.gov/files/company_tickers_exchange.json', 'https://www.sec.gov/about/privacy-information#security', '2026-09-16', true,
   'CIK to ticker/exchange mapping. Requires a declared User-Agent; max 10 requests/second.'),
  ('sec_sic_directory', 'SEC EDGAR company list by SIC', 'US', 'REGULATOR',
   'https://www.sec.gov/cgi-bin/browse-edgar', 'https://www.sec.gov/about/privacy-information#security', '2026-09-16', true,
   'Used to identify blank check companies (SIC 6770) and REITs (SIC 6798).')
on conflict (source_id) do nothing;

insert into universe.definitions (universe_version, spec_path, spec_sha256, description, effective_from) values
  ('universe-1.0.0',
   'docs/specs/universe-definition-v1.0.0.md',
   'pending',
   'JP: TSE Prime/Standard/Growth domestic common stock. US: NYSE / Nasdaq / NYSE American common stock plus eligible ADR. Excludes ETF, REIT, preferred, warrant, unit, right, OTC, pre-merger SPAC and TOKYO PRO Market.',
   date '2026-09-16')
on conflict (universe_version) do nothing;
