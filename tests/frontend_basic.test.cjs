const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const apiModule = require('../frontend/api-client.js');

const response = (data, etag = '"version-1"') => ({ ok: true, status: 200,
  headers: { get: name => name.toLowerCase() === 'etag' ? etag : null }, json: async () => data });

function appFixture(fetchImpl) {
  const root = path.join(__dirname, '..');
  const context = { console, setTimeout, clearTimeout, setInterval, clearInterval, FormData, confirm: () => true };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(root, 'frontend/vendor/vue.global.prod.js'), 'utf8'), context);
  const Vue = context.Vue;
  context.IAGApi = { ...apiModule, createApiClient: () => apiModule.createApiClient({ fetchImpl }) };
  context.IAGHistory = require('../frontend/history.js');
  context.IAGRealtime = require('../frontend/realtime.js');
  context.Vue = { ...Vue, onMounted() {}, onUnmounted() {}, createApp(options) {
    return { mount() { context.state = options.setup(); } };
  } };
  vm.runInContext(fs.readFileSync(path.join(root, 'frontend/app.js'), 'utf8'), context);
  return context.state;
}

test('connection draft saves reading and retention settings together after a background refresh', async () => {
  const calls = [];
  let stored = { mode: 'opcua', endpoint: 'opc.tcp://old-host:4840', poll_interval: 2,
    batch_size: 80, heartbeat_seconds: 1800, retention_days: 7 };
  const state = appFixture(async (url, options) => {
    calls.push({ url, options });
    if (url === '/api/config') return response(stored);
    if (url === '/api/tags') return response([]);
    if (url === '/api/connection') return response({ message: '配置已应用' });
    throw new Error('Unexpected request: ' + url);
  });
  state.managerPin.value = 'operator';
  await state.refresh(); await state.navigate('connection');
  Object.assign(state.connection.value, { endpoint: 'opc.tcp://new-host:4840', poll_interval: '0.5', batch_size: '1000',
    heartbeat_seconds: '900', retention_days: '3' });
  stored = { ...stored, poll_interval: 10, batch_size: 10 };
  await state.refresh(); await state.saveConnection();
  const request = calls.find(call => call.url === '/api/connection');
  assert.deepEqual(JSON.parse(request.options.body), { mode: 'opcua', endpoint: 'opc.tcp://new-host:4840',
    poll_interval: 0.5, batch_size: 1000, heartbeat_seconds: 900, retention_days: 3 });
  assert.equal(request.options.headers['X-Operator-Pin'], 'operator');
  assert.equal(state.connection.value.poll_interval, '0.5');
});

test('point editing keeps inherited interval null and sends configured precision as a number', async () => {
  const calls = [];
  const original = { id: 7, address: 'DB1.DBD20', name: '真空', type: 'FLOAT', device: '设备01',
    node_id: 'ns=2;s=Vacuum', save: true, threshold: 0.00005, revision: 1 };
  const state = appFixture(async (url, options) => {
    calls.push({ url, options });
    if (url === '/api/config') return response({ mode: 'simulation', endpoint: 'opc.tcp://localhost:4840' });
    if (url === '/api/tags') return response([original]);
    throw new Error('Unexpected request: ' + url);
  });
  state.managerPin.value = 'operator'; await state.refresh(); state.editTag(state.tags.value[0]);
  assert.equal(state.editing.value.precision, 5);
  assert.equal(state.editing.value.history_interval_seconds, null);
  state.editing.value.history_interval_seconds = ''; state.editing.value.precision = '8';
  await state.saveTag();
  const written = JSON.parse(calls.find(call => call.options?.method === 'PUT').options.body)[0];
  assert.equal(written.history_interval_seconds, null);
  assert.equal(written.precision, 8);
  assert.equal(written.threshold, 0.00005);
});

test('copying a point for a new address adds a new ID and preserves the original definition', async () => {
  const original = { id: 4, address: 'DB1.DBD20', name: '真空', type: 'FLOAT', device: '设备01',
    node_id: 'ns=2;s=Vacuum', save: true, threshold: 0.00005, history_interval_seconds: 600, precision: 5, revision: 3 };
  let written;
  const state = appFixture(async (url, options) => {
    if (url === '/api/config') return response({ mode: 'simulation', endpoint: 'opc.tcp://localhost:4840' });
    if (url === '/api/tags/next-id') return response({ next_id: 5 });
    if (url === '/api/tags') {
      if (options.method === 'PUT') written = JSON.parse(options.body);
      return response([original]);
    }
    throw new Error('Unexpected request: ' + url);
  });
  state.managerPin.value = 'operator'; await state.refresh(); await state.editTag(state.tags.value[0]); await state.copyTag();
  assert.equal(state.editingOriginal.value, null); assert.equal(state.editing.value.id, 5);
  assert.equal(state.editing.value.revision, undefined);
  state.editing.value.address = 'DB1.DBD24'; state.editing.value.node_id = 'ns=2;s=Vacuum2';
  await state.saveTag();
  assert.equal(written.length, 2); assert.deepEqual(written[0], original);
  assert.equal(written[1].id, 5); assert.equal(written[1].address, 'DB1.DBD24');
  assert.equal(written[1].history_interval_seconds, 600);
});

