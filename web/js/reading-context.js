// 本文清单不表示读者掌握；选择只在本页保留，确认后才写入概念记录。
const LABELS = { known: '已经会', basics: '需要补', unsure: '不确定' };
function node(tag, text, className) {
  const result = document.createElement(tag);
  if (text !== undefined) result.textContent = text;
  if (className) result.className = className;
  return result;
}

export function createReadingContext(root, { api, paper, setBusy, saveChoices, onChange, jump }) {
  const find = (name) => root.querySelector(`[data-context-${name}]`);
  const editor = find('editor'), choices = find('choices'), list = find('list');
  const input = find('input'), error = find('error'), progress = find('progress');
  const summary = find('summary'), description = find('description');
  const prepare = find('prepare'), stop = find('stop');
  const confirm = find('confirm'), zero = find('zero'), edit = find('edit'), cancel = find('cancel');
  let required = false, busy = false, editing = true, loading = false;
  let confirmed = null, session = null, items = [], selected = [], savedSelection = [], savedGoal = '';
  let task = null, stopping = false, preparing = false, generation = 0;

  function render() {
    root.hidden = !required;
    editor.hidden = !editing;
    summary.hidden = editing;
    choices.hidden = !items.length;
    prepare.hidden = Boolean(items.length);
    prepare.disabled = busy || loading || Boolean(task);
    prepare.textContent = loading ? '正在读取本文基础…' : '先看看需要哪些基础';
    stop.hidden = !task;
    stop.disabled = stopping;
    stop.textContent = stopping ? '正在停止…' : '停止分析';
    cancel.hidden = confirmed === null;
    for (const button of [confirm, zero, edit, cancel]) button.disabled = busy;
    input.disabled = busy;
    list.querySelectorAll('input').forEach((element) => { element.disabled = busy; });
    if (confirmed !== null) {
      description.textContent = `本次基础已确认：${savedSelection.filter((s) => s === 'known').length} 项已经会，${savedSelection.filter((s) => s === 'basics').length} 项需要补，${savedSelection.filter((s) => s === 'unsure').length} 项不确定。`;
      description.title = confirmed;
    }
  }

  function drawList() {
    list.replaceChildren(...items.map((item, index) => {
      const row = node('fieldset', undefined, 'prerequisite-item');
      row.append(node('legend', item.name));
      row.append(node('p', `是什么：${item.description}`), node('p', `本文用在哪里：${item.why}`));
      const place = node('button', item.where.page ? `原文第 ${item.where.page} 页` : `查看原文（${item.where.ids.join('、')}）`, 'loc');
      place.type = 'button';
      place.addEventListener('click', () => jump(item.where));
      row.append(place);
      const options = node('div', undefined, 'prerequisite-options');
      for (const [value, label] of Object.entries(LABELS)) {
        const option = node('label'), radio = node('input');
        radio.type = 'radio'; radio.name = `prerequisite-${index}`; radio.value = value;
        radio.checked = selected[index] === value;
        radio.addEventListener('change', () => { selected[index] = value; error.textContent = ''; });
        option.append(radio, document.createTextNode(label)); options.append(option);
      }
      row.append(options);
      return row;
    }));
    render();
  }

  function adopt(result) {
    if (!result || result.paper !== paper() || result.ready !== true || !Array.isArray(result.items)
      || result.items.length < 1 || result.items.length > 8
      || result.items.some((item) => !item || ['name', 'description', 'why'].some((key) => typeof item[key] !== 'string' || !item[key].trim())
        || !item.where || !Array.isArray(item.where.ids) || !(item.where.ids.length || (Number.isInteger(item.where.page) && item.where.page > 0)))) {
      throw new Error('本文基础清单不完整，暂时不能开始阅读。请重新分析。');
    }
    items = result.items;
    selected = items.map(() => null);
    drawList();
  }
  const failure = (reason) => reason.status === 404 ? '当前后端还不支持本文基础分析，请更新后端后重新打开。'
    : reason.message || '没能整理出本文基础，请稍后重试。';

  async function load() {
    if (!required || preparing) return;
    const version = generation;
    loading = true; render();
    try {
      const result = await api().getPrerequisites(paper());
      if (version === generation && result.ready) { adopt(result); task = null; }
    } catch (reason) { if (version === generation) error.textContent = failure(reason); }
    finally { if (version === generation) { loading = false; render(); onChange(); } }
  }

  prepare.addEventListener('click', async () => {
    if (busy || loading || task || items.length) return;
    const version = generation;
    preparing = true;
    error.textContent = ''; progress.textContent = '正在阅读本文并整理必需的基础…'; setBusy(true);
    try {
      const result = await api().preparePrerequisites(paper(), (text) => {
        if (version === generation) progress.textContent = text;
      }, (id) => { if (version === generation) { task = id; render(); } });
      if (version !== generation) return;
      adopt(result); progress.textContent = ''; task = null;
    } catch (reason) {
      if (version !== generation) return;
      const cleanupPending = /尚未完全(?:退出|停止)/.test(reason.message || '');
      error.textContent = cleanupPending ? failure(reason)
        : stopping && reason.kind === 'model' ? '已停止分析，尚未写入阅读记录。可以换模型后重试。'
        : reason.kind === 'disconnected' ? `${failure(reason)}分析可能仍在运行，请先点击“停止分析”再重试。` : failure(reason);
      progress.textContent = '';
      if (reason.kind !== 'disconnected' && !cleanupPending) task = null;
    } finally { if (version === generation) { preparing = false; stopping = false; setBusy(false); render(); onChange(); } }
  });
  stop.addEventListener('click', async () => {
    if (!task || stopping) return;
    const stoppingTask = task;
    stopping = true; render();
    try {
      await api().cancelTask(stoppingTask);
      if (!busy) { task = null; stopping = false; progress.textContent = ''; error.textContent = '已停止分析，可以重新开始。'; render(); }
    } catch (reason) {
      // 取消响应和 SSE 终态可能交错；取消失败仍须保留原任务的再次停止入口。
      task = stoppingTask; stopping = false; error.textContent = failure(reason); render();
    }
  });
  confirm.addEventListener('click', async () => {
    if (busy || !items.length) return;
    if (selected.some((value) => !value)) {
      error.textContent = '请为每项基础选择“已经会／需要补／不确定”，也可以选择全部从基础讲起。';
      list.querySelectorAll('fieldset')[selected.findIndex((value) => !value)].querySelector('input').focus(); return;
    }
    const goal = input.value.trim();
    const version = generation;
    const context = ['本文所需基础由读者本次逐项确认：', ...items.map((item, index) => `${item.name}：${LABELS[selected[index]]}。`),
      '已经会的可直接使用；需要补的先讲必要基础；不确定的先做简短确认，不假定掌握。', ...(goal ? [`本次阅读目标：${goal}`] : [])].join('\n');
    if (Array.from(context).length > 2000) { error.textContent = '阅读目标太长，请缩短后再确认（本次基础与目标合计最多 2000 字）。'; return; }
    error.textContent = ''; progress.textContent = '正在保存本次选择…'; setBusy(true);
    try {
      await saveChoices(items.map((item, index) => ({ name: item.name, choice: selected[index] })));
      if (version !== generation) return;
      confirmed = context; session = crypto.randomUUID(); savedSelection = [...selected]; savedGoal = goal; editing = false;
    } catch (reason) { error.textContent = failure(reason); }
    finally { progress.textContent = ''; setBusy(false); render(); onChange(); }
  });
  zero.addEventListener('click', () => { if (!busy) { selected = items.map(() => 'basics'); error.textContent = ''; drawList(); } });
  edit.addEventListener('click', () => {
    if (busy) return;
    editing = true; selected = [...savedSelection]; input.value = savedGoal; error.textContent = ''; drawList(); onChange();
  });
  cancel.addEventListener('click', () => {
    if (busy || confirmed === null) return;
    editing = false; selected = [...savedSelection]; input.value = savedGoal; error.textContent = ''; render(); onChange();
  });
  input.addEventListener('input', () => { error.textContent = ''; });
  return {
    reset(needsConfirmation) {
      // 前进/后退缓存会保留 JS 和正在运行的任务；保留它的停止入口与完成回调。
      if (!preparing && !task) generation += 1;
      required = needsConfirmation;
      confirmed = null; session = null; items = []; selected = []; savedSelection = []; savedGoal = '';
      editing = true; loading = false;
      input.value = ''; error.textContent = ''; description.textContent = ''; description.title = '';
      if (!preparing && !task) { stopping = false; progress.textContent = ''; }
      list.replaceChildren(); render(); onChange();
    },
    load,
    ready: () => !required || (confirmed !== null && !editing),
    withRequest(request) {
      if (!required) return request;
      if (confirmed === null || editing) throw new Error('先查看本文基础并确认本次选择。');
      return { ...request, reading_context: confirmed, reading_session: session };
    },
    setBusy(value) { busy = value; render(); },
  };
}
