// 概念记录与反馈（docs/agent-architecture.md 4.7 节）。
// 每篇论文一份记录：讲过哪些概念、读者说哪些还不懂、哪些已经会。每轮讲解和每次反馈都会改它，
// 下一轮讲解按最新的记录来讲。打开论文时记录不是空的，要读者先看过、确认或修改后才使用。
// 概念名和备注来自模型或读者，只当作文字显示。

import { renderInline, typeset } from './rich.js';

export const STATUS_LABEL = { explained: '讲过', unsure: '还不懂', known: '已经会' };
const STATUS_ORDER = ['unsure', 'explained', 'known'];
const MAX_NAME = 80;
const MAX_NOTE = 200;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

// 概念名里可能有公式（$k$）：按讲解卡片里同样的方式显示。renderInline 会先转义。
function rich(tag, className, text) {
  const node = el(tag, className);
  node.innerHTML = renderInline(text);
  typeset(node);
  return node;
}

function statusSelect(value) {
  const select = el('select', 'record-status');
  select.setAttribute('aria-label', '状态');
  for (const status of STATUS_ORDER) {
    const option = el('option', '', STATUS_LABEL[status]);
    option.value = status;
    option.selected = status === value;
    select.append(option);
  }
  return select;
}

// api() 返回接口对象；paper() 返回论文编号；busy() 返回是否正在生成；
// onChange() 在“能不能继续”变化时调用；jump(where) 让左边的原文跳到一个位置，跳成了返回 true。
export function createRecord({ api, paper, busy, onChange, jump }) {
  const button = document.getElementById('record-btn');
  const dialog = document.getElementById('record-dialog');
  const dialogBody = document.getElementById('record-body');
  const confirmBox = document.getElementById('record-confirm');
  let record = null;     // null：后端没有这个功能，或者还没读到
  let confirmed = true;  // 打开时记录不为空，要先确认
  let gateVisible = true, conflict = false;
  let chosenNames = new Set();
  const chosenRows = () => record.concepts.filter((c) => chosenNames.has(c.name.toLowerCase())).map((c) => ({ name: c.name, status: c.status, note: c.note || null }));

  const supported = () => record !== null;

  function showButton() {
    button.hidden = !supported();
    if (!supported()) return;
    const count = record.concepts.length;
    button.textContent = count ? `记录 ${count}` : '记录';
    button.title = '讲过的概念，和你说还不懂、已经会的词。下一轮讲解按它来讲。';
  }

  function adopt(next) {
    if (next && Array.isArray(next.concepts) && Number.isInteger(next.version)) record = next;
    showButton();
  }

  // 记录的编辑区：对话框和打开时的确认用同一份。返回 { root, rows() }。
  function editor(excluded = new Set()) {
    const root = el('div', 'record-editor');
    const list = el('ul', 'record-list');
    const empty = el('p', 'hint', '还没有记录。每讲完一轮，讲过的概念会自动记在这里。');
    const draft = record.concepts.filter((c) => !excluded.has(c.name.toLowerCase())).map((c) => ({ name: c.name, status: c.status, note: c.note || '', where: c.where || null }));

    function row(item) {
      const li = el('li', 'record-item');
      const head = el('div', 'record-item-head');
      head.append(rich('span', 'record-name', item.name));
      if (item.where) {
        const where = el('button', 'loc', item.where);
        where.type = 'button';
        where.title = '看这个概念是在原文哪里讲的';
        where.addEventListener('click', () => { if (jump(item.where) && dialog.open) dialog.close(); });
        head.append(where);
      }
      const select = statusSelect(item.status);
      select.addEventListener('change', () => { item.status = select.value; });
      const remove = el('button', 'icon-btn', '×');
      remove.type = 'button';
      remove.setAttribute('aria-label', `从记录里删掉“${item.name}”`);
      remove.addEventListener('click', () => { draft.splice(draft.indexOf(item), 1); draw(); });
      head.append(select, remove);
      const note = el('input', 'record-note');
      note.type = 'text';
      note.maxLength = MAX_NOTE;
      note.placeholder = '备注（可不填），比如哪里没懂';
      note.setAttribute('aria-label', `“${item.name}”的备注`);
      note.value = item.note;
      note.addEventListener('input', () => { item.note = note.value; });
      li.append(head, note);
      return li;
    }

    function draw() {
      empty.hidden = draft.length > 0;
      list.replaceChildren(...draft.map(row));
    }

    const add = el('form', 'record-add');
    const name = el('input');
    name.type = 'text';
    name.maxLength = MAX_NAME;
    name.placeholder = '加一个词';
    name.setAttribute('aria-label', '要加进记录的词');
    const status = statusSelect('known');
    const addBtn = el('button', 'btn btn-small', '添加');
    addBtn.type = 'submit';
    const addNote = el('p', 'status record-add-status');
    addNote.setAttribute('role', 'status');
    add.append(name, status, addBtn);
    add.addEventListener('submit', (event) => {
      event.preventDefault();
      const text = name.value.trim();
      if (!text) return;
      if (excluded.has(text.toLowerCase())) { addNote.textContent = '这项已在本次基础中确认，请通过“修改本次基础”调整。'; return; }
      const same = draft.find((item) => item.name.toLowerCase() === text.toLowerCase());
      if (same) {
        same.status = status.value;
        addNote.textContent = `“${same.name}”已经在记录里，状态改成了“${STATUS_LABEL[status.value]}”。`;
      } else {
        draft.unshift({ name: text, status: status.value, note: '', where: null });
        addNote.textContent = '';
      }
      name.value = '';
      draw();
    });

    draw();
    root.append(empty, list, add, addNote);
    return {
      root,
      rows: () => draft.map((item) => ({ name: item.name, status: item.status, note: item.note.trim() || null })),
    };
  }

  function changed(rows) {
    const before = record.concepts.map((c) => [c.name, c.status, c.note || null]);
    const after = rows.map((r) => [r.name, r.status, r.note]);
    return JSON.stringify(before) !== JSON.stringify(after);
  }

  // 保存整份记录。别处改过（409）时重新读取，让读者在新的内容上再改。
  async function save(rows, status) {
    if (busy()) { status.textContent = '正在生成讲解，等这一轮结束或停止后再改记录。'; return false; }
    status.textContent = '正在保存…';
    try {
      adopt(await api().saveRecord(paper(), record.version, rows));
      status.textContent = '';
      return true;
    } catch (error) {
      if (error.status === 409 && !busy()) {
        await refresh();
        status.textContent = '记录在别处改过了，已经读到最新的，请在它上面再改一次。';
      } else {
        status.textContent = error.message || '没能保存记录。';
      }
      return false;
    }
  }

  function openDialog() {
    if (!supported()) return;
    const view = editor();
    const status = el('p', 'status');
    status.setAttribute('role', 'status');
    const saveBtn = el('button', 'btn btn-primary', '保存');
    saveBtn.type = 'button';
    saveBtn.addEventListener('click', async () => {
      const rows = view.rows();
      if (!changed(rows)) { dialog.close(); return; }
      saveBtn.disabled = true;
      const ok = await save(rows, status);
      saveBtn.disabled = false;
      if (ok) dialog.close();
      else if (status.textContent.startsWith('记录在别处')) openDialog();
    });
    dialogBody.replaceChildren(
      el('p', 'hint', '下一轮讲解按这份记录来讲：“已经会”的直接用，不再铺垫；“还不懂”的用到时换一种讲法重讲；“讲过”的接着用，只带一句提醒。不对的地方直接改。'),
      view.root, saveBtn, status,
    );
    if (!dialog.open) dialog.showModal();
  }

  // 打开论文时：记录不是空的，先摆出来让读者确认。
  function showConfirm() {
    confirmBox.hidden = confirmed || (!gateVisible && !conflict);
    if (confirmed) { confirmBox.replaceChildren(); return; }
    const view = editor(chosenNames);
    const status = el('p', 'status');
    status.setAttribute('role', 'status');
    const keep = el('button', 'btn btn-primary', '按这份记录继续');
    keep.type = 'button';
    const clear = el('button', 'btn', chosenNames.size ? '清空其余旧记录' : '清空，从头开始');
    clear.type = 'button';
    const done = () => { confirmed = true; conflict = false; showConfirm(); onChange(); };
    keep.addEventListener('click', async () => {
      if (busy()) { status.textContent = '正在处理本次请求，请稍后再确认记录。'; return; }
      const rows = [...chosenRows(), ...view.rows()];
      if (!changed(rows) || await save(rows, status)) done();
      else if (status.textContent.startsWith('记录在别处')) showConfirm();
    });
    clear.addEventListener('click', async () => {
      if (await save(chosenRows(), status)) done();
    });
    const actions = el('div', 'record-confirm-actions');
    actions.append(keep, clear);
    confirmBox.replaceChildren(
      el('h2', '', '上次读这篇时的记录'),
      el('p', 'hint', chosenNames.size ? '这些是本次基础清单以外的旧记录，请另行核对后确认。刚才选过的基础会保留。' : '讲解会按这份记录来讲。先看一遍：已经会了的改成“已经会”，其实没懂的改成“还不懂”，不想要的删掉。确认之后才会使用。'),
      view.root, actions, status,
    );
  }

  // 重新读取记录；同一页面里读下去不用再确认。
  async function refresh() {
    if (!supported()) return;
    try { adopt(await api().getRecord(paper())); } catch { /* 读不到时留着原来的；版本不对后端会拒绝，不会用错 */ }
  }

  async function load() {
    record = null;
    confirmed = true;
    chosenNames = new Set();
    conflict = false;
    const source = api();
    if (source && source.getRecord) {
      try { adopt(await source.getRecord(paper())); } catch { record = null; }
    }
    confirmed = !supported() || record.concepts.length === 0;
    showButton();
    showConfirm();
    onChange();
  }

  // 只改变明确选择过的本文基础；其他条目与原来源由完整列表及后端共同保留。
  async function applyPrerequisites(choices) {
    if (!supported()) throw new Error('概念记录暂时无法读取，请重新打开论文后再确认。');
    const rows = record.concepts.map((c) => ({ name: c.name, status: c.status, note: c.note || null }));
    for (const { name, choice } of choices) {
      let row = rows.find((c) => c.name.toLowerCase() === name.toLowerCase());
      if (!row) { row = { name, status: 'unsure', note: null }; rows.push(row); }
      row.status = choice === 'known' ? 'known' : 'unsure';
      const label = { known: '已经会', basics: '需要补', unsure: '不确定' }[choice];
      const marker = `本次基础自评：${label}`;
      const previous = (row.note || '').replace(/(?:\n|^)本次基础自评：(?:已经会|需要补|不确定)/g, '').trim();
      row.note = previous ? `${Array.from(previous).slice(0, MAX_NOTE - marker.length - 1).join('')}\n${marker}` : marker;
    }
    try {
      adopt(await api().saveRecord(paper(), record.version, rows));
      chosenNames = new Set(choices.map((c) => c.name.toLowerCase()));
      if (!record.concepts.some((c) => !chosenNames.has(c.name.toLowerCase()))) confirmed = true;
      conflict = false;
      showConfirm();
      onChange();
    } catch (error) {
      if (error.status === 409) {
        await load();
        if (!supported()) throw new Error('记录已改变，但最新记录暂时无法读取。本次选择没有保存，请重新打开论文后再试。');
        conflict = true;
        confirmed = false;
        showConfirm();
        onChange();
        throw new Error('记录已在别处修改，本次选择没有保存。请核对下方最新记录，再重新确认本次基础。');
      }
      throw error;
    }
  }

  // 讲解卡片下面的反馈：还是不懂、卡在这个词、这个我会。isLatest() 说明这张卡片是不是最新的一轮。
  function feedbackBar(explanation, isLatest) {
    const bar = el('div', 'feedback');
    const buttons = el('div', 'feedback-buttons');
    buttons.append(el('span', 'feedback-label', '这一轮讲得怎样？'));
    const picker = el('div', 'feedback-pick');
    picker.hidden = true;
    const status = el('p', 'status feedback-status');
    status.setAttribute('role', 'status');

    const terms = [];
    for (const name of [
      ...(explanation.concepts || []).map((c) => c && c.name),
      ...(explanation.assumes || []).map((a) => a && a.concept),
      ...(explanation.unexpanded || []),
    ]) {
      if (typeof name === 'string' && name.trim() && !terms.includes(name.trim())) terms.push(name.trim());
    }

    async function send(kind, concept) {
      if (busy()) { status.textContent = '正在生成讲解，等这一轮结束再反馈。'; return; }
      status.textContent = '正在记下…';
      try {
        const body = { explanation: explanation.id, kind };
        if (concept) body.concept = concept;
        adopt(await api().sendFeedback(paper(), body));
        picker.hidden = true;
        if (kind === 'unclear') {
          status.textContent = isLatest()
            ? '已记下。点“继续”，这一点会换一种讲法再讲一遍。'
            : '已记下，这一轮的概念标成了“还不懂”，后面用到时会重新讲。';
        } else {
          const tail = kind === 'stuck'
            ? '”还不懂。后面用到它时会换一种讲法重讲；现在就想弄清楚，可以直接提问。'
            : '”已经会。后面不再为它铺垫。';
          status.replaceChildren('已记下：“', rich('span', '', concept), tail);
        }
      } catch (error) {
        status.textContent = error.message || '没能记下，可以再点一次。';
      }
    }

    function pick(kind) {
      picker.replaceChildren(el('span', 'feedback-label', kind === 'stuck' ? '卡在哪个词？' : '哪个词已经会了？'));
      for (const term of terms) {
        const chip = rich('button', 'feedback-term', term);
        chip.type = 'button';
        chip.addEventListener('click', () => send(kind, term));
        picker.append(chip);
      }
      const form = el('form', 'feedback-other');
      const input = el('input');
      input.type = 'text';
      input.maxLength = MAX_NAME;
      input.placeholder = '别的词';
      input.setAttribute('aria-label', '别的词');
      const ok = el('button', 'btn btn-small', '确定');
      ok.type = 'submit';
      form.append(input, ok);
      form.addEventListener('submit', (event) => {
        event.preventDefault();
        const text = input.value.trim();
        if (text) send(kind, text);
      });
      picker.append(form);
      picker.hidden = false;
      status.textContent = '';
    }

    for (const [kind, label] of [['unclear', '还是不懂'], ['stuck', '卡在这个词'], ['known', '这个我会']]) {
      const item = el('button', 'btn btn-small feedback-btn', label);
      item.type = 'button';
      item.addEventListener('click', () => { if (kind === 'unclear') { picker.hidden = true; send(kind); } else pick(kind); });
      buttons.append(item);
    }
    bar.append(buttons, picker, status);
    return bar;
  }

  button.addEventListener('click', openDialog);
  dialog.addEventListener('click', (event) => {
    if (event.target === dialog || event.target.closest('[data-close]')) dialog.close();
  });

  return {
    load,
    // 记录在别处改过：重新读取，不是空的就再让读者确认一次。
    reconfirm: load,
    refresh,
    supported,
    applyPrerequisites,
    setGateVisible(value) {
      gateVisible = value;
      confirmBox.hidden = confirmed || (!gateVisible && !conflict);
    },
    ready: () => !supported() || confirmed,
    // 请求讲解时带上读者确认过的记录版本。
    withRequest: (request) => (supported() ? { ...request, record_version: record.version } : request),
    feedbackBar,
    // 地图用：当前的记录（没有这个功能时是 null），以及打开记录的对话框。
    current: () => record,
    open: openDialog,
  };
}
