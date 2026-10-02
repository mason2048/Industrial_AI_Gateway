const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { createApiClient, managementHeaders } = require('../frontend/api-client.js');
const { staleSnapshot, canWrite, createRealtimePoller } = require('../frontend/realtime.js');
const { createHistoryService, makeSelection, variableKey, buildSeriesChart } = require('../frontend/history.js');
const response = (data, status = 200, etag = '"revision-1"') => ({ ok: status >= 200 && status < 300,
  status, statusText: 'test response', headers: { get: name => name.toLowerCase() === 'etag' ? etag : null }, json: async () => data });
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };
function appFixture(fetchImpl) {
  const root = path.join(__dirname, '..'), context = { console, setTimeout, clearTimeout, setInterval, clearInterval, FormData, confirm: () => true };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(root, 'frontend/vendor/vue.global.prod.js'), 'utf8'), context);
  const Vue = context.Vue;
  const api = createApiClient({ fetchImpl });
  context.IAGApi = { ...require('../frontend/api-client.js'), createApiClient: () => api };
  context.IAGHistory = require('../frontend/history.js'); context.IAGRealtime = require('../frontend/realtime.js');
  context.Vue = { ...Vue, onMounted() {}, onUnmounted() {}, createApp(options) {
    context.options = options; return { mount() { context.state = options.setup(); } };
  } };
  vm.runInContext(fs.readFileSync(path.join(root, 'frontend/app.js'), 'utf8'), context);
  return { state: context.state, api, Vue, chart: context.options.components.HistoryChart };
}

test('request timeout is bounded even if fetch ignores AbortSignal', async () => {
  let signal;
  const api = createApiClient({ timeoutMs: 15, fetchImpl: (_, options) => { signal = options.signal; return new Promise(() => {}); } });
  await assert.rejects(api.request('/hung'), error => error.message.includes('超时'));
  assert.equal(signal.aborted, true);
});

test('response body timeout is bounded', async () => {
  const api = createApiClient({ timeoutMs: 15, fetchImpl: async () => ({ ...response({}), json: () => new Promise(() => {}) }) });
  await assert.rejects(api.request('/hung-body'), error => error.message.includes('超时'));
});

test('latest request wins and old responses cannot overwrite the new result', async () => {
  const first = deferred(), second = deferred(); let calls = 0;
  const api = createApiClient({ fetchImpl: () => (++calls === 1 ? first.promise : second.promise) });
  const old = api.request('/old', { channel: 'history' });
  const oldRejected = assert.rejects(old, error => error.name === 'AbortError');
  const latest = api.request('/new', { channel: 'history' });
  second.resolve(response({ result: 'new' }));
  assert.deepEqual(await latest, { result: 'new' });
  first.resolve(response({ result: 'old' }));
  await oldRejected;
});

test('ETag is preserved and management mutations require a version and PIN', async () => {
  const api = createApiClient({ fetchImpl: async () => response([{ id: 1 }], 200, '"tags-3"') });
  const loaded = await api.requestResult('/api/tags');
  assert.equal(loaded.etag, '"tags-3"');
  assert.deepEqual(managementHeaders(' operator ', loaded.etag), { 'X-Operator-Pin': 'operator', 'If-Match': '"tags-3"' });
  assert.throws(() => managementHeaders(''), error => error.status === 403);
  assert.throws(() => managementHeaders('operator', null), error => error.status === 428);
  const conflict = createApiClient({ fetchImpl: async () => response({ detail: 'stale' }, 412) });
  await assert.rejects(conflict.request('/api/tags'), error => error.status === 412 && error.message.includes('其他页面'));
});

test('readiness 503 is inspectable only when explicitly accepted', async () => {
  const api = createApiClient({ fetchImpl: async () => response({ ready: false, reason: 'storage failed' }, 503) });
  assert.deepEqual(await api.request('/api/ready', { acceptedStatuses: [503] }), { ready: false, reason: 'storage failed' });
  await assert.rejects(api.request('/api/ready'), error => error.status === 503);
});

