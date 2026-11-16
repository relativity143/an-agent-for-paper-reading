// 接口层：真实后端和样例数据走同一组方法，页面其余部分不关心是哪一个。
// 接口约定见 docs/agent-architecture.md 第 4 节。

const MOCK_PAPER = '2610.27436v1';
const MAX_RECONNECTS = 4;

// kind：http（请求被拒绝或出错）、model（这一轮没有讲成）、disconnected（连接断了，结果未知）。
export class ApiError extends Error {
  constructor(message, kind, status = null) {
    super(message);
    this.kind = kind;
    this.status = status;
  }
}

async function getJson(url, options) {
  let response;
  try {
    response = await fetch(url, options);
  } catch {
    throw new ApiError('连不上本机服务。', 'disconnected');
  }
  let body = null;
  try { body = await response.json(); } catch { /* 留给下面统一报错 */ }
  if (!response.ok) {
    throw new ApiError((body && body.error) || `请求失败（${response.status}）`, 'http', response.status);
  }
  return body;
}

function sendJson(method, url, body) {
  return getJson(url, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

const postJson = (url, body) => sendJson('POST', url, body);

// 后端对上传的限制：只收 PDF，最大 50 MiB。先在页面上拦一次，省得白传。
export const MAX_UPLOAD_BYTES = 50 * 1024 * 1024;

// 订阅一项任务。每项任务只有一个终态：result 或 error。
// 连接意外断开时浏览器会自动带着 Last-Event-ID 重连；重连几次仍不行就放弃，
// 但不重新发起生成，由页面从历史记录恢复。
function watchTask(url, onStatus) {
  return new Promise((resolve, reject) => {
    const events = new EventSource(url);
    let failures = 0;
    const finish = (settle, value) => { events.close(); settle(value); };
    events.addEventListener('open', () => { failures = 0; });
    events.addEventListener('status', (event) => onStatus(event.data));
    events.addEventListener('result', (event) => {
      try { finish(resolve, JSON.parse(event.data)); } catch {
        finish(reject, new ApiError('收到的讲解不是有效的数据。', 'model'));
      }
    });
    events.addEventListener('error', (event) => {
      if (typeof event.data === 'string') {
        // 后端明确告知这一轮失败了。
        finish(reject, new ApiError(event.data || '这一轮没有讲成。', 'model'));
        return;
      }
      failures += 1;
      if (events.readyState === EventSource.CLOSED || failures > MAX_RECONNECTS) {
        finish(reject, new ApiError('和本机服务的连接断开了，这一轮的结果还不知道。', 'disconnected'));
      }
    });
  });
}

function realApi(health) {
  const base = '/api';
  const paperUrl = (paper) => `${base}/papers/${encodeURIComponent(paper)}`;
  return {
    mode: 'live',
    backend: typeof health.backend === 'string' ? health.backend : null,
    // 后端支持哪些可选功能（4.10 节）；旧版后端没有这个字段。
    features: Array.isArray(health.features) ? health.features : [],
    defaultPaper: null,
    // 论文列表、设置和上传（docs/agent-architecture.md 4.5 节）。
    listPapers: async () => (await getJson(`${base}/papers`)).papers || [],
    getSettings: () => getJson(`${base}/settings`),
    // 页面能改三样：用哪个后端、它用哪个模型、每次生成最多等多久（4.6 节）。密钥不经过页面。
    saveSettings: (changes) => sendJson('PUT', `${base}/settings`, changes),
    // 概念记录与反馈（4.7 节）。旧版后端没有这几个接口：读取得到 404 时返回 null，页面就不显示这一部分。
    async getRecord(paper) {
      try {
        return await getJson(`${paperUrl(paper)}/record`);
      } catch (error) {
        if (error.kind === 'http' && error.status === 404) return null;
        throw error;
      }
    },
    saveRecord: (paper, version, concepts) => sendJson('PUT', `${paperUrl(paper)}/record`, { version, concepts }),
    // 本文基础分析独立于讲解：不携带读者背景、记录版本，也不发送显示回执。
    getPrerequisites: (paper) => getJson(`${paperUrl(paper)}/prerequisites`),
    async preparePrerequisites(paper, onStatus, onTask) {
      const { task } = await postJson(`${paperUrl(paper)}/prerequisites`, {});
      if (onTask) onTask(task);
      return watchTask(`${base}/tasks/${encodeURIComponent(task)}/events`, onStatus);
    },
    sendFeedback: (paper, feedback) => postJson(`${paperUrl(paper)}/feedback`, feedback),
    // 概念地图的位置和批注（4.8 节）。后端还没有这个接口时返回 null，页面改存在浏览器里。
    async getMap(paper) {
      try {
        const value = await getJson(`${paperUrl(paper)}/map`);
        if (value === null) throw new ApiError('地图响应不完整，请重试读取。', 'http', 200);
        return value;
      } catch (error) {
        if (error.kind === 'http' && error.status === 404) return null;
        throw error;
      }
    },
    saveMap: (paper, version, nodes, notes) => sendJson('PUT', `${paperUrl(paper)}/map`, { version, nodes, notes }),
    // 停止正在生成的一轮。已经结束的任务也返回成功，不会删掉已保存的讲解。
    cancelTask: (task) => postJson(`${base}/tasks/${encodeURIComponent(task)}/cancel`, {}),
    // 请求体就是文件本身；文件名只取名字，不带本机路径。
    uploadPdf: (file) => getJson(`${base}/papers/upload`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/pdf', 'X-Paper-Name': encodeURIComponent(file.name || 'paper.pdf') },
      body: file,
    }),
    openPaper: (input) => postJson(`${base}/papers`, { input }),
    getPaper: (paper) => getJson(paperUrl(paper)),
    getContent: (paper) => getJson(`${paperUrl(paper)}/content`),
    listExplanations: (paper) => getJson(`${paperUrl(paper)}/explanations`),
    // 卡片真正显示出来之后才调用；后端据此记录“讲过”。
    acknowledge: (paper, id) => postJson(`${paperUrl(paper)}/explanations/${encodeURIComponent(id)}/displayed`, {}),
    // onTask 拿到任务编号，“停止”要用它。
    async explain(paper, request, onStatus, onTask) {
      const { task } = await postJson(`${paperUrl(paper)}/explain`, request);
      if (onTask) onTask(task);
      return watchTask(`${base}/tasks/${encodeURIComponent(task)}/events`, onStatus);
    },
  };
}

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// 样例模式下的概念记录：规则照 4.7 节，只放在这个标签页里（刷新还在，关掉就没有），不写任何读者数据。
function mockRecord() {
  const KEY = 'paperread-mock-record';
  let record = { version: 0, concepts: [] };
  try {
    const saved = JSON.parse(sessionStorage.getItem(KEY));
    if (saved && Array.isArray(saved.concepts) && Number.isInteger(saved.version)) record = saved;
  } catch { /* 读不到就从空的开始 */ }
  const copy = () => JSON.parse(JSON.stringify(record));
  const find = (name) => record.concepts.find((c) => c.name.toLowerCase() === name.toLowerCase());
  function commit(touched) {
    if (!touched) return copy();
    record.version += 1;
    record.concepts.sort((a, b) => (a.updated < b.updated ? 1 : a.updated > b.updated ? -1 : 0));
    try { sessionStorage.setItem(KEY, JSON.stringify(record)); } catch { /* 存不下时只留在内存里 */ }
    return copy();
  }
  // status 为 null 表示“讲过”：新条目记为 explained，已有条目的状态不动。
  function put(name, status, extra) {
    const now = new Date().toISOString();
    const existing = find(name);
    if (existing) {
      const before = JSON.stringify(existing);
      if (status) existing.status = status;
      Object.assign(existing, extra);
      if (JSON.stringify(existing) === before) return false;
      existing.updated = now;
      return true;
    }
    record.concepts.push({ name, status: status || 'explained', explained_in: null, where: null, note: null, ...extra, updated: now });
    return true;
  }
  const whereOf = (x) => (x.where && Array.isArray(x.where.ids) && x.where.ids[0]) || (x.where && x.where.page ? `PDF 第 ${x.where.page} 页` : null);
  return {
    get: copy,
    save(version, concepts) {
      if (version !== record.version) throw new ApiError('记录已经在别处改过，请重新读取。', 'http', 409);
      const now = new Date().toISOString();
      record.concepts = concepts.map((c) => {
        const old = find(c.name);
        return { name: c.name, status: c.status, note: c.note || null, explained_in: old ? old.explained_in : null, where: old ? old.where : null, updated: old && old.status === c.status && (old.note || null) === (c.note || null) ? old.updated : now };
      });
      return commit(true);
    },
    displayed(explanation) {
      if (!explanation || !['point', 'deep'].includes(explanation.type)) return copy();
      let touched = false;
      // 逐点讲解记它展开的概念；完整讲解的一段只记当场补讲的基础。
      const taught = explanation.type === 'deep'
        ? (explanation.foundations || []).filter((f) => f.taught)
        : (explanation.concepts || []);
      for (const concept of taught) {
        touched = put(concept.name, null, { explained_in: explanation.id, where: whereOf(explanation) }) || touched;
      }
      return commit(touched);
    },
    feedback(explanation, { kind, concept, note }) {
      const extra = note ? { note } : {};
      let touched = false;
      if (kind === 'unclear') {
        const expanded = explanation && explanation.type === 'deep'
          ? (explanation.foundations || []).filter((f) => f.taught)
          : (explanation && explanation.concepts) || [];
        for (const item of expanded) {
          const old = find(item.name);
          if (old && old.status === 'known') continue; // 读者说过已经会的，不因为“这一轮不懂”改掉
          touched = put(item.name, 'unsure', extra) || touched;
        }
      } else {
        if (!concept) throw new ApiError('要说明是哪个词。', 'http', 400);
        touched = put(concept, kind === 'known' ? 'known' : 'unsure', extra);
      }
      return commit(touched);
    },
  };
}

function mockApi() {
  let canned = null;
  let served = 0;
  let content = null;
  const record = mockRecord();
  async function loadCanned() {
    if (!canned) canned = (await getJson(`mock/explanations-${MOCK_PAPER}.json`)).explanations;
    return canned;
  }
  // “读完之后”的四个阶段（4.10 节）的样例。deepDone 记完整讲解讲到第几段，0 表示目录还没给。
  let stages = null;
  let deepDone = -1;
  let stageCount = 0;
  const produced = new Map(); // 样例模式里已经给出的阶段对象，回执和反馈按编号找它
  async function stage(mode) {
    if (!stages) stages = await getJson(`mock/stages-${MOCK_PAPER}.json`);
    let source;
    if (mode === 'deep') {
      if (deepDone >= stages.deep.length) throw new ApiError('完整讲解已经讲完了。', 'model');
      source = deepDone < 0 ? stages.deep_outline : stages.deep[deepDone];
    } else {
      source = stages[mode];
    }
    const result = JSON.parse(JSON.stringify(source));
    stageCount += 1;
    result.id = `mock-${result.type}-${stageCount}`;
    result.paper = MOCK_PAPER;
    if (result.type === 'recap') {
      // 术语表的状态和讲过的位置按概念记录填，和后端的做法一样。
      const known = new Map(record.get().concepts.map((c) => [c.name.toLowerCase(), c]));
      for (const item of result.glossary) {
        const entry = known.get(item.term.toLowerCase());
        item.status = entry ? entry.status : 'untaught';
        item.explained_at = entry ? entry.where : null;
      }
    }
    if (mode === 'deep') deepDone += 1;
    produced.set(result.id, result);
    return result;
  }
  async function loadContent() {
    if (!content) content = await getJson(`mock/paper-${MOCK_PAPER}.json`);
    return content;
  }
  return {
    mode: 'mock',
    backend: null,
    features: ['stages'],
    defaultPaper: MOCK_PAPER,
    async openPaper() {
      const data = await loadContent();
      return { paper: MOCK_PAPER, title: data.title, authors: data.authors, source: data.source, source_url: data.source_url, steps: data.steps };
    },
    async getPaper() {
      const data = await loadContent();
      return { paper: MOCK_PAPER, title: data.title, authors: data.authors, source: data.source, source_url: data.source_url, steps: data.steps };
    },
    getContent: loadContent,
    listExplanations: async () => [],
    // 样例模式不写任何读者数据；概念记录只留在这个标签页里。
    async acknowledge(paper, id) {
      record.displayed(produced.get(id) || (await loadCanned()).find((x) => x.id === id));
      return { id, displayed: true };
    },
    getRecord: async () => record.get(),
    saveRecord: async (paper, version, concepts) => record.save(version, concepts),
    async sendFeedback(paper, feedback) {
      const target = produced.get(feedback.explanation) || (await loadCanned()).find((x) => x.id === feedback.explanation);
      return record.feedback(target, feedback);
    },
    async explain(paper, request, onStatus) {
      await loadCanned();
      if (['recap', 'deep', 'position', 'next_reading'].includes(request.mode)) {
        if (!served) throw new ApiError('先生成全文导读，才能做这一步。', 'http', 400);
        onStatus('整理中…（样例数据）');
        await wait(500);
        return stage(request.mode);
      }
      if (served >= canned.length) {
        throw new ApiError('样例数据只有这几轮。接上后端后，这里会继续往下讲。', 'model');
      }
      const next = canned[served];
      const steps = next.type === 'overview'
        ? ['通读全文', '整理主线和需要的基础']
        : ['定位这一点', '对照你的背景找缺口', '核对原文位置'];
      for (const step of steps) {
        onStatus(`${step}…（样例数据）`);
        await wait(450);
      }
      served += 1;
      return next;
    },
  };
}

// 只有在启动时连不上后端，才进入样例模式；之后真实请求失败不会换成样例讲解。
export async function createApi() {
  const forceMock = new URLSearchParams(location.search).get('mock') === '1';
  if (!forceMock) {
    try {
      const response = await fetch('/api/health');
      if (response.ok) return realApi(await response.json());
    } catch { /* 没有后端时用样例数据 */ }
  }
  return mockApi();
}
