// What the runner keeps of a failed Gateway call.
//
// A Gateway error keeps the HTTP exchange on `cause` (an APICallError). Only
// the response side is copied: `cause.requestBodyValues` is the request body -
// the state, and so the canonical text - so the error object is never recorded
// whole. Response headers carry no credential (the key travels in the
// request's headers). `responseHeaders` is null when there was no HTTP
// response at all.
export const describe = (e) => {
  const http = e?.cause ?? e;
  const headers = http?.responseHeaders;
  const body = http?.responseBody ?? e?.responseBody;
  return {
    name: e?.name,
    type: e?.type,
    message: String(e?.message ?? e).slice(0, 400),
    statusCode: e?.statusCode ?? http?.statusCode,
    responseHeaders: headers && typeof headers === 'object' ? { ...headers } : null,
    responseBody: typeof body === 'string' ? body.slice(0, 400) : undefined,
  };
};
