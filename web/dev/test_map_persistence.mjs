// Offline browser regression with the real Application, HTTP routes and Store.
// PLAYWRIGHT_MODULE=/path/to/playwright-core node web/dev/test_map_persistence.mjs
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';

const { chromium } = createRequire(import.meta.url)(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = fileURLToPath(new URL('../../', import.meta.url));
const papers = ['2601.00001v1', '2601.00002v1'];
const home = await mkdtemp(path.join(tmpdir(), 'paperread-map-browser-'));
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
const servers = new Set(), serverStats = [], browserErrors = [];
let browser;

async function startServer(port = 0) {
  const child = spawn(process.env.PYTHON || 'python3', [
    '-u', path.join(root, 'web/dev/map_fixture_server.py'), '--home', home, '--port', String(port),
  ], { cwd: root, stdio: ['pipe', 'pipe', 'pipe'], env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' } });
  const resource = { child, errors: '', stopped: null };
  servers.add(resource);
  child.stderr.on('data', data => { resource.errors = (resource.errors + data).slice(-2000); });
  resource.closed = new Promise(resolve => child.once('close', (code, signal) => resolve({ code, signal })));
  const lines = createInterface({ input: child.stdout });
  resource.ready = new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`Fixture startup timed out: ${resource.errors}`)), 10000);
    child.once('error', error => { clearTimeout(timer); reject(error); });
    child.once('close', code => { clearTimeout(timer); reject(new Error(`Fixture exited ${code}: ${resource.errors}`)); });
    lines.on('line', line => {
      const value = JSON.parse(line);
      if (value.ready) { clearTimeout(timer); resolve(value); }
      if (value.stopped) resource.stopped = value;
    });
  });
  const ready = await resource.ready;
  resource.port = ready.port;
  resource.origin = `http://127.0.0.1:${ready.port}`;
  return resource;
}

async function stopServer(resource) {
  if (!servers.has(resource)) return;
  resource.child.stdin.end('stop\n');
  let result = await Promise.race([resource.closed, wait(5000).then(() => null)]);
  if (!result) {
    resource.child.kill('SIGTERM');
    result = await resource.closed;
  }
  servers.delete(resource);
  assert.equal(result.code, 0, `Fixture shutdown: ${JSON.stringify(result)} ${resource.errors}`);
  assert.deepEqual(resource.stopped, { stopped: true, model_calls: 0, network_calls: 0, tasks: 0 });
  serverStats.push(resource.stopped);
}

async function until(check, label, timeout = 6000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const value = await check();
    if (value) return value;
    await wait(40);
  }
  throw new Error(`Timed out: ${label}`);
}

