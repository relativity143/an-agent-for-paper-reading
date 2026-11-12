// 把带 $...$ 公式和少量 Markdown 的文字变成安全的 HTML。
// 论文和讲解的文字都当作数据：先转义，再只放行这里认识的几种标记。

const MARK_OPEN = '';
const MARK_CLOSE = '';

function escapeHtml(text) {
  return text.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function inlineMarkdown(escaped) {
  return escaped
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
    .replaceAll(MARK_OPEN, '<mark>')
    .replaceAll(MARK_CLOSE, '</mark>');
}

function mathSpan(tex, display) {
  const tag = display ? 'div' : 'span';
  return `<${tag} class="math${display ? ' math-display' : ''}" data-tex="${escapeHtml(tex)}">${escapeHtml(tex)}</${tag}>`;
}

// 行内文字：公式原样保留给 KaTeX，其余部分转义后处理标记。
export function renderInline(text) {
  const parts = String(text).split(/(\$[^$\n]+\$)/g);
  return parts.map((part) => {
    if (part.length > 2 && part.startsWith('$') && part.endsWith('$')) {
      return mathSpan(part.slice(1, -1), false);
    }
    return inlineMarkdown(escapeHtml(part));
  }).join('');
}

// 多段文字：空行分段。
export function renderBlocks(text) {
  return String(text).split(/\n{2,}/).map((p) => `<p>${renderInline(p.trim())}</p>`).join('');
}

// 在一段原文里标出读者选中的片段。片段跨过公式时不标，退回整段高亮。
export function markQuote(text, quote) {
  if (!quote || quote.includes('$') || quote.includes('⟨')) return text;
  const at = text.indexOf(quote);
  if (at < 0) return text;
  const before = text.slice(0, at);
  if ((before.match(/\$/g) || []).length % 2 === 1) return text;
  return before + MARK_OPEN + quote + MARK_CLOSE + text.slice(at + quote.length);
}

export function displayMath(tex) {
  return mathSpan(tex, true);
}

const MACROS = { '\\nicefrac': '\\frac{#1}{#2}' };

// 用 KaTeX 排公式。库没加载到时保留 LaTeX 原文，页面照常可读。
export function typeset(root) {
  if (!window.katex) return;
  root.querySelectorAll('.math[data-tex]:not([data-done])').forEach((el) => {
    try {
      window.katex.render(el.dataset.tex, el, {
        displayMode: el.classList.contains('math-display'),
        throwOnError: false,
        macros: MACROS,
      });
      el.dataset.done = '1';
    } catch { /* 保留原文 */ }
  });
}

export { escapeHtml };
