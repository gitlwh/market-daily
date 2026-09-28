// Verify the same frontend falls back from the local API to generated static JSON.
import assert from 'node:assert/strict';

const origin = process.env.MARKET_STATIC_URL || 'http://127.0.0.1:8766';
const pages = await (await fetch('http://127.0.0.1:9223/json/list')).json();
const page = pages.find(item => item.type === 'page');
assert(page, 'A Chrome page must be available');
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
let sequence = 0;
const pending = new Map();
ws.onmessage = event => {
  const message = JSON.parse(event.data);
  if (message.id && pending.has(message.id)) {
    const task = pending.get(message.id);
    pending.delete(message.id);
    message.error ? task.reject(new Error(JSON.stringify(message.error))) : task.resolve(message.result);
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
  for (let index = 0; index < 100; index++) {
    if (await evaluate(expression)) return;
    await new Promise(resolve => setTimeout(resolve, 200));
  }
  throw new Error(`Timeout: ${expression}`);
}

try {
  await send('Page.enable');
  await send('Runtime.enable');
  await send('Page.navigate', { url: origin });
  await until(`document.querySelector('#main')?.getAttribute('aria-busy') === 'false' && document.querySelector('#sentiment-score')?.textContent !== '—'`);
  assert.equal(await evaluate(`document.querySelector('#refresh-button span').textContent`), '读取云端日报');
  assert.equal(await evaluate(`document.querySelectorAll('.sentiment-component').length`), 6);
  assert.equal(await evaluate(`document.querySelectorAll('#leaders-sector-tabs button').length`), 12);
  assert.equal(await evaluate(`document.querySelectorAll('#leaders-strategy-tabs button').length`), 3);
  await evaluate(`document.querySelector('[data-leader-strategy="value_momentum"]').click()`);
  await until(`document.querySelector('[data-leader-strategy="value_momentum"]').classList.contains('selected')`);
  assert.equal(await evaluate(`document.querySelectorAll('#leaders-list .leader-stock').length`), await evaluate(`Number(document.querySelector('#leaders-count').textContent)`));
  assert(await evaluate(`document.querySelector('#leaders-rules').textContent.includes('市盈率')`));
  await evaluate(`document.querySelector('[data-leader-strategy="three_week_rise"]').click()`);
  assert(await evaluate(`document.querySelector('#leaders-rules').textContent.includes('完整自然周')`));
  assert.equal(await evaluate(`document.querySelector('#report-date').options.length`), 1);
  await evaluate(`document.querySelector('#export-button').click()`);
  assert((await evaluate(`document.querySelector('#export-json').getAttribute('href')`)).startsWith('data/reports/'));
  await evaluate(`document.querySelector('#close-export').click(); document.querySelector('#demo-mode').click()`);
  await until(`document.querySelector('#report-status').textContent === '演示数据' && Number(document.querySelector('#sentiment-score').textContent) === 64`);
  assert.equal(await evaluate(`Number(document.querySelector('#sentiment-score').textContent)`), 64);
  console.log('PASS: static JSON fallback, sentiment, leader screen, history selector, demo and static exports.');
} finally {
  ws.close();
}
