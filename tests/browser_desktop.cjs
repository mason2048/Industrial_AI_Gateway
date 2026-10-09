/* Isolated desktop acceptance; uses six fresh simulation points, never field data.
 * PLAYWRIGHT_MODULE=/path/to/playwright IAG_PYTHON=/path/to/python node tests/browser_desktop.cjs
 * IAG_DESKTOP_EXECUTABLE=/path/to/IndustrialAIGateway.exe runs the same flow against a packaged EXE.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const net = require('node:net');
const http = require('node:http');
const zlib = require('node:zlib');
const { spawn, execFile } = require('node:child_process');
const { promisify } = require('node:util');
const { once } = require('node:events');
const runFile = promisify(execFile);
const REPO = path.resolve(__dirname, '..');
const EXE = process.env.IAG_DESKTOP_EXECUTABLE;
const EXECUTABLE = EXE || process.env.IAG_PYTHON || path.join(REPO, '.venv',
  process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const PREFIX = EXE ? [] : [path.join(REPO, 'desktop.py')];
const playwright = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

async function freePort() {
  const probe = net.createServer(); probe.listen(0, '127.0.0.1'); await once(probe, 'listening');
  const port = probe.address().port; await new Promise(resolve => probe.close(resolve)); return port;
}
async function browserExecutable() {
  const candidates = [process.env.IAG_BROWSER_EXECUTABLE, playwright.chromium.executablePath(),
    process.platform === 'darwin' ? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' : null].filter(Boolean);
  for (const candidate of candidates) { try { await fs.access(candidate); return candidate; } catch {} }
  throw new Error('No isolated Chromium browser available; set IAG_BROWSER_EXECUTABLE.');
}
async function eventually(check, message, timeout = 12000) {
  const deadline = Date.now() + timeout; let last;
  while (Date.now() < deadline) {
    try { if (await check()) return; } catch (error) { last = error; }
    await wait(100);
  }
  throw new Error(message + (last ? ': ' + last.message : ''));
}
async function selectPage(page, label) {
  await page.locator('nav button').filter({ hasText: label }).click();
  await page.getByRole('heading', { level: 1, name: label, exact: true }).waitFor();
}
function worksheetXml(buffer) {
  // Read the ZIP central directory so the check needs no Python or Excel install.
  let eocd = -1;
  for (let offset = buffer.length - 22; offset >= Math.max(0, buffer.length - 65557); offset--) {
    if (buffer.readUInt32LE(offset) === 0x06054b50) { eocd = offset; break; }
  }
  assert.ok(eocd >= 0, 'Template is not an XLSX ZIP archive');
  let offset = buffer.readUInt32LE(eocd + 16);
  const entries = buffer.readUInt16LE(eocd + 10);
  for (let index = 0; index < entries; index++) {
    assert.equal(buffer.readUInt32LE(offset), 0x02014b50);
    const nameLength = buffer.readUInt16LE(offset + 28), extraLength = buffer.readUInt16LE(offset + 30);
    const commentLength = buffer.readUInt16LE(offset + 32);
    const name = buffer.subarray(offset + 46, offset + 46 + nameLength).toString('utf8');
    if (name === 'xl/worksheets/sheet1.xml') {
      const method = buffer.readUInt16LE(offset + 10), compressedSize = buffer.readUInt32LE(offset + 20);
      const local = buffer.readUInt32LE(offset + 42);
      assert.equal(buffer.readUInt32LE(local), 0x04034b50);
      const start = local + 30 + buffer.readUInt16LE(local + 26) + buffer.readUInt16LE(local + 28);
      const bytes = buffer.subarray(start, start + compressedSize);
      return (method === 8 ? zlib.inflateRawSync(bytes) : bytes).toString('utf8');
    }
    offset += 46 + nameLength + extraLength + commentLength;
  }
  throw new Error('Template workbook contains no first worksheet');
}

async function mockModelServer() {
  const calls = [];
  const server = http.createServer(async (request, response) => {
    try {
      const chunks = [];
      for await (const chunk of request) chunks.push(chunk);
      const body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
      const last = body.messages?.at(-1)?.content || '';
      let evidence;
      try { evidence = JSON.parse(last).read_only_evidence; } catch {}
      calls.push({ path: request.url, body, evidence, authorization: request.headers.authorization });
      const answer = evidence ? '浏览器验收模型回答：已读取本次只读证据，未执行PLC操作。' : '浏览器验收模型连接成功。';
      response.writeHead(200, { 'Content-Type': 'application/json' });
      if (request.url === '/api/chat') response.end(JSON.stringify({ message: { role: 'assistant', content: answer }, done: true }));
      else if (request.url === '/v1/chat/completions') response.end(JSON.stringify({ choices: [{ message: { role: 'assistant', content: answer }, finish_reason: 'stop' }] }));
      else { response.statusCode = 404; response.end(JSON.stringify({ error: 'Unexpected mock endpoint' })); }
    } catch (error) {
      response.writeHead(400, { 'Content-Type': 'application/json' }); response.end(JSON.stringify({ error: error.message }));
    }
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  return { server, calls, base: `http://127.0.0.1:${server.address().port}` };
}

async function main() {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'iag-desktop-browser-'));
  const common = [...PREFIX, '--root', root];
  const checked = await runFile(EXECUTABLE, [...common, '--check'], { cwd: REPO, timeout: 60000 });
  assert.equal(checked.stderr.trim(), '');
  const check = JSON.parse(await fs.readFile(path.join(root, 'check-result.json'), 'utf8'));
  assert.equal(check.ok, true); assert.equal(check.mode, 'simulation'); assert.equal(check.database_exists, false);
  if (EXE) assert.equal(check.standalone, true);
  const port = await freePort(), base = `http://127.0.0.1:${port}`;
  const server = spawn(EXECUTABLE, [...common, '--no-browser', '--port', String(port)],
    { cwd: REPO, stdio: ['ignore', 'pipe', 'pipe'] });
  let serverLog = '', spawnError, browser, page, mock, stoppedByUi = false;
  server.stdout.on('data', chunk => { serverLog += chunk.toString(); });
  server.stderr.on('data', chunk => { serverLog += chunk.toString(); });
  server.on('error', error => { spawnError = error; });
  const checks = [], screenshots = [], pageErrors = [], consoleErrors = [], benignConsole = [];
  const capture = async (name, { fullPage = false } = {}) => {
    const filename = path.join(root, name + '.png');
    await page.screenshot({ path: filename, fullPage }); screenshots.push(filename);
  };
  const step = async (name, operation) => { await operation(); checks.push(name); console.log('PASS ' + name); };
  try {
    mock = await mockModelServer();
    await eventually(async () => {
      if (spawnError || server.exitCode !== null) throw spawnError || new Error(serverLog);
      const response = await fetch(base + '/api/current');
      const current = await response.json();
      return current.connected && current.good === 6 && current.items.length === 6;
    }, 'Desktop did not collect six Good simulation points', 60000);
    browser = await playwright.chromium.launch({ headless: true, executablePath: await browserExecutable() });
    const context = await browser.newContext({ viewport: { width: 1440, height: 1080 },
      locale: 'zh-CN', timezoneId: 'Asia/Shanghai', acceptDownloads: true });
    page = await context.newPage(); page.setDefaultTimeout(12000);
    page.on('pageerror', error => pageErrors.push(error.message));
    page.on('console', entry => {
      if (entry.type() !== 'error') return;
      if (entry.location().url.endsWith('/favicon.ico')) benignConsole.push('Unconfigured browser favicon');
      else if (entry.text().startsWith('Failed to load resource:') && entry.text().includes('412')) benignConsole.push('Expected model version conflict');
      else consoleErrors.push(entry.text());
    });
    await page.goto(base);
    await step('01 page identity, six simulated points, and desktop first viewport', async () => {
      assert.equal(page.url(), base + '/');
      assert.equal(await page.title(), 'Industrial AI Gateway · 工业数据网关');
      await page.getByRole('heading', { level: 1, name: '设备总览', exact: true }).waitFor();
      await page.getByText('模拟数据模式', { exact: true }).waitFor();
      await eventually(() => page.locator('.side-connection').innerText().then(text => text.includes('运行中')),
        'Live collection state not rendered');
      assert.match(await page.locator('header .mode-pill').innerText(), /模拟 PLC/);
      assert.match(await page.locator('.metrics').innerText(), /6\s*\/\s*6/);
      assert.equal(await page.locator('vite-error-overlay, nextjs-portal, #webpack-dev-server-client-overlay').count(), 0);
      await capture('01-desktop-overview');
    });
    await step('02 real-time table shows six Good values and read-only controls', async () => {
      await selectPage(page, '实时监控');
      const rows = page.locator('.content table tbody tr');
      assert.equal(await rows.count(), 6);
      assert.ok((await rows.allTextContents()).every(text => text.includes('模拟') && text.includes('Good') && text.includes('只读')));
      assert.equal(await page.getByRole('button', { name: '模拟人工写入', exact: true }).count(), 0);
    });
    await step('03 local manager PIN gates exit; connection polling settings persist', async () => {
      await page.getByRole('button', { name: '退出软件', exact: true }).click();
      await page.getByRole('alert').getByText(/退出需要本机管理口令/).waitFor();
      assert.equal(server.exitCode, null);
      await page.getByRole('button', { name: '关闭错误', exact: true }).click();
      await selectPage(page, 'PLC连接');
      const pin = (await fs.readFile(path.join(root, 'data/operator_pin.txt'), 'utf8')).trim();
      await page.getByLabel('本机管理口令', { exact: true }).fill(pin);
      assert.equal(await page.getByLabel('OPC UA服务器地址', { exact: true }).inputValue(), 'opc.tcp://127.0.0.1:4840');
      await page.getByLabel('读取周期（秒）', { exact: true }).fill('0.5');
      await page.getByLabel('每批读取点数', { exact: true }).fill('3');
      assert.equal(await page.getByLabel('默认最大补存间隔（秒）', { exact: true }).inputValue(), '1800');
      assert.equal(await page.getByLabel('历史保留天数（最多7天）', { exact: true }).inputValue(), '7');
      const saved = page.waitForResponse(response => new URL(response.url()).pathname === '/api/connection'
        && response.request().method() === 'POST');
      await page.getByRole('button', { name: '保存并连接', exact: true }).click();
      const response = await saved; assert.equal(response.status(), 200);
      const config = await response.json(); assert.equal(config.poll_interval, .5); assert.equal(config.batch_size, 3);
      await page.getByRole('status').getByText(/配置已保存/).waitFor();
      const persisted = JSON.parse(await fs.readFile(path.join(root, 'config/config.json'), 'utf8'));
      assert.equal(persisted.poll_interval, .5); assert.equal(persisted.batch_size, 3);
      await capture('03-connection-settings');
    });
    await step('04 Excel template downloads with header only and requested policy columns', async () => {
      await selectPage(page, '点位管理');
      const downloaded = page.waitForEvent('download');
      await page.getByRole('link', { name: '↓ 下载Excel模板', exact: true }).click();
      const download = await downloaded;
      assert.equal(download.suggestedFilename(), 'plc-tags-template.xlsx');
      const file = path.join(root, 'downloaded-template.xlsx'); await download.saveAs(file);
      const xml = worksheetXml(await fs.readFile(file));
      assert.equal((xml.match(/<row\b/g) || []).length, 1, 'Template must contain no field or demo point rows');
      for (const column of ['NodeId', 'AI描述', '保存间隔秒', '小数位数', '记录变化', 'AI取数方式', 'AI取数间隔秒']) {
        const escaped = [...column].map(character => character.charCodeAt(0) > 127
          ? `&#${character.charCodeAt(0)};` : character).join('');
        assert.ok(xml.includes(column) || xml.includes(escaped), 'Missing template column ' + column);
      }
    });
    await step('05 deleted point IDs are skipped automatically while visible row numbers stay continuous', async () => {
      const pointDialog = () => page.getByRole('dialog', { name: '编辑点位' });
      async function saveDraft(id, name, address) {
        const dialog = pointDialog(), identity = dialog.getByLabel(/点位ID/);
        await eventually(() => identity.inputValue().then(value => Number(value) === id), 'New point ID was not allocated');
        assert.ok(await identity.isDisabled() || await identity.getAttribute('readonly') !== null, 'Point ID must be automatic');
        await dialog.getByLabel('变量名称', { exact: true }).fill(name);
        await dialog.getByLabel('PLC原始地址（用于识别）', { exact: true }).fill(address);
        const saved = page.waitForResponse(response => new URL(response.url()).pathname === '/api/tags'
          && response.request().method() === 'PUT');
        await dialog.getByRole('button', { name: '保存点位', exact: true }).click();
        assert.equal((await saved).status(), 200);
        await dialog.waitFor({ state: 'hidden' });
        await page.locator('table tbody tr').filter({ hasText: name }).waitFor();
      }
      for (const id of [7, 8, 9]) {
        await page.getByRole('button', { name: '＋ 新增点位', exact: true }).click();
        await saveDraft(id, `删除回归点${id}`, `REGRESSION.OLD.${id}`);
      }
      await eventually(async () => {
        const catalog = await fetch(base + '/api/history/variables?source=simulation').then(response => response.json());
        return [8, 9].every(id => catalog.items.some(row => row.tag_id === id));
      }, 'Points to be deleted have no persisted history');
      async function deletePoint(id) {
        const row = page.locator('table tbody tr').filter({ hasText: `删除回归点${id}` });
        page.once('dialog', dialog => dialog.accept());
        const deleted = page.waitForResponse(response => new URL(response.url()).pathname === '/api/tags'
          && response.request().method() === 'PUT');
        await row.getByRole('button', { name: '删除', exact: true }).click();
        assert.equal((await deleted).status(), 200);
        await row.waitFor({ state: 'hidden' });
      }
      await deletePoint(8); await deletePoint(9);
      await page.getByRole('button', { name: '＋ 新增点位', exact: true }).click();
      await saveDraft(10, '删除后新增温度', 'REGRESSION.NEW.10');
      const rows = page.locator('table tbody tr');
      assert.equal(await page.locator('table thead th').first().innerText(), '序号');
      assert.deepEqual(await rows.locator('td:first-child').allTextContents(), ['1', '2', '3', '4', '5', '6', '7', '8']);
      assert.deepEqual((await fetch(base + '/api/tags').then(response => response.json())).map(tag => tag.id), [1, 2, 3, 4, 5, 6, 7, 10]);
      await page.locator('.table-wrap').evaluate(element => { element.scrollLeft = 0; });
      await page.evaluate(() => window.scrollTo(0, 0));
      await capture('05-delete-and-add-continuous-rows', { fullPage: true });
      await rows.filter({ hasText: '删除后新增温度' }).getByRole('button', { name: '编辑', exact: true }).click();
      await pointDialog().getByRole('button', { name: '复制为新点位以更换地址', exact: true }).click();
      await saveDraft(11, '删除后复制温度', 'REGRESSION.COPY.11');
      assert.deepEqual(await rows.locator('td:first-child').allTextContents(), ['1', '2', '3', '4', '5', '6', '7', '8', '9']);
      const catalog = await fetch(base + '/api/history/variables?source=simulation').then(response => response.json());
      for (const id of [8, 9]) {
        assert.ok(catalog.items.some(row => row.tag_id === id && row.name === `删除回归点${id}` && !row.active), 'Deleted history identity lost');
      }
      const search = page.getByRole('textbox', { name: '搜索点位', exact: true });
      await search.fill('删除后新增温度');
      assert.equal(await rows.count(), 1); assert.equal(await rows.locator('td').first().innerText(), '1');
      await search.fill('');
      await page.reload(); await selectPage(page, '点位管理');
      await rows.filter({ hasText: '删除后复制温度' }).waitFor();
      assert.deepEqual(await rows.locator('td:first-child').allTextContents(), ['1', '2', '3', '4', '5', '6', '7', '8', '9']);
      const pin = (await fs.readFile(path.join(root, 'data/operator_pin.txt'), 'utf8')).trim();
      await page.getByLabel('本机管理口令', { exact: true }).fill(pin);
    });
    await step('06 mobile first viewport remains usable and has no document overflow', async () => {
      await selectPage(page, '设备总览');
      await page.setViewportSize({ width: 390, height: 844 });
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.getByRole('heading', { level: 1, name: '设备总览', exact: true }).waitFor();
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      assert.ok(overflow <= 1, 'Unexpected horizontal page overflow: ' + overflow);
      assert.equal(await page.getByRole('button', { name: '退出软件', exact: true }).isVisible(), true);
      await capture('06-mobile-overview');
      await page.setViewportSize({ width: 1440, height: 1080 });
    });
    await step('07 candidate model test sends no industrial data and saving remains idle', async () => {
      await selectPage(page, '模型设置');
      const before = await fetch(base + '/api/ai/config').then(response => response.json());
      assert.equal(before.provider, 'local_rules');
      await page.getByLabel('回答方式', { exact: true }).selectOption('ollama');
      await page.getByLabel('模型服务地址', { exact: true }).fill(mock.base);
      await page.getByLabel('模型名称', { exact: true }).fill('qa-ollama');
      await page.getByText('高级参数（超时、回答长度、随机程度）', { exact: true }).click();
      await page.getByLabel('等待回答的最长时间（秒）', { exact: true }).fill('10');
      await page.getByLabel('回答长度上限（Token）', { exact: true }).fill('128');
      await page.getByLabel('等待回答的最长时间（秒）', { exact: true }).fill('4');
      await page.getByText('高级参数（超时、回答长度、随机程度）', { exact: true }).click();
      assert.equal(await page.locator('.model-advanced').evaluate(element => element.open), false);
      await page.getByRole('button', { name: '保存模型设置', exact: true }).click();
      assert.equal(await page.locator('.model-advanced').evaluate(element => element.open), true,
        'Invalid hidden advanced inputs must expand their section');
      assert.equal(await page.getByLabel('等待回答的最长时间（秒）', { exact: true }).evaluate(element => document.activeElement === element), true);
      assert.deepEqual(await fetch(base + '/api/ai/config').then(response => response.json()), before);
      await page.getByLabel('等待回答的最长时间（秒）', { exact: true }).fill('10');
      await wait(350); assert.equal(mock.calls.length, 0, 'Opening model settings must not trigger model inference');
      const tested = page.waitForResponse(response => new URL(response.url()).pathname === '/api/ai/test');
      await page.getByRole('button', { name: '测试候选模型', exact: true }).click();
      assert.equal((await tested).status(), 200);
      await page.getByText(/测试不会保存候选设置/).waitFor();
      assert.equal(mock.calls.length, 1); assert.equal(mock.calls[0].path, '/api/chat');
      assert.equal(mock.calls[0].evidence, undefined);
      assert.equal(mock.calls[0].body.messages.some(message => message.content.includes('read_only_evidence')), false);
      assert.deepEqual(await fetch(base + '/api/ai/config').then(response => response.json()), before);
      await page.getByLabel('模型名称', { exact: true }).fill('changed-after-test');
      await page.getByText(/测试不会保存候选设置/).waitFor({ state: 'hidden' });
      assert.equal(mock.calls.length, 1, 'Changing a tested candidate must invalidate its result without calling the model');
      await page.getByLabel('模型名称', { exact: true }).fill('qa-ollama');
      const saved = page.waitForResponse(response => new URL(response.url()).pathname === '/api/ai/config' && response.request().method() === 'POST');
      await page.getByRole('button', { name: '保存模型设置', exact: true }).click();
      const applied = await saved; assert.equal(applied.status(), 200);
      const stored = await applied.json(); assert.equal(stored.provider, 'ollama'); assert.equal(stored.model, 'qa-ollama');
      await page.getByRole('status').getByText(/模型设置已保存/).waitFor();
      await wait(350); assert.equal(mock.calls.length, 1, 'Saving configuration must not trigger inference');
      await capture('07-ollama-model-settings', { fullPage: true });
    });
    await step('08 explicit model query includes PIN, configuration version and read-only current evidence', async () => {
      await selectPage(page, '按需数据查询');
      await page.locator('.content > .notice').filter({ hasText: /当前：Ollama模型 · qa-ollama/ }).waitFor();
      assert.notEqual(await page.getByLabel('本机管理口令', { exact: true }).inputValue(), '');
      const variables = page.getByLabel('分析变量', { exact: true });
      await variables.selectOption({ label: '模拟真空压力' });
      await page.getByLabel('输入问题', { exact: true }).fill('现在模拟真空压力是多少？说明数据质量');
      const submitted = page.waitForResponse(response => new URL(response.url()).pathname === '/api/ai/query');
      await page.getByRole('button', { name: '提交查询 ↗', exact: true }).click();
      const answer = await submitted; assert.equal(answer.status(), 200);
      const headers = answer.request().headers(); assert.ok(headers['x-operator-pin']); assert.match(headers['if-match'], /^"ai-\d+"$/);
      const data = await answer.json(); assert.equal(data.provider, 'ollama'); assert.equal(data.query_type, 'current');
      assert.equal(data.plc_write_allowed, false); assert.ok(data.evidence_count > 0);
      const modelCall = mock.calls.at(-1); assert.equal(modelCall.path, '/api/chat');
      assert.equal(modelCall.evidence.plc_write_allowed, false); assert.equal(modelCall.evidence.query_type, 'current');
      assert.equal(modelCall.evidence.current_items.length, 1); assert.equal(modelCall.evidence.history_items.length, 0);
      assert.ok(modelCall.evidence.current_items[0].quality); assert.ok(modelCall.evidence.current_items[0].timestamp);
      assert.deepEqual(data.evidence, modelCall.evidence, 'Displayed query evidence must match the actual model input');
      await page.locator('.ai-answer > p').filter({ hasText: /浏览器验收模型回答/ }).waitFor();
      await capture('08-ollama-read-only-answer', { fullPage: true });
      const count = mock.calls.length; await wait(350); assert.equal(mock.calls.length, count, 'Answer rendering must not restart model calls');
    });
    await step('09 OpenAI-compatible model tests, saves and queries without exposing its key', async () => {
      await selectPage(page, '模型设置');
      await page.getByLabel('回答方式', { exact: true }).selectOption('openai_compatible');
      await page.getByLabel('模型服务地址', { exact: true }).fill(mock.base + '/v1');
      await page.getByLabel('模型名称', { exact: true }).fill('qa-compatible');
      const key = page.getByLabel('API Key（可选）', { exact: true });
      assert.equal(await key.getAttribute('type'), 'password'); await key.fill('mock-acceptance-key');
      const tested = page.waitForResponse(response => new URL(response.url()).pathname === '/api/ai/test');
      await page.getByRole('button', { name: '测试候选模型', exact: true }).click();
      assert.equal((await tested).status(), 200);
      assert.equal(mock.calls.at(-1).path, '/v1/chat/completions'); assert.equal(mock.calls.at(-1).evidence, undefined);
      assert.equal(mock.calls.at(-1).authorization, 'Bearer mock-acceptance-key');
      assert.equal(await key.inputValue(), 'mock-acceptance-key', 'Candidate test must preserve an entered key for the following save');
      const count = mock.calls.length;
      const saved = page.waitForResponse(response => new URL(response.url()).pathname === '/api/ai/config' && response.request().method() === 'POST');
      await page.getByRole('button', { name: '保存模型设置', exact: true }).click();
      const applied = await saved; assert.equal(applied.status(), 200);
      const config = await applied.json(); assert.equal(config.api_key_set, true); assert.equal(Object.hasOwn(config, 'api_key'), false);
      await eventually(() => key.inputValue().then(value => value === ''), 'Saved key input was not cleared');
      assert.equal(mock.calls.length, count, 'Saving a compatible model must not call it');
      const publiclyReadable = await fetch(base + '/api/ai/config').then(response => response.text());
      assert.equal(publiclyReadable.includes('mock-acceptance-key'), false);
      await capture('09-compatible-model-settings', { fullPage: true });
      await selectPage(page, '按需数据查询');
      await page.locator('.content > .notice').filter({ hasText: /当前：OpenAI兼容模型 · qa-compatible/ }).waitFor();
      const submitted = page.waitForResponse(response => new URL(response.url()).pathname === '/api/ai/query');
      await page.getByRole('button', { name: '提交查询 ↗', exact: true }).click();
      const answer = await submitted; assert.equal(answer.status(), 200); assert.equal((await answer.json()).provider, 'openai_compatible');
      assert.equal(mock.calls.at(-1).authorization, 'Bearer mock-acceptance-key');
      assert.ok(mock.calls.at(-1).evidence.current_items.length > 0);
      await page.locator('.ai-answer > p').filter({ hasText: /浏览器验收模型回答/ }).waitFor();
    });
    await step('10 stale model settings preserve the user draft and can reload current configuration', async () => {
      await selectPage(page, '模型设置');
      await page.getByLabel('模型名称', { exact: true }).fill('unsaved-model-draft');
      const latest = await fetch(base + '/api/ai/config'); const config = await latest.json();
      const pin = (await fs.readFile(path.join(root, 'data/operator_pin.txt'), 'utf8')).trim();
      const changedElsewhere = await fetch(base + '/api/ai/config', { method: 'POST', headers: {
        'Content-Type': 'application/json', 'X-Operator-Pin': pin, 'If-Match': latest.headers.get('etag') },
        body: JSON.stringify({ model: 'modified-other-page' }) });
      assert.equal(changedElsewhere.status, 200);
      const saved = page.waitForResponse(response => new URL(response.url()).pathname === '/api/ai/config' && response.request().method() === 'POST');
      await page.getByRole('button', { name: '保存模型设置', exact: true }).click();
      assert.equal((await saved).status(), 412);
      await page.getByRole('alert').getByText(/模型草稿已保留/).waitFor();
      assert.equal(await page.getByLabel('模型名称', { exact: true }).inputValue(), 'unsaved-model-draft');
      assert.equal((await fetch(base + '/api/ai/config').then(response => response.json())).model, 'modified-other-page');
      await page.getByRole('button', { name: '放弃草稿，载入已保存配置', exact: true }).click();
      await eventually(() => page.getByLabel('模型名称', { exact: true }).inputValue().then(value => value === 'modified-other-page'), 'Reload did not show current model configuration');
      assert.equal(await page.getByLabel('API Key（可选）', { exact: true }).inputValue(), '');
      assert.equal(config.api_key_set, true);
    });
    await step('11 point AI policies are visible, persist independently, and apply only when querying history', async () => {
      const count = mock.calls.length;
      await selectPage(page, '点位管理');
      const tags = await fetch(base + '/api/tags').then(response => response.json());
      const tag = tags.find(item => item.id === 1);
      const row = () => page.locator('table tbody tr').filter({ hasText: tag.name });
      const dialog = () => page.getByRole('dialog', { name: '编辑点位' });
      const policy = () => dialog().getByLabel('AI历史取数方式', { exact: true });
      const interval = () => dialog().getByLabel('AI取样间隔（秒，留空继承保存间隔）', { exact: true });
      await row().getByRole('button', { name: '编辑', exact: true }).click();
      await policy().selectOption('interval');
      await interval().fill('1');
      await dialog().getByLabel('定时保存间隔（秒，留空继承全局）', { exact: true }).fill('0.2');
      await dialog().getByLabel('显示小数位数（0–10位）', { exact: true }).fill('6');
      await dialog().getByLabel('变化保存阈值（绝对差值）', { exact: true }).fill('0.000001');
      await dialog().getByLabel('记录变化事件', { exact: true }).selectOption('false');
      await capture('11-point-ai-policy-desktop');
      let applied = page.waitForResponse(response => new URL(response.url()).pathname === '/api/tags'
        && response.request().method() === 'PUT');
      await dialog().getByRole('button', { name: '保存点位', exact: true }).click();
      assert.equal((await applied).status(), 200);
      await dialog().waitFor({ state: 'hidden' });
      let stored = await fetch(base + '/api/tags').then(response => response.json()).then(items => items.find(item => item.id === tag.id));
      assert.equal(stored.ai_history_mode, 'interval'); assert.equal(stored.ai_history_interval_seconds, 1);
      assert.equal(stored.history_interval_seconds, .2); assert.equal(stored.record_changes, false);
      assert.equal(stored.precision, 6); assert.equal(stored.threshold, .000001);
      const configuredRevision = stored.revision;
      assert.equal(mock.calls.length, count, 'Editing point policies must not call a model');
      await page.reload(); await selectPage(page, '点位管理'); await row().waitFor();
      const pin = (await fs.readFile(path.join(root, 'data/operator_pin.txt'), 'utf8')).trim();
      await page.getByLabel('本机管理口令', { exact: true }).fill(pin);
      await row().getByRole('button', { name: '编辑', exact: true }).click();
      assert.equal(await policy().inputValue(), 'interval'); assert.equal(await interval().inputValue(), '1');
      assert.equal(await dialog().getByLabel('显示小数位数（0–10位）', { exact: true }).inputValue(), '6');
      await page.setViewportSize({ width: 390, height: 844 });
      await policy().scrollIntoViewIfNeeded();
      assert.equal(await policy().isVisible(), true); assert.equal(await interval().isVisible(), true);
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth) <= 1);
      await capture('11-point-ai-policy-mobile');
      await page.setViewportSize({ width: 1440, height: 1080 });
      await dialog().getByRole('button', { name: '复制为新点位以更换地址', exact: true }).click();
      assert.equal(await policy().inputValue(), 'interval'); assert.equal(await interval().inputValue(), '1');
      await dialog().getByRole('button', { name: '关闭编辑', exact: true }).click();
      const filters = new URLSearchParams({ source: 'simulation', variable: tag.name, device: tag.device });
      await eventually(async () => {
        const raw = await fetch(base + '/api/ai/history?' + filters + '&changed_only=false').then(response => response.json());
        return raw.items.filter(item => item.tag_revision === stored.revision).length >= 5;
      }, 'Point with a short local save interval has insufficient history');
      const selected = await fetch(base + '/api/ai/history?' + filters).then(response => response.json());
      assert.equal(selected.filter.point_settings, true);
      assert.equal(selected.filter.point_policies.find(item => item.tag_id === tag.id).ai_history_mode, 'interval');
      assert.ok(selected.items.every(item => item.ai_history_mode === 'interval'));
      assert.equal(mock.calls.length, count, 'Reading policies or history must not run inference');
      await selectPage(page, '按需数据查询');
      await page.getByLabel('设备', { exact: true }).selectOption(tag.device);
      await page.getByLabel('分析变量', { exact: true }).selectOption(tag.name);
      await page.getByLabel('输入问题', { exact: true }).fill('最近一周' + tag.name + '的数据有哪些变化？');
      let submitted = page.waitForResponse(response => new URL(response.url()).pathname === '/api/ai/query');
      await page.getByRole('button', { name: '提交查询 ↗', exact: true }).click();
      let answer = await submitted; assert.equal(answer.status(), 200);
      let evidence = (await answer.json()).evidence;
      assert.equal(mock.calls.length, count + 1);
      assert.ok(evidence.history_items.length > 0 && evidence.history_items.every(item => item.ai_history_mode === 'interval'));
      assert.deepEqual(evidence, mock.calls.at(-1).evidence);
      await selectPage(page, '点位管理'); await row().getByRole('button', { name: '编辑', exact: true }).click();
      await policy().selectOption('changes');
      applied = page.waitForResponse(response => new URL(response.url()).pathname === '/api/tags' && response.request().method() === 'PUT');
      await dialog().getByRole('button', { name: '保存点位', exact: true }).click();
      assert.equal((await applied).status(), 200); await dialog().waitFor({ state: 'hidden' });
      assert.equal(mock.calls.length, count + 1, 'Switching back to changes must remain idle');
      await selectPage(page, '按需数据查询');
      submitted = page.waitForResponse(response => new URL(response.url()).pathname === '/api/ai/query');
      await page.getByRole('button', { name: '提交查询 ↗', exact: true }).click();
      answer = await submitted; assert.equal(answer.status(), 200); evidence = (await answer.json()).evidence;
      assert.equal(mock.calls.length, count + 2);
      assert.ok(evidence.history_items.length > 0 && evidence.history_items.every(item => item.ai_history_mode === 'changes'));
      assert.deepEqual(evidence, mock.calls.at(-1).evidence);
      await selectPage(page, '点位管理'); await row().getByRole('button', { name: '编辑', exact: true }).click();
      await dialog().getByLabel('保存历史', { exact: true }).selectOption('false');
      assert.equal(await policy().isEnabled(), true);
      await policy().selectOption('interval'); await interval().fill('1');
      assert.equal(await interval().isEnabled(), true);
      await dialog().getByText(/当前值和此前保存的历史仍可查询/).waitFor();
      applied = page.waitForResponse(response => new URL(response.url()).pathname === '/api/tags' && response.request().method() === 'PUT');
      await dialog().getByRole('button', { name: '保存点位', exact: true }).click();
      assert.equal((await applied).status(), 200); await dialog().waitFor({ state: 'hidden' });
      assert.equal(mock.calls.length, count + 2, 'Stopping new history and changing its query policy must remain idle');
      stored = await fetch(base + '/api/tags').then(response => response.json()).then(items => items.find(item => item.id === tag.id));
      assert.equal(stored.save, false); assert.equal(stored.ai_history_mode, 'interval');
      await selectPage(page, '按需数据查询');
      submitted = page.waitForResponse(response => new URL(response.url()).pathname === '/api/ai/query');
      await page.getByRole('button', { name: '提交查询 ↗', exact: true }).click();
      answer = await submitted; assert.equal(answer.status(), 200); evidence = (await answer.json()).evidence;
      assert.equal(mock.calls.length, count + 3);
      assert.ok(evidence.history_items.length > 0, 'Previously stored history must remain readable after stopping new saves');
      assert.ok(evidence.history_items.every(item => item.ai_history_mode === 'interval'));
      assert.deepEqual(evidence, mock.calls.at(-1).evidence);
      assert.equal(evidence.current_items.find(item => item.id === tag.id).precision, 6);
      await selectPage(page, '实时监控');
      const reading = page.locator('table tbody tr').filter({ hasText: tag.name }).locator('.reading');
      assert.match(await reading.innerText(), /^-?\d+\.\d{6}$/);
      await selectPage(page, '历史曲线');
      const catalog = await fetch(base + '/api/history/variables?source=simulation').then(response => response.json());
      const definition = catalog.items.find(item => item.tag_id === tag.id && item.tag_revision === configuredRevision);
      assert.ok(definition, 'Six-decimal historical definition must remain available');
      await page.getByLabel('设备', { exact: true }).selectOption(definition.device);
      await page.getByLabel('变量 / 历史版本', { exact: true }).selectOption(JSON.stringify([
        definition.source, definition.connection_id, definition.tag_id, definition.tag_revision,
        definition.device, definition.name, definition.unit]));
      // The browser uses Asia/Shanghai on both macOS and Windows; construct its
      // local datetime rather than using the CI host timezone or minute floor.
      await page.getByLabel('结束时间', { exact: true }).fill(await page.evaluate(() => {
        const date = new Date(Date.now() + 120000);
        return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
      }));
      const plotted = page.waitForResponse(response => new URL(response.url()).pathname === '/api/history/series'
        && new URL(response.url()).searchParams.get('tag_revision') === String(configuredRevision));
      await page.getByRole('button', { name: '查询', exact: true }).click();
      const curve = await plotted; assert.equal(curve.status(), 200);
      const groups = (await curve.json()).series; assert.ok(groups.length > 0 && groups.every(group => group.precision === 6));
      await page.locator('.chart svg').waitFor();
      const labels = (await page.locator('.chart svg text').allTextContents()).slice(1, 5);
      assert.equal(new Set(labels).size, 4);
      assert.ok(labels.some(label => /^-?\d+\.\d{6,}$/.test(label)), 'Six-decimal chart precision must reach the rendered axis');
      assert.equal(mock.calls.length, count + 3, 'Realtime and history displays must not call a model');
      await capture('11-six-decimal-history');
    });
    await step('12 local rules restore without model calls and PLC clear options are explicit', async () => {
      await selectPage(page, '模型设置');
      const count = mock.calls.length;
      await page.getByLabel('回答方式', { exact: true }).selectOption('local_rules');
      const saved = page.waitForResponse(response => new URL(response.url()).pathname === '/api/ai/config' && response.request().method() === 'POST');
      await page.getByRole('button', { name: '保存模型设置', exact: true }).click();
      assert.equal((await saved).status(), 200); assert.equal(mock.calls.length, count);
      await selectPage(page, '按需数据查询'); await page.locator('.content > .notice').filter({ hasText: /当前：本地规则统计/ }).waitFor();
      await page.getByRole('button', { name: '清除口令', exact: true }).click();
      const submitted = page.waitForResponse(response => new URL(response.url()).pathname === '/api/ai/query');
      await page.getByRole('button', { name: '提交查询 ↗', exact: true }).click();
      assert.equal((await submitted).status(), 200); assert.equal(mock.calls.length, count);
      await selectPage(page, 'PLC连接');
      const pin = (await fs.readFile(path.join(root, 'data/operator_pin.txt'), 'utf8')).trim();
      await page.getByLabel('本机管理口令', { exact: true }).fill(pin);
      await page.getByText('证书与账号配置（空白保留现有设置）', { exact: true }).click();
      await page.getByLabel('清除已有证书配置', { exact: true }).check();
      await page.getByLabel('清除已有用户名，使用匿名连接', { exact: true }).check();
      assert.equal(await page.getByLabel('用户名', { exact: true }).isDisabled(), true);
      assert.equal(await page.getByLabel('安全策略与证书路径', { exact: true }).isDisabled(), true);
      const tested = page.waitForResponse(response => new URL(response.url()).pathname === '/api/connection/test');
      await page.getByRole('button', { name: '测试候选连接', exact: true }).click();
      const candidate = await tested; assert.equal(candidate.status(), 200);
      const body = candidate.request().postDataJSON(); assert.equal(body.username, ''); assert.equal(body.security_string, '');
      assert.equal(mock.calls.length, count);
    });
    await step('13 header exit confirmation stops acquisition and the desktop process', async () => {
      await selectPage(page, 'PLC连接');
      assert.notEqual(await page.getByLabel('本机管理口令', { exact: true }).inputValue(), '');
      const shutdown = page.waitForResponse(response => new URL(response.url()).pathname === '/api/shutdown');
      let confirmed = false;
      page.once('dialog', async dialog => {
        assert.equal(dialog.type(), 'confirm'); assert.match(dialog.message(), /停止采集并退出软件/);
        confirmed = true; await dialog.accept();
      });
      await page.getByRole('button', { name: '退出软件', exact: true }).click();
      assert.equal((await shutdown).status(), 202); assert.equal(confirmed, true);
      await page.getByRole('button', { name: '正在退出', exact: true }).waitFor();
      assert.equal(await page.getByRole('button', { name: '正在退出', exact: true }).isDisabled(), true);
      await page.getByRole('status').getByText(/重新使用时请双击/).waitFor();
      assert.equal(await page.getByLabel('本机管理口令', { exact: true }).inputValue(), '');
      await eventually(() => server.exitCode !== null, 'UI shutdown did not stop the desktop process', 40000);
      assert.equal(server.exitCode, 0, 'Desktop exit failed: ' + serverLog);
      stoppedByUi = true;
      await assert.rejects(fetch(base + '/api/health'), 'HTTP listener remains active after process exit');
      await assert.rejects(fs.access(path.join(root, 'data/gateway.pid')));
      await assert.rejects(fs.access(path.join(root, 'data/desktop-instance.json')));
      await capture('13-ui-shutdown');
    });
    assert.deepEqual(pageErrors, [], 'Unexpected browser JavaScript errors');
    assert.deepEqual(consoleErrors, [], 'Unexpected application console errors');
    console.log(JSON.stringify({ root, base, standalone: Boolean(EXE), checks, screenshots,
      browserErrors: pageErrors, consoleErrors, benignConsole, stoppedByUi }, null, 2));
  } catch (error) {
    if (page) await capture('failure').catch(() => {});
    console.error(JSON.stringify({ root, base, completed: checks, screenshots, browserErrors: pageErrors,
      consoleErrors, serverLog: serverLog.slice(-4000) }, null, 2));
    throw error;
  } finally {
    if (browser) await browser.close();
    if (server.exitCode === null && !spawnError) {
      await runFile(EXECUTABLE, [...common, '--stop'], { cwd: REPO, timeout: 45000 });
      await eventually(() => server.exitCode !== null, 'Test cleanup could not stop desktop process', 5000);
      assert.equal(server.exitCode, 0, serverLog);
    }
    if (mock) { mock.server.closeAllConnections(); await new Promise(resolve => mock.server.close(resolve)); }
  }
}
main().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
