// 本地固定数据浏览器回归：不启动服务、不联网、不调用真实模型。
// PLAYWRIGHT_MODULE=/path/to/playwright-core node web/dev/test_reading_context.mjs
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const { chromium } = createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = fileURLToPath(new URL('../', import.meta.url)), paper = '2610.27436v1';
const content = JSON.parse(await readFile(path.join(root, `mock/paper-${paper}.json`), 'utf8'));
const samples = JSON.parse(await readFile(path.join(root, `mock/explanations-${paper}.json`), 'utf8')).explanations;
const names = ['线性代数', '量子态', '简并'];
const sourceId = content.blocks.find(b => /^S\d+\.p\d+$/.test(b.id)).id;
const prerequisites = { paper, ready: true, items: names.map(name => ({ name, description: `${name}的简单解释`, why: `本文用${name}理解模型`, where: { ids: [`${sourceId}.s1`], page: null, quote: null } })) };
const initial = { version: 3, concepts: [
  { name: names[0], status: 'known', note: '原来的备注', explained_in: 'old-explanation', where: 'S1.p1', updated: '2026-01-01' },
  { name: '以前的其他概念', status: 'known', note: '需单独核对', explained_in: 'older', where: 'S1.p2', updated: '2026-01-01' },
] };
let record = structuredClone(initial), cached = false, prereqMode = 'normal', explanationMode = 'normal';
let saveMode = 'normal', cancelFails = false, receiptFails = false, pending = null, history = [{ ...samples[0], id: 'history-overview' }];
const requests = [], saves = [], preparations = [], writes = [], tasks = new Map(), browserErrors = [];
let taskNumber = 0;
const browser = await chromium.launch({ executablePath: process.env.CHROME_EXECUTABLE || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true, args: ['--paperread-reading-context-test'] });
console.log(`临时测试浏览器已启动（测试 PID ${process.pid}）。`);
try {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();
  page.on('pageerror', error => browserErrors.push(error.message));
  await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url()), pathname = decodeURIComponent(url.pathname);
    if (url.origin !== 'https://reading.test') return route.abort();
    const json = (data, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(data) });
    if (!pathname.startsWith('/api/')) {
      const filename = path.resolve(root, pathname === '/' ? 'index.html' : pathname.slice(1));
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
    if (pathname.endsWith('/map')) return json({ version: 0, nodes: [], notes: [] });
    if (pathname.endsWith('/record')) {
      if (request.method() === 'GET') return json(record);
      const body = request.postDataJSON(); saves.push(body);
      if (saveMode === 'fail') { saveMode = 'normal'; return json({ error: '记录保存暂时失败' }, 503); }
      if (saveMode === 'conflict') { saveMode = 'normal'; record.version += 1; record.concepts.push({ name: '别处新加的概念', status: 'unsure', note: null, where: null, explained_in: null }); return json({ error: '记录已改变' }, 409); }
      assert.equal(body.version, record.version);
      record = { version: record.version + 1, concepts: body.concepts.map(c => ({ ...record.concepts.find(old => old.name === c.name), ...c })) };
      return json(record);
    }
    if (pathname.endsWith('/prerequisites')) {
      if (request.method() === 'GET') return prereqMode === 'missing' ? json({ error: 'not supported' }, 404) : json(cached ? { ...prerequisites, paper: pathname.split('/')[3] } : { paper, ready: false, items: [] });
      const body = request.postDataJSON(); preparations.push(body); assert.deepEqual(body, {});
      if (prereqMode === 'conflict') return json({ error: '已有任务正在运行' }, 409);
      const task = `prepare-${++taskNumber}`;
      let release;
      const held = ['hold', 'cleanup-cancel'].includes(prereqMode) ? new Promise(resolve => { release = resolve; }) : null;
      tasks.set(task, { kind: 'prepare', mode: prereqMode, result: prerequisites, held, release });
      return json({ task });
    }
    if (pathname.endsWith('/displayed')) {
      writes.push(request.postDataJSON());
      if (receiptFails) { receiptFails = false; return json({ error: 'receipt unavailable' }, 503); }
      return json({ displayed: true });
    }
    if (pathname.endsWith('/explain')) {
      const body = request.postDataJSON(); requests.push(body);
      assert.ok(body.reading_context.trim()); assert.ok(Array.from(body.reading_context).length <= 2000);
      assert.match(body.reading_session, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i);
      assert.equal(body.record_version, record.version);
      const task = `explain-${++taskNumber}`, result = { ...structuredClone(samples[1]), id: `explanation-${requests.length}` };
      if (body.mode === 'ask') result.asked = { question: body.question };
      if (explanationMode === 'ended') result.progress.next = null;
      let release;
      const held = explanationMode === 'hold' ? new Promise(resolve => { release = resolve; }) : null;
      tasks.set(task, { kind: 'explain', mode: explanationMode, result, held, release });
      if (!held) history.push(result);
      return json({ task });
    }
    if (pathname.endsWith('/cancel')) {
      writes.push(request.postDataJSON());
      if (cancelFails) { cancelFails = false; return json({ error: '暂时无法停止，请重试' }, 503); }
      const task = tasks.get(pathname.split('/')[3]);
      if (task.mode === 'cleanup-cancel' && !task.cancelFailed) {
        task.cancelFailed = true; task.release();
        await new Promise(resolve => setTimeout(resolve, 100));
        return json({ error: '模型进程尚未完全停止，请再次停止。' }, 503);
      }
      if (task.kind === 'explain') pending = { question: requests.at(-1).question };
      task.release?.(); return json({ cancelled: true });
    }
    if (pathname.endsWith('/events')) {
      const task = tasks.get(pathname.split('/')[3]);
      if (task.mode === 'disconnect') return route.abort('failed');
      if (task.held) await task.held;
      if (task.kind === 'prepare' && task.mode === 'normal') cached = true;
      const body = task.mode.startsWith('cleanup') ? 'event: error\ndata: 模型进程或连接尚未完全退出，请再次停止。\n\n'
        : ['hold', 'fail'].includes(task.mode) ? 'event: error\ndata: 分析已停止或失败\n\n' : `event: status\ndata: 整理本文基础…\n\nevent: result\ndata: ${JSON.stringify(task.result)}\n\n`;
      return route.fulfill({ status: 200, contentType: 'text/event-stream', body });
    }
    if (/^\/api\/papers\/[^/]+$/.test(pathname)) return json({ paper: pathname.split('/')[3], title: content.title, authors: content.authors, pending });
    return json({ error: `Unexpected request: ${request.method()} ${pathname}` }, 500);
  });
  const prepare = page.locator('[data-context-prepare]'), confirm = page.locator('[data-context-confirm]');
  const input = page.locator('[data-context-input]'), next = page.locator('#continue-btn'), ask = page.locator('#ask-btn');
  const waitIdle = () => page.waitForFunction(() => !document.querySelector('[data-context-confirm]').disabled);
  const choose = async (index, value) => page.locator(`input[name="prerequisite-${index}"][value="${value}"]`).check();
  const save = async () => { await confirm.click(); await waitIdle(); };
  const confirmOld = async () => { await page.getByRole('button', { name: '按这份记录继续', exact: true }).click(); await page.locator('#record-confirm').waitFor({ state: 'hidden' }); };
  const waitExplanation = async count => { await page.locator(`.card[data-id="explanation-${count}"]`).waitFor(); await waitIdle(); };

  await page.goto(`https://reading.test/?paper=${paper}`); await waitIdle();
  assert.equal(await prepare.isVisible(), true); assert.equal(await input.isVisible(), false);
  assert.equal(preparations.length, 0); assert.equal(saves.length, 0); assert.equal(requests.length, 0);
  assert.equal(await next.isDisabled(), true); assert.equal(await page.locator('#record-confirm').isVisible(), false);
  assert.equal(await page.locator('.card[data-id]').count(), 1, 'history readable before preparation');
  await page.locator('#question').fill('保留这个问题'); await page.locator('#question').press('Control+Enter'); await next.dispatchEvent('click');
  assert.equal(requests.length, 0, 'keyboard and synthetic clicks cannot bypass gate');
  for (const [button, dialog] of [['#mode-chip', '#settings-dialog'], ['#library-btn', '#library-dialog'], ['#map-btn', '#map-dialog']]) {
    await page.locator(button).click(); assert.equal(await page.locator(dialog).isVisible(), true); await page.locator(`${dialog} [data-close]`).click();
  }
  await prepare.click(); await page.locator('.prerequisite-item').first().waitFor(); await waitIdle();
  assert.equal(await page.locator('.prerequisite-item').count(), 3); assert.equal(await page.locator('.prerequisite-item input:checked').count(), 0);
  assert.equal(saves.length, 0); assert.equal(record.version, 3); assert.equal(requests.length, 0);
  assert.deepEqual(record, initial, 'preparation never changes knowledge records');
  await page.locator('.prerequisite-item .loc').first().click();
  assert.equal(await page.locator(`#paper [data-id="${sourceId}"].hl`).count(), 1, 'sentence source locates its parent paragraph');
  await save(); assert.match(await page.locator('[data-context-error]').textContent(), /每项/);
  await choose(0, 'known'); await choose(1, 'basics'); await choose(2, 'unsure');
  await input.evaluate(element => { element.value = '字'.repeat(2000); }); await save();
  assert.equal(saves.length, 0, 'oversized combined background is rejected');
  await input.fill('理解主要结论'); await save();
  assert.deepEqual(record.concepts.filter(c => names.includes(c.name)).map(c => c.status), ['known', 'unsure', 'unsure']);
  assert.equal(record.concepts[0].explained_in, 'old-explanation'); assert.match(record.concepts[0].note, /原来的备注/);
  assert.match(record.concepts.find(c => c.name === names[1]).note, /需要补/); assert.match(record.concepts.find(c => c.name === names[2]).note, /不确定/);
  assert.equal(await next.isDisabled(), true, 'unlisted old records require explicit confirmation');
  assert.equal(await page.locator('#record-confirm .record-item').count(), 1, 'gate includes only unlisted old concepts');
  await page.locator('#record-confirm .record-item .record-status').selectOption('unsure'); await confirmOld();
  assert.equal(record.concepts.find(c => c.name === '以前的其他概念').status, 'unsure');
  assert.equal(record.concepts.find(c => c.name === names[0]).status, 'known', 'editing old records preserves new choices');
  assert.equal(requests.length, 0); assert.equal(await next.isEnabled(), true);
  await next.click(); await waitExplanation(1); await ask.click(); await waitExplanation(2);
  assert.equal(requests[0].reading_session, requests[1].reading_session);
  assert.match(requests[0].reading_context, /线性代数：已经会/); assert.match(requests[0].reading_context, /量子态：需要补/);
  assert.equal(requests[1].question, '保留这个问题');
  await page.locator('[data-context-edit]').click(); await input.fill('尚未保存的改动'); await page.locator('[data-context-cancel]').click();
  await page.locator('[data-context-edit]').click(); assert.equal(await input.inputValue(), '理解主要结论');
  await input.fill('先理解必要推导'); await save(); receiptFails = true;
  await next.click(); await page.locator('.receipt-warning').waitFor(); await waitIdle();
  assert.notEqual(requests[2].reading_session, requests[1].reading_session); assert.equal(await next.isDisabled(), true);
  await page.locator('.receipt-warning button').click(); await page.waitForFunction(() => !document.querySelector('#continue-btn').disabled);
  explanationMode = 'hold'; await page.locator('#question').fill('停止后仍要保留'); await ask.click();
  await page.locator('.pending-stop').waitFor(); assert.equal(await page.locator('[data-context-edit]').isDisabled(), true);
  await page.locator('.pending-stop').click(); await page.locator('.card-stopped').waitFor(); await waitIdle();
  assert.ok(await page.locator('[data-pending]').count() > 0);

  await page.reload(); await waitIdle();
  assert.equal(preparations.length, 1, 'cached list does not regenerate');
  assert.equal(await page.locator('.prerequisite-item input:checked').count(), 0, 'reload forgets all choices despite saved records');
  assert.equal(await input.inputValue(), ''); assert.equal(await next.isDisabled(), true);
  await page.setViewportSize({ width: 390, height: 844 });
  const geometry = await page.evaluate(() => ({ cards: document.querySelector('#cards').clientHeight, bottom: document.querySelector('#composer').getBoundingClientRect().bottom, height: innerHeight, width: document.documentElement.scrollWidth }));
  assert.ok(geometry.cards > 40 && geometry.bottom <= geometry.height + 1 && geometry.width <= 390, JSON.stringify(geometry));
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.locator('[data-context-zero]').click(); assert.equal(await next.isDisabled(), true); await save();
  await page.getByRole('button', { name: '清空其余旧记录', exact: true }).click(); await page.waitForFunction(() => !document.querySelector('#continue-btn').disabled);
  assert.deepEqual(record.concepts.map(c => c.name).sort(), [...names].sort(), 'clearing old records preserves freshly chosen prerequisites');
  assert.ok(record.concepts.every(c => c.status === 'unsure' && /需要补/.test(c.note)));
  explanationMode = 'ended'; await next.click(); await waitExplanation(5);
  assert.equal(await next.isDisabled(), true); await page.locator('#question').fill('主线后继续问'); assert.equal(await ask.isEnabled(), true);
  await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }))); await waitIdle();
  assert.equal(await page.locator('.prerequisite-item input:checked').count(), 0); assert.equal(await ask.isDisabled(), true);

  await page.locator('[data-context-zero]').click(); saveMode = 'fail'; await save();
  assert.match(await page.locator('[data-context-error]').textContent(), /保存暂时失败/); assert.equal(await ask.isDisabled(), true);
  saveMode = 'conflict'; const beforeConflict = saves.length; await save();
  assert.equal(saves.length, beforeConflict + 1, '409 never silently retries or overwrites');
  assert.match(await page.locator('[data-context-error]').textContent(), /本次选择没有保存/); assert.equal(await ask.isDisabled(), true);
  await confirmOld(); await save();
  assert.ok(record.concepts.some(c => c.name === '别处新加的概念'), 'retry preserves concurrent unlisted additions');
  const storage = await page.evaluate(() => ({ local: { ...localStorage }, session: { ...sessionStorage } }));
  assert.deepEqual(storage.session, {});
  assert.ok(Object.keys(storage.local).every(key => ['paperread-view', 'paperread-split'].includes(key)), 'only existing layout preferences may persist, never prerequisite choices');
  assert.ok(writes.every(body => !Object.hasOwn(body, 'reading_context') && !Object.hasOwn(body, 'reading_session')));

  cached = false; prereqMode = 'fail'; await page.goto(`https://reading.test/?paper=${paper}`); await waitIdle();
  await prepare.click(); await waitIdle(); assert.equal(await input.isVisible(), false); assert.equal(await next.isDisabled(), true);
  prereqMode = 'conflict'; await prepare.click(); await waitIdle(); assert.match(await page.locator('[data-context-error]').textContent(), /已有任务/);
  prereqMode = 'hold'; const savedBeforeStop = saves.length; await prepare.click(); await page.locator('[data-context-stop]').waitFor();
  await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true })));
  assert.equal(await page.locator('[data-context-stop]').isVisible(), true, 'bfcache keeps in-flight analysis cancellation');
  assert.equal(await prepare.isDisabled(), true);
  await page.locator('[data-context-stop]').click(); await waitIdle(); assert.match(await page.locator('[data-context-error]').textContent(), /停止/);
  assert.equal(saves.length, savedBeforeStop); assert.equal(await input.isVisible(), false);
  prereqMode = 'disconnect'; await prepare.click();
  await page.waitForFunction(() => document.querySelector('[data-context-error]').textContent.includes('分析可能仍在运行'));
  const beforeReconnect = preparations.length;
  assert.equal(await prepare.isDisabled(), true); await prepare.dispatchEvent('click');
  assert.equal(preparations.length, beforeReconnect, 'disconnected analysis cannot spawn duplicate request');
  cancelFails = true; await page.locator('[data-context-stop]').click();
  await page.waitForFunction(() => document.querySelector('[data-context-error]').textContent.includes('暂时无法停止'));
  assert.equal(await page.locator('[data-context-stop]').isEnabled(), true, 'failed cancel preserves task handle');
  await page.locator('[data-context-stop]').click(); await page.waitForFunction(() => !document.querySelector('[data-context-prepare]').disabled);
  prereqMode = 'cleanup'; await prepare.click(); await waitIdle();
  assert.match(await page.locator('[data-context-error]').textContent(), /尚未完全退出/);
  assert.equal(await page.locator('[data-context-stop]').isEnabled(), true); assert.equal(await prepare.isDisabled(), true);
  await page.locator('[data-context-stop]').click(); await page.waitForFunction(() => !document.querySelector('[data-context-prepare]').disabled);
  prereqMode = 'cleanup-cancel'; await prepare.click(); await page.locator('[data-context-stop]').waitFor();
  await page.locator('[data-context-stop]').click();
  await page.waitForFunction(() => document.querySelector('[data-context-error]').textContent.includes('尚未完全停止'));
  assert.equal(await page.locator('[data-context-stop]').isEnabled(), true, 'cleanup error and failed cancel keep the same task despite SSE/HTTP ordering');
  assert.equal(await prepare.isDisabled(), true);
  assert.doesNotMatch(await page.locator('[data-context-error]').textContent(), /已停止分析/);
  await page.locator('[data-context-stop]').click(); await page.waitForFunction(() => !document.querySelector('[data-context-prepare]').disabled);
  prereqMode = 'missing'; await page.reload(); await waitIdle(); assert.match(await page.locator('[data-context-error]').textContent(), /更新后端/);
  assert.equal(await input.isVisible(), false, 'old backend never falls back to blank self-report');
  cached = true; prereqMode = 'normal'; await page.goto('https://reading.test/?paper=another-paper'); await waitIdle();
  assert.equal(await page.locator('.prerequisite-item input:checked').count(), 0); assert.equal(await next.isDisabled(), true);
  await page.goto('https://reading.test/?mock=1'); await page.locator('.card[data-id]').waitFor();
  assert.equal(await page.locator('#reading-context').isVisible(), false);
  assert.deepEqual(browserErrors, []);
  console.log('PASS: prerequisite-first/cache/blank choices, no implicit knowledge, preserved sources/unlisted records, old-record edit/clear, versioned save/409/failure, context/session/receipts/cancel, reopen/mobile/map/settings/library/static mock.');
} finally { await browser.close(); console.log('本次临时测试浏览器已关闭。'); }
