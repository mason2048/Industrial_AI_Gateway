(function (root, factory) {
  const exported = factory();
  if (typeof module === 'object' && module.exports) module.exports = exported;
  else root.IAGApi = exported;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  class ApiError extends Error {
    constructor(message, status = 0) { super(message); this.name = 'ApiError'; this.status = status; }
  }
  function cancelled(message = '请求已被新的操作替代') {
    const error = new Error(message); error.name = 'AbortError'; return error;
  }
  function createApiClient({ fetchImpl = (...args) => fetch(...args), timeoutMs = 8000 } = {}) {
    const channels = new Map();
    async function requestResult(path, options = {}) {
      const { channel, timeout = timeoutMs, acceptedStatuses = [], ...fetchOptions } = options;
      if (channel) channels.get(channel)?.controller.abort();
      const controller = new AbortController();
      const task = { controller };
      if (channel) channels.set(channel, task);
      let timedOut = false;
      let rejectAbort;
      const aborted = new Promise((_, reject) => { rejectAbort = reject; });
      controller.signal.addEventListener('abort', () => rejectAbort(timedOut
        ? new ApiError('请求超时，请检查后台状态后重试') : cancelled()), { once: true });
      const timer = setTimeout(() => { timedOut = true; controller.abort(); }, timeout);
      try {
        // Race the complete response, including body reads; cancellation remains effective
        // even if an adapter ignores AbortSignal or the response body stops arriving.
        const work = (async () => {
          const response = await fetchImpl(path, { ...fetchOptions, signal: controller.signal });
          let data;
          try { data = await response.json(); }
          catch { throw new ApiError(response.ok ? '后台返回了无效的数据' : response.statusText || '请求失败', response.status); }
          if (!response.ok && !acceptedStatuses.includes(response.status)) {
            const detail = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail || data);
            const message = response.status === 412 ? '配置已被其他页面修改。请重新加载最新版本，再确认您的修改。'
              : response.status === 428 ? '缺少配置版本，请刷新页面后重试。' : detail;
            throw new ApiError(message, response.status);
          }
          return { data, etag: response.headers.get('ETag') };
        })();
        const result = await Promise.race([work, aborted]);
        if (controller.signal.aborted || (channel && channels.get(channel) !== task)) throw cancelled();
        return result;
      } finally {
        clearTimeout(timer);
        if (channel && channels.get(channel) === task) channels.delete(channel);
      }
    }
    return {
      requestResult,
      request: async (path, options) => (await requestResult(path, options)).data,
      cancel(channel) { channels.get(channel)?.controller.abort(); },
      cancelAll() { for (const task of channels.values()) task.controller.abort(); channels.clear(); }
    };
  }
  const json = (method, body, headers = {}) => ({ method, headers: { 'Content-Type': 'application/json', ...headers }, body: JSON.stringify(body) });
  function managementHeaders(pin, etag) {
    if (!pin || !pin.trim()) throw new ApiError('请先在页面上方输入本机管理口令。', 403);
    const headers = { 'X-Operator-Pin': pin.trim() };
    if (etag !== undefined) {
      if (!etag) throw new ApiError('尚未读取配置版本，请刷新后重试。', 428);
      headers['If-Match'] = etag;
    }
    return headers;
  }
  return { ApiError, createApiClient, json, managementHeaders };
});
