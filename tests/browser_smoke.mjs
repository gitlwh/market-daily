// Optional real-browser smoke test. Requires Node 22+ and Chrome with CDP on 9223.
// Start server.py and an isolated headless Chrome before running this file.
import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';

const origin = process.env.MARKET_TEST_URL || 'http://127.0.0.1:8765';
const debugURL = process.env.MARKET_CHROME_URL || 'http://127.0.0.1:9223';
const pages = await (await fetch(`${debugURL}/json/list`)).json();
const page = pages.find((item) => item.type === 'page');
assert(page, 'A Chrome page must be available');
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
let sequence = 0;
const pending = new Map();
const errors = [];
const badResponses = [];
ws.onmessage = (event) => {
  const message = JSON.parse(event.data);
  if (message.id && pending.has(message.id)) {
    const { resolve, reject } = pending.get(message.id);
    pending.delete(message.id);
    message.error ? reject(new Error(JSON.stringify(message.error))) : resolve(message.result);
  }
  if (message.method === 'Runtime.exceptionThrown') errors.push(message.params.exceptionDetails.text);
  if (message.method === 'Network.responseReceived' && message.params.response.status >= 400) {
    badResponses.push(`${message.params.response.status} ${message.params.response.url}`);
  }
};
function send(method, params = {}) {
  return new Promise((resolve, reject) => {
    const id = ++sequence;
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ id, method, params }));
  });
}
async function evaluate(expression) {
  const result = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
  assert(!result.exceptionDetails, JSON.stringify(result.exceptionDetails));
  return result.result.value;
}
async function until(expression) {
  for (let i = 0; i < 150; i++) {
    if (await evaluate(expression)) return;
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  throw new Error(`Timeout: ${expression}`);
}
async function click(selector) {
  await evaluate(`document.querySelector(${JSON.stringify(selector)}).click()`);
}
async function pressKey(key, keyCode) {
  await send('Input.dispatchKeyEvent', { type: 'keyDown', key, code: key, windowsVirtualKeyCode: keyCode });
  await send('Input.dispatchKeyEvent', { type: 'keyUp', key, code: key, windowsVirtualKeyCode: keyCode });
}
async function screenshot(name) {
  const shot = await send('Page.captureScreenshot', { format: 'png' });
  await writeFile(new URL(`../test-results/${name}.png`, import.meta.url), Buffer.from(shot.data, 'base64'));
}

if (process.argv.includes('--close-browser-only')) {
  await send('Browser.close');
  ws.close();
  process.exit(0);
}

try {
  await mkdir(new URL('../test-results/', import.meta.url), { recursive: true });
  await send('Page.enable');
  await send('Runtime.enable');
  await send('Network.enable');
  await send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 1100, deviceScaleFactor: 1, mobile: false });
  await send('Page.navigate', { url: origin });
  await until(`document.querySelector('#main')?.getAttribute('aria-busy') === 'false' && document.querySelectorAll('.heatmap-cell').length === 11`);
  assert.equal(await evaluate(`document.querySelectorAll('.index-card').length`), 4);
  assert.equal(await evaluate(`getComputedStyle(document.querySelector('.sidebar')).position`), 'fixed');
  assert.equal(await evaluate(`document.querySelector('#demo-banner').classList.contains('hidden')`), true);
  assert(await evaluate(`document.documentElement.scrollWidth <= innerWidth`), 'Desktop must fit viewport');
  await screenshot('desktop-live');
  assert(await evaluate(`!!document.querySelector('#earnings')`), 'Weekly earnings section is visible');
  assert(await evaluate(`!!document.querySelector('#sentiment') && document.querySelector('#sentiment-score').textContent !== '—'`), 'Market sentiment score is visible');
  assert.equal(await evaluate(`document.querySelectorAll('.sentiment-component').length`), 6);
  assert(await evaluate(`Boolean(document.querySelector('#sectors').compareDocumentPosition(document.querySelector('#earnings')) & Node.DOCUMENT_POSITION_FOLLOWING) && Boolean(document.querySelector('#earnings').compareDocumentPosition(document.querySelector('#news')) & Node.DOCUMENT_POSITION_FOLLOWING)`), 'Earnings follows sectors and precedes news');
  assert.equal(await evaluate(`document.querySelector('#news-range').value`), '24h');
  const liveReport = await evaluate(`fetch('/api/dashboard').then(r => r.json()).then(d => d.snapshot)`);
  if (liveReport?.recent_news) {
    assert.equal(await evaluate(`Number(document.querySelector('#news-total').textContent)`), liveReport.news_coverage.hours24_count);
    await evaluate(`document.querySelector('#news-range').value='today'; document.querySelector('#news-range').dispatchEvent(new Event('change'))`);
    assert.equal(await evaluate(`Number(document.querySelector('#news-total').textContent)`), liveReport.news.length);
    if (liveReport.news.some(item => item.title_zh && item.title_zh !== item.title)) {
      assert(await evaluate(`!!document.querySelector('.news-original')`), 'Chinese articles keep expandable originals');
      await click('.news-original summary');
      assert(await evaluate(`document.querySelector('.news-original').open`));
    }
    await evaluate(`document.querySelector('#news-range').value='72h'; document.querySelector('#news-range').dispatchEvent(new Event('change'))`);
    assert.equal(await evaluate(`Number(document.querySelector('#news-total').textContent)`), liveReport.recent_news.length);
    await evaluate(`document.querySelector('#news-range').value='24h'; document.querySelector('#news-range').dispatchEvent(new Event('change'))`);
    await evaluate(`document.documentElement.style.scrollBehavior='auto'; document.querySelector('#news').scrollIntoView({behavior:'instant'})`);
    await until(`document.querySelector('#news').getBoundingClientRect().top < 100`);
    await screenshot('news-chinese');
    await evaluate(`scrollTo({top:0,behavior:'instant'})`);
  }

  await click('#demo-mode');
  await until(`document.querySelector('#report-status').textContent === '演示数据' && document.querySelectorAll('.news-item').length === 6 && document.querySelector('#main').getAttribute('aria-busy') === 'false'`);
  assert.equal(await evaluate(`document.querySelector('#export-button').disabled`), true);
  assert.equal(await evaluate(`document.querySelectorAll('.ranking-table tbody tr').length`), 11);
  const demoMood = await evaluate(`fetch('/api/demo').then(r => r.json()).then(d => d.snapshot.sentiment)`);
  assert.equal(await evaluate(`Number(document.querySelector('#sentiment-score').textContent)`), Math.round(demoMood.score));
  assert(await evaluate(`document.querySelector('#sentiment-meta').textContent.includes('100%')`));
  await evaluate(`document.querySelector('#sentiment').scrollIntoView({behavior:'instant'})`);
  await screenshot('sentiment-desktop');
  const demoScreen = await evaluate(`fetch('/api/demo').then(r => r.json()).then(d => d.snapshot.leader_screen)`);
  assert.equal(demoScreen.universe_size, 110);
  assert.equal(await evaluate(`document.querySelectorAll('#leaders-sector-tabs button').length`), 11);
  assert.equal(await evaluate(`document.querySelectorAll('#leaders-strategy-tabs button').length`), 3);
  assert(await evaluate(`document.querySelector('#sectors').compareDocumentPosition(document.querySelector('#leaders')) & Node.DOCUMENT_POSITION_FOLLOWING`), 'Leader screen follows sectors');
  assert(await evaluate(`document.querySelector('#leaders').compareDocumentPosition(document.querySelector('#earnings')) & Node.DOCUMENT_POSITION_FOLLOWING`), 'Leader screen precedes earnings');
  assert.equal(await evaluate(`document.querySelectorAll('.leader-stock').length`), demoScreen.candidate_count);
  assert.equal(await evaluate(`document.querySelectorAll('.leader-stock.leader-candidate').length`), demoScreen.candidate_count);
  await evaluate(`document.querySelector('#leaders-sector').value='XLK'; document.querySelector('#leaders-sector').dispatchEvent(new Event('change'))`);
  assert.equal(await evaluate(`document.querySelectorAll('.leader-stock').length`), 1);
  await evaluate(`document.querySelector('#leaders-view').value='all'; document.querySelector('#leaders-view').dispatchEvent(new Event('change'))`);
  assert.equal(await evaluate(`document.querySelectorAll('.leader-stock').length`), 10);
  assert.equal(await evaluate(`document.querySelectorAll('.leader-check').length`), 40);
  await evaluate(`document.querySelector('#leaders-sector').value='all'; document.querySelector('#leaders-sector').dispatchEvent(new Event('change')); document.querySelector('#leaders-view').value='candidate'; document.querySelector('#leaders-view').dispatchEvent(new Event('change')); document.querySelector('#leaders').scrollIntoView({behavior:'instant'})`);
  await screenshot('leaders-desktop');
  const demoCalendar = await evaluate(`fetch('/api/demo').then(r => r.json()).then(d => d.snapshot.earnings)`);
  const demoCompanies = demoCalendar.companies.filter(item => item.market_cap >= 1e10).sort((a,b) => b.market_cap-a.market_cap).slice(0,15);
  const weekdays = Array.from({ length: 5 }, (_, index) => {
    const date = new Date(`${demoCalendar.week_start}T12:00:00Z`);
    date.setUTCDate(date.getUTCDate() + index);
    return date.toISOString().slice(0,10);
  });
  const tabSelector = date => `#earnings-tabs [role="tab"][data-earnings-day="${date}"]`;
  const selectedDate = () => evaluate(`document.querySelector('#earnings-tabs [aria-selected="true"]').dataset.earningsDay`);
  const easternToday = new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date());
  assert.deepEqual(await evaluate(`Array.from(document.querySelectorAll('#earnings-tabs [role="tab"]'), tab => tab.dataset.earningsDay)`), weekdays);
  assert.equal(await selectedDate(), weekdays.includes(easternToday) ? easternToday : weekdays[0]);
  assert.equal(await evaluate(`document.querySelector('#earnings-panel').getAttribute('role')`), 'tabpanel');
  assert.equal(await evaluate(`document.querySelector('#earnings-day') || document.querySelector('#earnings-sort')`), null);
  assert(await evaluate(`document.querySelector('#earnings-week').textContent.includes(${JSON.stringify(demoCalendar.week_start)})`));
  for (const day of weekdays) {
    await click(tabSelector(day));
    assert.equal(await selectedDate(), day);
    assert.deepEqual(await evaluate(`Array.from(document.querySelectorAll('.earnings-row'), row => row.dataset.symbol)`), demoCompanies.filter(item => item.report_date === day).map(item => item.symbol), `Companies match ${day}, including empty days`);
    assert.equal(await evaluate(`document.querySelectorAll('#earnings-tabs [aria-selected="true"]').length`), 1);
    assert.equal(await evaluate(`document.querySelectorAll('#earnings-tabs [tabindex="0"]').length`), 1);
    assert(await evaluate(`(() => { const tab = document.querySelector('#earnings-tabs [aria-selected="true"]'); const panel = document.querySelector('#earnings-panel'); return tab.tabIndex === 0 && tab.getAttribute('aria-controls') === panel.id && panel.getAttribute('aria-labelledby') === tab.id; })()`), 'Active tab and panel have matching accessibility attributes');
  }
  await evaluate(`document.querySelector(${JSON.stringify(tabSelector(weekdays[4]))}).focus()`);
  for (const [key, code, expectedDay] of [
    ['Home', 36, weekdays[0]],
    ['ArrowRight', 39, weekdays[1]],
    ['ArrowLeft', 37, weekdays[0]],
    ['ArrowLeft', 37, weekdays[4]],
    ['ArrowRight', 39, weekdays[0]],
    ['End', 35, weekdays[4]],
  ]) {
    await pressKey(key, code);
    assert.equal(await selectedDate(), expectedDay, `${key} activates the expected weekday`);
    assert.equal(await evaluate(`document.activeElement.dataset.earningsDay`), expectedDay, `${key} moves keyboard focus`);
  }
  const firstDay = weekdays.find(day => demoCompanies.some(item => item.report_date === day));
  assert(firstDay, 'Demo includes at least one weekday with an earnings company');
  await click(tabSelector(firstDay));
  await evaluate(`document.querySelector('#earnings').scrollIntoView({behavior:'instant'})`);
  assert(await evaluate(`document.querySelector('#earnings-note').textContent.includes('财报当天')`));
  await screenshot('earnings-desktop');
  await evaluate(`scrollTo({top:0,behavior:'instant'})`);
  await screenshot('desktop-demo');
  assert.equal(await evaluate(`Number(document.querySelector('#news-total').textContent)`), 9);
  await evaluate(`document.querySelector('#news-range').value='72h'; document.querySelector('#news-range').dispatchEvent(new Event('change'))`);
  assert.equal(await evaluate(`Number(document.querySelector('#news-total').textContent)`), 10);
  await evaluate(`document.querySelector('#news-range').value='today'; document.querySelector('#news-range').dispatchEvent(new Event('change'))`);
  await click('#show-more-news');
  assert.equal(await evaluate(`document.querySelectorAll('.news-item').length`), 8);
  await evaluate(`document.querySelector('#news-search').value='云服务'; document.querySelector('#news-search').dispatchEvent(new Event('input'))`);
  assert.equal(await evaluate(`document.querySelectorAll('.news-item').length`), 1);
  await evaluate(`document.querySelector('#news-search').value=''; document.querySelector('#news-search').dispatchEvent(new Event('input'))`);
  await click('[data-category="宏观政策"]');
  assert.equal(await evaluate(`document.querySelectorAll('.news-item').length`), 1);
  await click('[data-category="all"]');
  await evaluate(`document.querySelector('#news-sector').value='XLK'; document.querySelector('#news-sector').dispatchEvent(new Event('change'))`);
  assert.equal(await evaluate(`document.querySelectorAll('.news-item').length`), 2);
  await evaluate(`document.querySelector('#news-sector').value='all'; document.querySelector('#news-sector').dispatchEvent(new Event('change'))`);
  await evaluate(`document.querySelector('#sector-sort').value='change_pct'; document.querySelector('#sector-sort').dispatchEvent(new Event('change'))`);
  assert.equal(await evaluate(`document.querySelector('#sector-ranking [data-sector]').dataset.sector`), 'XLK');
  await click('.heatmap-cell[data-sector="XLK"]');
  assert.equal(await evaluate(`document.querySelector('#sector-dialog').open`), true);
  assert.equal(await evaluate(`document.querySelector('#sector-dialog-title').textContent`), '信息技术');
  assert(await evaluate(`document.querySelector('#sector-dialog-content').textContent.includes('1.78')`));
  await send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 });
  await send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 });
  assert.equal(await evaluate(`document.querySelector('#sector-dialog').open`), false);

  await send('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  await evaluate('scrollTo(0,0)');
  await screenshot('mobile-demo');
  assert(await evaluate(`document.documentElement.scrollWidth <= innerWidth`), 'Mobile must fit viewport');
  await evaluate(`document.querySelector('#leaders').scrollIntoView({behavior:'instant'})`);
  await screenshot('leaders-mobile');
  assert(await evaluate(`document.documentElement.scrollWidth <= innerWidth`), 'Leader screen must fit mobile viewport');
  await evaluate(`document.querySelector('#earnings').scrollIntoView({behavior:'instant'})`);
  await screenshot('earnings-mobile');
  await evaluate(`document.querySelector('#news').scrollIntoView({behavior:'instant'})`);
  await screenshot('mobile-news');
  await click('.heatmap-cell[data-sector="XLE"]');
  assert(await evaluate(`document.querySelector('#sector-dialog').getBoundingClientRect().width <= innerWidth`));
  await click('#close-dialog');

  await click('#live-mode');
  await until(`document.querySelector('#main').getAttribute('aria-busy') === 'false' && !document.querySelector('#export-button').disabled`);
  await click('#export-button');
  assert.equal(await evaluate(`document.querySelector('#export-dialog').open`), true);
  const exportResult = await evaluate(`fetch(document.querySelector('#export-markdown').href).then(async r => ({status:r.status, text:await r.text(), disposition:r.headers.get('content-disposition')}))`);
  assert.equal(exportResult.status, 200);
  assert(exportResult.text.startsWith('# 美股市场日报'));
  assert(exportResult.disposition.includes('attachment'));
  await click('#close-export');

  // Simulate a disconnected local API. It must keep the last loaded report without turning on demo.
  await evaluate(`window.savedFetch = window.fetch; window.fetch = () => Promise.reject(new TypeError('offline')); document.dispatchEvent(new Event('visibilitychange'))`);
  await until(`document.querySelector('#notice').textContent.includes('无法连接')`);
  assert.equal(await evaluate(`document.querySelector('#demo-banner').classList.contains('hidden')`), true);
  assert.equal(await evaluate(`document.querySelectorAll('.index-card').length`), 4);
  await evaluate(`window.fetch = window.savedFetch; delete window.savedFetch`);
  assert.deepEqual(errors, [], 'No uncaught browser exceptions');
  assert.deepEqual(badResponses, [], 'No failed resource requests');
  console.log('PASS: combined leader/top-100 screen, sector/status filters, weekday earnings tabs, keyboard navigation, section order, live data, Chinese originals, 24/72-hour windows, 11 sectors, demo isolation, mobile layout, export, offline handling.');
  console.log('Screenshots: test-results/desktop-live.png, desktop-demo.png, mobile-demo.png');
} finally {
  if (process.argv.includes('--close-browser')) await send('Browser.close').catch(() => {});
  ws.close();
}
