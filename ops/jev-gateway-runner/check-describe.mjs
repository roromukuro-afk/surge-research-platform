// Offline check of the runner's `describe`: a rate-limit error built the way
// the Gateway SDK builds it keeps status, type, headers and body - and never
// the request body. Sends nothing. Usage: node check-describe.mjs
import assert from 'node:assert/strict';
import { APICallError } from '@ai-sdk/provider';
import { GatewayRateLimitError } from '@ai-sdk/gateway';
import { describe } from './describe-error.mjs';

const cause = new APICallError({
  message: 'Too Many Requests',
  url: 'https://ai-gateway.vercel.sh/v4/ai/evaluation-model',
  requestBodyValues: { state: 'REQUEST-BODY-MUST-NOT-BE-RECORDED' },
  statusCode: 429,
  responseHeaders: { 'retry-after': '30', 'x-vercel-id': 'hnd1::test' },
  responseBody: '{"error":{"type":"rate_limit_exceeded","message":"Free tier requests on this model are rate-limited."}}',
});
const error = new GatewayRateLimitError({ message: 'Free tier requests on this model are rate-limited.', statusCode: 429, cause });
const recorded = describe(error);

assert.equal(recorded.name, 'GatewayRateLimitError');
assert.equal(recorded.type, 'rate_limit_exceeded');
assert.equal(recorded.statusCode, 429);
assert.deepEqual(recorded.responseHeaders, { 'retry-after': '30', 'x-vercel-id': 'hnd1::test' });
assert.match(recorded.responseBody, /rate_limit_exceeded/);
assert.ok(!JSON.stringify(recorded).includes('REQUEST-BODY-MUST-NOT-BE-RECORDED'));

// An error without an HTTP exchange still describes itself, with no headers.
assert.equal(describe(new Error('network down')).responseHeaders, null);
console.log(JSON.stringify({ ok: true, recorded }));
