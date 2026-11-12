# 给 Claude 的交接：完成 M1 前后端联调

更新日期：2026-11-12。请接着完成浏览器里的真实阅读闭环：打开论文 → 导读 → 显示确认 → 补必要基础 → 继续当前点或下一点 → 刷新恢复。重点保持“读者缺领域基础和基本术语时先补基础”的定位。

## 接手状态

- 后端已提交并推送：[9a95285 — Add M1 local reading service and model backends](https://github.com/relativity143/an-agent-for-paper-reading/commit/9a95285)。本机仓库为 `/Users/a666/Desktop/git/an-agent-for-paper-reading`。
- 本次交接时，`web/` 仍是本机未跟踪目录，`docs/agent-architecture.md` 和 `.gitignore` 有前端阶段的未提交修改。它们没有包含在上述后端提交里；请接着使用这些文件，先检查实际状态，不要覆盖或清理。换机器只拉 GitHub 会缺少这份前端。
- 前端与架构文档继续由 Claude 负责。后端缺陷请写出请求、预期、实际结果和复现步骤，交给 Codex 修；不要通过删掉校验或提前写“已显示”来绕过问题。
- 本轮只做 M1 联调，不顺带展开地图、反馈状态编辑、旁问、文献库或第二部分。

先读 [后端运行与对接说明](../server/README.md)、[实现记录第 21 节](implementation.md#21-独立-agent-m1-后端2026-11-12)，再对照 [架构文档](agent-architecture.md)。完整字段以 [schema.py](../server/schema.py)、[合成样例](../tests/fixtures/agent_explanations.json) 和已有 `web/mock/` 为准。请将第 21 节已经实现的约定合并回架构文档，特别是显示回执、首次请求只返回导读、`next` 可指回当前点或为 `null`。

## 按这个顺序对接

### 1. 先接显示回执，并避免重复卡片

当前 `web/js/api.js` 缺回执方法；`web/js/app.js` 的 `requestExplanation()` 和 `start()` 都只添加卡片。后端因此会反复恢复最后一轮，而不会往下讲。

在真实 API 层增加方法，调用：

```http
POST /api/papers/<paper>/explanations/<id>/displayed
Content-Type: application/json

{}
```

成功返回 `{"id":"<id>","displayed":true}`。样例模式提供同名的空实现，不能写真实读者数据。

- 新结果：收到 `result` → 验证/渲染并插入可读卡片 → 提交回执 → 成功后开放继续。不能在刚收到 SSE、卡片还未显示时提交。
- 历史恢复：按时间顺序渲染 `/explanations` 的对象，已成功显示的对象分别提交回执；成功后开放继续。使用可等待的循环，避免 `forEach(async ...)` 导致提前放开按钮。
- 回执失败：保留卡片，提示“记录显示状态失败”，提供重试同一回执；不能重跑模型或把讲解当作未生成。刷新后可以补回执。
- 后端回执幂等，但前端也需要按 `paper + id` 去重。当前 `addExplanation()` 虽然更新 Map，却每次都追加 DOM；重发同一对象时应复用已有卡片。
- “显示成功”只表示界面已经呈现内容，不是读者看懂。`known` 只能来自明确确认；M1 没有确认懂了的编辑接口。

### 2. 保留首轮导读、排队问题和当前点

每项任务只返回一份讲解，首次一定是 `overview`。首次直接 `ask` 时，问题存在后端 `pending` 中；导读显示并确认后，用户再点“继续”，才回答这个问题。不要为了尽快拿到 `point` 自动递归调用 `explain`，也不要把第一次提问的输入清空后弄丢。

当前页面启动会自动请求导读，因此“首次 ask”要通过专门的交互测试或固定请求场景覆盖，不能因为常规打开流程没遇到就省略。

基础未讲完时，`progress.next` 可能仍是当前点。同一个点可以有不同讲解 ID，必须显示为不同轮；只对相同 ID 去重。前端不自行将步骤加一，不用下一点的进度覆盖本轮实际位置。

`progress.next === null` 表示当前主线结束；`step`、`steps_total`、`section` 可以为空。保留提问能力，清楚呈现主线已结束，不自动进入第二部分。导读的 `next` 也允许为空，当前 `cards.js` 的 `overviewCard()` 直接访问 `x.next.text/where`，需加分支。

### 3. 导读之后刷新主线与论文信息

首次 `/content` 请求发生在导读生成之前，因此 HTML 的 `steps` 可能是空数组。生成导读后后端才写 `outline.md`；当前 `app.js` 不更新 `state.content.steps`。

可以从 `overview.route.steps` 同步前端主线，或请求已有 `GET /api/papers/<paper>` 读取最新元数据。历史恢复也要同步一次。定位栏表示“现在讲到哪里”，不要仅因当前位置是第 4 步，就认定前 3 步都实际讲过或已经理解。

`renderSteps()` 当前把步骤名直接拼入 `innerHTML` 的 `title` 属性。这些文字可能来自模型或大纲，需要用 DOM 属性赋值或一致的转义方式处理；含引号、尖括号的步骤名只能显示为文字。

### 4. 完成 PDF 的最低可用路径

现有 `paper.js` 已有 PDF iframe 和按 `highlights.page` 跳页的分支，可以继续使用。需要补齐以下接口差异：

- PDF 的 `/content` 当前只返回 `{"pdf":"/api/papers/<paper>/pdf"}`，不带标题、作者或 `steps`。合并 `openPaper()` 返回的元数据，或另取 metadata；当前 `app.js` 直接取 `content.title` 会丢失标题。
- PDF 页码是文件页序，从 1 开始。提问发送 `page`、`quote`、可选 `question`，不捏造 HTML `where`。纯问题不带选中文字时可以只传 `question`。
- 浏览器自带 PDF 阅读器的选择文本不能直接复用父页面的 `window.getSelection()`。M1 至少提供手工页码与原文片段输入；若要实现自动选段，需要 PDF 文字层，不能声称 iframe 已实现这一能力。
- `cards.js` 的 `whereChips()` 当前把 PDF 页码做成普通 span；概念的“PDF 第 N 页”字符串又被当成 HTML ID。统一 PDF 页码入口，让点击页码能跳页；找不到文字时只定位页，不伪造高亮。
- 扫描件、无法抽取的页面、过长全文要显示后端错误并允许恢复操作，不用空内容继续生成。图片理解、OCR、精确 PDF 文字层高亮不在本次最低验收范围。

### 5. 处理错误、断线和真实/样例模式

`status`、`error` 的 SSE data 是普通文字；`result` 是 JSON。保留现有“一项任务一份结果”的处理方式，终态后关闭连接。

当前 `api.js` 将业务 `error` 与原生 EventSource 网络错误都立即关流。请区分两者：业务错误显示说明；网络中断则做有界重连，或明确提示刷新恢复。不能一断线就再次 POST 生成，以免重复耗费模型请求。相同 EventSource 可用事件 ID 恢复；手工新建连接无法直接设置 `Last-Event-ID` 时，可重放同一任务并按事件/讲解 ID 去重。不要假设后端支持未实现的游标查询参数。

服务重启后旧任务编号失效，但 `/explanations` 仍能恢复已保存的对象。模型生成失败则不能伪造成功卡片或回执。409 表示已有任务或容量限制，不代表论文坏了。

`health` 只表示本机后端安装/配置情况，`authenticated` 目前为 `null`；不要显示成“已认证”。后端不可达时可以进入明确标示的样例模式，真实请求失败后不能悄悄换成样例讲解。

### 6. 同源运行并收尾静态资源

从仓库根目录运行 `python3 -m server --open`，由同一服务同时提供前端和 `/api`。不要另启一个不同端口的静态服务来请求 API；后端会检查 Host/Origin。

目前 `web/index.html` 的 KaTeX 仍来自 CDN，与架构中的离线静态资源目标不一致。请把需要的脚本、样式、字体及许可证放入 `web/vendor/` 并改为本地路径，验证断网仍能显示公式；暂时显示 LaTeX 原文只能记为降级，不算离线公式渲染已经完成。

## 联调与验收

先用合成材料与固定输出验证交互。可参考 [HTTP 全链路测试](../tests/test_server_integration.py)、[续读与档案测试](../tests/test_server_service.py)；目前没有可直接打开网页的 `--backend mock` 服务选项，不要臆造该命令。`?mock=1` 只用于前端样例展示，不能替代真实接口联调。

真实模型试读使用公开论文，并设置独立测试数据目录，避免改动默认阅读记录。需要重启恢复时继续使用同一个测试目录。启动前检查是否已有可复用服务，不要重复占用 8765。

```sh
cd /Users/a666/Desktop/git/an-agent-for-paper-reading
PAPERREAD_TEST_HOME="$(mktemp -d -t paperread-m1)"
export PAPERREAD_HOME="$PAPERREAD_TEST_HOME"
python3 -m server --port 8766
```

在浏览器打开 `http://127.0.0.1:8766/?paper=<公开论文的arXiv编号>`。默认 Claude 使用本机认证。若配置其他模型，只在这个测试目录放配置，不要覆盖用户已有配置、记录或认证信息。

| 场景 | 应看到的结果 |
| --- | --- |
| 新论文首次打开 | 导读出现，主线更新；成功显示后才有 overview 日志；导读提过的词不自动计作补讲 |
| 第一轮补基础 | 两个必需栏保留；每个概念先讲在本文中的用途，再讲基础；回到原文不抢在基础之前 |
| 基础分两轮讲 | 当前点和步骤不跳走，第二轮确实接着补；相同点的两个不同 ID 都保留 |
| 首次选段提问 | 先有导读；点继续后回答原问题，不丢问题，也不自动连发讲解 |
| 回执失败/重复回执 | 卡片仍在，可重试；同一讲解不重复追加 DOM、日志或概念记录 |
| 刷新/重启 | 历史恢复、回执补齐、从原位置继续，不重新生成已有卡片 |
| 模型失败/网络断开 | 明确提示且按钮可恢复；无无限重试，无伪造讲解或自动切样例 |
| PDF | 标题正常、原件能打开、页码能跳转；手工页码+原文可提问；不冒充精确高亮 |
| 原文公式与表格 | 段内公式不重复；表格标题可见；点击有效 HTML 位置能定位 |
| 空值与不可信文字 | `next:null`、未知步骤和空数组不崩溃；含引号/尖括号的步骤名不会作为 HTML 执行 |
| 档案边界 | 继续不写 known；重复讲解不覆盖原有 known/unsure；没展开的词不算已讲 |

最后运行 `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v`，补上必要的前端行为测试，并真实操作浏览器检查主流程。不要仅凭 HTTP 测试或截图就宣布完整联调通过。记录测试论文、模型、操作结果、仍有的限制和提交号；停止本轮启动且不再使用的服务，确认退出。

## 已有证据与尚未验证的部分

后端提交时全套 **228 项测试通过**；真实 loopback 流程、一键启动和退出已检查。Claude 仅做过一次极小的真实结构化请求，成功返回 `{"ok":true}`，尚未据此证明真实论文讲解可用。Codex/API 没有真实生成通过的记录。具体证据见 [验证记录](validation.md#独立-agent-m1-后端2026-11-12)。

Claude 曾因 `--setting-sources ""` 屏蔽本机认证助手而启动失败，已修复；不要重新加回这个选项或仅据旧错误要求用户重登。工具关闭、安全模式与空 MCP 设置仍应保留。

完整项目说明已经在 README、架构文档和后端说明中，不需要重写产品定位。若需要查讲解规则，直接读项目的 [paper-foundations/SKILL.md](../skills/paper-foundations/SKILL.md) 和其引用文件；它用于核对阅读行为，不是让开发联调改动真实读者档案。开发调试可按需要使用当前宿主的诊断或前端测试能力，无须安装额外 Agent 框架。
