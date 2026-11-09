export class ApiError extends Error {
  constructor(message, { code = 'request_failed', status = null, retryAfterSeconds = null } = {}) {
    super(message);
    this.name = 'ApiError';
    this.code = code;
    this.status = status;
    this.retryAfterSeconds = retryAfterSeconds;
  }
}

export function parseRetryAfter(value, now = Date.now()) {
  if (typeof value !== 'string' || !value.trim()) return null;
  const seconds = Number(value);
  if (Number.isFinite(seconds)) return seconds >= 0 ? seconds : null;
  const date = Date.parse(value);
  return Number.isFinite(date) ? Math.max(0, Math.ceil((date - now) / 1000)) : null;
}

function detailMessage(detail) {
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) return detail.map((item) => detailMessage(item)).filter(Boolean).join(' ');
  if (detail && typeof detail === 'object') return detailMessage(detail.message || detail.msg);
  return '';
}

export function createApiClient({ baseUrl = '/api', fetchImpl = globalThis.fetch, timeoutMs = 290_000 } = {}) {
  const base = baseUrl.replace(/\/+$/, '');
  return async function request(path, options = {}) {
    const controller = new AbortController();
    const callerSignal = options.signal;
    let timedOut = false;
    const cancel = () => controller.abort(callerSignal.reason);
    if (callerSignal?.aborted) cancel();
    else callerSignal?.addEventListener('abort', cancel, { once: true });
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, timeoutMs);
    try {
      const response = await fetchImpl(`${base}${path}`, {
        ...options,
        signal: controller.signal,
        headers: { 'Content-Type': 'application/json', ...options.headers },
      });
      const body = await response.json().catch(() => null);
      if (!response.ok) {
        const detail = body?.detail;
        throw new ApiError(detailMessage(detail) || detailMessage(body?.message) || `Request failed (${response.status}).`, {
          code: (typeof detail?.code === 'string' && detail.code) || (typeof body?.code === 'string' && body.code) || 'request_failed',
          status: response.status,
          retryAfterSeconds: parseRetryAfter(response.headers.get('Retry-After')) ?? (Number.isFinite(detail?.retry_after_seconds) ? Math.max(0, detail.retry_after_seconds) : null),
        });
      }
      if (body === null) throw new ApiError('The server returned an unreadable response.', { code: 'invalid_response', status: response.status });
      return body;
    } catch (error) {
      if (timedOut) throw new ApiError('Request timed out. Check the session before trying again.', { code: 'request_timeout' });
      if (controller.signal.aborted) throw controller.signal.reason || error;
      if (error instanceof ApiError) throw error;
      throw new ApiError('Could not reach the server. Check the connection and try again.', { code: 'connection_error' });
    } finally {
      clearTimeout(timer);
      callerSignal?.removeEventListener('abort', cancel);
    }
  };
}
