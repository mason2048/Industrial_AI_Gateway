const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const apiModule = require('../frontend/api-client.js');
const response = (data, status = 200, etag = '"ai-1"') => ({ ok: status >= 200 && status < 300, status,
  headers: { get: name => name.toLowerCase() === 'etag' ? etag : null }, json: async () => data });
const config = overrides => ({ provider: 'local_rules', base_url: 'http://127.0.0.1:11434', model: '', timeout_seconds: 60,
  max_output_tokens: 1024, temperature: 0.2, api_key_set: false, revision: 1, deployment: 'local_rules', ...overrides });
function appFixture(fetchImpl) {
  const root = path.join(__dirname, '..');
  const context = { console, setTimeout, clearTimeout, setInterval, clearInterval, FormData, confirm: () => true };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(root, 'frontend/vendor/vue.global.prod.js'), 'utf8'), context);
  const Vue = context.Vue;
  context.IAGApi = { ...apiModule, createApiClient: () => apiModule.createApiClient({ fetchImpl }) };
  context.IAGHistory = require('../frontend/history.js'); context.IAGRealtime = require('../frontend/realtime.js');
  context.Vue = { ...Vue, onMounted() {}, onUnmounted() {}, createApp(options) {
    return { mount() { context.state = options.setup(); } };
  } };
  vm.runInContext(fs.readFileSync(path.join(root, 'frontend/app.js'), 'utf8'), context);
  return context.state;
}

test('visiting model or AI settings reads configuration without running inference', async () => {
  const calls = [];
  const state = appFixture(async (url, options) => {
    calls.push({ url, options });
    if (url === '/api/ai/config') return response(config());
    throw new Error('Unexpected request: ' + url);
  });
  await state.navigate('model');
  assert.equal(state.showManagement.value, true); assert.equal(state.modelDraftInitialized.value, true);
  assert.equal(state.modelForm.value.api_key, ''); assert.equal(state.modelForm.value.provider, 'local_rules');
  await state.navigate('ai');
  assert.equal(state.showManagement.value, true); assert.equal(state.modelLoaded.value, true);
  assert.equal(state.modelLabel.value, '本地规则统计');
  assert.deepEqual(calls.map(call => call.url), ['/api/ai/config', '/api/ai/config']);
});

test('failed configuration read blocks a query instead of assuming local rules', async () => {
  const calls = [];
  const state = appFixture(async url => { calls.push(url); return response({ detail: 'configuration unavailable' }, 500); });
  await state.navigate('ai'); await state.askAI();
  assert.equal(state.modelLoaded.value, false); assert.deepEqual(calls, ['/api/ai/config']);
  assert.match(state.error.value, /尚未载入模型配置/);
});

test('model save sends numeric parameters, a PIN and draft ETag, and clears key input', async () => {
  const calls = [];
  let stored = config();
  const state = appFixture(async (url, options) => {
    calls.push({ url, options });
    if (options.method === 'POST') { stored = config({ provider: 'ollama', base_url: 'http://127.0.0.1:11434', model: 'test-model', api_key_set: true, revision: 2, deployment: 'local_network' }); return response(stored, 200, '"ai-2"'); }
    return response(stored, 200, stored.revision === 2 ? '"ai-2"' : '"ai-1"');
  });
  state.managerPin.value = 'operator'; await state.navigate('model');
  Object.assign(state.modelForm.value, { provider: 'ollama', base_url: 'http://127.0.0.1:11434', model: 'test-model',
    api_key: 'temporary-test-key', timeout_seconds: '120', max_output_tokens: '2048', temperature: '0.3' });
  await state.saveModel();
  const written = calls.find(call => call.options.method === 'POST');
  assert.equal(written.url, '/api/ai/config'); assert.equal(written.options.headers['X-Operator-Pin'], 'operator');
  assert.equal(written.options.headers['If-Match'], '"ai-1"');
  assert.deepEqual(JSON.parse(written.options.body), { provider: 'ollama', base_url: 'http://127.0.0.1:11434', model: 'test-model',
    timeout_seconds: 120, max_output_tokens: 2048, temperature: 0.3, api_key: 'temporary-test-key', clear_api_key: false });
  assert.equal(state.modelForm.value.api_key, ''); assert.equal(state.modelConfig.value.api_key_set, true);
  assert.equal(state.modelLabel.value, 'Ollama模型 · test-model');
});

