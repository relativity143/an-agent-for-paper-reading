// 右侧：把讲解对象（docs/agent-architecture.md 4.2 节）显示成卡片。
// 讲解里的文字可能来自模型，一律先转义；缺少的可选字段不应让卡片崩掉。

import { renderInline, renderBlocks, typeset, escapeHtml } from './rich.js';

const STATUS_LABEL = {
  known: '你确认懂了',
  explained: '讲过',
  unsure: '仍不确定',
  new: '本轮基础',
};

// PDF 论文里，位置写成“PDF 第 2 页”“page 2”“p. 2”这样的文字。
const PDF_PAGE = /^(?:(?:PDF\s*)?第\s*([1-9][0-9]*)\s*页|(?:PDF\s+)?(?:page|p\.)\s*([1-9][0-9]*))/i;

function pageChip(page) {
  const n = Number(page);
  if (!Number.isInteger(n) || n < 1) return '';
  return `<button type="button" class="loc" data-page="${n}">第 ${n} 页</button>`;
}

function refChip(ref) {
  if (typeof ref !== 'string' || !ref) return '';
  const match = PDF_PAGE.exec(ref.trim());
  if (match) return pageChip(match[1] || match[2]);
  return `<button type="button" class="loc" data-loc="${escapeHtml(ref)}">${escapeHtml(ref)}</button>`;
}

// where 可以是位置对象 {ids, page, quote}，也可以是位置文字的数组。
function whereChips(where) {
  if (!where) return '';
  if (Array.isArray(where)) return where.map(refChip).join('');
  const chips = (where.ids || []).map(refChip);
  if (where.page) chips.push(pageChip(where.page));
  return chips.join('');
}

function locs(where) {
  const chips = whereChips(where);
  return chips ? `<p class="locs">${chips}</p>` : '';
}

function blocks(text) {
  return text ? renderBlocks(text) : '';
}

function section(title, body, extraClass = '') {
  if (!body) return '';
  return `<section class="card-sec ${extraClass}"><h4>${title}</h4>${body}</section>`;
}