test('history paging remains attached to submitted source, connection and retired definition', async () => {
  const calls = [], api = { request: async url => { calls.push(url); return { items: [], total: 4001 }; } };
  const service = createHistoryService(api);
  const retired = { tag_id: 9, name: 'Old pressure', unit: 'Pa', device: 'Retired equipment', source: 'opcua', connection_id: 'old-connection', tag_revision: 2, active: false };
  const form = { source: 'opcua', device: retired.device, variableKey: variableKey(retired), start: '2026-09-01T00:00:00Z', end: '2026-09-02T00:00:00Z' };
  const selection = makeSelection(form, [retired]);
  await service.submit(selection);
  form.source = 'simulation'; form.start = '2026-09-03T00:00:00Z';
  await service.page(2000);
  const query = new URL(calls.at(-1), 'http://localhost').searchParams;
  assert.equal(query.get('source'), 'opcua'); assert.equal(query.get('connection_id'), 'old-connection');
  assert.equal(query.get('tag_revision'), '2'); assert.equal(query.get('offset'), '2000');
  assert.equal(query.get('start'), '2026-09-01T00:00:00.000Z');
  assert.throws(() => makeSelection(form, [retired]), /请选择/);
});

test('failed history query does not replace the previous paging snapshot', async () => {
  let fail = false; const calls = [];
  const service = createHistoryService({ request: async url => { calls.push(url); if (fail) throw new Error('failed'); return { items: [] }; } });
  await service.submit({ tag_id: 1, source: 'opcua' });
  fail = true; await assert.rejects(service.submit({ tag_id: 2, source: 'simulation' }));
  fail = false; await service.page(2000);
  assert.equal(new URL(calls.at(-1), 'http://localhost').searchParams.get('tag_id'), '1');
});

test('bad quality and empty buckets break trends while extrema remain visible', () => {
  const rows = [
    { timestamp: '2026-09-01T00:00:00Z', first: 5, last: 6, minimum: -20, maximum: 30, quality: 'Good' },
    { timestamp: '2026-09-01T00:01:00Z', first: null, last: null, minimum: null, maximum: null, quality: 'Gap' },
    { timestamp: '2026-09-01T00:02:00Z', first: 7, last: 8, minimum: 7, maximum: 8, quality: 'Good' }
  ];
  const chart = buildSeriesChart(rows, '2026-09-01T00:00:00Z', '2026-09-02T00:00:00Z');
  assert.equal(chart.segments.length, 2); assert.equal(chart.ranges.length, 2);
  assert.ok(chart.min < -20); assert.ok(chart.max > 30);
  assert.equal(chart.times.at(-1).timestamp, Date.parse('2026-09-02T00:00:00Z'));
});

test('history failure cannot change realtime state, but realtime failure clears good values', async () => {
  const snapshot = { connected: true, good: 1, items: [{ value: 8, quality: 'Good' }] };
  let current = snapshot;
  const api = createApiClient({ fetchImpl: async url => url === '/api/current' ? response(snapshot) : response({ detail: 'history failed' }, 500) });
  const poller = createRealtimePoller(api, { onData: data => { current = data; }, onError: error => { current = staleSnapshot(current, error.message); }, interval: 1000 });
  poller.start();
  await assert.rejects(api.request('/api/history', { channel: 'history' }));
  assert.equal(current.connected, true); assert.equal(current.items[0].value, 8); poller.stop();
  const stale = staleSnapshot(current, 'backend timeout');
  assert.equal(stale.connected, false); assert.equal(stale.good, 0); assert.equal(stale.items[0].value, null); assert.equal(stale.items[0].quality, 'Stale');
});

test('write controls are available only for explicitly enabled simulation', () => {
  assert.equal(canWrite({ mode: 'opcua', write_enabled: true }), false);
  assert.equal(canWrite({ mode: 'simulation' }), false);
  assert.equal(canWrite({ mode: 'simulation', write_enabled: false }), false);
  assert.equal(canWrite({ mode: 'simulation', write_enabled: true }), true);
});