test('configuration refresh preserves a model draft and its original version on conflict', async () => {
  const calls = [];
  let revision = 1;
  const state = appFixture(async (url, options) => {
    calls.push({ url, options });
    if (url === '/api/config') return response({ mode: 'simulation' });
    if (url === '/api/tags') return response([]);
    if (options.method === 'POST') return response({ detail: 'stale' }, 412);
    return response(config({ revision }), 200, `"ai-${revision}"`);
  });
  state.managerPin.value = 'operator'; await state.navigate('model');
  Object.assign(state.modelForm.value, { provider: 'openai_compatible', base_url: 'http://127.0.0.1:1234/v1', model: 'draft-model', api_key: 'test-secret' });
  revision = 2; await state.refresh(); await state.saveModel();
  const written = calls.find(call => call.options.method === 'POST');
  assert.equal(written.options.headers['If-Match'], '"ai-1"');
  assert.equal(state.modelForm.value.model, 'draft-model'); assert.equal(state.modelForm.value.api_key, 'test-secret');
  assert.match(state.error.value, /草稿已保留/);
  await state.reloadModel(); assert.equal(state.modelForm.value.provider, 'local_rules');
});

test('candidate test contains no device data or saved mutation and retains its key until save or leaving', async () => {
  for (const status of [200, 502]) {
    const calls = [];
    const state = appFixture(async (url, options) => {
      calls.push({ url, options });
      if (url === '/api/ai/config') return response(config({ api_key_set: true }));
      if (url === '/api/ai/test') return response(status === 200 ? { success: true, message: '测试通过', answer: '测试回答' } : { detail: '模型连接失败' }, status);
      throw new Error('Unexpected request: ' + url);
    });
    state.managerPin.value = 'operator'; await state.navigate('model');
    Object.assign(state.modelForm.value, { provider: 'ollama', base_url: 'http://127.0.0.1:11434', model: 'test-model', api_key: 'test-secret' });
    state.current.value.items = [{ name: '现场真空', value: 0.00005 }];
    await state.testModel();
    assert.deepEqual(calls.map(call => call.url), ['/api/ai/config', '/api/ai/test']);
    const request = calls.at(-1); const body = JSON.parse(request.options.body);
    assert.equal(Object.hasOwn(body, 'items'), false); assert.equal(Object.hasOwn(body, 'device'), false);
    assert.equal(Object.hasOwn(request.options.headers, 'If-Match'), false);
    assert.equal(request.options.headers['X-Operator-Pin'], 'operator');
    assert.equal(state.modelForm.value.api_key, 'test-secret');
    assert.equal(state.modelConfig.value.provider, 'local_rules');
    assert.equal(status === 200 ? state.modelTest.value.success : state.modelTest.value, status === 200 ? true : null);
    await state.navigate('ai'); assert.equal(state.modelForm.value.api_key, '');
    assert.equal(state.modelTest.value, null);
  }
});

test('changing any candidate model parameter invalidates its successful test without clearing the key or calling a model', async () => {
  const calls = [];
  const state = appFixture(async (url, options) => {
    calls.push({ url, options });
    if (url === '/api/ai/config') return response(config({ provider: 'ollama', model: 'working-model' }));
    if (url === '/api/ai/test') return response({ success: true, model: JSON.parse(options.body).model, message: '测试通过' });
    throw new Error('Unexpected request: ' + url);
  });
  state.managerPin.value = 'operator'; await state.navigate('model');
  state.modelForm.value.api_key = 'candidate-secret';
  for (const [field, value] of Object.entries({ provider: 'openai_compatible', base_url: 'http://127.0.0.1:1234/v1',
    model: 'nonexistent-model', timeout_seconds: 120, max_output_tokens: 2048, temperature: 0.5,
    api_key: 'replacement-secret', clear_api_key: true })) {
    await state.testModel();
    assert.equal(state.modelTest.value.success, true);
    const count = calls.length, key = state.modelForm.value.api_key;
    state.modelForm.value[field] = value;
    assert.equal(state.modelTest.value, null, `Changing ${field} must immediately invalidate the old result`);
    assert.equal(calls.length, count, `Changing ${field} must not request inference or save settings`);
    assert.equal(state.modelForm.value.api_key, field === 'api_key' ? value : key,
      'Invalidating a test must preserve the key entered by the user');
  }
});

