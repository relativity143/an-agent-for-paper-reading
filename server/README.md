# M1 本机后端

复用 `paperread.py` 获取、解析和定位论文，通过同一组讲解规则接入 Claude Code、Codex 或兼容 Chat Completions 的模型 API。提供 HTTP、SSE、历史恢复和显示确认，所有数据留在 `PAPERREAD_HOME`（默认 `~/.paperread/`）。

## 启动

后端需要 Python 3.10 或以上，HTML 阅读只用标准库。在仓库根目录运行：

```sh
python3 -m server --open
```

地址是 `http://127.0.0.1:8765`。默认选择已安装的 Claude Code，沿用本机登录；安装可用不等于已通过模型认证。`--port 8766` 可修改端口。按 Ctrl+C 停止，正在运行的模型请求会在结束或超时后回收；同一数据目录只启动一个服务。

服务在同一地址提供 `web/` 静态文件与 `/api`，无需另启静态服务器。没有前端文件时首页显示服务提示，API 仍可使用。

## 选择模型

将 `config.example.json` 复制到本机 `~/.paperread/config.json` 后编辑；如果已有配置，请修改而不是覆盖。该文件不进仓库，API 密钥不进入浏览器。也可以用 `PAPERREAD_HOME` 指定其他私有目录。

最小配置：

```json
{"backend": "claude"}
```

或者：

```json
{"backend": "codex", "timeout_seconds": 180}
```

API 模式：

```json
{
  "backend": "api",
  "timeout_seconds": 120,
  "max_retries": 1,
  "api": {
    "base_url": "https://your-provider.example/v1",
    "model": "your-model-id",
    "api_key": "your-local-key",
    "response_format": "json_schema"
  }
}
```

`base_url` 使用提供方的根地址，后端追加 `/chat/completions`。不支持严格结构输出的兼容提供方可改为 `json_object`，输出仍要通过本机校验。只允许 HTTPS；本机模型地址可用 loopback HTTP。配置修改后重启服务。支持 CLI 的 `executable`、`model` 覆盖，完整字段见样例。

CLI 每轮使用临时工作目录，关闭工具、MCP、自定义指令及相关能力，并限定时间与输出长度；需要支持这些开关的较新版本。CLI 模式当前支持 macOS/Linux，Windows 可用 API 模式。Claude 的安全模式保留本机认证助手和模型设置；Codex 使用忽略用户自定义配置的模式，需要自定义模型服务时可用 API 分支。任务不会把提示或密钥放到命令行参数中；结构 schema 会作为参数或临时文件传入。

## 前端对接

主体接口见 `docs/agent-architecture.md`。本轮新增细节记录在 `docs/implementation.md` 第 21 节。

继续做浏览器联调时，先看 [给 Claude 的 M1 交接说明](../docs/claude-m1-handoff.md)，其中列出了前端待改位置、恢复流程和验收清单。

1. `POST /api/papers` 打开论文，随后读取 `/content` 与 `/explanations`。
2. `POST /api/papers/<paper>/explain` 请求体为 `{"mode":"continue"}`，或带选中文本的 `ask`。
3. 使用返回的任务编号订阅 `/api/tasks/<task>/events`。`status` 和 `error` 是普通文字，`result` 是一个 JSON 讲解对象；每个任务只有一个终态。SSE 支持 `Last-Event-ID`，终态后连接关闭。
4. **卡片成功显示后**，调用 `POST /api/papers/<paper>/explanations/<id>/displayed`，请求体 `{}`。重开历史时同样调用；重复请求幂等。只有这一步才写阅读日志及实际讲过的概念。

首次任务只返回导读。首次选中的问题会保留，下一次继续回答。基础尚未补完时，`progress.next` 指回当前点；服务恢复这个位置，不按按钮次数递增。主线已结束时不会自动开启第二部分。

**前端必须接入显示回执后，继续按钮才能推进。** 缺回执时服务会恢复上一轮，避免跳过未显示的基础。`history` 是已经生成的对象列表，生成成功本身不证明已显示或已理解。

`assumes`、`unexpanded` 和导读提过的词不会自动写成 `explained`；已有 `known` 和 `unsure` 也不会被覆盖。没有读者确认，模型不能把概念显示为 `known`。

## 数据与模块

- `service.py`：调用现有核心、准备阅读上下文、生成任务、重试验证、保存与续读。
- `schema.py`：导读和逐点对象的结构及位置校验，供三种后端共用。
- `backends.py`、`config.py`：模型通信和本机配置，公开健康检查不返回密钥或路径。
- `http.py`：只监听 `127.0.0.1`，检查 Host/Origin，提供 JSON、SSE、PDF 和静态文件。
- 每篇论文新增 `agent-state.json`，保存完整讲解、待处理问题和显示回执；既有 `log.jsonl`、`profile.json`、`outline.md` 继续使用。服务记录带 `explanation_id`，用于中断后的去重。

不把自由填写的个人背景、身份或确认原话送给模型；仅发送学科层级、概念状态和相关阅读位置。论文与读者主动提交的问题会送到选定的模型后端。模型获得的数据都是材料，服务不会执行材料中的命令。

## 当前边界

- 这是后端 M1。前端显示回执、PDF 与真实阅读体验需要联调；结构校验不能证明物理正确，也不能证明补基础的教学顺序真的合格。
- 全文上限为 160,000 字符，总提示上限为 260,000 字符。超过上限会明确报错，不悄悄截断后声称读过全文。模型自身的上下文限制可能更小。
- HTML 返回正文、公式和图表标题，图片尚未交给模型。PDF 需要本机 `pdftotext` 或可选 `pypdf`，按页抽取文字。扫描件、空白/图像页及无法可靠抽取的文档会报错；不支持 OCR、图像理解或保证数学符号无损。
- 本轮不启用外部检索工具。来源链接限定为给定论文及其参考文献中已有的链接；一般背景说明需明确未检索核验。链接存在和结构有效不能替代逐条证据核查。
- 模型输出结构不合格时只修正一次；API 暂时性网络错误另按配置最多重试两次。不会无限重试或保存残缺对象。
- SSE 任务保留最多约 128 项，重启后恢复持久化讲解而不是旧任务连接。每篇同时一项生成，全局最多四项等待或执行。
- 不包含反馈界面、概念地图、旁问和文献库；现有 Skill 的第二部分仍可单独使用。

## 验证

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

测试使用临时读者目录、合成论文和固定模型输出。HTTP 集成测试会启动临时 loopback 服务并在结束后关闭；不会修改真实档案。CLI 进程测试使用测试子进程，API 测试使用模拟响应。它们不构成真实用户理解增益的证据。
