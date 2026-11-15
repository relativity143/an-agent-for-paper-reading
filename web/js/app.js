// 页面主逻辑：打开论文，请求讲解，把讲解和原文位置连起来。
// 和后端的配合方式见 docs/agent-architecture.md 第 4 节。

import { createApi } from './api.js';
import { createPaperView } from './paper.js';
import { explanationCard, pendingCard, errorCard, noteCard, questionCard, setReceiptWarning } from './cards.js';
import { createManager, backendLabel, chosenModel } from './manage.js';
import { createReadingContext } from './reading-context.js';
import { createRecord } from './record.js';

const $ = (id) => document.getElementById(id);

const els = {
  title: $('paper-title'), meta: $('paper-meta'), steps: $('steps'), mode: $('mode-chip'), library: $('library-btn'),
  paperPane: $('paper-pane'), paper: $('paper'), cards: $('cards'),
  composer: $('composer'), status: $('status'), question: $('question'),
  quoteChip: $('quote-chip'), quoteText: $('quote-text'), quoteClear: $('quote-clear'),
  pdfFields: $('pdf-fields'), pdfPage: $('pdf-page'), pdfQuote: $('pdf-quote'),
  askBtn: $('ask-btn'), continueBtn: $('continue-btn'), askSelection: $('ask-selection'),
};

const state = {
  api: null,
  paper: null,
  content: null,
  steps: [],
  explanations: new Map(), // 讲解编号 → 讲解对象
  cards: new Map(),        // 讲解编号 → 卡片元素；同一编号只有一张卡片
  unacked: new Set(),      // 已显示、但“已显示”还没记到后端的讲解
  pendingQuote: null,      // HTML 论文里选中的原文：{quote, where}
  busy: false,
  manager: null,           // 论文列表、上传、后端设置
  ended: false,            // 主线已经讲完
};

const paperView = createPaperView(els.paper, els.paperPane);
const readingContext = createReadingContext($('reading-context'), refreshButtons);
// 概念记录与反馈（4.7 节）。位置文字认得出来时，点一下让左边的原文跳过去。
const record = createRecord({
  api: () => state.api,
  paper: () => state.paper,
  busy: () => state.busy,
  onChange: refreshButtons,
  jump(where) {
    const target = paperView.resolve(where);
    if (!target) return false;
    return target.page ? paperView.gotoPage(target.page) : paperView.flash(target.id);
  },
});

function setStatus(text) {
  els.status.textContent = text || '';
}

function hasQuestionInput() {
  if (els.question.value.trim() || state.pendingQuote) return true;
  return paperView.isPdf() && Boolean(els.pdfQuote.value.trim());
}

// “继续”要等上一轮的显示状态记好之后才放开，否则后端会重复返回同一轮。
function refreshButtons() {
  const blocked = state.busy || !state.paper || state.unacked.size > 0 || !readingContext.ready() || !record.ready();
  els.continueBtn.disabled = blocked || state.ended;
  els.continueBtn.title = !readingContext.ready() ? '先确认本次阅读的基础。'
    : !record.ready() ? '先确认上次读这篇时的记录。'
      : state.ended ? '主线已经讲完，可以继续提问。' : '';
  els.askBtn.disabled = blocked || !hasQuestionInput();
}

function setBusy(busy) {
  state.busy = busy;
  readingContext.setBusy(busy);
  refreshButtons();
  // 设置面板开着时，让它跟着变：生成中不能切换后端，生成完放开。
  if (state.manager) state.manager.busyChanged();
}

// 步骤名可能来自模型或大纲，只当作文字：用 DOM 属性赋值，不拼进 HTML。
// 这里只表示“现在讲到哪一步”，不表示前面的步骤都讲过或看懂了。
function renderSteps(current) {
  els.steps.replaceChildren(...state.steps.map((s) => {
    const cls = current && s.step < current ? 'done' : current && s.step === current ? 'current' : '';
    const label = `第 ${s.step} 步：${s.name}${cls === 'current' ? '（现在讲到这里）' : ''}`;
    const item = document.createElement('li');
    if (cls) item.className = cls;
    item.title = label;
    const text = document.createElement('span');
    text.className = 'sr-only';
    text.textContent = label;
    item.append(text);
    return item;
  }));
  // 导读卡片里的路线图跟着标出现在讲到哪一站。
  els.cards.querySelectorAll('.route li').forEach((item) => {
    const step = Number(item.dataset.step);
    const here = Boolean(current) && step === current;
    item.classList.toggle('is-past', Boolean(current) && step < current);
    item.classList.toggle('is-current', here);
    if (here) item.setAttribute('aria-current', 'step'); else item.removeAttribute('aria-current');
    const badge = item.querySelector('.route-here');
    if (here && !badge) {
      const pill = document.createElement('span');
      pill.className = 'route-here';
      pill.textContent = '现在讲到这里';
      item.querySelector('.route-name').append(pill);
    } else if (!here && badge) {
      badge.remove();
    }
  });
}

