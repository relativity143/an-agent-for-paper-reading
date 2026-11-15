# Agent 阶段：前后端分工与接口约定

本文规定独立 Agent 的前后端边界，供前端和后端并行开发。设计理念、阅读流程和讲解规则仍以 [README.zh-CN.md](../README.zh-CN.md) 和 [implementation.md](implementation.md) 为准；本文只规定 Agent 阶段的结构、接口和分工。

**状态（2026-11-15）：** M1 的前端（见 [web/README.md](../web/README.md)）和后端（见 [server/README.md](../server/README.md)）已经接上，包括双击启动、在页面上选模型后端和模型、调时限、停止正在生成的一轮、论文列表和打开本地 PDF。页面与后端的配合用固定输出的联调服务核对过。真实模型试过一轮（[记录](m1-round2-live-trial.md)）：Codex 能在一两分钟内生成导读，Claude Code 两次都在 600 秒内没有答完，原因还不清楚；讲解质量还没有人工核对过。本文第 4 节按后端的实际实现写。接口有改动时，先改本文，再改代码。

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

- **前端**用原生 HTML、CSS、JS，加少量库（公式渲染），不依赖构建工具。库文件放在 `web/vendor/`，页面不依赖外网。界面复杂到维护不动时再换框架。
- **同一个地址。** 页面和接口都由本地服务提供：双击仓库根目录的 `start.command`，或运行 `python3 -m server --open`。默认 `http://127.0.0.1:8765/`，用 `--port` 换端口。服务只接受同源请求，页面不能放在另一个端口上去调它。
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

地址为 `http://127.0.0.1:<端口>/api`，请求和响应都是 JSON，出错时返回 `{"error": "<说明>"}` 和对应的 HTTP 状态码（请求不合格 400，非同源 403，找不到 404，与当前状态冲突 409）。论文标识 `<paper>` 与 `paperread.py` 相同，如 `2610.27436v1`。

| 方法与路径 | 作用 |
| --- | --- |
| `GET /api/health` | 返回服务版本 `version`、当前选用的后端 `backend` 和可用的后端 `backends`。它不检查模型是否已经登录，登录问题在请求讲解时以 `error` 报出。 |
| `POST /api/papers` | 请求体 `{"input": "<arXiv 编号、链接或本地 PDF 路径>"}`。获取并解析论文，返回元数据：`paper`、`title`、`authors`、`source`（`arxiv_html`、`pdf_only` 或 `local_pdf`）、`source_url`、`steps`。 |
| `GET /api/papers/<paper>` | 返回同样的元数据。`steps` 是主线步骤（来自 `outline.md` 的“主线步骤”），还没有导读时为 `null`。 |
| `GET /api/papers/<paper>/content` | 返回论文正文，格式见 4.3。PDF 论文只返回 `{"pdf": "/api/papers/<paper>/pdf"}`，标题和作者要从元数据接口取。 |
| `GET /api/papers/<paper>/pdf` | 返回 PDF 文件本身。 |
| `POST /api/papers/<paper>/explain` | 请求一轮讲解，返回 202 和 `{"task": "<任务编号>"}`。请求体见 4.1。 |
| `GET /api/tasks/<任务编号>/events` | 以 Server-Sent Events 推送这一轮的进展，见 4.4。 |
| `GET /api/papers/<paper>/explanations` | 返回这篇论文已有的讲解对象，按时间排序，供重新打开时恢复界面。 |
| `POST /api/papers/<paper>/explanations/<讲解编号>/displayed` | 显示回执，请求体 `{}`，返回 `{"id": "<讲解编号>", "displayed": true}`。重复发送只记一次。见 4.4。 |

### 4.1 请求一轮讲解

```json
{"mode": "continue"}
```

```json
{"mode": "ask", "quote": "<读者选中的原文>", "where": "S2.SS2.p1", "question": "<可选，读者的问题>"}
```

```json
{"mode": "ask", "page": 3, "quote": "<读者粘贴的原文>", "question": "<可选>"}
```

