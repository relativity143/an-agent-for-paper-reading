// 不用敲命令的那几件事：换论文、打开本地 PDF、选模型后端和模型、调时限。
// 接口见 docs/agent-architecture.md 4.5、4.6 节。论文标题、作者、后端返回的说明都当作文字显示。

import { MAX_UPLOAD_BYTES } from './api.js';

const BACKEND_LABEL = { claude: 'Claude Code', codex: 'Codex', api: '模型 API' };
const BACKEND_NOTE = {
  claude: '用本机已经登录的 Claude Code。',
  codex: '用本机已经登录的 Codex。',
  api: '用配置文件里填的模型接口。',
};

const MIN_TIMEOUT = 30;
const MAX_TIMEOUT = 600;
// 模型名的写法，和后端的校验一致（docs/agent-architecture.md 4.6 节）。
const MODEL_NAME = /^[A-Za-z0-9][A-Za-z0-9._:\-[\]]{0,99}$/;

// 只有 Claude Code 和 Codex 能在页面上选模型；旧版服务不带 model 字段时也不显示。
function hasModelChoice(backend) {
  return backend.name !== 'api' && 'model' in backend;
}

// 当前后端选了哪个模型；跟随默认或读不到时是空字符串。
export function chosenModel(settings) {
  const backend = (settings.backends || []).find((b) => b.name === settings.backend);
  return backend && hasModelChoice(backend) && typeof backend.model === 'string' ? backend.model : '';
}

export function backendLabel(name) {
  return BACKEND_LABEL[name] || name || '';
}