function validSteps(steps) {
  return Array.isArray(steps) ? steps.filter((s) => s && Number.isInteger(s.step) && typeof s.name === 'string') : [];
}

// 第一次取正文时导读还没生成，主线是空的；导读出来之后再同步一次。
async function syncSteps(overview) {
  let steps = validSteps(overview && overview.route && overview.route.steps);
  if (!steps.length) {
    try { steps = validSteps((await state.api.getPaper(state.paper)).steps); } catch { /* 保留原来的主线 */ }
  }
  if (steps.length) state.steps = steps;
}

function setQuote(quote) {
  state.pendingQuote = quote;
  els.quoteChip.hidden = !quote;
  els.quoteText.textContent = quote ? quote.quote : '';
  refreshButtons();
}

function selectCard(card, explanation, { scroll = true } = {}) {
  els.cards.querySelectorAll('.card.is-current').forEach((c) => c.classList.remove('is-current'));
  card.classList.add('is-current');
  paperView.highlight(explanation.highlights, { scroll });
  const step = explanation.progress && Number.isInteger(explanation.progress.step) ? explanation.progress.step : null;
  renderSteps(step);
}

// 等浏览器把卡片画出来。页面在后台时不会有动画帧，所以加一个时间上限。
function painted() {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, 250);
    requestAnimationFrame(() => requestAnimationFrame(() => { clearTimeout(timer); resolve(); }));
  });
}

// 告诉后端“这张卡片已经显示”。失败时保留卡片，给出重试，不重新生成。
async function acknowledge(card, explanation) {
  try {
    await state.api.acknowledge(state.paper, explanation.id);
    // 这一轮讲过的概念已经记进记录：先读到新的版本，再放开“继续”。
    await record.refresh();
    state.unacked.delete(explanation.id);
    setReceiptWarning(card, null);
  } catch {
    state.unacked.add(explanation.id);
    setReceiptWarning(card, () => acknowledge(card, explanation));
  }
  refreshButtons();
}

// 最新一份讲解的编号（卡片是按顺序加的）。
function lastExplanationId() {
  let last = null;
  for (const id of state.cards.keys()) last = id;
  return last;
}

// 显示一份讲解：同一编号复用已有卡片，显示出来之后再记“已显示”。
async function showExplanation(explanation, { select = true } = {}) {
  if (!explanation || typeof explanation.id !== 'string') return null;
  state.explanations.set(explanation.id, explanation);
  let card = state.cards.get(explanation.id);
  if (!card) {
    card = explanationCard(explanation);
    linkRoute(card);
    if (record.supported() && explanation.type !== 'overview') {
      card.append(record.feedbackBar(explanation, () => lastExplanationId() === explanation.id));
    }
    state.cards.set(explanation.id, card);
    els.cards.append(card);
    showAsked(card, explanation.asked);
  }
  if (explanation.type === 'overview') await syncSteps(explanation);
  if (explanation.type !== 'overview') {
    state.ended = Boolean(explanation.progress) && explanation.progress.next === null;
  }
  if (select) {
    selectCard(card, explanation);
    card.scrollIntoView({ block: 'start', behavior: 'smooth' });
  }
  await painted();
  await acknowledge(card, explanation);
  return card;
}

// 路线图里认得出位置的站点，把位置文字变成按钮：点一下，左边的原文跳到那里。
// 认不出来的保持普通文字。点击由卡片区统一处理（data-loc / data-page）。
function linkRoute(card) {
  card.querySelectorAll('.route .route-range').forEach((range) => {
    const target = paperView.resolve(range.textContent);
    if (!target) return;
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'route-range route-jump';
    button.textContent = range.textContent;
    button.title = '跳到原文的这个位置';
    if (target.page) button.dataset.page = String(target.page);
    else button.dataset.loc = target.id;
    range.replaceWith(button);
  });
}