- `continue`：沿主线讲下一个点；当前点的基础还没讲完时，先接着讲当前点。
- `ask`：读者选中原文或输入问题。HTML 论文的 `where` 必须是正文里真实存在的段落、公式或图的编号。PDF 论文用 `page`（文件里的第几页，从 1 开始的整数）和 `quote`；有 `quote` 就必须有 `page`。
- **一个任务只返回一份讲解。** 这篇论文还没有导读时，不管请求是什么，这一轮都只返回导读（`type` 为 `overview`）。这时的 `ask` 会被存下来，下一次 `continue` 回答它；再发一个 `ask` 会替换它。
- **上一份讲解还没有显示回执时**，后端不往下讲，把那一份原样再返回一次。
- **主线讲完后**（最新一份讲解的 `progress.next` 为 `null`），`continue` 会以 `error` 结束；`ask` 仍然可以用。
- **409** 表示这篇论文已经有一轮讲解在生成，或者同时进行的任务到了上限。它不表示论文有问题；页面应当等一下再恢复，不要重发请求。

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

- **必有字段由后端校验。** `intro`、`assumes`、`concepts`、`together`、`back_to_text`、`unexpanded`、`progress` 缺一不可；数组可以为空但不能省略，文字不能是空的。每个概念必须同时有 `in_this_paper` 和 `itself`，`in_this_paper_where` 至少一项。模型输出不合格时，后端让模型修正一次，仍不合格就返回 `error`，不保存也不把残缺的讲解交给前端。这样，讲解规则里“两栏不能省”“先讲在本文中的作用”这类要求由结构保证，不只靠提示词。
- **后端还核对内容和实际情况是否相符。** 讲的位置必须是上一轮说的“接下来”的位置，或读者提问的位置；`assumes` 里 `new` 以外的状态必须和读者档案一致；`sources` 的链接必须是论文的来源网址或参考文献里已有的链接；导读的主线是 3 到 7 步；PDF 页码不能超过总页数。
- **位置。** HTML 论文用 `ids`。PDF 论文的 `ids` 为空数组，用 `page`（文件里的第几页，从 1 开始）和 `quote`（原文片段）；概念里的位置（`in_this_paper_where`、`explained_at`）写成 `"PDF 第 2 页"`、`"page 2"` 或 `"p. 2"` 这样的文字。M1 的页面只按页跳转，不在 PDF 上画高亮。
- **概念状态。** `assumes` 里的 `status` 取 `known`（读者确认懂了）、`explained`（讲过）、`unsure`（仍不确定）或 `new`（这一轮要补）。`explained_at` 是讲过的位置，没有时为 `null`。
- **可以为空的字段。** `section`（这一点所在的节标题）、`progress.step`、`progress.steps_total` 可以是 `null`。`progress.next` 可以指回当前这一点（基础还没讲完），也可以是 `null`（主线讲完了）；导读的 `next` 同样可以是 `null`。
- **`quote`。** `where.quote` 只在读者选中原文提问时填写；要在原文里标出这句话时，把同一段文字放进对应 `highlights` 项的 `quote`。
- **导读**（`type` 为 `overview`）的字段：`title`；`question`（`text`、`where`）；`model`（`summary`，以及 `items` 数组，每项 `label`、`text`、`where`）；`conclusions`（`text`、`where`）；`foundations`（每项 `name`、`note`）；`previously`（在其他论文里补过的基础，每项 `name`、`paper`、`status`，没有时为空数组）；`route`（`steps` 数组，每项 `step`、`name`、`range`，以及可选的 `skim`）；`next`（`text`、`where`）；`highlights`。
- 完整的例子见 [web/mock/explanations-2610.27436v1.json](../web/mock/explanations-2610.27436v1.json)，一份导读和两轮讲解。
- 一轮讲解在页面上实际显示、并且收到显示回执后（见 4.4），后端才按现有规则写 `sentence` 或 `overview` 日志，并把 `concepts` 里实际展开的概念记为 `explained`；`assumes`、`unexpanded` 和导读里只是提到的词不算，已有的 `known` 和 `unsure` 不被覆盖。前端不直接写日志。

### 4.3 论文正文

```json
{
  "paper": "2610.27436v1",
  "title": "<标题>",
  "authors": ["<作者>"],
  "source": "arxiv_html",
  "source_url": "https://arxiv.org/html/2610.27436v1",
  "steps": [{"step": 1, "name": "问题与杂质模型", "range": "S1–S2.SS1"}],
  "blocks": [
    {"type": "section", "id": "S2.SS1", "title": "II.1 Substitutional impurities", "level": 2},
    {"type": "paragraph", "id": "S2.SS1.p1", "section": "S2.SS1", "text": "… is given by [33, 3]: ⟨S2.E1⟩ where $v$ and $w$ …"},
    {"type": "figure", "id": "S2.F1", "section": "S2.SS1", "caption": "Figure 1: …"}
  ],
  "equations": {"S2.E1": "\\hat{H}=v\\sum_{m=1}^{N}…"}
}
```

