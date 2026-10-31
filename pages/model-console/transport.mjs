/**
 * AstrBot Plugin Page 传输层。
 *
 * 页面运行在 dashboard 的受限 iframe 里（sandbox 无 allow-same-origin），因此：
 * - 不能 fetch/XHR 直连后端，也不能用 localStorage/sessionStorage；
 * - 所有请求经 window.AstrBotPluginPage（别名 AstrBotPluginView）转发，由 Dashboard 自动带鉴权；
 * - endpoint 是插件内相对路径（不带插件名前缀、不带前导 `/`、不能带 query/hash）；
 * - bridge 返回值：普通 JSON 原样返回；`{status:"ok",data}` 由父页解包；`{status:"error"}` 或
 *   HTTP 非 2xx 会 reject（只剩 message）。
 *
 * 本插件约定：成功 = HTTP 200 + 业务 JSON；业务错误 = HTTP 200 + `{"error": CODE}`。
 * 因此这里把 body.error 转成带 code 的 Error，让上层继续按错误码渲染文案；**不依赖 HTTP 状态码**。
 */

const DEFAULT_TIMEOUT_MS = 30000;
/** 搜索 Key / 原生搜索测试在后端会发真实网络请求，30–60s 是正常范围。 */
export const LONG_REQUEST_MS = 150000;
const CODE_PATTERN = /^[A-Z][A-Z0-9_]{0,79}$/;

/** Build a transport error carrying a stable business code. */
export function transportError(code, message, details) {
  const error = new Error(message || code || 'BRIDGE_ERROR');
  error.code = code || 'BRIDGE_ERROR';
  if (details !== undefined) error.details = details;
  return error;
}

/** Current bridge object (AstrBotPluginView is the preferred name, AstrBotPluginPage is the alias). */
export function bridge() {
  if (typeof window === 'undefined') return null;
  return window.AstrBotPluginView || window.AstrBotPluginPage || null;
}

export function bridgeAvailable() {
  return Boolean(bridge());
}

/** Resolve the initial context; rejects when the page is not embedded in an AstrBot plugin view. */
export async function ready() {
  const api = bridge();
  if (!api || typeof api.ready !== 'function') {
    throw transportError('BRIDGE_UNAVAILABLE', '未检测到 AstrBot Plugin Page bridge。');
  }
  try {
    return await api.ready();
  } catch (error) {
    const fallback = typeof api.getContext === 'function' ? api.getContext() : null;
    if (fallback) return fallback;
    throw transportError('BRIDGE_UNAVAILABLE', error?.message || 'AstrBot Plugin Page bridge 未就绪。');
  }
}

/** Latest dashboard context ({pluginName,pageName,locale,i18n,isDark,...}) or null. */
export function context() {
  const api = bridge();
  if (!api || typeof api.getContext !== 'function') return null;
  try {
    return api.getContext() ?? null;
  } catch {
    return null;
  }
}

/** Translation through the dashboard i18n bundle, with fallback. */
export function translate(key, fallback = '') {
  const api = bridge();
  if (!api || typeof api.t !== 'function') return fallback;
  try {
    const value = api.t(key, fallback);
    return typeof value === 'string' && value ? value : fallback;
  } catch {
    return fallback;
  }
}

/** Subscribe to context changes (theme/locale). Returns an unsubscribe function. */
export function onContext(handler) {
  const api = bridge();
  if (!api || typeof api.onContext !== 'function') return () => {};
  try {
    return api.onContext(handler) || (() => {});
  } catch {
    return () => {};
  }
}

function normalizeEndpoint(endpoint) {
  if (typeof endpoint !== 'string') {
    throw transportError('BRIDGE_ENDPOINT_INVALID', '插件的 bridge endpoint 必须是字符串。');
  }
  const value = endpoint.trim().replace(/^\/+/, '');
  if (!value) {
    throw transportError('BRIDGE_ENDPOINT_INVALID', '插件的 bridge endpoint 不能为空。');
  }
  if (value.includes('\\') || value.includes('://') || value.includes('?') || value.includes('#')) {
    throw transportError('BRIDGE_ENDPOINT_INVALID', '插件的 bridge endpoint 不能包含 query、hash、协议或反斜杠。');
  }
  if (value.split('/').some(segment => !segment || segment === '.' || segment === '..')) {
    throw transportError('BRIDGE_ENDPOINT_INVALID', '插件的 bridge endpoint 路径不合法。');
  }
  return value;
}

function parseMaybeJson(value) {
  if (typeof value !== 'string') return value;
  try {
    return JSON.parse(value);
  } catch {
    return value;
  }
}