test('vacuum values retain five decimal places and values below precision remain visible', () => {
  const state = appFixture(async () => response([]));
  assert.equal(state.fmt(0.12345), '0.12345');
  assert.equal(state.fmt(0.00005), '0.00005');
  assert.equal(state.fmt(0.000001), '1.00000e-6');
  assert.equal(state.fmt(1.23456789, 8), '1.23456789');
  assert.equal(state.fmt(1.5, 0), '2');
  assert.equal(state.fmt(false), 'FALSE'); assert.equal(state.fmt(null), '—');
});

test('new points default to timed history and an explicit change recording choice is submitted', async () => {
  let written;
  const state = appFixture(async (url, options) => {
    if (url === '/api/config') return response({ mode: 'simulation', endpoint: 'opc.tcp://localhost:4840' });
    if (url === '/api/tags/next-id') return response({ next_id: 1 });
    if (url === '/api/tags') {
      if (options.method === 'PUT') written = JSON.parse(options.body);
      return response([]);
    }
    throw new Error('Unexpected request: ' + url);
  });
  state.managerPin.value = 'operator'; await state.refresh(); await state.editTag();
  assert.equal(state.editing.value.record_changes, false);
  Object.assign(state.editing.value, { name: '真空', address: 'D100', device: '泵01', record_changes: true,
    history_interval_seconds: '1800', threshold: '0.00005' });
  await state.saveTag();
  assert.equal(written[0].record_changes, true); assert.equal(written[0].history_interval_seconds, 1800);
  assert.equal(written[0].threshold, 0.00005);
});

test('desktop exit requires a PIN and clears polling and credentials after a stop is accepted', async () => {
  const calls = [];
  const state = appFixture(async (url, options) => {
    calls.push({ url, options });
    if (url === '/api/shutdown') return response({ status: 'stopping', message: '正在停止' });
    throw new Error('Unexpected request: ' + url);
  });
  await state.shutdownGateway();
  assert.equal(calls.length, 0);
  assert.match(state.error.value, /管理口令/);
  state.managerPin.value = 'temporary-operator';
  await state.shutdownGateway();
  assert.equal(calls[0].url, '/api/shutdown');
  assert.equal(calls[0].options.headers['X-Operator-Pin'], 'temporary-operator');
  assert.equal(state.shutdownRequested.value, true);
  assert.equal(state.current.value.state, 'stopping');
  assert.equal(state.managerPin.value, '');
});

test('clearing saved PLC credentials is explicit and consistent in candidate tests and saving', async () => {
  const calls = [];
  const state = appFixture(async (url, options) => {
    calls.push({ url, options });
    if (url === '/api/config') return response({ mode: 'opcua', endpoint: 'opc.tcp://localhost:4840',
      poll_interval: 1, batch_size: 100, heartbeat_seconds: 1800, retention_days: 7 });
    if (url === '/api/tags') return response([]);
    if (url === '/api/connection/test') return response({ success: true, message: '候选可连接' });
    if (url === '/api/connection') return response({ message: '已应用' });
    throw new Error('Unexpected request: ' + url);
  });
  state.managerPin.value = 'operator'; await state.refresh(); await state.navigate('connection');
  await state.testConnection();
  const unchanged = JSON.parse(calls.find(call => call.url === '/api/connection/test').options.body);
  assert.equal(Object.hasOwn(unchanged, 'username'), false); assert.equal(Object.hasOwn(unchanged, 'security_string'), false);
  state.connection.value.username = 'draft-user'; state.connection.value.security_string = 'draft-security';
  state.connection.value.clear_username = true; state.connection.value.clear_security_string = true;
  await state.testConnection(); await state.saveConnection();
  const candidate = JSON.parse(calls.filter(call => call.url === '/api/connection/test').at(-1).options.body);
  const saved = JSON.parse(calls.find(call => call.url === '/api/connection').options.body);
  assert.equal(candidate.username, ''); assert.equal(candidate.security_string, '');
  assert.deepEqual(saved, candidate);
  assert.equal(Object.hasOwn(saved, 'clear_username'), false);
});

test('an empty point table can allocate a new point and accept a first device', async () => {
  let written;
  const state = appFixture(async (url, options) => {
    if (url === '/api/config') return response({ mode: 'simulation' });
    if (url === '/api/tags/next-id') return response({ next_id: 11 });
    if (url === '/api/tags') {
      if (options.method === 'PUT') written = JSON.parse(options.body);
      return response([]);
    }
    throw new Error('Unexpected request: ' + url);
  });
  state.managerPin.value = 'operator'; await state.refresh(); await state.editTag();
  assert.equal(state.devices.value.length, 0); assert.equal(state.editing.value.device, '');
  Object.assign(state.editing.value, { name: '首个点位', device: '新设备', address: 'M0.0' });
  await state.saveTag();
  assert.equal(written.length, 1); assert.equal(written[0].device, '新设备'); assert.equal(written[0].id, 11);
  assert.equal(state.tagRowNumber(0), 1);
});