- `blocks` 按原文顺序排列，类型为 `section`、`paragraph`、`figure`，以及不属于任何段落的 `equation`。
- 段落文字与 `paper.json` 相同：行内公式写成 `$...$`，行间公式用 `⟨公式编号⟩` 占位，LaTeX 在 `equations` 里按编号取。
- `steps` 是 `outline.md` 的主线步骤，还没有导读时为 `null`。导读生成后，页面用导读的 `route.steps` 或元数据接口更新步骤。步骤名可能来自模型，只能当文字显示。
- 可选的 `license`、`attribution` 用于在页面底部注明来源。
- 完整的例子见 [web/mock/paper-2610.27436v1.json](../web/mock/paper-2610.27436v1.json)。

### 4.4 任务、显示回执与恢复

**事件。** `GET /api/tasks/<任务编号>/events` 推送三种事件：

| 事件 | `data` | 说明 |
| --- | --- | --- |
| `status` | 一句话的文字 | 正在做什么，可以有多条。 |
| `result` | 讲解对象的 JSON | 这一轮的结果。 |
| `error` | 一句话的文字 | 这一轮没有讲成。已有的记录不受影响。 |

每个任务以一条 `result` 或一条 `error` 结束，之后服务关闭连接。连接空闲时服务会发注释行保活。重连时带 `Last-Event-ID`，服务从断开的地方接着发。

**显示回执。** 模型生成成功、事件发出，都不等于读者看到了。页面把一份讲解的卡片实际显示出来之后，调用显示回执接口；从历史恢复出来的卡片也要调用。后端收到回执才写阅读日志、更新概念状态，也才会往下讲。回执没发成时，页面应当提示并允许重试，并在这之前停用“继续”。

**页面不自动开始。** 打开一篇还没有讲解的论文时，页面不自动请求导读，由读者点“继续”开始。原因：生成导读要调用模型，可能要等一两分钟，期间不能切换后端；读者应当先有机会选好模型。样例数据模式不调用模型，仍然直接显示导读。

**页面怎样处理失败。**

- 收到 `error` 事件：这一轮确实没讲成。显示原因，读者可以再试。
- 连接断开、没有收到结束事件：结果未知。页面在有限次数内重连同一个任务；仍然连不上时，不重新发 `explain` 请求，而是让读者点“恢复”，重新读取 `GET …/explanations`，把还没显示的讲解显示出来并补发回执。
- 409：同上，等一下再恢复。
- 服务在启动时就连不上：页面才改用样例数据，并在右上角标明。运行中途连不上时不悄悄切换。

**联调用的固定输出服务。** `python3 web/dev/fixture_server.py` 用真实的服务代码和一个固定输出的假模型，检查页面和后端的配合；它不联网，不调用模型，数据放在临时目录里。它不能说明真实模型的讲解质量。

### 4.5 启动、设置与论文列表

目的：读者不用敲命令、不用手改配置文件，就能启动、选模型后端、换论文。用户已确认两点（2026-11-15）：启动先做到“双击一个文件”；API 密钥仍然只写在本机配置文件里，页面不接触。

**状态（2026-11-15）：** 已实现。后端和启动文件由 GPT 完成（任务说明 [gpt-m1-launch-handoff.md](gpt-m1-launch-handoff.md)，交付说明 [m1-launch-delivery.md](m1-launch-delivery.md)），页面由 Claude 接上，并在联调服务上核对过。

**双击启动。** 仓库根目录放一个 macOS 的启动文件 `start.command`。双击后：服务已经在运行就直接打开页面；否则启动服务并打开页面。关掉它的终端窗口，服务就停。它用读者真实的 `~/.paperread/`。登录时自动启动、打包成桌面应用，都留到以后。

**新增接口。**

| 方法与路径 | 作用 |
| --- | --- |
| `GET /api/settings` | 返回当前设置，结构见下。 |
| `PUT /api/settings` | 请求体 `{"backend": "claude"}`，取 `claude`、`codex` 或 `api`。只改“选用哪个后端”这一项，返回更新后的设置。下一轮讲解就生效，不用重启。 |
| `GET /api/papers` | 返回本机已有的论文列表，最近读过的在前。 |
| `POST /api/papers/upload` | 上传一份本地 PDF。请求体是 PDF 文件本身，`Content-Type: application/pdf`。返回和 `POST /api/papers` 相同的元数据。 |

