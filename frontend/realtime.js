(function (root, factory) {
  const exported = factory();
  if (typeof module === 'object' && module.exports) module.exports = exported;
  else root.IAGRealtime = exported;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  function staleSnapshot(snapshot, message) {
    return { ...snapshot, connected: false, good: 0, state: 'backend_unavailable',
      error: message, items: (snapshot.items || []).map(item => ({ ...item, value: null, quality: 'Stale' })) };
  }
  const canWrite = snapshot => snapshot?.mode === 'simulation' && snapshot.write_enabled === true;
  function createRealtimePoller(api, { onData, onError, interval = 1000 }) {
    let stopped = true, timer;
    async function tick() {
      if (stopped) return;
      try {
        const data = await api.request('/api/current', { channel: 'realtime', timeout: 5000 });
        if (!stopped) onData(data);
      } catch (error) {
        if (!stopped && error.name !== 'AbortError') onError(error);
      } finally {
        if (!stopped) timer = setTimeout(tick, interval);
      }
    }
    return {
      start() { if (!stopped) return; stopped = false; tick(); },
      stop() { stopped = true; clearTimeout(timer); api.cancel('realtime'); }
    };
  }
  return { staleSnapshot, canWrite, createRealtimePoller };
});
