-- Correct what the database says about the hosted analysis provider.
--
-- Three corrections, all of them in the direction of claiming less.
--
-- The Services Agreement has a canonical URL and the row pointed at the terms of
-- sale instead. Retention was recorded as "none by default", which is one of two
-- things the data page says; the other is that data may be retained transiently
-- for reliability and abuse handling, and until Zero Data Retention is confirmed
-- switched on for the account the weaker statement is the true one. And the row
-- said nothing about the free tier's token limit, which is the thing that
-- actually decides whether this provider can run the analysis at all.
--
-- The measured floor is recorded here because it is a property of this
-- repository rather than of Groq: Canonical v5.1 is 32,012 bytes, and the
-- smallest possible request - the canonical prompt, the addenda, the output
-- contract and an empty bundle - estimates at about 13,800 tokens. The
-- gpt-oss family's free limit is 8,000 tokens per minute, so one request cannot
-- be sent. That is a fact worth storing next to the provider, because the
-- obvious remedy is to shorten the prompt and the whole point is that we do not.

set search_path = '';

update analysis.llm_providers
   set notes = 'Chosen on how it treats what is sent to it rather than on quality: every request carries Canonical v5.1 in full. Services Agreement (https://console.groq.com/docs/legal/services-agreement): "Groq is not permitted to use Inputs or Outputs for training or fine-tuning any AI Model Services or other models, unless explicitly granted permission or instructed by Customer." Data page (https://console.groq.com/docs/your-data): "By default, Groq does not retain customer data for inference requests", alongside a documented possibility of transient retention for reliability and abuse handling; until Zero Data Retention is confirmed enabled ON THIS ACCOUNT the retention state is TRANSIENT_FOR_ABUSE_AND_RELIABILITY, and the privacy gate does not pass. ZDR being available is a fact about the product, not about the account. The free Gemini tier was rejected on its own terms, which state that submitted content is used to develop Google products and that human reviewers may read it. model_id is null because the model is configuration: naming one here would make an output row''s model_id a fiction the first time the catalogue changed. FEASIBILITY (measured 2026-09-17, no credential needed): the smallest possible request is about 13,800 estimated tokens - Canonical v5.1 is 32,012 bytes and estimates at about 9,900 alone. Groq''s published free limit for openai/gpt-oss-* is 8,000 tokens per minute, so a single request cannot be sent: ANALYSIS_FREE_QUOTA_BLOCKED. groq/compound* publishes 70,000 tokens per minute and fits on size, but is not among the models documented for strict structured outputs, which is unverified rather than absent. The canonical prompt is NOT shortened to fit a quota.'
 where provider_id = 'groq_hosted';

comment on column analysis.llm_providers.enabled is
  'Whether this provider may be used. Enabling a hosted model requires two separate things to be true: the account''s real rate limits have been measured (not read from a documentation page), and Zero Data Retention has been confirmed switched on. Neither is knowable from inside this database.';