```json
{
  "backend": "claude",
  "backends": [
    {"name": "claude", "available": true, "configured": true, "authenticated": null},
    {"name": "codex", "available": false, "configured": true, "authenticated": null},
    {"name": "api", "available": false, "configured": false, "authenticated": null}
  ],
  "api": {"base_url": "https://…/v1", "model": "<模型名>", "key_configured": false}
}
```

- **不返回的东西。** 密钥、本机路径、可执行文件的位置都不出现在任何接口里。API 密钥只用 `key_configured` 表示有没有。
- **页面能改的只有 `backend`。** `api` 的地址、模型和密钥仍由读者在本机的 `config.json` 里填；页面在“API”一项未配置时，提示去哪个文件里填、填什么。
- **`PUT` 的拒绝情况。** 所选后端不可用（`available` 为 `false`）时返回 400 并说明原因；有讲解正在生成时返回 409。写配置文件时保留文件里的其他内容，包括密钥。
- **`authenticated`** 仍是 `null`：服务不为了检查登录去调用模型。登录有问题时，在下一次请求讲解时以 `error` 报出。
- **论文列表**的每一项：`paper`、`title`、`authors`、`source`、`explanations`（已有几份讲解）、`last_read`（最近一次显示讲解的时间，没读过为 `null`）。缓存损坏的论文跳过，不让整个列表失败。
- **上传**只接受 PDF（检查文件头），上限 50 MiB，超过时返回 413。可选的请求头 `X-Paper-Name` 是百分号编码的文件名，只用来生成论文标识和显示标题，不保留读者本机的路径。本地 PDF 的 `source_url` 一律是 `null`。
- **论文标识可以含中文。** 上传的 PDF 用文件名生成标识（如 `我的-论文-d1139ddc`），页面拼地址时要编码。打开论文时先用 `GET /api/papers/<paper>` 读本机已有的，读不到再用 `POST /api/papers` 去取；上传的 PDF 只能走前一条。
- **提问随讲解保存。** 由读者提问触发的讲解，对象里有字段 `asked`：`{"quote", "question", "where", "page"}`，没有的项为 `null`；不是提问触发的为 `null`。`where` 是 HTML 论文的位置编号（一个字符串），不是位置对象。导读之前的提问，记在后来回答它的那一份讲解上。页面据此在那份讲解前面显示“你的问题”，重新打开时也能恢复。

### 4.6 选模型、时限、停止与留着的问题

用真实模型试用后（2026-11-15）定下的补充。

