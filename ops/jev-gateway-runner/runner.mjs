// One Jev evaluation through Vercel AI Gateway (AI SDK experimental_evaluate).
// Usage: node runner.mjs <input.json> <output.json>
//   input: { model, state, questions }  (questions already in the Gateway's shape)
//        node runner.mjs --credits <output.json>
//   the team's Gateway credit balance and total used; no model request.
// Records only what is safe to keep: answers, usage, provider metadata, the
// response id/model/headers and the latency. Never the request - it carries the
// state, and so the canonical text. The API key comes from AI_GATEWAY_API_KEY
// in this process's environment and is never read here.
import { readFileSync, writeFileSync } from 'node:fs';
import { experimental_evaluate as evaluate, gateway } from 'ai';

const describe = (e) => ({
  name: e?.name,
  message: String(e?.message ?? e).slice(0, 400),
  statusCode: e?.statusCode,
  responseBody: typeof e?.responseBody === 'string' ? e.responseBody.slice(0, 400) : undefined,
});

async function credits(outputPath) {
  let out;
  try {
    const { balance, totalUsed } = await gateway.getCredits();
    out = { balance, totalUsed, checkedAt: new Date().toISOString() };
  } catch (e) {
    out = { error: describe(e) };
  }
  writeFileSync(outputPath, JSON.stringify(out, null, 2));
  console.log(JSON.stringify(out.error ? { ok: false, error: out.error.message?.slice(0, 200) } : { ok: true, ...out }));
}

async function evaluateOnce(inputPath, outputPath) {
  const input = JSON.parse(readFileSync(inputPath, 'utf8'));
  const started = performance.now();
  let result;
  let error;
  try {
    result = await evaluate({ model: input.model, state: input.state, questions: input.questions, maxRetries: 0 });
  } catch (e) {
    error = describe(e);
  }
  const latencyMs = Math.round(performance.now() - started);

  const out = error
    ? { latencyMs, error }
    : {
        latencyMs,
        answers: result.answers,
        usage: result.usage,
        providerMetadata: result.providerMetadata,
        response: result.response
          ? { id: result.response.id, modelId: result.response.modelId, timestamp: result.response.timestamp, headers: result.response.headers }
          : undefined,
        warnings: result.warnings,
        resultKeys: Object.keys(result),
      };
  writeFileSync(outputPath, JSON.stringify(out, null, 2));
  console.log(JSON.stringify({ ok: !error, latencyMs, usage: out.usage, modelId: out.response?.modelId, error: error?.message?.slice(0, 200) }));
}

const [, , first, second] = process.argv;
if (first === '--credits') {
  await credits(second);
} else {
  await evaluateOnce(first, second);
}
