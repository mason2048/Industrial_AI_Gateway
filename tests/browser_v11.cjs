/* Optional browser acceptance: uses the bundled Playwright runtime, never production data.
 * PLAYWRIGHT_MODULE=/absolute/path/to/playwright IAG_PYTHON=/path/to/python node tests/browser_v11.cjs
 */
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const net = require('node:net');
const { spawn, execFile } = require('node:child_process');
const { promisify } = require('node:util');
const { once } = require('node:events');
const runFile = promisify(execFile);
const REPO = path.resolve(__dirname, '..');
const PYTHON = process.env.IAG_PYTHON || path.join(REPO, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
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
  throw new Error('No isolated Chromium executable available; set IAG_BROWSER_EXECUTABLE to an installed browser.');
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
const localInput = stamp => {
  const date = new Date(stamp); return new Date(stamp - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
};

async function main() {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'iag-browser-v11-'));
  await fs.cp(path.join(REPO, 'frontend'), path.join(root, 'frontend'), { recursive: true });
  // Seed before opening the service so fixture writes cannot race the collector.
  const seedCode = `
import sys,time
from pathlib import Path
from seed_data import seed
from backend.database import Database
from backend.configuration import ConfigStore
from backend.models import Tag
root=Path(sys.argv[1]); seed(root)
db=Database(root/'data/history.db'); connection_id=ConfigStore(root).snapshot()['connection_id']
original=[Tag.model_validate(row) for row in db.tags()]
flow=Tag(id=9001,address='TEST.WORD',name='产线流量',type='WORD',unit='L/min',device='验收产线',threshold=0,permission='WRITE',node_id='ns=2;s=E2E.Flow')
retired=Tag(id=9002,address='TEST.OLD',name='旧站点压力',type='WORD',unit='Pa',device='已退役设备',node_id='ns=2;s=E2E.Old')
db.replace_tags([*original,flow,retired]); now=time.time()
rows=[(flow.id,now-30000+i*8,i%2000,'Good','simulation',flow.device,flow.name,flow.unit,flow.type,connection_id,1,None,None,None) for i in range(3001)]
rows.append((retired.id,now-10000,400,'Good','simulation',retired.device,retired.name,retired.unit,retired.type,connection_id,1,None,None,None))
db.insert_history(rows); db.replace_tags([*original,flow])
`;
  await runFile(PYTHON, ['-c', seedCode, root], { cwd: REPO, maxBuffer: 1024 * 1024 });
  const port = await freePort(), base = `http://127.0.0.1:${port}`;
  assert.notEqual(port, 8080);
  const server = spawn(PYTHON, ['launch.py', '--root', root, '--demo-init', '--no-browser', '--port', String(port)],
    { cwd: REPO, stdio: ['ignore', 'pipe', 'pipe'] });
  let serverLog = '', serverSpawnError;
  server.stdout.on('data', chunk => { serverLog += chunk.toString(); });
  server.stderr.on('data', chunk => { serverLog += chunk.toString(); });
  server.on('error', error => { serverSpawnError = error; });
  let browser, context, page;
  const checks = [], screenshots = [], pageErrors = [];
  const capture = async name => { const filename = path.join(root, name + '.png'); await page.screenshot({ path: filename, fullPage: true }); screenshots.push(filename); };
  const step = async (name, operation) => { await operation(); checks.push(name); console.log('PASS ' + name); };
  try {
    await eventually(async () => {
      if (serverSpawnError || server.exitCode !== null) throw serverSpawnError || new Error(serverLog);
      const result = await fetch(base + '/api/current').then(response => response.json()); return result.connected && result.good === 7;
    }, 'Isolated gateway did not become ready', 30000);
    const pin = (await fs.readFile(path.join(root, 'data/operator_pin.txt'), 'utf8')).trim();
    browser = await playwright.chromium.launch({ headless: true, executablePath: await browserExecutable() });
    context = await browser.newContext({ viewport: { width: 1440, height: 1080 }, locale: 'zh-CN', timezoneId: 'Asia/Shanghai' });
    context.on('page', opened => opened.on('pageerror', error => pageErrors.push(error.message)));
    page = await context.newPage(); page.setDefaultTimeout(10000);
    await page.goto(base);
    await step('01 actual Vue render, generic device and explicit read-only defaults', async () => {
      await page.getByRole('heading', { level: 1, name: '设备总览' }).waitFor();
      await eventually(() => page.locator('.side-connection').innerText().then(text => text.includes('运行中')), 'Realtime did not render');
      await page.getByLabel('设备', { exact: true }).selectOption('验收产线');
      await page.getByLabel('趋势变量', { exact: true }).selectOption('9001');
      await page.locator('.sensor-list').getByText('产线流量', { exact: true }).waitFor();
      await eventually(() => page.locator('.overview-grid .chart svg').count().then(count => count > 0), 'Generic series did not render');
      await capture('01-generic-overview');
      await selectPage(page, '实时监控');
      assert.equal(await page.getByRole('button', { name: '模拟人工写入', exact: true }).count(), 0);
      assert.match(await page.locator('.content').innerText(), /模拟写入未启用/);
    });
    await step('02 full-range series retains all 3001 saved samples', async () => {
      const seriesResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/api/history/series'
        && new URL(response.url()).searchParams.get('tag_id') === '9001');
      await selectPage(page, '设备总览');
      const data = await (await seriesResponse).json();
      assert.equal(new Date(data.end) - new Date(data.start), 86400000);
      const items = data.series.flatMap(group => group.items);
      assert.ok(items.reduce((sum, item) => sum + item.count, 0) >= 3001);
      assert.ok(items.length <= 240);
      assert.ok(items.some(item => item.maximum === 1999));
      assert.ok(items.some(item => item.minimum === 0));
    });
    await step('03 historical catalogue includes retired devices and paging freezes submitted filters', async () => {
      await selectPage(page, '历史曲线');
      await eventually(() => page.getByLabel('设备', { exact: true }).locator('option').allTextContents().then(names => names.includes('已退役设备')), 'Retired device is missing');
      await page.getByLabel('设备', { exact: true }).selectOption('已退役设备');
      assert.match(await page.getByLabel('变量 / 历史版本', { exact: true }).innerText(), /旧站点压力.*历史\/退役/);
      await page.getByLabel('设备', { exact: true }).selectOption('验收产线');
      const start = localInput(Date.now() - 12 * 3600000), end = localInput(Date.now());
      await page.getByLabel('开始时间', { exact: true }).fill(start);
      await page.getByLabel('结束时间', { exact: true }).fill(end);
      const firstResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/api/history'
        && new URL(response.url()).searchParams.get('tag_id') === '9001' && new URL(response.url()).searchParams.get('offset') === '0');
      await page.getByRole('button', { name: '查询', exact: true }).click();
      const first = await firstResponse; const firstData = await first.json();
      assert.ok(firstData.total >= 3001); assert.equal(firstData.items.length, 2000);
      const firstQuery = new URL(first.url()).searchParams;
      await eventually(() => page.locator('.history-table tbody tr').count().then(count => count === 2000), 'First detail page not rendered');
      await page.getByLabel('开始时间', { exact: true }).fill(localInput(Date.now() - 3600000));
      const nextResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/api/history'
        && new URL(response.url()).searchParams.get('offset') === '2000');
      await page.getByRole('button', { name: '下一页', exact: true }).click();
      const next = await nextResponse, nextData = await next.json(), nextQuery = new URL(next.url()).searchParams;
      for (const key of ['start', 'end', 'source', 'connection_id', 'tag_revision', 'tag_id']) assert.equal(nextQuery.get(key), firstQuery.get(key));
      assert.ok(nextData.items.length >= 1001);
      await capture('03-history-pagination');
    });
    await step('04 two browser pages reject stale point edits and preserve the draft', async () => {
      await selectPage(page, '点位管理'); await page.getByLabel('本机管理口令', { exact: true }).fill(pin);
      const second = await context.newPage(); await second.goto(base); await selectPage(second, '点位管理');
      await second.getByLabel('本机管理口令', { exact: true }).fill(pin);
      for (const target of [page, second]) {
        await target.locator('tbody tr').filter({ hasText: '产线流量' }).getByRole('button', { name: '编辑', exact: true }).click();
      }
      await page.getByLabel('变量名称', { exact: true }).fill('已审核流量');
      await second.getByLabel('变量名称', { exact: true }).fill('保留的未提交草稿');
      const saved = page.waitForResponse(response => new URL(response.url()).pathname === '/api/tags' && response.request().method() === 'PUT');
      await page.getByRole('button', { name: '保存点位', exact: true }).click(); assert.equal((await saved).status(), 200);
      const rejected = second.waitForResponse(response => new URL(response.url()).pathname === '/api/tags' && response.request().method() === 'PUT');
      await second.getByRole('button', { name: '保存点位', exact: true }).click(); assert.equal((await rejected).status(), 412);
      await second.getByRole('dialog').getByText(/配置已被其他页面修改/).waitFor();
      assert.equal(await second.getByLabel('变量名称', { exact: true }).inputValue(), '保留的未提交草稿');
      const tags = await fetch(base + '/api/tags').then(response => response.json());
      assert.equal(tags.find(tag => tag.id === 9001).name, '已审核流量');
      const filename = path.join(root, '04-concurrent-edit-conflict.png'); await second.screenshot({ path: filename, fullPage: true }); screenshots.push(filename);
      await second.close();
    });
    await step('05 Excel preview shows changes before the confirmed versioned apply', async () => {
      const tags = await fetch(base + '/api/tags').then(response => response.json());
      const fixture = path.join(root, 'import-preview.xlsx');
      const code = `import json,sys\nfrom pathlib import Path\nfrom backend.models import Tag\nfrom backend.tag_manager import export_excel\ntags=json.loads(sys.argv[1]); tags[0]['ai_description']='浏览器验收导入更新'; Path(sys.argv[2]).write_bytes(export_excel([Tag.model_validate(row) for row in tags]))`;
      await runFile(PYTHON, ['-c', code, JSON.stringify(tags), fixture], { cwd: REPO });
      const requests = []; const listener = request => { if (new URL(request.url()).pathname === '/api/tags/import') requests.push(request); };
      page.on('request', listener);
      await page.locator('input[type=file]').setInputFiles(fixture);
      await page.getByRole('dialog', { name: '确认导入差异' }).waitFor();
      assert.match(await page.getByRole('dialog').innerText(), /修改 1 项/);
      assert.equal(requests.length, 1); assert.equal(new URL(requests[0].url()).searchParams.get('dry_run'), 'true');
      const before = await fetch(base + '/api/tags').then(response => response.json());
      assert.notEqual(before[0].ai_description, '浏览器验收导入更新');
      const applied = page.waitForResponse(response => new URL(response.url()).pathname === '/api/tags/import'
        && !new URL(response.url()).searchParams.has('dry_run'));
      await page.getByRole('button', { name: '确认应用此差异', exact: true }).click(); assert.equal((await applied).status(), 200);
      assert.match(requests[1].headers()['if-match'], /^"tags-\d+"$/);
      assert.equal((await fetch(base + '/api/tags').then(response => response.json()))[0].ai_description, '浏览器验收导入更新');
      page.off('request', listener);
    });
    await step('06 candidate connection test and node validation leave saved configuration unchanged', async () => {
      const before = await fetch(base + '/api/config').then(response => response.json());
      await selectPage(page, 'PLC连接');
      await page.getByLabel('OPC UA服务器地址', { exact: true }).fill('opc.tcp://127.0.0.1:49321');
      const tested = page.waitForResponse(response => new URL(response.url()).pathname === '/api/connection/test');
      await page.getByRole('button', { name: '测试候选连接', exact: true }).click();
      assert.equal((await (await tested).json()).success, true);
      const validated = page.waitForResponse(response => new URL(response.url()).pathname === '/api/tags/validate');
      await page.getByRole('button', { name: '校验候选点表', exact: true }).click();
      assert.equal((await (await validated).json()).items.length, 7);
      assert.deepEqual(await fetch(base + '/api/config').then(response => response.json()), before);
    });
    await step('07 historical HTTP 500 remains isolated from live collection', async () => {
      await page.route('**/api/history/series?*', route => route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ detail: 'E2E history unavailable' }) }));
      await selectPage(page, '设备总览');
      await page.getByText(/趋势查询失败：E2E history unavailable/).waitFor();
      await eventually(() => page.locator('.side-connection').innerText().then(text => text.includes('运行中')), 'Historical error incorrectly changed realtime status');
      assert.ok(await page.locator('.sensor-list .quality').allTextContents().then(values => values.every(value => value === 'Good')));
      await capture('07-history-fault-isolation'); await page.unroute('**/api/history/series?*');
    });
    await step('08 readiness 503 is displayed alongside independent diagnostics', async () => {
      await page.route('**/api/ready', route => route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ ready: false, reason: 'E2E storage unavailable', collector_alive: true }) }));
      await selectPage(page, '运行诊断');
      await page.getByText(/E2E storage unavailable/).waitFor();
      await eventually(() => page.locator('pre').allTextContents().then(values => values.some(value => value.includes('writer_alive'))), 'Operational diagnostics missing');
      await capture('08-readiness-diagnostics');
    });
    assert.deepEqual(pageErrors, [], 'Unexpected browser JavaScript errors');
    console.log(JSON.stringify({ root, base, checks, screenshots, browserErrors: pageErrors }, null, 2));
  } catch (error) {
    if (page) await capture('failure').catch(() => {});
    console.error(JSON.stringify({ root, base, completed: checks, screenshots, browserErrors: pageErrors, serverLog: serverLog.slice(-8000) }, null, 2));
    throw error;
  } finally {
    if (browser) await browser.close();
    const stopped = await runFile(PYTHON, ['-m', 'scripts.manage', '--root', root, 'stop'], { cwd: REPO, timeout: 40000 });
    console.log('STOP ' + stopped.stdout.trim());
    await eventually(() => server.exitCode !== null, 'Gateway process did not exit after cooperative stop', 5000);
    assert.equal(server.exitCode, 0, serverLog);
  }
}
main().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
