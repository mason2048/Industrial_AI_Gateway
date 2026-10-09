const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function precisionTools() {
  const root = path.join(__dirname, '..');
  const source = fs.readFileSync(path.join(root, 'frontend/app.js'), 'utf8');
  const context = { Vue: { computed: callback => ({ get value() { return callback(); } }) },
    IAGHistory: require('../frontend/history.js') };
  vm.createContext(context);
  // Exercise the exact component registered by the application without starting
  // polling or mocking any of its display implementation.
  vm.runInContext(source.slice(0, source.indexOf('\ncreateApp({')) + '\nthis.tools = { fmt, HistoryChart };', context);
  return context.tools;
}

const start = '2026-10-09T00:00:00Z', end = '2026-10-09T00:01:00Z';
const row = (minimum, maximum) => ({ timestamp: start, quality: 'Good', minimum, maximum,
  first: minimum, last: maximum });

test('configured six decimal readings and small nonzero values remain visible', () => {
  const { fmt } = precisionTools();
  assert.equal(fmt(1.123456, 6), '1.123456');
  assert.equal(fmt(.000001, 6), '0.000001');
  assert.equal(fmt(.0000001, 6), '1.000000e-7');
  assert.equal(fmt(1.123456), '1.12346'); // Legacy defaults stay unchanged.
});

test('narrow six decimal vacuum curve has distinct ticks and space for complete labels', () => {
  const { HistoryChart } = precisionTools();
  const chart = HistoryChart.setup({ items: [row(1.123456, 1.123457)], start, end, precision: 6 }).chart.value;
  const labels = Array.from(chart.ticks, tick => tick.label);
  assert.equal(new Set(labels).size, 4);
  assert.ok(labels.every(label => Math.abs(Number(label) - 1.1234565) < .000001));
  assert.ok(chart.axisLeft > 62);
  assert.ok(Math.max(...labels.map(label => label.length)) * 6.2 <= chart.axisLeft - 8);
  assert.ok(chart.axisLeft <= 180);
  assert.equal(chart.times[0].x, chart.axisLeft);
  assert.equal(chart.times.at(-1).x, 566);
  assert.equal(chart.segments[0][0].x, chart.axisLeft);
});

test('very large values use compact scientific ticks without collapsing a small difference', () => {
  const { HistoryChart } = precisionTools();
  const chart = HistoryChart.setup({ items: [row(1e20, 1e20 + 1e10)], start, end, precision: 6 }).chart.value;
  const labels = Array.from(chart.ticks, tick => tick.label);
  assert.equal(new Set(labels).size, 4);
  assert.ok(labels.every(label => label.includes('e+')));
  assert.ok(labels.every(label => label.length <= 23));
  assert.ok(chart.axisLeft <= 180);
});

test('legacy curve responses without precision and empty curves remain usable', () => {
  const { HistoryChart } = precisionTools();
  const chart = HistoryChart.setup({ items: [row(1, 2)], start, end }).chart.value;
  assert.ok(Array.from(chart.ticks, tick => tick.label).every(label => /^\d\.\d{5}$/.test(label)));
  assert.equal(HistoryChart.setup({ items: [], start, end }).chart.value, null);
});
