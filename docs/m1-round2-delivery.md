# 给 Claude：第二轮模型设置、停止与恢复接口

2026-11-15。对应 [第二轮任务](gpt-m1-launch-handoff.md#第二轮任务2026-11-15) 与架构 §4.6。后端实现已经补齐；本轮未修改 `web/` 和架构文档。真实模型计时、样本与限制见 [试跑记录](m1-round2-live-trial.md)。

更新代码后请先停止旧的启动窗口，再双击启动，以加载这轮后端代码；刷新网页不会重载已经运行的 Python 服务。之后在设置接口修改模型和时限才是即时生效，无须每次重启。

验收结果：**347 项离线测试通过**。真实试跑启动 8 个案例；Codex 保存成功 4 例（包含补充提示后的定向复验），另有 1 例定位校验失败。Claude 默认模型与 Sonnet 各有 1 例在 600 秒超时，还有 1 例默认模型任务按试验策略取消。Claude 在本机环境下的成功生成仍未验证，原因未知；不要把提高时限写成已经解决。试验进程均已退出。

## 设置面板可接的内容

`GET /api/settings` 保留原字段，顶层增加 `timeout_seconds`。`backends` 中只有 Claude、Codex 增加 `model` 与 `model_suggestions`：

```json
{
  "backend": "claude",
  "timeout_seconds": 600,
  "backends": [
    {"name": "claude", "available": true, "configured": true, "authenticated": null,
     "model": null, "model_suggestions": ["sonnet", "opus"]},
    {"name": "codex", "available": true, "configured": true, "authenticated": null,
     "model": null, "model_suggestions": ["gpt-6.1-sol", "gpt-6-astra", "gpt-6.1-terra"]},
    {"name": "api", "available": false, "configured": false, "authenticated": null}
  ],
  "api": {"base_url": "", "model": "", "key_configured": false}
}
```

以上是结构示例，可用性取决于本机安装和配置，建议列表不代表账号有权使用全部模型。Claude 别名由本机 CLI 帮助确认，Codex 名称来自本机当日模型目录。读者可在 `config.json` 的 `claude.model_suggestions` / `codex.model_suggestions` 配置字符串数组，空数组也有效。查看或保存设置不调用模型、不验证登录。

`PUT /api/settings` 接受非空对象，只能包含 `backend`、`model`、`timeout_seconds` 的任意组合。例如：

```json
{"model": "sonnet", "timeout_seconds": 450}
```

```json
{"backend": "codex", "model": null}
```

未给 backend 时修改当前实际运行的后端；`model:null` 恢复 CLI 自己的默认模型，省略 model 则保留已有选择。API 的模型仍只能在本机配置文件修改，选择 API 时携带 model（包括 null）均返回 400。模型名限制与 §4.6 一致，请允许读者输入合规的自定义名称。手写配置也使用相同校验。时限是 30–600 秒整数，布尔值、小数不接受。

生成中修改设置返回 409，先完成或停止任务。无效字段、不可用后端、非法模型名/时限返回中文 400；配置文件保持不变。成功写入保留原有密钥和扩展字段，下一轮立即使用新设置，health 的 backend 同步。GET 显示当前实际生效的所选模型与时限；直接修改配置文件后，重新 PUT 选择后端或重启才生效。未选后端与建议列表可以反映本机配置的新内容。密钥和本机路径仍不返回。

## 停止按钮

生成期间保存 explain 返回的 task，点击停止发送：

```http
POST /api/tasks/<task>/cancel
Content-Type: application/json

{}
```

成功返回 `200 {"task":"<task>","cancelled":true}`。此布尔值表示停止请求已处理；已经结束的任务也返回相同成功结果，不会删除先前保存的讲解。未知/已淘汰任务返回 404，多余请求字段 400，跨源请求 403。

未结束任务的 SSE 以 `error`、文字 **“已停止这一轮。”** 结束。只结束这一任务所属的 CLI 进程组或 API 连接；另一篇论文不受影响。同篇可以立即再发 explain，没有其他活跃任务时可以立即改设置。尚在排队的任务不会再调用模型。取消与保存互斥：取消先发生则不写本轮讲解；若保存及 result 已完成则保留结果。

不要把取消视为阅读成功，不发 displayed 回执。API 连接断开不保证提供商立即停止远端计算或不再计费。极少数本机进程清理失败时返回中文错误并保留忙碌状态，允许再次停止；不把清理失败包装成成功。

## 刷新后恢复排队问题

**新增字段提议，已实现：论文元数据增加 `pending`。** `GET /api/papers/<paper>` 返回：

```json
{
  "paper": "example",
  "title": "...",
  "authors": [],
  "source": "arxiv_html",
  "source_url": "https://arxiv.org/html/example",
  "steps": null,
  "pending": {"quote": "...", "question": "这个术语是什么？", "where": "S1.p1", "page": null}
}
```

没有待答问题时 pending 为 null；四项缺失值均为 null，含义与 asked 相同。导读前的提问在生成导读时已排队，生成或停止后仍可恢复；导读的 asked 仍为 null。回答成功保存后 pending 清空，问题转到那份讲解的 asked。页面按“元数据中的 pending + 历史讲解中的 asked”恢复卡片，避免重复。

保持 `GET /api/papers/<paper>/explanations` 的数组格式，未新增历史包装对象或迁移旧记录。读取 HTML `/content` 时也包含元数据 pending；PDF `/content` 仍只返回原有 pdf 地址，所以统一恢复时请读元数据。旧状态缺少 pending 时返回 null，不改写文件。请 Claude 将该新增字段补入架构文档。

## 时限与报错

配置未显式填写时限时默认 **600 秒**；已有读者配置里的显式 120 秒仍被保留，可通过设置调整。一次 `backend.generate` 共用一个时限，包含 CLI 能力检查、模型生成或 API 的网络重试。结构/定位不合格时，服务最多额外修正一次，第二次有独立完整时限；因此一轮最多接近两倍设置时限，另有本机解析及清理时间。状态流会说明正在修正，期间仍可停止。实测 Claude 默认模型超过 300 秒仍未返回，因此没有采用 300 秒；600 秒取约定上限，不能据此保证所有案例都成功。

后端和 HTTP 请求层报错均改为中文，区分超时、未安装、版本过旧、认证/权限、模型名可能不对等，并给出下一步。只分类有限长度的 CLI 诊断，不向页面回传原始 stderr、提供商响应或凭据。

真实 PDF 试跑还发现模型会逐个补定位字段，耗完一次修正机会。已在提示中明确所有 HTML/PDF 位置对象的既有要求，特别是 PDF 的页码和原文摘录均不可为 null；未放宽 schema、定位校验或增加重试次数。补充前后的运行在试跑记录中分开，不能作为同条件模型性能比较。

## 启动恢复、兼容与验证边界

启动器将实际地址记录在当前数据目录的 `.server.json`，与 `.server.lock` 一起核对 PID、版本、目录和随机实例标识。默认端口被占用后，再次启动可以找到同一服务的实际地址。记录仅含本机回环地址，权限 0600；退出只删除自己的记录，不自动清理其他实例或崩溃遗留锁。已有默认目录旧版服务的复用保留兼容。

`Application` 构造方法、`backend.generate(prompt,schema)`、`create_server` 和原有 fixture 调用保持兼容。取消通过任务作用域管理，不要求 Claude 修改假模型的 generate 签名。任务只能按自身 ID 清理，不关闭共享后端。基础先行、显示回执、已讲/已懂/不确定区分和原有输出校验保持不变。

离线测试位于 `test_server_round2_settings.py`、`test_server_round2_backends.py`、`test_server_round2_service.py`、`test_server_round2_launcher.py`；覆盖设置原子保存、模型参数、取消竞态/进程组/连接、HTTP 同源与中文错误、排队问题、换端口复用与退出。全量结果在 [验证记录](validation.md)。所有自动测试使用隔离目录和测试桩；真实试跑独立进行。

本轮没有替 Claude 完成前端模型控件、时限输入、停止按钮、pending 卡片恢复，也未重复 Finder 人工双击。启动回归使用真实子进程和临时端口；API 的真实付费生成、其他操作系统和真人理解增益没有验证。

## Claude 下一步对接清单

1. 设置面板给 Claude/Codex 增加“跟随默认”、建议列表和自定义模型名；API 分支不发送 model。增加 30–600 秒整数输入，一次 PUT 保存改动，成功后使用返回的完整设置刷新面板。
2. 生成时显示“停止这一轮”，使用当前 task 调用 cancel；收到终态后解除忙碌。不要把停止后的 error 当成理解确认，也不要清掉之前已经显示的讲解。
3. 刷新/换论文时读取元数据 pending，与历史 asked 合并恢复问题卡片；答复保存后用新的状态去重。已取消但仍待答的问题要保留。
4. 在 fixture 中补“运行中停止→马上换模型”“停止A不影响B”“重开首轮待答问题”“时限/模型错误”和“已完成结果再次停止”场景；现有假模型 generate 的签名无需改变。
5. 将已实现的 pending 字段和 cancel 成功响应补到架构文档；实际页面联调后另记结果，本交付不代替浏览器验收。
