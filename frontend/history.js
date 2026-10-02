(function (root, factory) {
  const exported = factory();
  if (typeof module === 'object' && module.exports) module.exports = exported;
  else root.IAGHistory = exported;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  const variableKey = row => JSON.stringify([row.source, row.connection_id, row.tag_id, row.tag_revision, row.device, row.name, row.unit]);
  function queryString(selection, extra = {}) {
    const fields = { ...selection, ...extra };
    const query = new URLSearchParams();
    for (const key of ['tag_id', 'device', 'source', 'connection_id', 'tag_revision', 'start', 'end', 'limit', 'offset', 'buckets']) {
      if (fields[key] !== undefined && fields[key] !== null && fields[key] !== '') query.set(key, fields[key]);
    }
    return query.toString();
  }
  function makeSelection(form, catalog) {
    const row = catalog.find(item => variableKey(item) === form.variableKey);
    if (!row || row.device !== form.device || row.source !== form.source) throw new Error('请选择要查询的历史变量版本。');
    const start = new Date(form.start), end = new Date(form.end);
    if (!Number.isFinite(start.getTime()) || !Number.isFinite(end.getTime()) || start >= end) throw new Error('请输入有效的开始和结束时间，且开始时间早于结束时间。');
    return Object.freeze({ tag_id: row.tag_id, device: row.device, source: row.source,
      connection_id: row.connection_id, tag_revision: row.tag_revision,
      variable: row.name, unit: row.unit, start: start.toISOString(), end: end.toISOString() });
  }
  function createHistoryService(api) {
    let committed = null, generation = 0;
    return {
      catalog: source => api.request('/api/history/variables?' + new URLSearchParams({ source }), { channel: 'history-catalog' }),
      async submit(selection) {
        const selected = Object.freeze({ ...selection }), sequence = ++generation;
        const [detail, series] = await Promise.all([
          api.request('/api/history?' + queryString(selected, { offset: 0, limit: 2000 }), { channel: 'history-detail' }),
          api.request('/api/history/series?' + queryString(selected, { buckets: 500 }), { channel: 'history-series' })
        ]);
        if (sequence !== generation) { const error = new Error('查询已替换'); error.name = 'AbortError'; throw error; }
        committed = selected;
        return { selection: selected, detail, series };
      },
      async page(offset) {
        if (!committed) throw new Error('请先提交一次历史查询。');
        const selected = committed;
        const detail = await api.request('/api/history?' + queryString(selected, { offset, limit: 2000 }), { channel: 'history-detail' });
        if (selected !== committed) { const error = new Error('查询已替换'); error.name = 'AbortError'; throw error; }
        return { selection: selected, detail };
      },
      overview: selection => api.request('/api/history/series?' + queryString(selection, { buckets: 240 }), { channel: 'overview-series' })
    };
  }
  function buildSeriesChart(items, start, end) {
    const rows = items || [];
    const good = rows.filter(row => row.quality === 'Good' && row.minimum !== null && row.maximum !== null && Number.isFinite(Number(row.minimum)) && Number.isFinite(Number(row.maximum)));
    if (!good.length) return null;
    let min = Math.min(...good.map(row => Number(row.minimum))), max = Math.max(...good.map(row => Number(row.maximum)));
    const pad = (max - min) * .15 || Math.abs(max) * .1 || 1;
    min -= pad; max += pad;
    const first = Date.parse(start || rows[0].timestamp), last = Date.parse(end || rows.at(-1).timestamp), span = last - first || 1;
    const x = stamp => 62 + (Date.parse(stamp) - first) / span * 504;
    const y = value => 172 - (Number(value) - min) / (max - min) * 145;
    const segments = [], ranges = []; let segment = [];
    for (const row of rows) {
      if (row.quality !== 'Good' || row.minimum === null || row.maximum === null || row.first === null || row.last === null) {
        if (segment.length) segments.push(segment); segment = []; continue;
      }
      ranges.push({ x: x(row.timestamp), y1: y(row.minimum), y2: y(row.maximum) });
      segment.push({ x: x(row.timestamp), y: y(row.first) }, { x: x(row.timestamp), y: y(row.last) });
    }
    if (segment.length) segments.push(segment);
    return { segments, ranges, min, max,
      ticks: Array.from({ length: 4 }, (_, index) => ({ y: 27 + index * 145 / 3, value: max - index * (max - min) / 3 })),
      times: Array.from({ length: 5 }, (_, index) => ({ x: 62 + index * 126, timestamp: first + span * index / 4 })) };
  }
  return { variableKey, queryString, makeSelection, createHistoryService, buildSeriesChart };
});