// 正在生成的那一轮的任务编号。连接断开或刷新页面后还要能停止它，所以按论文记在这个标签页里。
const taskKey = () => `paperread-task:${state.paper}`;
function rememberTask(task) {
  try { sessionStorage.setItem(taskKey(), task); } catch { /* 记不下来时只是少一个“停止”按钮 */ }
}
function forgetTask() {
  try { sessionStorage.removeItem(taskKey()); } catch { /* 同上 */ }
}
function knownTask() {
  if (!state.api || !state.api.cancelTask) return null;
  try { return sessionStorage.getItem(taskKey()); } catch { return null; }
}

// 连接断开后，那一轮可能还在生成：按记下的任务编号停止它，再看看有没有已经保存的讲解。
async function stopLostTask() {
  const task = knownTask();
  if (!task || state.busy) return;
  setStatus('正在停止…');
  try {
    await state.api.cancelTask(task);
  } catch (error) {
    // 服务重启过就不认识这个任务了，那一轮也已经不在了。
    if (error.status === 404) forgetTask();
    setStatus(error.status === 404 ? '那一轮已经不在了，可以直接再试。' : (error.message || '没能停止，可以再点一次。'));
    return;
  }
  forgetTask();
  await recover();
  const note = noteCard('已停止这一轮。上面是已经保存的讲解；可以换模型或调时限，然后再点“继续”。');
  note.classList.add('card-stopped');
  els.cards.append(note);
}

// “恢复”之外，知道任务编号时再给一个“停止这一轮”。
function recoverActions() {
  const actions = [{ label: '恢复', run: recover }];
  if (knownTask()) actions.push({ label: '停止这一轮', run: stopLostTask });
  return actions;
}

// 这份讲解是回答读者提问的：把“你的问题”放在它前面。
// 后端把提问随讲解保存（asked 字段），所以重新打开页面时也能恢复。
function showAsked(card, asked) {
  if (!asked || !(asked.quote || asked.question)) return;
  // 刚才提问时先放的那张卡片，和“点继续后回答”的提示，由这一张代替。
  els.cards.querySelectorAll('.card-question[data-provisional], .card-note, [data-pending]').forEach((c) => c.remove());
  const question = questionCard({
    quote: asked.quote, question: asked.question,
    page: Number.isInteger(asked.page) ? asked.page : null,
    where: typeof asked.where === 'string' ? asked.where : null,
  });
  card.before(question);
}

// 后端还留着、没有回答的问题（论文信息里的 pending）：有就显示在最后，没有就拿掉。
// 提问那一轮没讲成或被停止时，问题仍然留在后端，下一次点“继续”会回答它。
function showPending(pending) {
  els.cards.querySelectorAll('[data-pending]').forEach((c) => c.remove());
  if (!pending || !(pending.quote || pending.question)) return false;
  const question = questionCard({
    quote: pending.quote, question: pending.question,
    page: Number.isInteger(pending.page) ? pending.page : null,
    where: typeof pending.where === 'string' ? pending.where : null,
  });
  const note = noteCard('这个问题已经留着，还没有回答。点“继续”后回答；想换一个问题，直接重新提问。');
  question.dataset.pending = '1';
  note.dataset.pending = '1';
  els.cards.append(question, note);
  return true;
}

async function refreshPending() {
  if (state.api.mode !== 'live') return false;
  try {
    return showPending((await state.api.getPaper(state.paper)).pending);
  } catch {
    return els.cards.querySelector('[data-pending]') !== null;
  }
}

function clearNotices() {
  els.cards.querySelectorAll('.card-error, .card-stopped').forEach((c) => c.remove());
}

// 从后端的历史记录恢复：补上还没显示的讲解，已经有的卡片不重复添加。
async function recover() {
  if (state.busy) return;
  setBusy(true);
  setStatus('正在恢复…');
  try {
    const history = await state.api.listExplanations(state.paper);
    clearNotices();
    for (let i = 0; i < history.length; i += 1) {
      const known = state.cards.has(history[i].id);
      await showExplanation(history[i], { select: i === history.length - 1 && !known });
    }
    await refreshPending();
    await record.refresh();
    setStatus(history.length ? '' : '还没有生成好的讲解。');
  } catch (error) {
    setStatus(error.message || '恢复失败，请稍后再试。');
  } finally {
    setBusy(false);
  }
}

