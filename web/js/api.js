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

function postJson(url, body) {
  return getJson(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

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
    defaultPaper: null,
    openPaper: (input) => postJson(`${base}/papers`, { input }),
    getPaper: (paper) => getJson(paperUrl(paper)),
    getContent: (paper) => getJson(`${paperUrl(paper)}/content`),
    listExplanations: (paper) => getJson(`${paperUrl(paper)}/explanations`),
    // 卡片真正显示出来之后才调用；后端据此记录“讲过”。
    acknowledge: (paper, id) => postJson(`${paperUrl(paper)}/explanations/${encodeURIComponent(id)}/displayed`, {}),
    async explain(paper, request, onStatus) {
      const { task } = await postJson(`${paperUrl(paper)}/explain`, request);
      return watchTask(`${base}/tasks/${encodeURIComponent(task)}/events`, onStatus);
    },
  };
}

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function mockApi() {
  let canned = null;
  let served = 0;
  let content = null;
  async function loadContent() {
    if (!content) content = await getJson(`mock/paper-${MOCK_PAPER}.json`);
    return content;
  }
  return {
    mode: 'mock',
    backend: null,
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
    // 样例模式不写任何读者数据。
    acknowledge: async (paper, id) => ({ id, displayed: true }),
    async explain(paper, request, onStatus) {
      if (!canned) canned = (await getJson(`mock/explanations-${MOCK_PAPER}.json`)).explanations;
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
