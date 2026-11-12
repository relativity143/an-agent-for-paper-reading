// 接口层：真实后端和样例数据走同一组方法，页面其余部分不关心是哪一个。
// 接口约定见 docs/agent-architecture.md 第 4 节。

const MOCK_PAPER = '2610.27436v1';

async function getJson(url, options) {
  const response = await fetch(url, options);
  let body = null;
  try { body = await response.json(); } catch { /* 留给下面统一报错 */ }
  if (!response.ok) {
    throw new Error((body && body.error) || `请求失败（${response.status}）`);
  }
  return body;
}

function realApi(health) {
  const base = '/api';
  return {
    mode: 'live',
    backend: health.backend || null,
    defaultPaper: null,
    openPaper: (input) => getJson(`${base}/papers`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ input }),
    }),
    getContent: (paper) => getJson(`${base}/papers/${encodeURIComponent(paper)}/content`),
    listExplanations: (paper) => getJson(`${base}/papers/${encodeURIComponent(paper)}/explanations`),
    async explain(paper, request, onStatus) {
      const { task } = await getJson(`${base}/papers/${encodeURIComponent(paper)}/explain`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(request),
      });
      return new Promise((resolve, reject) => {
        const events = new EventSource(`${base}/tasks/${encodeURIComponent(task)}/events`);
        events.addEventListener('status', (event) => onStatus(event.data));
        events.addEventListener('result', (event) => {
          events.close();
          try { resolve(JSON.parse(event.data)); } catch (error) { reject(error); }
        });
        events.addEventListener('error', (event) => {
          events.close();
          reject(new Error(event.data || '讲解中断了，请再试一次。'));
        });
      });
    },
  };
}

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function mockApi() {
  let canned = null;
  let served = 0;
  async function load() {
    if (!canned) {
      const data = await getJson(`mock/explanations-${MOCK_PAPER}.json`);
      canned = data.explanations;
    }
    return canned;
  }
  return {
    mode: 'mock',
    backend: null,
    defaultPaper: MOCK_PAPER,
    openPaper: async () => ({ paper: MOCK_PAPER }),
    getContent: () => getJson(`mock/paper-${MOCK_PAPER}.json`),
    listExplanations: async () => [],
    async explain(paper, request, onStatus) {
      const list = await load();
      if (served >= list.length) {
        throw new Error('样例数据只有这几轮。接上后端后，这里会继续往下讲。');
      }
      const next = list[served];
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
