// 页面主逻辑：打开论文，请求讲解，把讲解和原文位置连起来。

import { createApi } from './api.js';
import { createPaperView } from './paper.js';
import { explanationCard, pendingCard, errorCard, questionCard } from './cards.js';

const $ = (id) => document.getElementById(id);

const els = {
  title: $('paper-title'), meta: $('paper-meta'), steps: $('steps'), mode: $('mode-chip'),
  paperPane: $('paper-pane'), paper: $('paper'), cards: $('cards'),
  composer: $('composer'), status: $('status'), question: $('question'),
  quoteChip: $('quote-chip'), quoteText: $('quote-text'), quoteClear: $('quote-clear'),
  askBtn: $('ask-btn'), continueBtn: $('continue-btn'), askSelection: $('ask-selection'),
};

const state = {
  api: null,
  paper: null,
  content: null,
  explanations: new Map(),
  pendingQuote: null, // {quote, where}
  busy: false,
};

const paperView = createPaperView(els.paper, els.paperPane);

function setStatus(text) {
  els.status.textContent = text || '';
}

function setBusy(busy) {
  state.busy = busy;
  els.continueBtn.disabled = busy;
  refreshAskButton();
}

function refreshAskButton() {
  els.askBtn.disabled = state.busy || !(state.pendingQuote || els.question.value.trim());
}

// 步骤名可能来自模型或大纲，只当作文字：用 DOM 属性赋值，不拼进 HTML。
function renderSteps(steps, current) {
  els.steps.replaceChildren(...(steps || []).map((s) => {
    const cls = current && s.step < current ? 'done' : current && s.step === current ? 'current' : '';
    const label = `第 ${s.step} 步：${s.name}${cls === 'current' ? '（正在读）' : cls === 'done' ? '（在这之前）' : ''}`;
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

function setQuote(quote) {
  state.pendingQuote = quote;
  els.quoteChip.hidden = !quote;
  els.quoteText.textContent = quote ? quote.quote : '';
  refreshAskButton();
}

function selectCard(card, explanation, { scroll = true } = {}) {
  els.cards.querySelectorAll('.card.is-current').forEach((c) => c.classList.remove('is-current'));
  card.classList.add('is-current');
  paperView.highlight(explanation.highlights, { scroll });
  if (explanation.progress && explanation.progress.step) {
    renderSteps(state.content.steps, explanation.progress.step);
  }
}

function addExplanation(explanation, { select = true } = {}) {
  state.explanations.set(explanation.id, explanation);
  const card = explanationCard(explanation);
  els.cards.append(card);
  if (select) {
    selectCard(card, explanation);
    card.scrollIntoView({ block: 'start', behavior: 'smooth' });
  }
  return card;
}

async function requestExplanation(request, questionInfo) {
  if (state.busy) return;
  setBusy(true);
  els.cards.querySelectorAll('.card-error').forEach((c) => c.remove());
  if (questionInfo) els.cards.append(questionCard(questionInfo));
  const pending = pendingCard();
  els.cards.append(pending);
  pending.scrollIntoView({ block: 'end', behavior: 'smooth' });
  try {
    const explanation = await state.api.explain(state.paper, request, (text) => {
      setStatus(text);
      pending.querySelector('.pending-text').textContent = text;
    });
    pending.remove();
    addExplanation(explanation);
    setStatus('');
  } catch (error) {
    pending.remove();
    const card = errorCard(error.message || '这一轮没有讲成，请再试一次。');
    els.cards.append(card);
    card.scrollIntoView({ block: 'end', behavior: 'smooth' });
    setStatus('');
  } finally {
    setBusy(false);
  }
}

function submitQuestion() {
  const question = els.question.value.trim();
  const quote = state.pendingQuote;
  if (!question && !quote) return;
  const request = { mode: 'ask' };
  if (quote) { request.quote = quote.quote; request.where = quote.where; }
  if (question) request.question = question;
  els.question.value = '';
  setQuote(null);
  requestExplanation(request, { quote: quote && quote.quote, question });
}

function bindEvents() {
  els.continueBtn.addEventListener('click', () => requestExplanation({ mode: 'continue' }));
  els.composer.addEventListener('submit', (event) => { event.preventDefault(); submitQuestion(); });
  els.question.addEventListener('input', refreshAskButton);
  els.question.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) { event.preventDefault(); submitQuestion(); }
  });
  els.quoteClear.addEventListener('click', () => setQuote(null));

  // 卡片里的位置标签、没展开的词，以及点卡片本身。
  els.cards.addEventListener('click', (event) => {
    const loc = event.target.closest('[data-loc]');
    if (loc) { paperView.flash(loc.dataset.loc); return; }
    const term = event.target.closest('[data-term]');
    if (term) {
      els.question.value = `讲一下：${term.dataset.term}`;
      refreshAskButton();
      els.question.focus();
      return;
    }
    if (event.target.closest('a, button, textarea')) return;
    const card = event.target.closest('.card[data-id]');
    if (card && !card.classList.contains('is-current')) {
      const explanation = state.explanations.get(card.dataset.id);
      if (explanation) selectCard(card, explanation);
    }
  });

  // 在原文上选中一段后，旁边出现“问这一段”。
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

async function start() {
  bindEvents();
  setBusy(true);
  try {
    state.api = await createApi();
    if (state.api.mode === 'mock') {
      els.mode.textContent = '样例数据';
      els.mode.title = '没有连接到后端，显示的是手写的样例讲解';
      els.mode.hidden = false;
    }
    const input = new URLSearchParams(location.search).get('paper') || state.api.defaultPaper;
    if (!input) {
      els.title.textContent = '还没有打开论文';
      setStatus('在地址栏加上 ?paper=<arXiv 编号> 打开一篇论文。');
      return;
    }
    const opened = await state.api.openPaper(input);
    state.paper = opened.paper;
    state.content = await state.api.getContent(state.paper);
    els.title.textContent = state.content.title;
    els.meta.textContent = `${(state.content.authors || []).join('、')} · ${state.paper}`;
    document.title = `${state.content.title} · 论文阅读`;
    renderSteps(state.content.steps, null);
    paperView.render(state.content);

    const earlier = await state.api.listExplanations(state.paper);
    earlier.forEach((x, i) => addExplanation(x, { select: i === earlier.length - 1 }));
    setBusy(false);
    if (!earlier.length) await requestExplanation({ mode: 'continue' });
  } catch (error) {
    els.title.textContent = '没能打开论文';
    els.cards.append(errorCard(error.message || '打开论文时出错了。'));
  } finally {
    setBusy(false);
  }
}

start();