function describeFailure(error) {
  if (error.kind === 'disconnected') {
    return {
      message: `${error.message}不会重新生成；点“恢复”看看这一轮是不是已经讲完。`,
      action: recoverActions(),
    };
  }
  if (error.kind === 'http' && error.status === 409) {
    return {
      message: `${error.message || '已有一轮讲解正在生成。'}这不是论文的问题，稍等一下再点“恢复”。`,
      action: recoverActions(),
    };
  }
  if (/超时|timed out/i.test(error.message || '')) {
    return {
      message: '模型在限定的时间内没有答完，这一轮没有保存。可以再试一次，或者点右上角的模型后端，换一个模型、把时限调大（最大 600 秒）。',
      action: { label: '打开设置', run: () => state.manager.openSettings() },
    };
  }
  return { message: error.message || '这一轮没有讲成，可以再试一次。', action: null };
}

// 请求一轮讲解。每次只返回一份讲解，这里不自动接着请求下一轮。
async function requestExplanation(request, questionInfo, restoreInput) {
  if (state.busy || !state.paper || state.unacked.size || !readingContext.ready() || !record.ready()) return;
  request = record.withRequest(readingContext.withRequest(request));
  setBusy(true);
  clearNotices();
  els.cards.querySelectorAll('.card-start').forEach((c) => c.remove());
  let questionEl = null;
  if (questionInfo) {
    questionEl = questionCard(questionInfo);
    // 先显示出来；讲解回来后，如果它带着这次提问（asked），就换成绑定在那份讲解上的卡片。
    questionEl.dataset.provisional = '1';
    els.cards.append(questionEl);
  }
  const pending = pendingCard();
  els.cards.append(pending);
  pending.scrollIntoView({ block: 'end', behavior: 'smooth' });
  // 真实模型讲一轮可能要一两分钟。显示已经等了多久，让读者知道它还在进行。
  const startedAt = Date.now();
  let phase = '正在准备这一轮讲解…';
  const showProgress = () => {
    const seconds = Math.round((Date.now() - startedAt) / 1000);
    const text = seconds >= 5 ? `${phase}（已等 ${seconds} 秒）` : phase;
    setStatus(text);
    pending.querySelector('.pending-text').textContent = text;
  };
  const ticker = setInterval(showProgress, 1000);
  // 停止：只对后端支持的情况显示按钮，拿到任务编号后才能点。
  const stopBtn = pending.querySelector('.pending-stop');
  let stopped = false;
  const onTask = (task) => {
    if (!state.api.cancelTask) return;
    rememberTask(task);
    stopBtn.hidden = false;
    stopBtn.addEventListener('click', async () => {
      stopped = true;
      stopBtn.disabled = true;
      stopBtn.textContent = '正在停止…';
      try {
        await state.api.cancelTask(task);
      } catch (error) {
        // 没停成（比如本机进程没清理掉）：放开按钮，可以再点一次。
        stopped = false;
        stopBtn.disabled = false;
        stopBtn.textContent = '停止这一轮';
        phase = error.message || '没能停止，可以再点一次。';
        showProgress();
      }
    });
  };
  try {
    const explanation = await state.api.explain(state.paper, request, (text) => {
      phase = text;
      showProgress();
    }, onTask);
    forgetTask();
    pending.remove();
    const live = state.api.mode === 'live';
    if (!live && request.mode === 'ask' && explanation.type === 'overview') {
      els.cards.append(noteCard('先给出了全文导读。你的问题已经留着，点“继续”后回答。'));
    }
    setStatus('');
    await showExplanation(explanation);
    // 这一轮给的是导读或上一轮讲解时，问题还留在后端：换成“留着的问题”那张卡片。
    if (await refreshPending() && questionEl) questionEl.remove();
  } catch (error) {
    // 后端明确说这一轮结束了，才忘掉任务编号；断开时它可能还在生成。
    if (error.kind === 'model') forgetTask();
    pending.remove();
    // 提问的那一轮没讲成时，后端一般已经把问题留下了；没留下才把输入放回去，不让问题丢掉。
    const queued = request.mode === 'ask' && await refreshPending();
    if (questionEl) questionEl.remove();
    if (!queued && restoreInput) restoreInput();
    let card;
    if (stopped && error.kind === 'model') {
      // 读者自己停的，不算出错，也不算读过。
      card = noteCard('已停止这一轮，没有保存讲解。现在可以换模型或调时限，然后再点“继续”。');
      card.classList.add('card-stopped');
    } else {
      const failure = describeFailure(error);
      card = errorCard(failure.message, failure.action);
    }
    els.cards.append(card);
    card.scrollIntoView({ block: 'end', behavior: 'smooth' });
    setStatus('');
  } finally {
    clearInterval(ticker);
    setBusy(false);
  }
}