/** Convert bridge/axios failures (which only keep a message) into a coded transport error. */
function toTransportError(cause) {
  if (cause && typeof cause === 'object' && typeof cause.code === 'string' && cause.code) return cause;
  const raw = typeof cause?.message === 'string' ? cause.message.trim() : '';
  let code = 'BRIDGE_ERROR';
  if (CODE_PATTERN.test(raw)) {
    code = raw;
  } else {
    const http = /^Request failed with status code (\d{3})/i.exec(raw);
    if (http) code = `ASTRBOT_HTTP_${http[1]}`;
    else if (/^timeout of \d+ms exceeded$/i.test(raw) || /timeout/i.test(raw) && /exceeded/i.test(raw)) code = 'REQUEST_TIMEOUT';
    else if (/network error/i.test(raw)) code = 'CONSOLE_UNREACHABLE';
    else if (/^Plugin bridge endpoint is invalid/i.test(raw)) code = 'BRIDGE_ENDPOINT_INVALID';
    else if (/^Plugin bridge request failed/i.test(raw)) code = 'BRIDGE_FAILED';
    else if (/^Missing plugin bridge request id/i.test(raw)) code = 'BRIDGE_FAILED';
  }
  return transportError(code, raw || code);
}

function withTimeout(promise, timeoutMs) {
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) return promise;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      reject(transportError('REQUEST_TIMEOUT', `请求超过 ${Math.round(timeoutMs / 1000)} 秒仍未返回，操作结果未确认。`));
    }, timeoutMs);
    promise.then(
      value => {
        clearTimeout(timer);
        resolve(value);
      },
      error => {
        clearTimeout(timer);
        reject(error);
      },
    );
  });
}

async function request(kind, endpoint, payload, timeoutMs) {
  const api = bridge();
  if (!api) {
    throw transportError('BRIDGE_UNAVAILABLE', '未检测到 AstrBot Plugin Page bridge，请从 AstrBot Dashboard 的插件页面打开本页。');
  }
  const normalized = normalizeEndpoint(endpoint);
  const method = kind === 'get' ? 'apiGet' : 'apiPost';
  if (typeof api[method] !== 'function') {
    throw transportError('BRIDGE_UNAVAILABLE', `当前 bridge 不支持 ${method}。`);
  }
  let pending;
  try {
    pending = kind === 'get'
      ? api.apiGet(normalized, payload ?? undefined)
      : api.apiPost(normalized, payload ?? {});
  } catch (error) {
    throw toTransportError(error);
  }
  let raw;
  try {
    raw = await withTimeout(Promise.resolve(pending), timeoutMs);
  } catch (error) {
    throw toTransportError(error);
  }
  const body = parseMaybeJson(raw);
  if (body && typeof body === 'object' && typeof body.error === 'string' && body.error) {
    const details = body;
    throw transportError(body.error, typeof body.message === 'string' && body.message ? body.message : body.error, details);
  }
  return body;
}

/**
 * GET an endpoint. `params` becomes the query string (bridge/axios handles encoding).
 * Business `{"error": CODE}` payloads reject with `error.code === CODE`.
 */
export async function apiGet(endpoint, params, { timeoutMs = DEFAULT_TIMEOUT_MS } = {}) {
  return request('get', endpoint, params, timeoutMs);
}

/**
 * POST JSON to an endpoint. The bridge cannot attach query parameters, so callers that need
 * a device scope must put it in the request body (see CONTRACT §4 note).
 */
export async function apiPost(endpoint, body, { timeoutMs = DEFAULT_TIMEOUT_MS } = {}) {
  return request('post', endpoint, body, timeoutMs);
}

/**
 * Trigger a browser download through the dashboard bridge.
 * Falls back to a JSON blob download (allow-downloads is present in the sandbox) when the
 * bridge has no download() implementation (e.g. older AstrBot or a test stub).
 */
export async function downloadFile(endpoint, params, filename, { timeoutMs = DEFAULT_TIMEOUT_MS } = {}) {
  const normalized = normalizeEndpoint(endpoint);
  const api = bridge();
  if (api && typeof api.download === 'function') {
    try {
      const result = await withTimeout(Promise.resolve(api.download(normalized, params ?? {}, filename)), timeoutMs);
      return { filename: filename || result?.filename || 'download.json' };
    } catch (error) {
      throw toTransportError(error);
    }
  }
  const data = await apiGet(normalized, params, { timeoutMs });
  const name = filename || 'download.json';
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  return { filename: name };
}
