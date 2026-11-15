// 本次基础只活在当前页面内存中；刷新、换论文或返回页面都重新确认。
const FROM_BASICS = '这次请从零开始，不假定我已经掌握相关知识。先用简单例子补齐读懂论文必需的基础，再逐步回到原文。目标是看懂论文的问题、主要结论和必要的推导。';

export function createReadingContext(root, onChange) {
  const find = (name) => root.querySelector(`[data-context-${name}]`);
  const editor = find('editor'), input = find('input'), error = find('error');
  const summary = find('summary'), description = find('description');
  const confirm = find('confirm'), zero = find('zero'), edit = find('edit'), cancel = find('cancel');
  let required = false;
  let confirmed = null;
  let session = null;
  let editing = true;
  let busy = false;

  function render() {
    root.hidden = !required;
    editor.hidden = !editing;
    summary.hidden = editing;
    cancel.hidden = confirmed === null;
    for (const button of [confirm, zero, edit, cancel]) button.disabled = busy;
    input.disabled = busy;
    if (confirmed !== null) {
      const brief = confirmed.length > 90 ? `${confirmed.slice(0, 90)}…` : confirmed;
      description.textContent = `本次基础：${brief}`;
      description.title = confirmed;
    }
  }

  function accept(value) {
    if (busy) return;
    const text = value.trim();
    if (!text || Array.from(text).length > 2000) {
      error.textContent = '写一点你的基础和阅读目标（最多 2000 字），或选择“从基础讲起”。';
      input.focus();
      return;
    }
    confirmed = text;
    session = crypto.randomUUID();
    editing = false;
    error.textContent = '';
    // 这是当前页面的确认，不会自动调用模型。
    render();
    onChange();
  }

  confirm.addEventListener('click', () => accept(input.value));
  zero.addEventListener('click', () => accept(FROM_BASICS));
  edit.addEventListener('click', () => {
    if (busy) return;
    editing = true;
    input.value = confirmed || '';
    error.textContent = '';
    render();
    onChange();
    input.focus();
  });
  cancel.addEventListener('click', () => {
    if (busy || confirmed === null) return;
    editing = false;
    error.textContent = '';
    render();
    onChange();
  });
  input.addEventListener('input', () => { error.textContent = ''; });

  return {
    reset(needsConfirmation) {
      required = needsConfirmation;
      confirmed = null;
      session = needsConfirmation ? crypto.randomUUID() : null;
      editing = true;
      input.value = '';
      error.textContent = '';
      description.textContent = '';
      description.title = '';
      render();
      onChange();
    },
    ready: () => !required || (confirmed !== null && !editing),
    withRequest(request) {
      if (!required) return request;
      if (confirmed === null || editing) throw new Error('先确认本次阅读的基础。');
      return { ...request, reading_context: confirmed, reading_session: session };
    },
    setBusy(value) { busy = value; render(); },
  };
}