function submitQuestion() {
  if (state.busy || state.unacked.size || !readingContext.ready() || !record.ready()) return;
  const question = els.question.value.trim();
  const request = { mode: 'ask' };
  const info = { question };
  let restore;

  if (paperView.isPdf()) {
    // PDF：页码是文件里的第几页（从 1 开始），原文片段由读者粘贴。
    const quote = els.pdfQuote.value.trim();
    const pageText = els.pdfPage.value.trim();
    const page = pageText ? Number(pageText) : null;
    if (pageText && (!Number.isInteger(page) || page < 1)) { setStatus('页码要填从 1 开始的整数。'); return; }
    if (quote && !page) { setStatus('粘贴了原文片段时，请同时填上它在第几页。'); return; }
    if (!quote && !question) return;
    if (quote) request.quote = quote;
    if (page) request.page = page;
    info.quote = quote;
    info.page = page;
    restore = () => { els.pdfQuote.value = quote; els.pdfPage.value = pageText; els.question.value = question; refreshButtons(); };
    els.pdfQuote.value = '';
  } else {
    const picked = state.pendingQuote;
    if (!picked && !question) return;
    if (picked) { request.quote = picked.quote; request.where = picked.where; }
    info.quote = picked && picked.quote;
    info.where = picked && picked.where;
    restore = () => { els.question.value = question; setQuote(picked); };
    setQuote(null);
  }
  if (question) request.question = question;
  els.question.value = '';
  refreshButtons();
  requestExplanation(request, info, restore);
}

function bindEvents() {
  els.continueBtn.addEventListener('click', () => requestExplanation({ mode: 'continue' }));
  els.composer.addEventListener('submit', (event) => { event.preventDefault(); submitQuestion(); });
  for (const input of [els.question, els.pdfPage, els.pdfQuote]) input.addEventListener('input', refreshButtons);
  els.question.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) { event.preventDefault(); submitQuestion(); }
  });
  els.quoteClear.addEventListener('click', () => setQuote(null));

  // 卡片里的位置标签、页码、没展开的词，以及点卡片本身。
  els.cards.addEventListener('click', (event) => {
    const loc = event.target.closest('[data-loc]');
    if (loc) { paperView.flash(loc.dataset.loc); return; }
    const page = event.target.closest('[data-page]');
    if (page) {
      if (paperView.gotoPage(Number(page.dataset.page))) els.pdfPage.value = page.dataset.page;
      return;
    }
    const term = event.target.closest('[data-term]');
    if (term) {
      els.question.value = `讲一下：${term.dataset.term}`;
      refreshButtons();
      els.question.focus();
      return;
    }
    if (event.target.closest('a, button, textarea, input')) return;
    const card = event.target.closest('.card[data-id]');
    if (card && !card.classList.contains('is-current')) {
      const explanation = state.explanations.get(card.dataset.id);
      if (explanation) selectCard(card, explanation);
    }
  });

  // 在原文上选中一段后，旁边出现“问这一段”。PDF 嵌在页面里显示时也可以选；退回浏览器自带的阅读器时读不到选中的文字。
  const hideAsk = () => { els.askSelection.hidden = true; };
  els.paper.addEventListener('mouseup', () => {
    setTimeout(() => {
      const picked = paperView.selection();
      if (!picked) { hideAsk(); return; }
      els.askSelection.hidden = false;
      const width = els.askSelection.offsetWidth;
      const left = Math.min(window.innerWidth - width - 12, Math.max(12, picked.rect.left + picked.rect.width / 2 - width / 2));
      els.askSelection.style.left = `${left}px`;
      els.askSelection.style.top = `${Math.max(12, picked.rect.top - 44)}px`;
      els.askSelection.onclick = () => {
        if (picked.page) {
          // PDF：把选中的原文和它所在的页填进提问区的那两栏，和手动粘贴走同一条路。
          els.pdfQuote.value = picked.quote;
          els.pdfPage.value = String(picked.page);
          refreshButtons();
        } else {
          setQuote({ quote: picked.quote, where: picked.where });
        }
        hideAsk();
        window.getSelection().removeAllRanges();
        els.question.focus();
      };
    }, 0);
  });
  els.paperPane.addEventListener('scroll', hideAsk, { passive: true });
  // 从浏览器的前进/后退缓存返回也算重新打开，不能复用上次确认。
  window.addEventListener('pageshow', (event) => {
    if (event.persisted && state.api?.mode === 'live' && state.paper) readingContext.reset(true);
  });
  document.addEventListener('mousedown', (event) => {
    if (event.target !== els.askSelection && !els.paper.contains(event.target)) hideAsk();
  });
}

