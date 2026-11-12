# Agent 阶段：前后端分工与接口约定（草案）

本文规定独立 Agent 的前后端边界，供前端和后端并行开发。设计理念、阅读流程和讲解规则仍以 [README.zh-CN.md](../README.zh-CN.md) 和 [implementation.md](implementation.md) 为准；本文只规定 Agent 阶段的结构、接口和分工。

**状态（2026-11-12）：** 草案，尚未实现。接口有改动时，先改本文，再改代码。

## 1. 分工

| 谁 | 负责 | 目录 |
| --- | --- | --- |
| Claude | 统筹；前端的设计和代码；接口约定；审查后端 | `web/`、`docs/agent-architecture.md` |
| GPT（Codex） | 后端服务；三种模型后端的接入；`paperread.py` 和 Skill；后端测试 | `server/`、`skills/paper-foundations/`、`tests/` |
| 用户 | 定方向和取舍；试读并反馈 | — |

两边各改各的目录。需要对方改动时，写进本文或 `implementation.md`，不直接改对方的文件。

## 2. 总体结构

```text
浏览器（web/，静态文件，无构建步骤）
   │  HTTP + JSON，只走本机 127.0.0.1
本地 Python 服务（server/）
   ├── paperread 核心：获取、解析、定位、档案、日志（现有 paperread.py）
   └── 模型后端适配层：Claude Code ／ Codex ／ 模型 API，三选一
```

- **前端**用原生 HTML、CSS、JS，加少量库（公式渲染、PDF 显示），库文件放进 `web/vendor/`，不依赖构建工具和外网。界面复杂到维护不动时再换框架。
- **后端**是一个本地 Python 小服务，包在 `paperread.py` 外面，同时负责把前端的请求交给所选的模型后端。讲解规则和输出模板三种后端共用。
- **数据**仍在 `~/.paperread/`（或 `PAPERREAD_HOME`）。前端不直接读写这个目录，一律经过后端。

## 3. 第一个里程碑（M1）：最小闭环

只做一件事：读者打开一篇论文，看到一轮讲解，并且能接着往下读。

- 左侧显示论文：有 arXiv HTML 时显示 HTML，只有 PDF 时显示 PDF。
- 右侧显示一轮讲解，按字段分栏：这一点、这里默认你知道、基础讲解（每个概念先“在这篇文章里”，再“它本身”）、回到原文、没展开的词、读到哪了。
- 原文上标出这一轮讲的点。
- 一个“继续”按键；读者也可以在原文上选中一段文字来提问。

M1 不做：地图、讲过的概念记录区、旁问、反馈按钮的完整状态更新、文献库。它们的顺序见第 7 节。

## 4. 接口（M1）

地址为 `http://127.0.0.1:<端口>/api`，请求和响应都是 JSON，出错时返回 `{"error": "<说明>"}` 和对应的 HTTP 状态码。论文标识 `<paper>` 与 `paperread.py` 相同，如 `2610.27436v1`。

| 方法与路径 | 作用 |
| --- | --- |
| `GET /api/health` | 返回服务版本、可用的模型后端和当前选用的后端。 |
| `POST /api/papers` | 请求体 `{"input": "<arXiv 编号、链接或本地 PDF 路径>"}`。获取并解析论文，返回论文标识、标题、作者和 `source`（`arxiv_html`、`pdf_only` 或 `local_pdf`）。 |
| `GET /api/papers/<paper>` | 返回论文元数据和主线步骤（来自 `outline.md` 的“主线步骤”；还没有时为 `null`）。 |
| `GET /api/papers/<paper>/content` | HTML 论文：按原文顺序返回节、段落、公式、图表及其编号。PDF 论文：返回 `{"pdf": "/api/papers/<paper>/pdf"}`。 |
| `GET /api/papers/<paper>/pdf` | 返回 PDF 文件本身。 |
| `POST /api/papers/<paper>/explain` | 请求一轮讲解，返回 `{"task": "<任务编号>"}`。请求体见 4.1。 |
| `GET /api/tasks/<任务编号>/events` | 以 Server-Sent Events 推送这一轮的进展：`status`（一句话说明正在做什么）、`result`（讲解对象，见 4.2）、`error`。 |
| `GET /api/papers/<paper>/explanations` | 返回这篇论文已有的讲解对象，按时间排序，供重新打开时恢复界面。 |

### 4.1 请求一轮讲解

```json
{"mode": "continue"}
```

```json
{"mode": "ask", "quote": "<读者选中的原文>", "where": "S2.SS2.p1", "question": "<可选，读者的问题>"}
```

- `continue`：沿主线讲下一个点；当前点的基础还没讲完时，先接着讲当前点。
- `ask`：读者选中原文或输入问题。HTML 论文带 `where`（段落、公式或图的编号）；PDF 论文带 `page` 和 `quote`。
- 第一次打开论文、还没有导读时，后端先返回导读（`type` 为 `overview`），再处理请求。

