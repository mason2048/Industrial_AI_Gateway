/* Isolated desktop acceptance; uses six fresh simulation points, never field data.
 * PLAYWRIGHT_MODULE=/path/to/playwright IAG_PYTHON=/path/to/python node tests/browser_desktop.cjs
 * IAG_DESKTOP_EXECUTABLE=/path/to/IndustrialAIGateway.exe runs the same flow against a packaged EXE.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const net = require('node:net');
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
  let serverLog = '', spawnError, browser, page, stoppedByUi = false;
  server.stdout.on('data', chunk => { serverLog += chunk.toString(); });
  server.stderr.on('data', chunk => { serverLog += chunk.toString(); });
  server.on('error', error => { spawnError = error; });
  const checks = [], screenshots = [], pageErrors = [], consoleErrors = [], benignConsole = [];
  const capture = async name => {
    const filename = path.join(root, name + '.png');
    await page.screenshot({ path: filename, fullPage: false }); screenshots.push(filename);
  };
  const step = async (name, operation) => { await operation(); checks.push(name); console.log('PASS ' + name); };
  try {
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
      for (const column of ['NodeId', 'AI描述', '保存间隔秒', '小数位数', '记录变化']) {
        const escaped = [...column].map(character => character.charCodeAt(0) > 127
          ? `&#${character.charCodeAt(0)};` : character).join('');
        assert.ok(xml.includes(column) || xml.includes(escaped), 'Missing template column ' + column);
      }
    });
    await step('05 mobile first viewport remains usable and has no document overflow', async () => {
      await selectPage(page, '设备总览');
      await page.setViewportSize({ width: 390, height: 844 });
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.getByRole('heading', { level: 1, name: '设备总览', exact: true }).waitFor();
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      assert.ok(overflow <= 1, 'Unexpected horizontal page overflow: ' + overflow);
      assert.equal(await page.getByRole('button', { name: '退出软件', exact: true }).isVisible(), true);
      await capture('05-mobile-overview');
      await page.setViewportSize({ width: 1440, height: 1080 });
    });
    await step('06 header exit confirmation stops acquisition and the desktop process', async () => {
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
      await capture('06-ui-shutdown');
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
  }
}
main().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