try {
  let server = await startServer();
  const origin = server.origin;
  const recordFiles = await Promise.all(papers.map(paper => readFile(path.join(home, 'papers', paper, 'agent-state.json'), 'utf8')));
  browser = await chromium.launch({
    executablePath: process.env.CHROME_EXECUTABLE || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    headless: true, args: ['--paperread-map-persistence-test'],
  });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  context.setDefaultTimeout(6000);
  const page = await context.newPage();
  page.on('pageerror', error => browserErrors.push(error.message));
  page.on('dialog', dialog => dialog.accept());
  const attempts = [], responses = [], forbidden = [];
  let getFault = null, putFault = null, holdGet = null, holdNext = null, activePuts = 0, maxActivePuts = 0;
  await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url());
    if (url.origin !== origin) return route.abort();
    if (/\/api\/.*\/(explain|feedback|displayed)$/.test(url.pathname)
        || (url.pathname.endsWith('/record') && request.method() !== 'GET')) {
      forbidden.push(`${request.method()} ${url.pathname}`);
    }
    if (!url.pathname.endsWith('/map')) return route.continue();
    if (request.method() === 'GET') {
      if (holdGet) { const held = holdGet; holdGet = null; held.started(); await held.release; }
      if (getFault === 'disconnect') return route.abort('failed');
      if (getFault === 'null' || getFault === 'empty-object') return route.fulfill({
        status: 200, contentType: 'application/json', body: getFault === 'null' ? 'null' : '{}',
      });
      if (getFault !== null) return route.fulfill({ status: getFault, contentType: 'application/json', body: JSON.stringify({ error: `Fixture read failure ${getFault}` }) });
      return route.continue();
    }
    const body = request.postDataJSON();
    attempts.push({ paper: url.pathname.split('/')[3], body });
    activePuts += 1;
    maxActivePuts = Math.max(maxActivePuts, activePuts);
    try {
      if (holdNext) { const held = holdNext; holdNext = null; held.started(); await held.release; }
      if (putFault !== null) {
        const fault = putFault; putFault = null;
        if (fault === 'disconnect') return await route.abort('failed');
        return await route.fulfill({ status: fault, contentType: 'application/json', body: JSON.stringify({ error: `Fixture save failure ${fault}` }) });
      }
      const response = await route.fetch();
      responses.push({ status: response.status(), body: await response.json() });
      await route.fulfill({ response });
    } finally { activePuts -= 1; }
  });
  const requestJson = async (paper, method = 'GET', body = null) => {
    const response = await fetch(`${origin}/api/papers/${paper}/map`, {
      method, ...(body ? { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {}),
    });
    assert.equal(response.status, 200);
    return response.json();
  };
  const localSnapshot = () => page.evaluate(() => ({ ...localStorage }));
  const alpha = () => page.locator('.map-node[aria-label^="Alpha，"]');
  const position = () => alpha().evaluate(node => ({ x: Math.round(parseFloat(node.style.left)), y: Math.round(parseFloat(node.style.top)) }));
  const waitEditable = () => page.waitForFunction(() => !document.querySelector('#map-add-note').disabled);
  const waitSaved = () => page.waitForFunction(() => /已保存|已存/.test(document.querySelector('#map-status').textContent));
  const open = async (paper = papers[0]) => {
    await page.goto(`${origin}/?paper=${paper}`);
    await page.locator('#map-btn').waitFor({ state: 'visible' });
    await page.locator('#map-btn').click();
    await waitEditable();
    await alpha().waitFor({ state: 'attached' });
  };
  const dragAlpha = async (dx = 60, dy = 35) => {
    const triangle = alpha().locator('.map-tri');
    const box = await triangle.boundingBox();
    assert.ok(box, 'Node triangle is visible');
    const before = await position();
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width / 2 + dx, box.y + box.height / 2 + dy, { steps: 6 });
    await page.mouse.up();
    const after = await position();
    assert.notDeepEqual(after, before, 'Real pointer drag moves the node');
    return after;
  };
  const addNote = async text => {
    await page.locator('#map-add-note').click();
    const input = page.locator('.map-note textarea').last();
    if (text !== undefined) { await input.fill(text); await input.press('Tab'); }
    return input;
  };
  const waitMap = (paper, check, label) => until(async () => {
    const saved = await requestJson(paper);
    return check(saved) && saved;
  }, label);

  // A legacy browser map must neither be imported nor overwritten on live HTTP.
  await page.goto(`${origin}/?paper=${papers[0]}`);
  const legacy = { nodes: [{ name: 'Alpha', x: 101, y: 202 }], notes: [
    { id: 'legacy', x: 300, y: 300, text: 'Legacy browser only' },
    { id: 'legacy-blank', x: 400, y: 300, text: '   ' },
  ] };
  await page.evaluate(({ paper, legacy }) => {
    localStorage.setItem(`paperread-map:${paper}`, JSON.stringify(legacy));
    localStorage.setItem('map-test-unrelated', 'untouched');
  }, { paper: papers[0], legacy });
  const originalStorage = await localSnapshot();
  let releaseInitial, startedInitial;
  const initialStarted = new Promise(resolve => { startedInitial = resolve; });
  holdGet = { started: startedInitial, release: new Promise(resolve => { releaseInitial = resolve; }) };
  await page.locator('#map-btn').click();
  await initialStarted;
  assert.equal(await page.locator('#map-add-note').isDisabled(), true, 'Initial GET gates edits');
  assert.equal(await page.locator('#map-stage').evaluate(stage => stage.inert), true);
  await page.locator('#map-add-note').dispatchEvent('click');
  assert.equal(await page.locator('.map-note textarea').count(), 0);
  assert.equal(attempts.length, 0);
  releaseInitial();
  await waitEditable();
  let saved = await waitMap(papers[0], value => value.nodes.length === 2, 'generated node positions');
  await waitSaved();
  assert.equal(saved.notes.length, 0, 'Live map does not migrate old localStorage');
  assert.notDeepEqual(await position(), { x: 101, y: 202 });

  const moved = await dragAlpha();
  saved = await waitMap(papers[0], value => value.nodes.some(node => node.name === 'Alpha' && node.x === moved.x && node.y === moved.y), 'drag saved');
  await addNote('First note');
  saved = await waitMap(papers[0], value => value.notes[0]?.text === 'First note', 'note added');
  await page.locator('.map-note textarea').fill('Edited note');
  await page.locator('.map-note textarea').press('Tab');
  saved = await waitMap(papers[0], value => value.notes[0]?.text === 'Edited note', 'note edited');
  await waitSaved();
  assert.deepEqual(await localSnapshot(), originalStorage, 'Live writes leave localStorage untouched');
  await open();
  assert.deepEqual(await position(), moved, 'Refresh restores position');
  assert.equal(await page.locator('.map-note textarea').inputValue(), 'Edited note');
  await stopServer(server);
  server = await startServer(server.port);
  await open();
  assert.deepEqual(await requestJson(papers[0]), saved, 'Full service restart restores exact saved map');
  assert.deepEqual(await position(), moved);
  assert.equal(await page.locator('.map-note textarea').inputValue(), 'Edited note');
  console.log('PASS real drag, note add/edit, refresh and full HTTP service restart');

  // Blank drafts cannot poison a save for an unrelated movement.
  await addNote('   ');
  const movedWithBlank = await dragAlpha(-45, 20);
  await waitMap(papers[0], value => value.nodes.some(node => node.name === 'Alpha' && node.x === movedWithBlank.x), 'move beside blank draft');
  assert.equal((await requestJson(papers[0])).notes.length, 1);
  assert.ok(attempts.at(-1).body.notes.every(note => note.text.trim()));
  await page.locator('.map-note-remove').last().click();
  await page.locator('.map-note textarea').fill('');
  await page.locator('.map-note textarea').press('Tab');
  await waitMap(papers[0], value => value.notes.length === 0, 'cleared saved note removed');
  await page.locator('.map-note-remove').click();
  await addNote('Delete this note');
  await waitMap(papers[0], value => value.notes[0]?.text === 'Delete this note', 'deletable note saved');
  await page.locator('.map-note-remove').click();
  await waitMap(papers[0], value => value.notes.length === 0, 'note deletion saved');
  await waitSaved();
  console.log('PASS blank note omission, clearing and explicit deletion');

  // One response's version must reach the next PUT even if edits happen in flight.
  let releaseHeld, startedHeld;
  const heldStarted = new Promise(resolve => { startedHeld = resolve; });
  holdNext = { started: startedHeld, release: new Promise(resolve => { releaseHeld = resolve; }) };
  const firstSerial = attempts.length;
  await dragAlpha(35, 0);
  await heldStarted;
  const finalSerialPosition = await dragAlpha(35, 0);
  await wait(650);
  assert.equal(attempts.length, firstSerial + 1, 'Second edit waits while first PUT is in flight');
  releaseHeld();
  await waitMap(papers[0], value => value.nodes.some(node => node.name === 'Alpha' && node.x === finalSerialPosition.x), 'queued edit saved');
  await waitSaved();
  assert.equal(maxActivePuts, 1, 'Map PUTs are serialized');
  const serial = attempts.slice(firstSerial);
  assert.equal(serial.length, 2);
  assert.equal(serial[1].body.version, serial[0].body.version + 1);
  console.log('PASS rapid edits serialize PUT requests and versions');

  for (const failure of [503, 'disconnect']) {
    putFault = failure;
    const before = await requestJson(papers[0]), count = attempts.length;
    const draft = await dragAlpha(-25, 15);
    await page.locator('#map-save-retry').waitFor({ state: 'visible' });
    assert.deepEqual(await position(), draft, 'Failed PUT preserves visible draft');
    assert.deepEqual(await requestJson(papers[0]), before, 'Failed PUT did not save');
    await wait(650);
    assert.equal(attempts.length, count + 1, 'Failure does not silently retry');
    await page.locator('#map-save-retry').click();
    await waitMap(papers[0], value => value.nodes.some(node => node.name === 'Alpha' && node.x === draft.x && node.y === draft.y), 'explicit save retry');
    await waitSaved();
  }
  console.log('PASS failed/disconnected save keeps draft and requires explicit retry');

  // Use a real stale version: another client commits after this tab loaded.
  const beforeConflict = await requestJson(papers[0]);
  const remoteWinner = await requestJson(papers[0], 'PUT', {
    ...beforeConflict, nodes: beforeConflict.nodes.map(node => node.name === 'Alpha' ? { ...node, x: 800, y: 650 } : node),
    notes: [{ id: 'other-client', x: 540, y: 400, text: 'Other client note' }],
  });
  const conflictStart = attempts.length;
  const conflictDraft = await dragAlpha(30, -20);
  await page.waitForFunction(() => document.querySelector('#map-status').textContent.includes('在别处改过'));
  assert.deepEqual(await position(), conflictDraft, '409 preserves unsaved visible content');
  assert.equal(await page.locator('#map-add-note').isDisabled(), true);
  await wait(650);
  assert.equal(attempts.length, conflictStart + 1, '409 never automatically overwrites');
  assert.deepEqual(await requestJson(papers[0]), remoteWinner);
  await page.locator('#map-reload').click();
  await waitEditable();
  assert.deepEqual(await position(), { x: 800, y: 650 });
  assert.equal(await page.locator('.map-note textarea').inputValue(), 'Other client note');
  await wait(650);
  assert.equal(attempts.length, conflictStart + 1, 'Explicit reload does not resubmit discarded draft');
  console.log('PASS real 409 preserves draft until explicit reload, without overwrite');

  // Non-404 reads are hard failures; stale browser state must never become a PUT.
  for (const failure of [400, 500, 'disconnect', 'null', 'empty-object']) {
    getFault = failure;
    const before = attempts.length;
    await page.goto(`${origin}/?paper=${papers[0]}`);
    await page.locator('#map-btn').click();
    await page.waitForFunction(() => document.querySelector('#map-status').textContent.includes('当前内容已保留'));
    assert.equal(await page.locator('#map-add-note').isDisabled(), true);
    assert.equal(await page.locator('.map-note textarea').count(), 0, 'Read error never loads legacy local map');
    await page.locator('#map-add-note').dispatchEvent('click');
    await wait(650);
    assert.equal(attempts.length, before, `GET ${failure} never writes a blank map`);
    assert.deepEqual(await localSnapshot(), originalStorage);
    getFault = null;
    await page.locator('#map-reload').click();
    await waitEditable();
    assert.equal(await page.locator('.map-note textarea').inputValue(), 'Other client note');
  }
  console.log('PASS GET 400/500/disconnection and malformed 200 responses block edits and never fall back or PUT');

  const paperOne = await requestJson(papers[0]);
  await open(papers[1]);
  await waitMap(papers[1], value => value.nodes.length === 2, 'second paper positions');
  assert.equal(await page.locator('.map-note textarea').count(), 0);
  await addNote('Second paper only');
  const paperTwo = await waitMap(papers[1], value => value.notes[0]?.text === 'Second paper only', 'second paper note');
  await waitSaved();
  await open(papers[0]);
  assert.equal(await page.locator('.map-note textarea').inputValue(), 'Other client note');
  assert.deepEqual(await requestJson(papers[0]), paperOne);
  assert.deepEqual(await requestJson(papers[1]), paperTwo);
  assert.deepEqual(await localSnapshot(), originalStorage);
  console.log('PASS per-paper isolation and live localStorage unchanged');

  getFault = 404;
  const fallbackStart = attempts.length;
  await open();
  assert.deepEqual(await position(), { x: 101, y: 202 });
  assert.equal(await page.locator('.map-note textarea').inputValue(), 'Legacy browser only');
  const fallbackPosition = await dragAlpha(40, 20);
  await until(async () => {
    const data = JSON.parse((await localSnapshot())[`paperread-map:${papers[0]}`]);
    return data.nodes.some(node => node.name === 'Alpha' && node.x === fallbackPosition.x);
  }, '404 fallback saved in browser');
  assert.equal(attempts.length, fallbackStart);
  assert.deepEqual(await requestJson(papers[0]), paperOne, 'Fallback does not write live map');
  getFault = null;
  await open();
  assert.deepEqual(await requestJson(papers[0]), paperOne, 'Returning live never migrates fallback data');
  assert.equal(await page.locator('.map-note textarea').inputValue(), 'Other client note');
  console.log('PASS only HTTP 404 permits browser fallback; no implicit migration');

  // Static/mock mode has no map endpoint and keeps its existing browser-only path.
  const mock = await browser.newContext();
  await mock.addInitScript(() => sessionStorage.setItem('paperread-mock-record', JSON.stringify({ version: 0, concepts: [
    { name: 'Alpha', status: 'known', note: null, explained_in: null, where: null, updated: '2026-01-01T00:00:00Z' },
  ] })));
  await mock.route('**/*', route => {
    const url = new URL(route.request().url());
    if (url.origin !== origin) return route.abort();
    if (url.pathname.startsWith('/api/')) return route.fulfill({ status: 404, body: 'not found' });
    return route.continue();
  });
  const mockPage = await mock.newPage();
  mockPage.on('pageerror', error => browserErrors.push(error.message));
  await mockPage.goto(`${origin}/?paper=2610.27436v1`);
  await mockPage.locator('#map-btn').click();
  await mockPage.locator('#map-add-note').click();
  await mockPage.locator('.map-note textarea').fill('Static mode note');
  await mockPage.locator('.map-note textarea').press('Tab');
  await mockPage.waitForFunction(() => JSON.parse(localStorage.getItem('paperread-map:2610.27436v1'))?.notes[0]?.text === 'Static mode note');
  await mock.close();

  assert.deepEqual(forbidden, [], 'Map operations never call models or mutate concept records');
  assert.deepEqual(await Promise.all(papers.map(paper => readFile(path.join(home, 'papers', paper, 'agent-state.json'), 'utf8'))), recordFiles);
  assert.deepEqual(browserErrors, []);
  assert.ok(responses.some(response => response.status === 409));
  await context.close();
  await stopServer(server);
  console.log(`PASS static fallback and unchanged concept records; ${attempts.length} UI PUTs, ${serverStats.length} service lifetimes, zero model/network calls`);
} finally {
  await browser?.close();
  const results = await Promise.allSettled([...servers].map(stopServer));
  await rm(home, { recursive: true, force: true });
  for (const result of results) if (result.status === 'rejected') console.error(result.reason.message);
}