test('a pending model test cannot attach an old success after its candidate changes and changes back', async () => {
  const calls = [];
  let resolveTest;
  const state = appFixture(async (url, options) => {
    calls.push({ url, options });
    if (url === '/api/ai/config') return response(config({ provider: 'ollama', model: 'working-model' }));
    if (url === '/api/ai/test') return await new Promise(resolve => { resolveTest = resolve; });
    throw new Error('Unexpected request: ' + url);
  });
  state.managerPin.value = 'operator'; await state.navigate('model');
  state.modelForm.value.api_key = 'candidate-secret';
  const pending = state.testModel();
  assert.equal(state.busy.value, true); assert.equal(typeof resolveTest, 'function');
  state.modelForm.value.model = 'different-model'; state.modelForm.value.model = 'working-model';
  resolveTest(response({ success: true, model: 'working-model', message: '测试通过' }));
  await pending;
  assert.equal(state.modelTest.value, null, 'A response for an invalidated candidate must be discarded');
  assert.equal(state.modelForm.value.api_key, 'candidate-secret');
  assert.deepEqual(calls.map(call => call.url), ['/api/ai/config', '/api/ai/test']);
  assert.equal(state.busy.value, false);
  const repeated = state.testModel();
  resolveTest(response({ success: true, model: 'working-model', message: '测试通过' }));
  await repeated;
  assert.equal(state.modelTest.value.success, true, 'Testing the unchanged current candidate can succeed normally');
});

test('model query requires a PIN, permits a slow response, and surfaces failures without stale answers', async () => {
  const calls = [];
  const state = appFixture(async (url, options) => {
    calls.push({ url, options });
    if (url === '/api/ai/config') return response(config({ provider: 'ollama', model: 'test-model', timeout_seconds: 120, deployment: 'local_network' }));
    if (url === '/api/ai/query') return response({ detail: '模型响应超时，请稍后重试' }, 504);
    throw new Error('Unexpected request: ' + url);
  });
  await state.navigate('ai'); await state.askAI();
  assert.deepEqual(calls.map(call => call.url), ['/api/ai/config']); assert.match(state.error.value, /管理口令/);
  state.managerPin.value = 'operator'; state.aiForm.value = { device: '模拟设备', variable: '真空', question: '现在真空是多少' };
  state.aiResult.value = { answer: 'previous answer' }; await state.askAI();
  const request = calls.at(-1); assert.equal(request.url, '/api/ai/query');
  assert.equal(request.options.headers['X-Operator-Pin'], 'operator');
  assert.equal(request.options.headers['If-Match'], '"ai-1"');
  assert.equal(state.aiResult.value, null); assert.match(state.error.value, /模型响应超时/);
});

test('local rule queries remain available without a PIN and model changes clear draft secrets', async () => {
  let request;
  const state = appFixture(async (url, options) => {
    if (url === '/api/ai/config') return response(config());
    request = options; return response({ answer: '规则统计', note: '采集正常不代表设备健康' });
  });
  await state.navigate('ai'); await state.askAI();
  assert.equal(state.aiResult.value.answer, '规则统计'); assert.equal(Object.hasOwn(request.headers, 'X-Operator-Pin'), false);
  assert.equal(request.headers['If-Match'], '"ai-1"');
  state.modelForm.value.api_key = 'draft-key'; state.modelForm.value.clear_api_key = true;
  state.modelForm.value.provider = 'ollama'; state.selectModelProvider();
  assert.equal(state.modelForm.value.base_url, 'http://127.0.0.1:11434'); assert.equal(state.modelForm.value.api_key, '');
  assert.equal(state.modelForm.value.clear_api_key, false);
  state.modelForm.value.provider = 'openai_compatible'; state.selectModelProvider();
  assert.equal(state.modelForm.value.base_url, 'http://127.0.0.1:1234/v1');
});

test('switching back to local rules sends a valid base URL and needs no model or key', async () => {
  let written;
  const state = appFixture(async (url, options) => {
    if (options.method === 'POST') { written = JSON.parse(options.body); return response(config()); }
    return response(config({ provider: 'openai_compatible', base_url: 'http://127.0.0.1:1234/v1', model: 'old-model', api_key_set: true }));
  });
  state.managerPin.value = 'operator'; await state.navigate('model');
  state.modelForm.value.provider = 'local_rules'; state.selectModelProvider(); await state.saveModel();
  assert.equal(written.provider, 'local_rules'); assert.equal(new URL(written.base_url).protocol, 'http:');
  assert.equal(written.api_key, ''); assert.equal(state.modelForm.value.api_key, '');
});