function conceptHtml(concept, index) {
  const sources = (concept.sources || [])
    .filter((s) => s && /^https?:\/\//.test(s.url || ''))
    .map((s) => `<a href="${escapeHtml(s.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(s.title || s.url)}</a>`)
    .join('；');
  return `
    <li class="concept">
      <h5><span class="concept-no">${index + 1}</span>${renderInline(concept.name || '')}</h5>
      <div class="role">
        <span class="tag">在这篇文章里</span>
        ${blocks(concept.in_this_paper)}
        ${locs(concept.in_this_paper_where)}
      </div>
      <div class="itself">
        <span class="tag tag-plain">它本身</span>
        ${blocks(concept.itself)}
        ${sources ? `<p class="sources">来源：${sources}</p>` : ''}
      </div>
    </li>`;
}

function pointCard(x) {
  const assumes = (x.assumes || []).map((a) => `
    <li><span class="pill pill-${STATUS_LABEL[a.status] ? a.status : 'other'}">${STATUS_LABEL[a.status] || escapeHtml(String(a.status || ''))}</span>
    <span class="assume-name">${renderInline(a.concept || '')}</span>
    ${a.explained_at ? refChip(a.explained_at) : ''}
    ${a.note ? `<span class="assume-note">${renderInline(a.note)}</span>` : ''}</li>`).join('');
  const unexpanded = (x.unexpanded || []).map((t) => `<button type="button" class="term" data-term="${escapeHtml(t)}">${renderInline(t)}</button>`).join('');
  const progress = x.progress || {};
  const next = progress.next;
  const stepLine = Number.isInteger(progress.step)
    ? `<p class="step-line">主线第 ${progress.step} 步${Number.isInteger(progress.steps_total) ? `，共 ${progress.steps_total} 步` : ''}</p>`
    : '';
  const nextLine = next
    ? `<p class="next">接下来：${renderInline(next.point || '')} ${whereChips(next.where)}</p>`
    : '<p class="next">主线到这里讲完了。还有不明白的地方，可以继续提问。</p>';
  return `
    <header class="card-head">
      <p class="card-kind">这一点${x.section ? ` · ${escapeHtml(x.section)}` : ''}</p>
      <h3>${renderInline(x.point || '')}</h3>
      ${locs(x.where)}
    </header>
    ${section('', blocks(x.intro), 'intro')}
    ${section('这里默认你知道', assumes ? `<ul class="assumes">${assumes}</ul>` : '<p class="none">无新的缺口。</p>')}
    ${section('基础讲解', `<ol class="concepts">${(x.concepts || []).map(conceptHtml).join('')}</ol>${x.together ? `<div class="together"><span class="tag tag-plain">合起来</span>${blocks(x.together)}</div>` : ''}`)}
    ${section('回到原文', blocks(x.back_to_text))}
    ${section('用到但没展开的词', unexpanded ? `<p class="terms">${unexpanded}</p><p class="hint">不熟的话点一下，我来讲。</p>` : '<p class="none">无。</p>')}
    ${section('读到哪了', `${stepLine}${blocks(progress.text)}${nextLine}`, 'progress')}`;
}

function overviewCard(x) {
  const model = x.model || {};
  const items = (model.items || []).map((item) => `
    <li><strong>${escapeHtml(item.label || '')}：</strong>${renderInline(item.text || '')} <span class="locs">${whereChips(item.where)}</span></li>`).join('');
  const foundations = (x.foundations || []).map((f) => `<li><strong>${renderInline(f.name || '')}：</strong>${renderInline(f.note || '')}</li>`).join('');
  const previously = (x.previously || []).map((p) => `<li>${renderInline(p.name || '')}（${escapeHtml(p.paper || '')}${STATUS_LABEL[p.status] ? `，${STATUS_LABEL[p.status]}` : ''}）</li>`).join('');
  const route = x.route || {};
  // 路线图：每一步一个编号的站点，站点之间连一条线；名字单独一行，位置写在名字下面，再长也不会把名字挤窄。
  const steps = (route.steps || []).map((s, i) => {
    const number = Number.isInteger(s.step) ? s.step : i + 1;
    const range = s.range ? `<span class="route-range">${escapeHtml(s.range)}</span>` : '';
    return `<li data-step="${number}"><span class="route-mark" aria-hidden="true">${number}</span>`
      + `<span class="route-body"><span class="route-name"><span class="sr-only">第 ${number} 步：</span>${escapeHtml(s.name || '')}</span>${range}</span></li>`;
  }).join('');
  const question = x.question || {};
  const conclusions = x.conclusions || {};
  const next = x.next;
  return `
    <header class="card-head">
      <p class="card-kind">全文导读</p>
      <h3>${escapeHtml(x.title || '')}</h3>
    </header>
    ${section('这篇论文在问什么', `${blocks(question.text)}${locs(question.where)}`)}
    ${section('核心模型或实验', `${blocks(model.summary)}${items ? `<ul class="plain">${items}</ul>` : ''}`)}
    ${section('主要结论', `${blocks(conclusions.text)}${locs(conclusions.where)}`)}
    ${section('整篇依赖的基础', `${foundations ? `<ul class="plain">${foundations}</ul>` : ''}${previously ? `<p class="hint">已在前几篇补过的：</p><ul class="plain">${previously}</ul>` : ''}`)}
    ${section('阅读路线', `${steps ? `<ol class="route">${steps}</ol>` : ''}${route.skim ? `<p class="hint">${renderInline(route.skim)}</p>` : ''}`)}
    ${section('接下来', next ? `${blocks(next.text)}${locs(next.where)}` : '<p>可以点“继续”，从主线开始讲；也可以直接提问。</p>', 'progress')}`;
}

export function explanationCard(x) {
  const card = document.createElement('article');
  card.className = `card card-${x.type === 'overview' ? 'overview' : 'point'}`;
  card.dataset.id = x.id;
  card.tabIndex = -1;
  card.innerHTML = x.type === 'overview' ? overviewCard(x) : pointCard(x);
  typeset(card);
  return card;
}

export function pendingCard() {
  const card = document.createElement('div');
  card.className = 'card card-pending';
  card.innerHTML = '<p class="pending-text">正在准备这一轮讲解…</p>'
    + '<button type="button" class="btn btn-small pending-stop" hidden>停止这一轮</button>';
  return card;
}

// 出错提示。可以带一个按钮（如“恢复”“再试一次”）。
// action 可以是一个按钮，也可以是几个按钮的数组。
export function errorCard(message, action) {
  const card = document.createElement('div');
  card.className = 'card card-error';
  card.setAttribute('role', 'alert');
  const text = document.createElement('p');
  text.textContent = message;
  card.append(text);
  const actions = [].concat(action || []);
  if (actions.length) {
    const row = document.createElement('div');
    row.className = 'card-actions';
    for (const item of actions) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'btn btn-small';
      button.textContent = item.label;
      button.addEventListener('click', () => item.run(card));
      row.append(button);
    }
    card.append(row);
  }
  return card;
}

export function noteCard(message) {
  const card = document.createElement('div');
  card.className = 'card card-note';
  const text = document.createElement('p');
  text.textContent = message;
  card.append(text);
  return card;
}

// 读者的提问。where 是 HTML 论文里的位置编号，page 是 PDF 的页码。
export function questionCard({ quote, question, page, where }) {
  const card = document.createElement('div');
  card.className = 'card card-question';
  const place = `${where ? refChip(where) : ''}${page ? pageChip(page) : ''}`;
  card.innerHTML = `
    <p class="card-kind">你的问题</p>
    ${quote ? `<blockquote>${escapeHtml(quote)}</blockquote>` : ''}
    ${question ? `<p>${escapeHtml(question)}</p>` : ''}
    ${place ? `<p class="locs">${place}</p>` : ''}`;
  return card;
}

// 卡片已经显示，但“已显示”没有记到后端时，在卡片底部给出提示和重试。
export function setReceiptWarning(card, retry) {
  card.querySelector('.receipt-warning')?.remove();
  if (!retry) return;
  const box = document.createElement('div');
  box.className = 'receipt-warning';
  box.setAttribute('role', 'alert');
  const text = document.createElement('span');
  text.textContent = '记录显示状态失败。讲解内容没有丢，记上之后才能继续往下讲。';
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'btn btn-small';
  button.textContent = '重试';
  button.addEventListener('click', retry);
  box.append(text, button);
  card.append(box);
}
