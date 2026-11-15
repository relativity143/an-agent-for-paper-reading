// 页面主逻辑：打开论文，请求讲解，把讲解和原文位置连起来。
// 和后端的配合方式见 docs/agent-architecture.md 第 4 节。

import { createApi } from './api.js';
import { createPaperView } from './paper.js';
import { explanationCard, pendingCard, errorCard, noteCard, questionCard, setReceiptWarning } from './cards.js';
import { createManager, backendLabel } from './manage.js';

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
  ended: false,            // 主线已经讲完
};

const paperView = createPaperView(els.paper, els.paperPane);

function setStatus(text) {
  els.status.textContent = text || '';
}

function hasQuestionInput() {
  if (els.question.value.trim() || state.pendingQuote) return true;
  return paperView.isPdf() && Boolean(els.pdfQuote.value.trim());
}

// “继续”要等上一轮的显示状态记好之后才放开，否则后端会重复返回同一轮。
function refreshButtons() {
  const blocked = state.busy || !state.paper || state.unacked.size > 0;
  els.continueBtn.disabled = blocked || state.ended;
  els.continueBtn.title = state.ended ? '主线已经讲完，可以继续提问。' : '';
  els.askBtn.disabled = blocked || !hasQuestionInput();
}

function setBusy(busy) {
  state.busy = busy;
  refreshButtons();
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
    state.unacked.delete(explanation.id);
    setReceiptWarning(card, null);
  } catch {
    state.unacked.add(explanation.id);
    setReceiptWarning(card, () => acknowledge(card, explanation));
  }
  refreshButtons();
}

// 显示一份讲解：同一编号复用已有卡片，显示出来之后再记“已显示”。
async function showExplanation(explanation, { select = true } = {}) {
  if (!explanation || typeof explanation.id !== 'string') return null;
  state.explanations.set(explanation.id, explanation);
  let card = state.cards.get(explanation.id);
  if (!card) {
    card = explanationCard(explanation);
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

// 这份讲解是回答读者提问的：把“你的问题”放在它前面。
// 后端把提问随讲解保存（asked 字段），所以重新打开页面时也能恢复。
function showAsked(card, asked) {
  if (!asked || !(asked.quote || asked.question)) return;
  // 刚才提问时先放的那张卡片，和“点继续后回答”的提示，由这一张代替。
  els.cards.querySelectorAll('.card-question[data-provisional], .card-note').forEach((c) => c.remove());
  const question = questionCard({
    quote: asked.quote, question: asked.question,
    page: Number.isInteger(asked.page) ? asked.page : null,
    where: typeof asked.where === 'string' ? asked.where : null,
  });
  card.before(question);
}

function clearNotices() {
  els.cards.querySelectorAll('.card-error').forEach((c) => c.remove());
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
      action: { label: '恢复', run: recover },
    };
  }
  if (error.kind === 'http' && error.status === 409) {
    return {
      message: `${error.message || '已有一轮讲解正在生成。'}这不是论文的问题，稍等一下再点“恢复”。`,
      action: { label: '恢复', run: recover },
    };
  }
  return { message: error.message || '这一轮没有讲成，可以再试一次。', action: null };
}

// 请求一轮讲解。每次只返回一份讲解，这里不自动接着请求下一轮。
async function requestExplanation(request, questionInfo, restoreInput) {
  if (state.busy || !state.paper) return;
  setBusy(true);
  clearNotices();
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
  try {
    const explanation = await state.api.explain(state.paper, request, (text) => {
      setStatus(text);
      pending.querySelector('.pending-text').textContent = text;
    });
    pending.remove();
    if (request.mode === 'ask' && explanation.type === 'overview') {
      els.cards.append(noteCard('先给出了全文导读。你的问题已经留着，点“继续”后回答。'));
    }
    setStatus('');
    await showExplanation(explanation);
  } catch (error) {
    pending.remove();
    // 没讲成时把读者的输入放回去，不让问题丢掉。
    if (questionEl) questionEl.remove();
    if (restoreInput) restoreInput();
    const failure = describeFailure(error);
    const card = errorCard(failure.message, failure.action);
    els.cards.append(card);
    card.scrollIntoView({ block: 'end', behavior: 'smooth' });
    setStatus('');
  } finally {
    setBusy(false);
  }
}

function submitQuestion() {
  if (state.busy || state.unacked.size) return;
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

  // 在原文上选中一段后，旁边出现“问这一段”。PDF 由浏览器自带的阅读器显示，读不到选中的文字。
  const hideAsk = () => { els.askSelection.hidden = true; };
  els.paper.addEventListener('mouseup', () => {
    setTimeout(() => {
      const picked = paperView.isPdf() ? null : paperView.selection();
      if (!picked) { hideAsk(); return; }
      els.askSelection.hidden = false;
      const width = els.askSelection.offsetWidth;
      const left = Math.min(window.innerWidth - width - 12, Math.max(12, picked.rect.left + picked.rect.width / 2 - width / 2));
      els.askSelection.style.left = `${left}px`;
      els.askSelection.style.top = `${Math.max(12, picked.rect.top - 44)}px`;
      els.askSelection.onclick = () => {
        setQuote({ quote: picked.quote, where: picked.where });
        hideAsk();
        window.getSelection().removeAllRanges();
        els.question.focus();
      };
    }, 0);
  });
  els.paperPane.addEventListener('scroll', hideAsk, { passive: true });
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

function showBackend(name) {
  state.api.backend = name;
  // 只说明用的是哪种后端，不表示模型已经登录或可用。
  els.mode.textContent = name ? `后端：${backendLabel(name)}` : '已连接后端';
}

async function start() {
  bindEvents();
  setBusy(true);
  try {
    state.api = await createApi();
    const manager = createManager({ api: state.api, current: () => state.paper, onBackend: showBackend });
    if (manager.live) {
      showBackend(state.api.backend);
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
      els.question.placeholder = '想问什么？要问某一句原文，请在上面填页码并粘贴那句话。';
    }

    setStatus('');
    const history = await state.api.listExplanations(state.paper);
    for (let i = 0; i < history.length; i += 1) {
      await showExplanation(history[i], { select: i === history.length - 1 });
    }
    setBusy(false);
    // 第一次打开时先请求导读；之后每一轮都由读者点“继续”或提问触发。
    if (!history.length) await requestExplanation({ mode: 'continue' });
  } catch (error) {
    if (!state.paper) els.title.textContent = '没能打开论文';
    els.cards.append(errorCard(error.message || '打开论文时出错了。', { label: '重新加载', run: () => location.reload() }));
    setStatus('');
  } finally {
    setBusy(false);
  }
}

start();
