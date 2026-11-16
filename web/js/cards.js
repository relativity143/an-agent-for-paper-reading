// 右侧：把讲解对象（docs/agent-architecture.md 4.2 节）显示成卡片。
// 讲解里的文字可能来自模型，一律先转义；缺少的可选字段不应让卡片崩掉。

import { renderInline, renderBlocks, typeset, escapeHtml } from './rich.js';

// 和概念记录里的说法一致（record.js）：已经会、讲过、还不懂；记录里没有的是这一轮要补的。
const STATUS_LABEL = {
  known: '已经会',
  explained: '讲过',
  unsure: '还不懂',
  new: '这一轮补',
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

// ---- 读完之后的四个阶段（接口约定 4.10 节） ----

const GLOSSARY_STATUS = { known: '已经会', explained: '讲过', unsure: '还不懂', untaught: '未讲' };
const ROLE_LABEL = { foundation: '奠基工作', predecessor: '直接前作', contrast: '对照工作' };
const KIND_LABEL = { notes: '讲义或综述', foundation: '奠基论文', recent: '相关的新工作' };
const CHECKED_LABEL = { citation: '只看了本文里引用它的地方', abstract: '核对过它的摘要', fulltext: '核对过它的全文' };

// 位置文字（“S2.SS2.p1”“PDF 第 2 页”）：先当普通文字放着，页面再把认得出来的变成可以点的。
function whereText(text) {
  return text ? `<span class="where-text">${escapeHtml(text)}</span>` : '';
}

// 一条路线式的清单：编号、名字、范围。导读的阅读路线、完整讲解的目录、主线上还没讲到的步骤都用它。
function routeList(items, extraClass) {
  const rows = items.map((item) => {
    const range = item.range ? `<span class="route-range">${escapeHtml(item.range)}</span>` : '';
    return `<li data-step="${item.n}"><span class="route-mark" aria-hidden="true">${item.n}</span>`
      + `<span class="route-body"><span class="route-name">${escapeHtml(item.name || '')}</span>${range}</span></li>`;
  }).join('');
  return rows ? `<ol class="route ${extraClass}">${rows}</ol>` : '';
}

function recapCard(x) {
  const point = (c) => `<li><strong>${renderInline(c.point || '')}</strong> ${whereChips(c.where)}`
    + `${(c.foundations || []).length ? `<br><span class="recap-found">补了：${c.foundations.map((f) => renderInline(f)).join('、')}</span>` : ''}`
    + `${c.back ? `<br>${renderInline(c.back)}` : ''}</li>`;
  const steps = (x.steps || []).map((s) => `
    <div class="recap-step"><p class="step-line">第 ${escapeHtml(String(s.step))} 步：${escapeHtml(s.name || '')}</p>
    <ul class="recap-points">${(s.covered || []).map(point).join('')}</ul></div>`).join('');
  const off = (x.off_route || []).length
    ? `<div class="recap-step"><p class="step-line">主线之外</p><ul class="recap-points">${x.off_route.map(point).join('')}</ul></div>` : '';
  const remaining = routeList((x.remaining || []).map((r) => ({ n: r.step, name: r.name, range: r.range })), 'route-plain');
  const rows = (x.glossary || []).map((g) => `
    <tr><th scope="row">${renderInline(g.term || '')}
    <span class="pill pill-${g.status === 'untaught' ? 'other' : escapeHtml(g.status || 'other')}">${GLOSSARY_STATUS[g.status] || escapeHtml(String(g.status || ''))}</span></th>
    <td>${renderInline(g.meaning || '')}</td>
    <td>${whereText(g.first_seen)}</td><td>${g.explained_at ? whereText(g.explained_at) : '—'}</td></tr>`).join('');
  const glossary = rows ? `<div class="table-wrap"><table class="glossary">
    <thead><tr><th scope="col">术语或符号</th><th scope="col">本文中的意思</th><th scope="col">首次出现</th><th scope="col">讲过的位置</th></tr></thead>
    <tbody>${rows}</tbody></table></div>` : '';
  return `
    <header class="card-head"><p class="card-kind">第一部分回顾</p><h3>前面讲过什么</h3></header>
    ${x.note ? section('', blocks(x.note), 'intro') : ''}
    ${section('沿着主线', steps + off)}
    ${section('主线上还没讲到的', remaining ? `${remaining}<p class="hint">完整讲解会补上这些部分。</p>` : '')}
    ${section(`本篇术语表（${(x.glossary || []).length} 条）`, glossary ? `${glossary}<p class="hint">“已经会／讲过／还不懂”取自你的概念记录，在右上角的“记录”里可以改。</p>` : '')}
    ${section('接下来', blocks(x.next), 'progress')}`;
}

function deepOutlineCard(x) {
  const list = routeList((x.sections || []).map((s) => ({ n: s.n, name: s.title, range: s.range })), 'route-plain');
  return `
    <header class="card-head"><p class="card-kind">完整讲解 · 目录</p><h3>严格地再讲一遍</h3></header>
    ${section('范围', blocks(x.scope), 'intro')}
    ${section(`分 ${(x.sections || []).length} 段`, list)}
    ${section('接下来', '<p>点“下一段”，从第 1 段开始。这一遍不再为读懂而简化；哪里卡住了直接提问，答完再接着讲。</p>', 'progress')}`;
}

function deepCard(x) {
  const assumes = (x.assumes || []).map((a) => `
    <li><span class="pill pill-${STATUS_LABEL[a.status] ? a.status : 'other'}">${STATUS_LABEL[a.status] || escapeHtml(String(a.status || ''))}</span>
    <span class="assume-name">${renderInline(a.concept || '')}</span>
    ${a.explained_at ? refChip(a.explained_at) : ''}
    ${a.note ? `<span class="assume-note">${renderInline(a.note)}</span>` : ''}</li>`).join('');
  const foundations = (x.foundations || []).map((f) => `
    <li><strong>${renderInline(f.name || '')}</strong>
    <span class="pill ${f.taught ? 'pill-new' : ''}">${f.taught ? '这里补讲' : '前面讲过'}</span>
    ${!f.taught && f.explained_at ? whereText(f.explained_at) : ''}
    ${f.use ? `<div>${renderInline(f.use)}</div>` : ''}
    ${f.taught && f.itself ? `<div class="deep-itself">${renderBlocks(f.itself)}</div>` : ''}</li>`).join('');
  const simplified = (x.simplified_before || []).map((t) => `<li>${renderInline(t)}</li>`).join('');
  const evidence = (x.evidence || []).map((e) => `
    <li><p><strong>${renderInline(e.claim || '')}</strong></p>
    <dl class="evidence"><dt>依据</dt><dd>${renderInline(e.support || '')}</dd>
    <dt>支持到什么程度</dt><dd>${renderInline(e.strength || '')}</dd>
    <dt>局限</dt><dd>${renderInline(e.limits || '')}</dd></dl></li>`).join('');
  const unexpanded = (x.unexpanded || []).map((t) => `<button type="button" class="term" data-term="${escapeHtml(t)}">${renderInline(t)}</button>`).join('');
  const last = x.section === x.sections_total;
  return `
    <header class="card-head">
      <p class="card-kind">完整讲解 · 第 ${escapeHtml(String(x.section))} 段，共 ${escapeHtml(String(x.sections_total))} 段</p>
      <h3>${renderInline(x.title || '')}</h3>
      <p class="locs">${whereText(x.range)}${whereChips(x.where)}</p>
    </header>
    ${section('先说结论', blocks(x.picture), 'intro')}
    ${assumes ? section('这里默认你知道', `<ul class="assumes">${assumes}</ul>`) : ''}
    ${foundations ? section('这一段用到的基础', `<ul class="deep-foundations">${foundations}</ul>`) : ''}
    ${section('正文', blocks(x.body), 'deep-body')}
    ${simplified ? section('前面说得简单的地方', `<ul>${simplified}</ul>`) : ''}
    ${evidence ? section('结论由什么支持', `<ol class="evidence-list">${evidence}</ol>`) : ''}
    ${unexpanded ? section('用到但没展开的词', `<p class="terms">${unexpanded}</p><p class="hint">不熟的话点一下，我来讲。</p>`) : ''}
    ${section('讲到哪了', `${blocks((x.progress || {}).text)}${last ? '<p class="next">完整讲解到这里结束。看懂了多少由你自己判断，我不出题。</p>' : ''}`, 'progress')}`;
}

function sourceLine(item) {
  const checked = CHECKED_LABEL[item.checked] || '没有说明核对到哪一步';
  const link = item.url ? ` <a href="${escapeHtml(item.url)}" target="_blank" rel="noopener noreferrer">打开来源</a>` : '';
  return `<p class="source-line">${item.ref ? `参考文献 ${escapeHtml(item.ref)} · ` : ''}${checked}${link}</p>`;
}

function positionCard(x) {
  const items = (x.items || []).map((item) => `
    <li><p><span class="pill">${ROLE_LABEL[item.role] || escapeHtml(String(item.role || ''))}</span> <strong>${renderInline(item.name || '')}</strong></p>
    <p>${renderInline(item.what || '')}</p>
    <p>${renderInline(item.relation || '')}</p>
    <p class="hint">${item.said_by === 'source' ? '这是核对过来源后的说法。' : '这是本文作者对它的说法，没有另外核对。'}</p>
    ${sourceLine(item)}</li>`).join('');
  return `
    <header class="card-head"><p class="card-kind">本篇在领域中的位置</p><h3>它依赖和对照的工作</h3></header>
    ${section('', items ? `<ul class="field-list">${items}</ul>` : '<p class="none">没有核实得了的条目，这次不列。</p>')}
    ${x.note ? section('说明', blocks(x.note)) : ''}`;
}

function nextReadingCard(x) {
  const items = (x.items || []).map((item) => `
    <li><p><span class="pill">${KIND_LABEL[item.kind] || escapeHtml(String(item.kind || ''))}</span> <strong>${renderInline(item.name || '')}</strong></p>
    <p>${renderInline(item.why || '')}</p>
    ${(item.missing || []).length ? `<p class="hint">按你的记录，读它之前可能还缺：${item.missing.map((m) => renderInline(m)).join('、')}（暂定）</p>` : ''}
    ${sourceLine(item)}</li>`).join('');
  return `
    <header class="card-head"><p class="card-kind">下一篇读什么</p><h3>最多三篇</h3></header>
    ${section('', items ? `<ol class="field-list">${items}</ol>` : '<p class="none">没有核实得了的推荐，这次不列。</p>')}
    ${x.note ? section('说明', blocks(x.note)) : ''}`;
}

const CARD_BUILDERS = {
  overview: overviewCard, point: pointCard, recap: recapCard, deep_outline: deepOutlineCard,
  deep: deepCard, position: positionCard, next_reading: nextReadingCard,
};

export function explanationCard(x) {
  const card = document.createElement('article');
  // 不认识的类型按逐点讲解显示，至少不丢内容。
  const type = CARD_BUILDERS[x.type] ? x.type : 'point';
  card.className = `card card-${type.replace('_', '-')}`;
  card.dataset.id = x.id;
  card.tabIndex = -1;
  card.innerHTML = CARD_BUILDERS[type](x);
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