// 上传在请求头这一层被拒绝时，后端给的是英文短句；这里按状态码换成中文。
function uploadFailure(error) {
  if (error.kind === 'http' && error.status === 413) return '这份 PDF 超过 50 MB，放不进来。';
  if (error.kind === 'http' && error.status === 400 && !/[\u4e00-\u9fff]/.test(error.message || '')) {
    return '这个文件不是有效的 PDF。';
  }
  return error.message || '没能读入这份 PDF。';
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function paperLink(paper) {
  return `?paper=${encodeURIComponent(paper)}`;
}

function readTime(value) {
  const date = value ? new Date(value) : null;
  if (!date || Number.isNaN(date.getTime())) return '';
  return date.toLocaleString('zh-CN', { dateStyle: 'medium', timeStyle: 'short' });
}

// current() 返回正在读的论文编号；onBackend(name, model) 在设置保存成功后调用。
// busy() 返回现在是否正在生成讲解。
export function createManager({ api, current, onBackend, busy = () => false }) {
  const libraryDialog = document.getElementById('library-dialog');
  const libraryBody = document.getElementById('library-body');
  const settingsDialog = document.getElementById('settings-dialog');
  const settingsBody = document.getElementById('settings-body');
  const overlay = document.getElementById('drop-overlay');
  const live = api.mode === 'live';
  let uploading = false;
  let shownSettings = null; // 设置面板现在显示的那份设置

  // 上传一份 PDF，成功后直接打开它。status 是显示进度和错误的地方。
  async function upload(file, status) {
    if (!live || uploading || !file) return;
    const isPdf = file.type === 'application/pdf' || /\.pdf$/i.test(file.name || '');
    if (!isPdf) { status.textContent = '只能打开 PDF 文件。'; return; }
    if (file.size > MAX_UPLOAD_BYTES) { status.textContent = '这份 PDF 超过 50 MB，放不进来。'; return; }
    uploading = true;
    status.textContent = `正在读入 ${file.name || 'PDF'}…`;
    try {
      const meta = await api.uploadPdf(file);
      status.textContent = '读入完成，正在打开…';
      location.search = paperLink(meta.paper);
    } catch (error) {
      status.textContent = uploadFailure(error);
    } finally {
      uploading = false;
    }
  }

  // 打开论文的入口：输入编号、选本地 PDF、读过的论文列表。
  // 没有打开论文时直接放在页面上，打开之后放在“论文”对话框里。
  function libraryView() {
    const root = el('div', 'library');
    const status = el('p', 'status library-status');
    status.setAttribute('role', 'status');

    const form = el('form', 'open-form');
    const label = el('label', '', '输入 arXiv 编号或链接，打开一篇论文');
    const input = el('input');
    input.type = 'text';
    input.name = 'paper';
    input.placeholder = '例如 2203.00107';
    input.autocomplete = 'off';
    label.append(input);
    const open = el('button', 'btn btn-primary', '打开');
    open.type = 'submit';
    form.append(label, open);
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      const value = input.value.trim();
      if (value) location.search = paperLink(value);
    });
    root.append(form);

    if (live) {
      const row = el('div', 'library-upload');
      const pick = el('button', 'btn btn-small', '打开本地 PDF…');
      pick.type = 'button';
      const file = el('input');
      file.type = 'file';
      file.accept = 'application/pdf,.pdf';
      file.hidden = true;
      pick.addEventListener('click', () => file.click());
      file.addEventListener('change', () => { upload(file.files[0], status); file.value = ''; });
      row.append(pick, file, el('span', 'hint', '也可以把 PDF 文件直接拖到页面上。'));
      root.append(row, status);

      const heading = el('h3', 'library-heading', '本机已有的论文');
      const list = el('ul', 'library-list');
      const note = el('p', 'hint', '正在读取…');
      root.append(heading, note, list);
      api.listPapers().then((papers) => {
        note.textContent = papers.length ? '' : '还没有。打开一篇论文后，它会出现在这里。';
        note.hidden = papers.length > 0;
        list.replaceChildren(...papers.map(paperItem));
      }).catch((error) => {
        note.textContent = error.message || '没能读取论文列表。';
      });
    }
    return { root, focus: () => input.focus() };
  }

  function paperItem(paper) {
    const item = el('li');
    const isCurrent = paper.paper === current();
    const link = el(isCurrent ? 'div' : 'a', 'library-item');
    if (!isCurrent) link.href = paperLink(paper.paper);
    else link.setAttribute('aria-current', 'true');
    link.append(el('span', 'library-title', paper.title || paper.paper));
    const authors = Array.isArray(paper.authors) ? paper.authors.join('、') : '';
    link.append(el('span', 'library-meta', [authors, paper.paper].filter(Boolean).join(' · ')));
    const count = Number.isInteger(paper.explanations) ? paper.explanations : 0;
    const when = readTime(paper.last_read);
    const progress = [
      isCurrent ? '正在读' : '',
      count ? `${count} 份讲解` : '还没有讲解',
      when ? `上次读到 ${when}` : '',
      paper.source === 'local_pdf' ? '本地 PDF' : paper.source === 'pdf_only' ? '只有 PDF' : '',
    ].filter(Boolean).join(' · ');
    link.append(el('span', 'library-meta', progress));
    item.append(link);
    return item;
  }

  function openLibrary() {
    const view = libraryView();
    libraryBody.replaceChildren(view.root);
    libraryDialog.showModal();
    view.focus();
  }

  // 设置：选后端、给 Claude Code / Codex 选模型、调每次生成的时限，点“保存”一次写入。
  // 模型 API 的地址、模型名和密钥只在本机的配置文件里，页面不填也不看密钥。
  function renderSettings(settings, message) {
    shownSettings = settings;
    const generating = busy();
    const backends = settings.backends || [];
    const root = el('form', 'settings');
    root.noValidate = true;
    root.append(el('p', 'hint', '选择用哪个模型来讲解。保存后，下一轮讲解就生效。'));
    const group = el('fieldset', 'backend-list');
    group.append(el('legend', 'sr-only', '模型后端'));
    const status = el('p', 'status settings-status',
      generating ? '现在正在生成一轮讲解，生成完或停止后才能改。到时这里会自动放开。' : (message || ''));
    status.setAttribute('role', 'status');

    // 每个后端各记一份模型名，换选项时不丢已经填的内容。空着表示跟随它自己的默认模型。
    const models = new Map(backends.filter(hasModelChoice).map((b) => [b.name, b.model || '']));
    let chosen = settings.backend;

    const modelRow = el('label', 'settings-field');
    const modelName = el('span', 'settings-label');
    const modelInput = el('input');
    modelInput.type = 'text';
    modelInput.maxLength = 100;
    modelInput.autocomplete = 'off';
    modelInput.spellcheck = false;
    modelInput.placeholder = '不填：跟随它自己的默认模型';
    modelInput.setAttribute('list', 'model-suggestions');
    const suggestions = el('datalist');
    suggestions.id = 'model-suggestions';
    const modelHint = el('span', 'hint');
    modelRow.append(modelName, modelInput, suggestions, modelHint);
    modelInput.addEventListener('input', () => models.set(chosen, modelInput.value));

    function showModelRow() {
      const backend = backends.find((b) => b.name === chosen);
      modelRow.hidden = !backend || !models.has(chosen);
      if (modelRow.hidden) return;
      modelName.textContent = `${backendLabel(chosen)} 用哪个模型`;
      modelInput.value = models.get(chosen);
      const list = Array.isArray(backend.model_suggestions) ? backend.model_suggestions : [];
      suggestions.replaceChildren(...list.map((name) => { const o = el('option'); o.value = name; return o; }));
      modelHint.textContent = list.length
        ? `可以从建议里选（${list.join('、')}），也可以填别的名字。列在这里不代表你的账号一定能用。`
        : '填模型名；不填就用它自己的默认模型。';
    }

    for (const backend of backends) {
      const option = el('label', 'backend-option');
      const radio = el('input');
      radio.type = 'radio';
      radio.name = 'backend';
      radio.value = backend.name;
      radio.checked = backend.name === settings.backend;
      radio.disabled = !backend.available;
      const text = el('span', 'backend-text');
      const head = el('span', 'backend-name', backendLabel(backend.name));
      const state = backend.available ? '可用'
        : backend.name === 'api' ? '还没有配置' : '没有找到命令行程序';
      head.append(el('span', `pill ${backend.available ? '' : 'pill-unsure'}`, state));
      text.append(head, el('span', 'backend-note', BACKEND_NOTE[backend.name] || ''));
      option.append(radio, text);
      group.append(option);
      radio.addEventListener('change', () => { chosen = backend.name; showModelRow(); });
    }
    showModelRow();

    // 时限：旧版服务不返回这一项，那时不显示。
    const hasTimeout = Number.isInteger(settings.timeout_seconds);
    const timeRow = el('label', 'settings-field');
    const timeInput = el('input');
    timeInput.type = 'number';
    timeInput.min = String(MIN_TIMEOUT);
    timeInput.max = String(MAX_TIMEOUT);
    timeInput.step = '1';
    timeInput.inputMode = 'numeric';
    if (hasTimeout) timeInput.value = String(settings.timeout_seconds);
    timeRow.append(el('span', 'settings-label', '每次生成最多等多少秒'), timeInput,
      el('span', 'hint', `${MIN_TIMEOUT} 到 ${MAX_TIMEOUT} 之间的整数。模型的回答不合格时会自动重来一次，所以一轮最长可能等到这个数的两倍。`));
    timeRow.hidden = !hasTimeout;

    const save = el('button', 'btn btn-primary', '保存');
    save.type = 'submit';
    const fields = el('fieldset', 'settings-fields');
    fields.append(group, modelRow, timeRow, save);
    fields.disabled = generating;

    root.addEventListener('submit', async (event) => {
      event.preventDefault();
      const changes = { backend: chosen };
      if (models.has(chosen)) {
        const model = models.get(chosen).trim();
        if (model && !MODEL_NAME.test(model)) {
          status.textContent = '模型名只能用字母、数字和 . _ : - [ ]，以字母或数字开头，最多 100 个字符。';
          modelInput.focus();
          return;
        }
        changes.model = model || null;
      }
      if (hasTimeout) {
        const seconds = Number(timeInput.value);
        if (!Number.isInteger(seconds) || seconds < MIN_TIMEOUT || seconds > MAX_TIMEOUT) {
          status.textContent = `时限要填 ${MIN_TIMEOUT} 到 ${MAX_TIMEOUT} 之间的整数。`;
          timeInput.focus();
          return;
        }
        changes.timeout_seconds = seconds;
      }
      fields.disabled = true;
      status.textContent = '正在保存…';
      try {
        const updated = await api.saveSettings(changes);
        onBackend(updated.backend, chosenModel(updated));
        const what = [backendLabel(updated.backend), chosenModel(updated) || '默认模型'].join(' · ');
        settingsBody.replaceChildren(renderSettings(updated, `已保存：${what}。下一轮讲解生效。`));
      } catch (error) {
        // 没保存成：留着读者填的内容，只放开表单、写明原因。
        fields.disabled = busy();
        status.textContent = error.message || '没能保存。';
      }
    });
    root.append(fields, status);

    const apiInfo = settings.api || {};
    const details = el('dl', 'api-details');
    for (const [name, value] of [
      ['接口地址', apiInfo.base_url || '未填'],
      ['模型', apiInfo.model || '未填'],
      ['密钥', apiInfo.key_configured ? '已配置' : '未配置'],
    ]) {
      details.append(el('dt', '', name), el('dd', '', value));
    }
    const apiBox = el('section', 'api-box');
    apiBox.append(el('h3', '', '模型 API 的配置'), details);
    apiBox.append(el('p', 'hint', '接口地址、模型和密钥在本机数据目录下的 config.json 里填写（默认是 ~/.paperread/config.json），写法见 server/README.md。页面不接触密钥，这里只显示有没有配置。填好以后，在上面选“模型 API”再点保存，或者重启服务。'));
    root.append(apiBox);
    root.append(el('p', 'hint', '“可用”只表示本机装了、配置齐了。有没有登录，要到请求讲解时才知道；没登录时那一轮会报错。'));
    return root;
  }

  async function openSettings() {
    settingsBody.replaceChildren(el('p', 'hint', '正在读取设置…'));
    settingsDialog.showModal();
    try {
      settingsBody.replaceChildren(renderSettings(await api.getSettings()));
    } catch (error) {
      settingsBody.replaceChildren(el('p', 'status', error.message || '没能读取设置。'));
    }
  }

  // 对话框：点关闭按钮或点对话框外面的遮罩时关上。
  for (const dialog of [libraryDialog, settingsDialog]) {
    dialog.addEventListener('click', (event) => {
      if (event.target === dialog || event.target.closest('[data-close]')) dialog.close();
    });
  }

  // 把 PDF 拖到页面上：出现提示层，松开后上传。
  if (live) {
    const hasFiles = (event) => Array.from(event.dataTransfer?.types || []).includes('Files');
    const overlayText = overlay.querySelector('p');
    const idleText = overlayText.textContent;
    let depth = 0;
    const hide = () => { depth = 0; overlay.hidden = true; overlayText.textContent = idleText; };
    window.addEventListener('dragenter', (event) => {
      if (!hasFiles(event) || uploading) return;
      event.preventDefault();
      depth += 1;
      overlay.hidden = false;
    });
    window.addEventListener('dragover', (event) => { if (hasFiles(event)) event.preventDefault(); });
    window.addEventListener('dragleave', (event) => {
      if (!hasFiles(event)) return;
      depth -= 1;
      if (depth <= 0 && !uploading) hide();
    });
    window.addEventListener('drop', async (event) => {
      if (!hasFiles(event)) return;
      event.preventDefault();
      depth = 0;
      await upload(event.dataTransfer.files[0], overlayText);
      // 成功时页面已经在跳转；没成功时把原因留在提示层上，点一下关掉。
      if (overlayText.textContent === idleText) hide();
    });
    overlay.addEventListener('click', hide);
  }

  // 生成开始或结束时，开着的设置面板跟着更新。
  function busyChanged() {
    if (settingsDialog.open && shownSettings) settingsBody.replaceChildren(renderSettings(shownSettings));
  }

  return { live, libraryView, openLibrary, openSettings, busyChanged };
}
