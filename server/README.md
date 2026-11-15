# 本机后端（M1 + M2）

复用 `paperread.py` 获取、解析和定位论文，通过同一组讲解规则接入 Claude Code、Codex 或兼容 Chat Completions 的模型 API。提供 HTTP、SSE、历史恢复和显示确认，所有数据留在 `PAPERREAD_HOME`（默认 `~/.paperread/`）。

## 启动

macOS 可以直接双击仓库根目录的 [start.command](../start.command)。它会找到 Python 3.10 或以上版本并打开页面；没有合适的 Python 时给出中文说明。默认端口已有本服务时直接复用，被其他程序占用时使用空闲端口，并打印实际地址。关闭启动服务的窗口，或在该窗口按 Ctrl+C，服务即停止；终端询问是否终止时选择“终止”。复用服务时新开的窗口不拥有旧服务，停止仍需关闭原窗口。

换过端口后，再次启动也会通过当前数据目录的 `.server.json` 找到原服务。该记录和锁、进程、服务标识共同核对，关闭时移除；不会借用另一个数据目录的服务。

从 GitHub 下载压缩包得到的文件，第一次双击可能被系统拦下，需要在访达里右键选“打开”。启动文件使用真实的 `~/.paperread/`，会清除继承的 `PAPERREAD_HOME`；测试和其他数据目录仍通过下面的命令行入口指定。它不会设置登录时自动启动，也没有后台常驻程序。

后端需要 Python 3.10 或以上，HTML 阅读只用标准库。命令行仍可在仓库根目录运行：

```sh
python3 -m server --open
```

默认地址是 `http://127.0.0.1:8765`，占用时以窗口打印的实际地址为准。默认选择已安装的 Claude Code，沿用本机登录；安装可用不等于已通过模型认证。`--port 8766` 可修改首选端口。关闭时取消排队任务、停止所属 CLI 进程组并中断活动连接；独立主进程对仍阻塞的网络线程最多再等待 3 秒。同一数据目录只启动一个服务。

服务在同一地址提供 `web/` 静态文件与 `/api`，无需另启静态服务器。没有前端文件时首页显示服务提示，API 仍可使用。

## 选择模型

将 `config.example.json` 复制到本机 `~/.paperread/config.json` 后编辑；如果已有配置，请修改而不是覆盖。该文件不进仓库，API 密钥不进入浏览器。也可以用 `PAPERREAD_HOME` 指定其他私有目录。

最小配置：

```json
{"backend": "claude"}
```

或者：

```json
{"backend": "codex", "timeout_seconds": 600}
```

API 模式：

```json
{
  "backend": "api",
  "timeout_seconds": 600,
  "max_retries": 1,
  "api": {
    "base_url": "https://your-provider.example/v1",
    "model": "your-model-id",
    "api_key": "your-local-key",
    "response_format": "json_schema"
  }
}
```

`base_url` 使用提供方的根地址，后端追加 `/chat/completions`。不支持严格结构输出的兼容提供方可改为 `json_object`，输出仍要通过本机校验。只允许 HTTPS；本机模型地址可用 loopback HTTP。选择后端可通过设置接口立即生效，无须重启；手改 API 或 CLI 配置后，需要重新选择后端（可选择同名后端）或重启以载入新配置。支持 CLI 的 `executable`、`model` 覆盖，完整字段见样例。

Claude/Codex 的 `model:null` 跟随命令行程序默认值；可选的 `model_suggestions` 是模型名字符串数组，允许空列表。请求和手写配置中的模型名均须以英文字母或数字开头，只能包含字母、数字和 `. _ : - [ ]`，最长 100 字符。模型建议不保证账号权限，保存时不消耗额度验证。

默认单次模型调用时限为 600 秒，可设为 30–600 秒整数；已有配置里的显式时限不会自动覆盖。API 网络重试共用本次时限；结构/定位修正的第二次模型调用另算完整时限，一轮最多接近两倍设置值。600 秒是本轮约定的可配置上限，不保证所有模型和网络环境都能在限时内完成；真实试跑与限制见 [计时记录](../docs/m1-round2-live-trial.md)。

CLI 每轮使用临时工作目录，关闭工具、MCP、自定义指令及相关能力，并限定时间与输出长度；需要支持这些开关的较新版本。CLI 模式当前支持 macOS/Linux，Windows 可用 API 模式。Claude 的安全模式保留本机认证助手和模型设置；Codex 使用忽略用户自定义配置的模式，需要自定义模型服务时可用 API 分支。任务不会把提示或密钥放到命令行参数中；结构 schema 会作为参数或临时文件传入。

