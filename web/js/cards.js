// 右侧：把讲解对象（docs/agent-architecture.md 4.2 节）显示成卡片。

import { renderInline, renderBlocks, typeset, escapeHtml } from './rich.js';

const STATUS_LABEL = {
  known: '你确认懂了',
  explained: '讲过',
  unsure: '仍不确定',
  new: '这一轮补',
};

function whereChips(where) {
  const ids = Array.isArray(where) ? where : (where && where.ids) || [];
  const chips = ids.map((id) => `<button type="button" class="loc" data-loc="${escapeHtml(id)}">${escapeHtml(id)}</button>`);
  if (where && !Array.isArray(where) && where.page) chips.push(`<span class="loc loc-page">第 ${Number(where.page)} 页</span>`);
  return chips.join('');
}

function section(title, body, extraClass = '') {
  if (!body) return '';
  return `<section class="card-sec ${extraClass}"><h4>${title}</h4>${body}</section>`;
}

function conceptHtml(concept, index) {
  const sources = (concept.sources || []).map((s) => `<a href="${escapeHtml(s.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(s.title)}</a>`).join('；');
  return `
    <li class="concept">
      <h5><span class="concept-no">${index + 1}</span>${renderInline(concept.name)}</h5>
      <div class="role">
        <span class="tag">在这篇文章里</span>
        ${renderBlocks(concept.in_this_paper)}
        <p class="locs">${whereChips(concept.in_this_paper_where)}</p>
      </div>
      <div class="itself">
        <span class="tag tag-plain">它本身</span>
        ${renderBlocks(concept.itself)}
        ${sources ? `<p class="sources">来源：${sources}</p>` : ''}
      </div>
    </li>`;
}

function pointCard(x) {
  const assumes = (x.assumes || []).map((a) => `
    <li><span class="pill pill-${escapeHtml(a.status)}">${STATUS_LABEL[a.status] || escapeHtml(a.status)}</span>
    <span class="assume-name">${renderInline(a.concept)}</span>
    ${a.explained_at ? `<button type="button" class="loc" data-loc="${escapeHtml(a.explained_at)}">${escapeHtml(a.explained_at)}</button>` : ''}
    ${a.note ? `<span class="assume-note">${renderInline(a.note)}</span>` : ''}</li>`).join('');
  const unexpanded = (x.unexpanded || []).map((t) => `<button type="button" class="term" data-term="${escapeHtml(t)}">${renderInline(t)}</button>`).join('');
  const progress = x.progress || {};
  const next = progress.next;
  return `
    <header class="card-head">
      <p class="card-kind">这一点${x.section ? ` · ${escapeHtml(x.section)}` : ''}</p>
      <h3>${renderInline(x.point)}</h3>
      <p class="locs">${whereChips(x.where)}</p>
    </header>
    ${section('', renderBlocks(x.intro), 'intro')}
    ${section('这里默认你知道', assumes ? `<ul class="assumes">${assumes}</ul>` : '<p class="none">无新的缺口。</p>')}
    ${section('基础讲解', `<ol class="concepts">${(x.concepts || []).map(conceptHtml).join('')}</ol>${x.together ? `<div class="together"><span class="tag tag-plain">合起来</span>${renderBlocks(x.together)}</div>` : ''}`)}
    ${section('回到原文', renderBlocks(x.back_to_text))}
    ${section('用到但没展开的词', unexpanded ? `<p class="terms">${unexpanded}</p><p class="hint">不熟的话点一下，我来讲。</p>` : '<p class="none">无。</p>')}
    ${section('读到哪了', `
      <p class="step-line">主线第 ${Number(progress.step) || '?'} 步，共 ${Number(progress.steps_total) || '?'} 步</p>
      ${renderBlocks(progress.text || '')}
      ${next ? `<p class="next">接下来：${renderInline(next.point || '')} ${whereChips(next.where)}</p>` : ''}`, 'progress')}`;
}

function overviewCard(x) {
  const items = ((x.model && x.model.items) || []).map((item) => `
    <li><strong>${escapeHtml(item.label)}：</strong>${renderInline(item.text)} <span class="locs">${whereChips(item.where)}</span></li>`).join('');
  const foundations = (x.foundations || []).map((f) => `<li><strong>${renderInline(f.name)}：</strong>${renderInline(f.note)}</li>`).join('');
  const previously = (x.previously || []).map((p) => `<li>${renderInline(p.name)}（${escapeHtml(p.paper || '')}，${STATUS_LABEL[p.status] || ''}）</li>`).join('');
  const steps = ((x.route && x.route.steps) || []).map((s) => `<li><span class="route-name">${escapeHtml(s.name)}</span><span class="route-range">${escapeHtml(s.range)}</span></li>`).join('');
  return `
    <header class="card-head">
      <p class="card-kind">全文导读</p>
      <h3>${escapeHtml(x.title || '')}</h3>
    </header>
    ${section('这篇论文在问什么', `${renderBlocks(x.question.text)}<p class="locs">${whereChips(x.question.where)}</p>`)}
    ${section('核心模型或实验', `${x.model && x.model.summary ? renderBlocks(x.model.summary) : ''}<ul class="plain">${items}</ul>`)}
    ${section('主要结论', `${renderBlocks(x.conclusions.text)}<p class="locs">${whereChips(x.conclusions.where)}</p>`)}
    ${section('整篇依赖的基础', `<ul class="plain">${foundations}</ul>${previously ? `<p class="hint">已在前几篇补过的：</p><ul class="plain">${previously}</ul>` : ''}`)}
    ${section('阅读路线', `<ol class="route">${steps}</ol>${x.route && x.route.skim ? `<p class="hint">${renderInline(x.route.skim)}</p>` : ''}`)}
    ${section('接下来', `${renderBlocks(x.next.text)}<p class="locs">${whereChips(x.next.where)}</p>`, 'progress')}`;
}

export function explanationCard(x) {
  const card = document.createElement('article');
  card.className = `card card-${x.type}`;
  card.dataset.id = x.id;
  card.tabIndex = -1;
  card.innerHTML = x.type === 'overview' ? overviewCard(x) : pointCard(x);
  typeset(card);
  return card;
}

export function pendingCard() {
  const card = document.createElement('div');
  card.className = 'card card-pending';
  card.innerHTML = '<p class="pending-text">正在准备这一轮讲解…</p>';
  return card;
}

export function errorCard(message) {
  const card = document.createElement('div');
  card.className = 'card card-error';
  card.setAttribute('role', 'alert');
  card.innerHTML = `<p>${escapeHtml(message)}</p>`;
  return card;
}

export function questionCard({ quote, question }) {
  const card = document.createElement('div');
  card.className = 'card card-question';
  card.innerHTML = `
    <p class="card-kind">你的问题</p>
    ${quote ? `<blockquote>${escapeHtml(quote)}</blockquote>` : ''}
    ${question ? `<p>${escapeHtml(question)}</p>` : ''}`;
  return card;
}