// 还没指定论文时：直接把打开论文的入口放在页面上。
function showOpenForm(manager) {
  els.title.textContent = '还没有打开论文';
  const view = manager.libraryView();
  view.root.classList.add('card');
  els.cards.append(view.root);
  view.focus();
}

// 先按“已经在本机的论文”打开（包括上传的本地 PDF）；本机没有，再让后端去取。
async function openPaper(input) {
  try {
    return await state.api.getPaper(input);
  } catch (error) {
    if (error.kind !== 'http') throw error;
    return state.api.openPaper(input);
  }
}

function showBackend(name, model) {
  state.api.backend = name;
  // 只说明用的是哪种后端、选了哪个模型，不表示模型已经登录或可用。
  const label = [backendLabel(name), model].filter(Boolean).join(' · ');
  els.mode.textContent = name ? `后端：${label}` : '已连接后端';
}

async function start() {
  bindEvents();
  setBusy(true);
  try {
    state.api = await createApi();
    const manager = createManager({ api: state.api, current: () => state.paper, onBackend: showBackend, busy: () => state.busy });
    state.manager = manager;
    if (manager.live) {
      showBackend(state.api.backend);
      // 选了具体模型时，把模型名也显示在右上角；读不到就只显示后端。
      state.api.getSettings().then((settings) => {
        showBackend(settings.backend, chosenModel(settings));
      }).catch(() => {});
      els.mode.title = '选择用哪个模型来讲解';
      els.mode.addEventListener('click', manager.openSettings);
      els.library.addEventListener('click', manager.openLibrary);
      els.library.title = '换一篇论文，或打开本地 PDF';
    } else {
      els.mode.textContent = '样例数据';
      els.mode.title = '没有连接到后端，显示的是手写的样例讲解，不会写入任何阅读记录';
      els.mode.disabled = true;
    }
    els.mode.hidden = false;

    const input = new URLSearchParams(location.search).get('paper') || state.api.defaultPaper;
    if (!input) { showOpenForm(manager); return; }
    els.library.hidden = !manager.live;

    setStatus('正在打开论文…');
    const meta = await openPaper(input);
    state.paper = meta.paper;
    // 每次打开都重新问；历史仅用于回看，不作为这一次的基础。
    readingContext.reset(manager.live);
    // 上次的概念记录：不是空的就先摆出来，确认之后才能继续。
    await record.load();
    const content = await state.api.getContent(state.paper);
    // PDF 的正文接口只给出文件地址，标题和作者取自论文信息。
    state.content = { ...meta, ...content };
    state.steps = validSteps(state.content.steps);
    const title = state.content.title || state.paper;
    els.title.textContent = title;
    els.meta.textContent = [(state.content.authors || []).join('、'), state.paper].filter(Boolean).join(' · ');
    document.title = `${title} · 论文阅读`;
    renderSteps(null);
    paperView.render(state.content);
    if (paperView.isPdf()) {
      els.pdfFields.hidden = false;
      els.question.placeholder = '想问什么？可以在左边选中一句原文再问；选不了的时候，在上面填页码并粘贴那句话。';
    }

    setStatus('');
    const history = await state.api.listExplanations(state.paper);
    for (let i = 0; i < history.length; i += 1) {
      await showExplanation(history[i], { select: i === history.length - 1 });
    }
    showPending(meta.pending);
    setBusy(false);
    if (!history.length) {
      if (manager.live) {
        // 不自动开始：生成导读要调用模型，要等一两分钟，期间也换不了后端。让读者自己点。
        const start = noteCard('这篇论文还没有读过。点“继续”，先生成全文导读；长论文可能要等一到三分钟。用的是右上角显示的模型后端，想换的话先点它。');
        start.classList.add('card-start');
        els.cards.append(start);
      } else {
        // 样例数据不调用模型，直接显示导读。
        await requestExplanation({ mode: 'continue' });
      }
    }
  } catch (error) {
    if (!state.paper) els.title.textContent = '没能打开论文';
    els.cards.append(errorCard(error.message || '打开论文时出错了。', { label: '重新加载', run: () => location.reload() }));
    setStatus('');
  } finally {
    setBusy(false);
  }
}

start();