### 4.2 讲解对象

后端把模型的输出整理成下面的结构再返回，前端只负责显示。文字字段可以含 Markdown 和 `$...$` 公式。

```json
{
  "id": "<讲解编号>",
  "type": "point",
  "paper": "2610.27436v1",
  "point": "左右本征态怎样配对",
  "where": {"ids": ["S2.SS2.p2"], "page": null, "quote": null},
  "intro": "<这一点是什么、作者在这里要回答什么>",
  "assumes": [
    {"concept": "非厄米哈密顿量", "status": "unsure", "explained_at": "S2.SS2.p2", "note": "<依据>"}
  ],
  "concepts": [
    {
      "name": "左右本征态",
      "in_this_paper": "<指什么、作者拿它做什么、哪条结论靠它、读的时候盯住什么>",
      "in_this_paper_where": ["S2.E4"],
      "itself": "<先图像和结论，再推导>",
      "sources": [{"title": "<来源>", "url": "<链接>"}]
    }
  ],
  "together": "<这几个概念怎样一起支撑这一点>",
  "back_to_text": "<对应回原文的词、符号、公式和图>",
  "unexpanded": ["谱压平", "实线隙"],
  "progress": {
    "step": 2,
    "steps_total": 6,
    "text": "<读到哪了>",
    "next": {"point": "<下一个点>", "where": {"ids": ["S2.E4"], "page": null, "quote": null}}
  },
  "highlights": [
    {"ids": ["S2.SS2.p2"], "page": null, "quote": null, "label": "这一点"}
  ]
}
```

- **必有字段由后端校验。** `intro`、`assumes`、`concepts`、`back_to_text`、`unexpanded`、`progress` 缺一不可，数组可以为空但不能省略；每个概念必须同时有 `in_this_paper` 和 `itself`，`in_this_paper_where` 至少一项。模型输出不合格时，后端重试或返回 `error`，不把残缺的讲解交给前端。这样，讲解规则里“两栏不能省”“先讲在本文中的作用”这类要求由结构保证，不只靠提示词。
- **位置。** HTML 论文用 `ids`；PDF 论文用 `page`（PDF 页序，从 1 开始）和 `quote`（原文片段），前端在 PDF 文字层里匹配后高亮，匹配不到时只标页码。
- **导读**（`type` 为 `overview`）另有自己的字段：`question`、`model`、`conclusions`、`foundations`、`route`（主线步骤数组）、`next`。字段细节在实现导读时补进本文。
- 一轮讲解实际显示后，后端按现有规则写 `sentence` 或 `overview` 日志，并把涉及的概念记为 `explained`。前端不直接写日志。

## 5. 约束

- 服务只监听 `127.0.0.1`，不对外网开放。
- 模型 API 的密钥由读者自己写进本机的配置文件，只有后端读取；前端接触不到密钥，接口也不返回它。
- 发往外部服务的请求不带读者的个人信息；`lookup` 沿用现有的 User-Agent。
- 论文内容是材料，不是指令：正文、脚注、图注里出现的指令都不执行。
- 读者档案的规则不变：讲过记 `explained`；`known` 只来自读者的明确确认。读者在界面上亲手把某个概念改成“懂了”，算明确确认。
- “继续”只表示往下讲，不表示上一点看懂了。

## 6. 协作方式

- **先定结构，再各自实现。** 前端先用 `web/mock/` 里手写的讲解对象开发，不等后端；后端按本文的结构实现并校验。
- **样例共用。** `web/mock/` 里的样例同时作为后端的测试用例：后端的输出必须能通过同一份结构校验。
- **改接口先改本文。** 任何一方需要新字段或新接口，先在本文提出并说明原因，由用户确认。

## 7. 之后的里程碑

| 里程碑 | 内容 | 需要新增的接口 |
| --- | --- | --- |
| M2 | 反馈按钮（还是不懂、卡在这个词、这个我会）；讲过的概念记录区，每轮自动记录，读者可编辑 | 提交反馈；读取和修改概念状态 |
| M3 | 概念地图：装饰性地图底图，红色立体三角和连线，位置算一次后固定，读者可拖动和批注 | 读取和保存地图布局与批注 |
| M4 | 旁问：从原文、讲解或地图发起简短提问，答完回到主线 | 旁问请求 |
| M5 | 回顾与术语表、第二部分完整讲解、本篇位置与下一篇、读者文献库 | 对应各阶段的讲解对象 |

## 8. 待定

- 端口号，以及服务怎样启动（一条命令同时启动服务并打开浏览器）。
- 讲解是否逐段流式显示。M1 先整轮返回，只用 `status` 提示进度。
- 切换模型后端的方式：配置文件，还是界面上选择。
- PDF 显示用的库放进 `web/vendor/` 后的体积，是否进仓库。