**所有模型调用的 effort 固定为 `high`。** Claude Code 显式传入 `--effort high`，Codex 显式覆盖 `model_reasoning_effort="high"`，Chat Completions API 请求固定携带 `reasoning_effort:"high"`（字段见 [OpenAI 推理参数说明](https://developers.openai.com/api/docs/guides/reasoning#reasoning-effort)）。默认模型、自定义模型、切换后端和修正重试均沿用此规则，不增加 effort 选择项。API 提供方或模型须支持此参数，不支持时报告错误，不静默降级。此设置作用于阅读 Agent 的请求；更新代码后重启阅读服务生效。

## 前端对接

启动、设置、列表与上传接口见 [首轮交付](../docs/m1-launch-delivery.md)；模型名、时限、停止与排队问题恢复见 [第二轮交付](../docs/m1-round2-delivery.md)；概念记录、反馈、版本检查与重讲见 [M2 交付](../docs/m2-delivery.md)。

主体接口见 `docs/agent-architecture.md`。实现记录见 `docs/implementation.md` 第 21–23 节。

继续做浏览器联调时，先看 [给 Claude 的 M1 交接说明](../docs/claude-m1-handoff.md)，其中列出了前端待改位置、恢复流程和验收清单。

1. `POST /api/papers` 打开论文，随后读取 `/content` 与 `/explanations`。打开未读论文不自动生成；由读者点击“继续”开始。
2. 每次打开论文先说明本次基础，并确认或修改这篇论文已有的概念记录。`POST /api/papers/<paper>/explain` 请求体为 `{"mode":"continue","reading_context":"本次基础与目标","reading_session":"本页随机UUID","record_version":0}`，或带选中文本的 `ask`。`reading_context` 与当前 `record_version` 必填；版本过期返回 409，不启动模型。背景输入不自动登记到旧档案，本篇概念记录可保存并编辑，完整规则见 [M2 交付](../docs/m2-delivery.md)。
3. 使用返回的任务编号订阅 `/api/tasks/<task>/events`。`status` 和 `error` 是普通文字，`result` 是一个 JSON 讲解对象；每个任务只有一个终态。SSE 支持 `Last-Event-ID`，终态后连接关闭。
4. **卡片成功显示后**，调用 `POST /api/papers/<paper>/explanations/<id>/displayed`，请求体 `{}`。重开历史时同样调用；重复请求幂等。只有这一步才写阅读日志及实际讲过的概念。
5. 生成期间可 `POST /api/tasks/<task>/cancel`，请求体 `{}`；成功返回 `{task,cancelled:true}`。事件流以中文 error 结束，不保存本轮讲解，可立即重试；别的论文不受影响。已经结束的任务再次取消也成功，保留已保存结果；未知任务 404。API 断开本机连接不保证提供商立即停止计费。

首次任务只返回导读。首次选中的问题会保留，下一次继续回答。基础尚未补完时，`progress.next` 指回当前点；服务恢复这个位置，不按按钮次数递增。主线已结束时不会自动开启第二部分。

**前端必须接入显示回执后，继续按钮才能推进。** 缺回执时服务会恢复上一轮，避免跳过未显示的基础。`history` 是已经生成的对象列表，生成成功本身不证明已显示或已理解。

`assumes`、`unexpanded` 和导读提过的词不会自动写成 `explained`；显示回执只更新实际展开的概念，保留已有 `known/unsure` 状态。生成读取本篇最新已确认记录，`assumes.status/explained_at` 必须与记录一致，未记录概念使用 `new/null`；`previously` 仍为空。不从旧 `profile.json` 或旧日志迁移基础。“讲过”仍不等于“懂了”；最新一份逐点讲解收到 `unclear` 后，下次正常继续在原位置、原步骤换一种讲法。

## 设置、列表、提问与上传

- `GET /api/settings` 返回后端状态、`timeout_seconds`、Claude/Codex 的 `model/model_suggestions` 及 API 的 `base_url/model/key_configured`，不返回密钥、配置路径或 CLI 路径；`authenticated` 始终是 `null`。仅做本机安装/配置检查，不请求模型检查登录。所选模型和时限反映当前实际生效值。
- `PUT /api/settings` 接受 `backend/model/timeout_seconds` 的非空组合，例如 `{"backend":"codex","model":null,"timeout_seconds":450}`。不带 backend 时针对当前后端；API 不接受 model 字段。多余字段、非法模型名/时限或不可用后端返回 400；生成中返回 409，需完成或停止后再改。成功后 health 和下一轮讲解同步生效；原子保存保留其他配置及扩展字段，非法原文件不改写。
- `GET /api/papers` 返回 `{papers:[...]}`，每项含 `paper/title/authors/source/explanations/last_read`。`explanations` 是可恢复的讲解对象数，可能包含尚未显示的结果。`last_read` 只在显示回执时更新，生成不算阅读；重复回执只更新时间，不重复日志或概念状态。最近显示的在前，未读的在后；损坏缓存跳过。
- 讲解新增服务填写的 `asked`：含 `quote/question/where/page`，缺项为 `null`。非提问触发和导读为 `null`；导读前排队的问题记在之后回答它的讲解上。旧对象读回时补 `null`，不迁移原文件。该字段不进入模型输出 schema，也不把历史中的字段包装重新发给模型。
- `GET /api/papers/<paper>` 元数据带 `pending`：无待答问题时为 null，否则是同样四项的提问对象；取消生成不丢掉已排队的问题。回答保存后清空并转到讲解的 asked。历史接口仍返回数组，刷新时同时读取元数据恢复待答卡片。
- `POST /api/papers/upload` 接收原始 PDF 字节，不是 JSON 或 multipart。要求 `Content-Type: application/pdf` 和 Content-Length；可选 `X-Paper-Name` 是 UTF-8 文件名经百分号编码后的值。最多 50 MiB，文件头须为 `%PDF-`，逐块写入临时文件，成功和失败后均清理。返回与打开论文相同的元数据。文件名去除路径后用于现有本地导入标识和显示标题；同内容同安全文件名复用缓存。
- 本地 PDF 对外的 `source_url` 现在为 `null`，不会把 `file://` 本机路径传给页面或模型。PDF 文件仍通过 `/api/papers/<paper>/pdf` 获取。

## 数据与模块

- `service.py`：调用现有核心、准备阅读上下文、生成任务、重试验证、保存与续读。
- `schema.py`：导读和逐点对象的结构及位置校验，供三种后端共用。
- `backends.py`、`config.py`：模型通信和本机配置，公开健康检查不返回密钥或路径。
- `launcher.py`、`settings.py`、`catalog.py`：本机服务复用、设置原子切换、缓存列表。
- `http.py`：只监听 `127.0.0.1`，检查 Host/Origin，提供 JSON、SSE、PDF 和静态文件。
- `records.py`：概念记录的字段校验与状态转换，不执行文件读写。
- 每篇论文的 `agent-state.json` 保存完整讲解、待处理问题、显示回执和 M2 概念记录；继续使用 `log.jsonl` 与 `outline.md`，不再读写旧 `profile.json`。显示与反馈使用独立去重依据；M2 状态同锁原子写入。

论文、读者本次主动填写的基础和问题、本篇确认过的概念记录（含备注），以及必要的本篇旧讲法会送到选定的模型后端。不发送旧全局档案或整份日志；来源说明只取当前讲解需要的材料。模型获得的数据都是材料，服务不会执行材料中的命令。

## 当前边界

- 已实现 M1 与 M2 后端。M2 页面与真实阅读体验仍需联调；结构校验不能证明物理正确，也不能证明补基础的教学顺序真的合格。
- 全文上限为 160,000 字符，总提示上限为 260,000 字符。超过上限会明确报错，不悄悄截断后声称读过全文。模型自身的上下文限制可能更小。
- HTML 返回正文、公式和图表标题，图片尚未交给模型。PDF 需要本机 `pdftotext` 或可选 `pypdf`，按页抽取文字。扫描件、空白/图像页及无法可靠抽取的文档会报错；不支持 OCR、图像理解或保证数学符号无损。
- 本轮不启用外部检索工具。来源链接限定为给定论文及其参考文献中已有的链接；一般背景说明需明确未检索核验。链接存在和结构有效不能替代逐条证据核查。
- 模型输出结构不合格时只修正一次；API 暂时性网络错误另按配置最多重试两次。不会无限重试或保存残缺对象。
- SSE 任务保留最多约 128 项，重启后恢复持久化讲解而不是旧任务连接。每篇同时一项生成，全局最多四项等待或执行。
- M2 提供反馈与概念记录接口，页面由 Claude 对接；概念地图、旁问和文献库仍未实现。现有 Skill 的第二部分仍可单独使用。

## 验证

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

测试使用临时读者目录、合成论文和固定模型输出。HTTP 集成测试会启动临时 loopback 服务并在结束后关闭；不会修改真实档案。CLI 进程测试使用测试子进程，API 测试使用模拟响应。它们不构成真实用户理解增益的证据。