test('Excel preflight does not write until confirmation and uses its own ETag', async () => {
  const calls = [];
  const { state } = appFixture(async (url, options) => {
    calls.push({ url, options });
    if (url.endsWith('dry_run=true')) return response({ count: 1, diff: { added: [{ id: 3, name: 'Pump' }], removed: [], changed: [] } }, 200, '"preview-7"');
    if (url === '/api/tags/import') return response({ count: 1 });
    if (url === '/api/tags') return response([{ id: 3, name: 'Pump', device: 'D' }]);
    throw new Error('Unexpected call ' + url);
  });
  state.managerPin.value = 'operator';
  await state.importTags({ target: { files: [new File(['sheet'], 'tags.xlsx')], value: 'file' } });
  assert.equal(calls.length, 1); assert.equal(state.importPreview.value.count, 1);
  await state.applyImport();
  const applied = calls.find(call => call.url === '/api/tags/import');
  assert.equal(applied.options.headers['If-Match'], '"preview-7"');
  assert.equal(applied.options.headers['X-Operator-Pin'], 'operator');
  assert.equal(state.importPreview.value, null);
});

test('connection editing keeps its original ETag when unrelated refresh gets a newer version', async () => {
  let configVersion = '"config-1"'; const calls = [];
  const { state } = appFixture(async (url, options) => {
    calls.push({ url, options });
    if (url === '/api/tags') return response([]);
    if (url === '/api/config') return response({ mode: 'simulation', endpoint: 'opc.tcp://localhost:4840' }, 200, configVersion);
    if (url === '/api/connection') return response({ detail: 'changed elsewhere' }, 412);
    throw new Error('Unexpected call ' + url);
  });
  state.managerPin.value = 'operator'; await state.refresh(); await state.navigate('connection');
  state.connection.value.endpoint = 'opc.tcp://newhost:4840';
  configVersion = '"config-2"'; await state.refresh(); await state.saveConnection();
  const applied = calls.find(call => call.url === '/api/connection');
  assert.equal(applied.options.headers['If-Match'], '"config-1"');
  assert.match(state.error.value, /其他页面/);
  assert.equal(state.connection.value.endpoint, 'opc.tcp://newhost:4840');
});

test('overview queries the current connection and revision rather than legacy history', async () => {
  const calls = [];
  const { state, Vue, chart } = appFixture(async url => {
    calls.push(url);
    if (url === '/api/tags') return response([{ id: 3, name: 'Flow', device: 'Line 2', unit: 'm3/h', revision: 4 }]);
    if (url === '/api/config') return response({ mode: 'opcua', endpoint: 'opc.tcp://plc:4840' });
    if (url.startsWith('/api/history/series')) return response({ series: [], items: [] });
    throw new Error('Unexpected call ' + url);
  });
  state.current.value = { mode: 'opcua', connection_id: 'new-connection', items: [{ id: 3, revision: 4 }] };
  await state.refresh();
  const query = new URL(calls.filter(url => url.startsWith('/api/history/series')).at(-1), 'http://localhost').searchParams;
  assert.equal(query.get('connection_id'), 'new-connection'); assert.equal(query.get('tag_revision'), '4');
  assert.equal(state.overviewSeriesGroups.value.length, 0);
  const errors = []; Vue.compile(chart.template, { decodeEntities: value => value, onError: error => errors.push(error.message) });
  assert.deepEqual(errors, []);
});

test('Vue templates compile without errors and BOOL confirmation freezes its target', () => {
  const root = path.join(__dirname, '..'), html = fs.readFileSync(path.join(root, 'frontend/index.html'), 'utf8');
  const context = { console }; vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(root, 'frontend/vendor/vue.global.prod.js'), 'utf8'), context);
  const template = html.slice(html.indexOf('<div id="app"'), html.indexOf('<script src="/api-client.js"'));
  const errors = [];
  context.Vue.compile(template, { decodeEntities: value => value, onError: error => errors.push(error.message) });
  assert.deepEqual(errors, []);
  assert.match(html, /v-if="writeTag.type==='BOOL'"[^>]*:disabled="!!writeProposal"/);
  for (const file of ['api-client.js', 'realtime.js', 'history.js', 'app.js']) new vm.Script(fs.readFileSync(path.join(root, 'frontend', file), 'utf8'), { filename: file });
});
