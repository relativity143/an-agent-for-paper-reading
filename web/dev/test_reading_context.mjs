// 无后端浏览器回归：所有请求在浏览器里截获，不联网、不启动服务、不调用模型。
// PLAYWRIGHT_MODULE=/path/to/playwright-core node web/dev/test_reading_context.mjs
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = fileURLToPath(new URL('../', import.meta.url));
const paper = '2610.27436v1';
const content = JSON.parse(await readFile(path.join(root, `mock/paper-${paper}.json`), 'utf8'));
const samples = JSON.parse(await readFile(path.join(root, `mock/explanations-${paper}.json`), 'utf8')).explanations;
const overview = { ...samples[0], id: 'history-overview' };
const requests = [], otherWrites = [], browserErrors = [];
const tasks = new Map();
let history = [overview], pending = null, resultMode = 'normal', failedReceipt = false;
const browser = await chromium.launch({
  executablePath: process.env.CHROME_EXECUTABLE || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  headless: true,
  args: ['--paperread-reading-context-test'],
});
console.log(`已启动本次临时浏览器（测试进程 PID ${process.pid}）；所有页面请求都使用本地固定数据。`);
try {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();
  page.on('pageerror', error => browserErrors.push(error.message));
  await context.route('https://reading.test/**', async route => {
    const request = route.request(), url = new URL(request.url()), pathname = decodeURIComponent(url.pathname);
    const json = data => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(data) });
    if (!pathname.startsWith('/api/')) {
      const relative = pathname === '/' ? 'index.html' : pathname.slice(1);
      const filename = path.resolve(root, relative);
      if (!filename.startsWith(root)) return route.abort();
      try {
        const types = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json', '.woff2': 'font/woff2' };
        return route.fulfill({ status: 200, contentType: types[path.extname(filename)] || 'application/octet-stream', body: await readFile(filename) });
      } catch { return route.fulfill({ status: 404, body: 'not found' }); }
    }
    if (pathname === '/api/health') return json({ backend: 'codex' });
    if (pathname === '/api/settings') return json({ backend: 'codex', backends: [{ name: 'codex', available: true, model: 'test-model' }], timeout_seconds: 120 });
    if (pathname === '/api/papers') return json({ papers: [{ paper, title: content.title, authors: content.authors }] });
    if (pathname.endsWith('/content')) return json(content);
    if (pathname.endsWith('/explanations')) return json(history);
    if (pathname.endsWith('/displayed')) {
      otherWrites.push(request.postDataJSON());
      if (failedReceipt) { failedReceipt = false; return route.fulfill({ status: 503, contentType: 'application/json', body: '{"error":"receipt unavailable"}' }); }
      return json({ displayed: true });
    }
    if (pathname.endsWith('/explain')) {
      const body = request.postDataJSON();
      assert.ok(typeof body.reading_context === 'string' && body.reading_context.trim());
      assert.match(body.reading_session, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i);
      requests.push(body);
      const id = `task-${requests.length}`;
      const result = structuredClone(samples[1]);
      result.id = `explanation-${requests.length}`;
      if (body.mode === 'ask') result.asked = { question: body.question };
      if (resultMode === 'ended') result.progress.next = null;
      let release;
      const held = resultMode === 'hold' ? new Promise(resolve => { release = resolve; }) : null;
      tasks.set(id, { mode: resultMode, result, held, release });
      if (resultMode !== 'hold') history.push(result);
      return json({ task: id });
    }
    if (pathname.endsWith('/cancel')) {
      otherWrites.push(request.postDataJSON());
      const task = tasks.get(pathname.split('/')[3]);
      pending = { question: requests.at(-1).question };
      task.release();
      return json({ cancelled: true });
    }
    if (pathname.endsWith('/events')) {
      const task = tasks.get(pathname.split('/')[3]);
      if (task.held) await task.held;
      const body = task.mode === 'hold' ? 'event: error\ndata: 已停止\n\n' : `event: result\ndata: ${JSON.stringify(task.result)}\n\n`;
      return route.fulfill({ status: 200, contentType: 'text/event-stream', body });
    }
    if (/^\/api\/papers\/[^/]+$/.test(pathname)) return json({ paper: pathname.split('/')[3], title: content.title, authors: content.authors, pending });
    throw new Error(`Unexpected request: ${request.method()} ${pathname}`);
  });
  const input = page.locator('[data-context-input]'), confirm = page.locator('[data-context-confirm]');
  const next = page.locator('#continue-btn'), ask = page.locator('#ask-btn');
  const waitIdle = () => page.waitForFunction(() => !document.querySelector('[data-context-confirm]').disabled);
  const waitRequests = count => page.waitForFunction(count => document.querySelectorAll('.card[data-id]').length >= count + 1, count);

  await page.goto(`https://reading.test/?paper=${paper}`);
  await waitIdle();
  await page.setViewportSize({ width: 390, height: 844 });
  const narrowLayout = await page.evaluate(() => ({
    historyHeight: document.querySelector('#cards').clientHeight,
    composerBottom: document.querySelector('#composer').getBoundingClientRect().bottom,
    viewport: window.innerHeight,
  }));
  assert.ok(narrowLayout.historyHeight > 40, `history stays reachable on narrow screens: ${JSON.stringify(narrowLayout)}`);
  assert.ok(narrowLayout.composerBottom <= narrowLayout.viewport + 1, `composer remains on screen: ${JSON.stringify(narrowLayout)}`);
  await page.setViewportSize({ width: 1280, height: 900 });
  assert.equal(await input.inputValue(), '');
  assert.equal(await next.isDisabled(), true);
  assert.equal(await page.locator('.card[data-id]').count(), 1, 'history remains readable before confirmation');
  await page.locator('#question').fill('先保留这个问题');
  await page.locator('#question').press('Control+Enter');
  await next.dispatchEvent('click');
  assert.equal(requests.length, 0, 'keyboard and synthetic clicks cannot bypass confirmation');
  assert.equal(await page.locator('#question').inputValue(), '先保留这个问题');
  await page.locator('#mode-chip').click();
  assert.equal(await page.locator('#settings-dialog').isVisible(), true);
  await page.locator('#settings-dialog [data-close]').click();
  await page.locator('#library-btn').click();
  assert.equal(await page.locator('#library-dialog').isVisible(), true);
  await page.locator('#library-dialog [data-close]').click();

  await input.fill('  ');
  await confirm.click();
  assert.equal(await next.isDisabled(), true);
  assert.ok(await page.locator('[data-context-error]').textContent());
  await input.evaluate(element => { element.value = '字'.repeat(2001); });
  await confirm.click();
  assert.equal(await next.isDisabled(), true);
  const firstContext = '学过线性代数；量子力学不熟悉。\n想看懂主要结论。';
  await input.fill(`  ${firstContext}  `);
  await confirm.click();
  assert.equal(requests.length, 0, 'confirmation itself never calls a model');
  assert.equal(await next.isEnabled(), true);
  assert.equal(await input.isVisible(), false);
  await next.click();
  await waitRequests(1);
  await waitIdle();
  assert.equal(requests[0].reading_context, firstContext);
  await ask.click();
  await waitRequests(2);
  await waitIdle();
  assert.equal(requests[1].reading_context, firstContext, 'subsequent requests reuse this page confirmation');
  assert.equal(requests[1].question, '先保留这个问题');
  assert.equal(requests[1].reading_session, requests[0].reading_session, 'same confirmation uses one session');

  await page.locator('[data-context-edit]').click();
  assert.equal(await next.isDisabled(), true);
  await input.fill('修改后只想补必要数学基础');
  await page.locator('[data-context-cancel]').click();
  await page.locator('[data-context-edit]').click();
  assert.equal(await input.inputValue(), firstContext, 'cancel keeps the previously confirmed background');
  const revised = '还没有学过量子力学，先补必要基础。';
  await input.fill(revised);
  await confirm.click();
  failedReceipt = true;
  await next.click();
  await page.locator('.receipt-warning').waitFor();
  await waitIdle();
  assert.equal(requests[2].reading_context, revised);
  assert.notEqual(requests[2].reading_session, requests[1].reading_session, 'confirming edited background starts a fresh session');
  assert.equal(await next.isDisabled(), true, 'display receipt continues to block advancement');
  await page.locator('.receipt-warning button').click();
  await page.waitForFunction(() => !document.querySelector('#continue-btn').disabled);

  resultMode = 'hold';
  await page.locator('#question').fill('停止后留着的问题');
  await ask.click();
  await page.locator('.pending-stop').waitFor();
  assert.equal(await page.locator('[data-context-edit]').isDisabled(), true);
  await page.locator('.pending-stop').click();
  await page.locator('.card-stopped').waitFor();
  assert.ok(await page.locator('[data-pending]').count(), 'cancelled question remains visible');
  await waitIdle();

  await page.reload();
  await waitIdle();
  assert.equal(await input.inputValue(), '', 'reload forgets background');
  assert.equal(await next.isDisabled(), true);
  assert.ok(await page.locator('.card[data-id]').count() > 1, 'reload still restores history');
  assert.ok(await page.locator('[data-pending]').count(), 'reload still restores pending question');
  await page.locator('[data-context-zero]').click();
  resultMode = 'ended';
  await next.click();
  await waitRequests(4);
  await waitIdle();
  assert.match(requests.at(-1).reading_context, /从零开始/);
  assert.notEqual(requests.at(-1).reading_session, requests[2].reading_session, 'reopening starts a fresh session');
  assert.equal(await next.isDisabled(), true, 'finished main route remains disabled');
  await page.locator('#question').fill('主线结束后还能问吗');
  assert.equal(await ask.isEnabled(), true);
  await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true })));
  assert.equal(await input.inputValue(), '', 'back-forward cache restoration asks again');
  assert.equal(await ask.isDisabled(), true);

  await page.goto('https://reading.test/?paper=another-paper');
  await waitIdle();
  assert.equal(await input.inputValue(), '');
  assert.equal(await ask.isDisabled(), true, 'another paper needs its own confirmation');
  const storage = await page.evaluate(() => ({ local: { ...localStorage }, session: { ...sessionStorage } }));
  assert.deepEqual(storage, { local: {}, session: {} });
  assert.ok(otherWrites.every(body => !Object.hasOwn(body, 'reading_context') && !Object.hasOwn(body, 'reading_session')), 'background and session are only sent to explain');

  await page.goto('https://reading.test/?mock=1');
  await page.locator('.card[data-id]').waitFor();
  assert.equal(await page.locator('#reading-context').isVisible(), false, 'static samples need no confirmation');
  assert.deepEqual(browserErrors, []);
  console.log('PASS: blank/length validation, keyboard gate, history/settings/library, context/session reuse/edit/cancel, receipt retry, stop/pending, refresh/new paper/bfcache, end-of-route, storage isolation, static mock.');
} finally {
  await browser.close();
  console.log('本次临时浏览器已关闭。');
}