**状态（2026-11-15）：** 已实现。后端由 GPT 完成（任务说明见 [gpt-m1-launch-handoff.md 的“第二轮任务”](gpt-m1-launch-handoff.md#第二轮任务2026-11-15)，交付说明 [m1-round2-delivery.md](m1-round2-delivery.md)，真实模型的计时见 [m1-round2-live-trial.md](m1-round2-live-trial.md)），页面由 Claude 接上，并在联调服务上核对过。

**选模型。** 4.5 只能选“用哪个后端”，这里补上选这个后端里用哪个模型：

- `GET /api/settings` 的 `backends` 里，`claude` 和 `codex` 两项各有 `model`（现在用的模型名；`null` 表示跟随本机命令行程序自己的默认）和 `model_suggestions`（可以直接选的几个名字，字符串数组，可以为空；列出来不代表账号一定能用）。`api` 一项没有这两个字段，它的模型仍在配置文件里。
- `PUT /api/settings` 除 `backend` 外，接受 `model`：字符串或 `null`，改的是这次请求里 `backend` 指的那个后端；没有带 `backend` 时改当前的后端；不带 `model` 时保留原来的选择。对 `api` 带 `model`（包括 `null`）返回 400。
- 模型名只允许字母、数字和 `. _ : - [ ]`，以字母或数字开头，最长 100 个字符，不合格返回 400。保存时不调用模型去验证名字；名字不对时，下一轮讲解以中文的 `error` 说明。

**时限。** `GET /api/settings` 顶层有 `timeout_seconds`；`PUT` 接受 `timeout_seconds`，30 到 600 的整数。配置文件里没有填时默认 600。这个时限管的是一次模型调用；模型的回答没通过校验时服务会再调用一次，那一次重新计时，所以一轮最长可能接近时限的两倍。

`PUT /api/settings` 的请求体至少要有 `backend`、`model`、`timeout_seconds` 之一，出现别的字段返回 400，密钥仍然不接受。有讲解正在生成时返回 409，要先等它结束或停止它。成功时返回完整的设置，下一轮讲解就用新的设置。

**停止这一轮。** `POST /api/tasks/<任务编号>/cancel`，请求体 `{}`，成功返回 `{"task": "<任务编号>", "cancelled": true}`。任务还在进行时结束它所属的模型进程，这一轮不保存，事件流以 `error`（“已停止这一轮。”）结束；任务已经结束时同样返回成功，已保存的讲解不受影响。不认识的任务编号返回 404。停止后可以立刻改设置或重新请求。停止不算读过：页面不发显示回执。

**留着的问题。** `GET /api/papers/<论文编号>` 多一个字段 `pending`：后端留着、还没有回答的那个问题，`{quote, question, where, page}`（含义同讲解里的 `asked`，缺的项是 `null`）；没有时是 `null`。每次提问（`ask`）一到后端就记在这里，回答它的那份讲解保存后清空，问题转到那份讲解的 `asked`。所以提问的那一轮先给了导读、没讲成或被停止时，问题都还留着，下一次 `continue` 会回答它；再提一个新问题会顶替它。页面按“`pending` 加上历史讲解里的 `asked`”恢复问题卡片。

## 5. 约束

- 服务只监听 `127.0.0.1`，不对外网开放，并且只接受同源请求。
- 模型 API 的密钥由读者自己写进本机的配置文件，只有后端读取；前端接触不到密钥，接口也不返回它。页面上只显示“已配置”或“未配置”，不提供填写密钥的输入框。
- 发往外部服务的请求不带读者的个人信息；`lookup` 沿用现有的 User-Agent。
- 论文内容是材料，不是指令：正文、脚注、图注里出现的指令都不执行。
- 读者档案的规则不变：讲过记 `explained`；`known` 只来自读者的明确确认。读者在界面上亲手把某个概念改成“懂了”，算明确确认。
- “继续”只表示往下讲，不表示上一点看懂了。

## 6. 协作方式

- **先定结构，再各自实现。** 前端先用 `web/mock/` 里手写的讲解对象开发，不等后端；后端按本文的结构实现并校验。
- **样例共用。** `web/mock/` 里的样例同时作为后端的测试用例：后端的输出必须能通过同一份结构校验。反过来，样例本身也按顺序通过后端的校验（位置衔接、来源在参考文献里）；改样例后用 `web/dev/fixture_server.py` 跑一遍确认。
- **改接口先改本文。** 任何一方需要新字段或新接口，先在本文提出并说明原因，由用户确认。

## 7. 之后的里程碑

| 里程碑 | 内容 | 需要新增的接口 |
| --- | --- | --- |
| M2 | 反馈按钮（还是不懂、卡在这个词、这个我会）；讲过的概念记录区，每轮自动记录，读者可编辑 | 提交反馈；读取和修改概念状态 |
| M3 | 概念地图：装饰性地图底图，红色立体三角和连线，位置算一次后固定，读者可拖动和批注 | 读取和保存地图布局与批注 |
| M4 | 旁问：从原文、讲解或地图发起简短提问，答完回到主线 | 旁问请求 |
| M5 | 回顾与术语表、第二部分完整讲解、本篇位置与下一篇、读者文献库 | 对应各阶段的讲解对象 |

## 8. 待定

- 讲解是否逐段流式显示。M1 先整轮返回，只用 `status` 提示进度。
- 登录时自动启动服务、打包成桌面应用：先不做，双击启动用一段时间后再看。
- 只有 PDF 的论文：M1 用浏览器自带的阅读器显示，读者提问时手动填页码、粘贴原文。在页面里选中 PDF 文字、在文字层上高亮，需要引入 PDF 显示库，体积和是否进仓库待定。
- 导读之前提的、还没被回答的问题，后端存着但不在任何接口里返回。这时刷新页面，“你的问题”卡片会暂时不见，点“继续”得到回答后才出现。是否在历史接口里带上它，待定。
- 服务因为默认端口被占用而换了端口时，再双击一次启动文件找不到它，只会提示“同一数据目录是否已有服务运行”，不会打开页面。待后端改进。
