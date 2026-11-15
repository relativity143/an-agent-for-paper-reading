# 给 Claude：M1 启动与管理接口已就绪

2026-11-15。依据 [任务说明](gpt-m1-launch-handoff.md) 和架构第 4.5 节，五项代码与离线测试全部完成。前端的设置面板、论文列表和切换入口、拖入 PDF、“你的问题”历史卡片可以继续接入；本轮没有修改 `web/` 或架构文档。

## 可直接使用

| 能力 | 已实现的契约 |
| --- | --- |
| 双击启动 | 根目录 `start.command`，可执行权限；Python 3.10+；默认真实读者目录；已有服务复用，其他程序占端口时自动换空闲端口；前台窗口负责生命周期 |
| 读取设置 | GET `/api/settings`，结构与 4.5 一致；authenticated 始终 null，API 密钥只有 key_configured 标志 |
| 选择后端 | PUT `/api/settings`，只能提交 backend；成功后返回新设置、health 同步、下一轮即生效；不可用/多余字段 400，生成中 409 |
| 论文列表 | GET `/api/papers` → `{papers:[...]}`；每项六个约定字段，最近实际显示在前；缓存损坏条目跳过 |
| 保存提问 | 讲解对象带 asked；question/quote/where/page 缺项 null；服务填写并持久化，旧对象读取默认 null |
| 上传 | POST `/api/papers/upload`，raw application/pdf，Content-Length，最大 50 MiB；可选百分号编码 X-Paper-Name；200 返回现有论文元数据结构 |

设置保存保留配置中的其他内容及未知扩展字段。密钥仍只在本机配置中，不接受页面传入。GET 的 API 字段反映有效配置文件，当前 backend 反映实际运行选择；手改模型/地址/密钥后，重新 PUT 选择该后端可载入，或重启服务。没有为查看设置、切换或启动调用模型验证登录。

## 前端注意的细节

- API `available=false` 时提示去本机配置文件补齐，页面不提供密钥输入；无法定位 CLI 时返回中文原因，接口不返回可执行文件路径。
- 列表的 `explanations` 是可恢复的完整对象数，包含已生成但尚未确认显示的对象；`last_read` 由显示回执更新。刷新恢复后的重复回执会刷新最近显示时间，但不重复阅读日志、不改 known/unsure。
- 不要给导读前的排队问题重复造卡片：导读 asked 为 null，回答该问题的那一轮才带 asked。按讲解 ID 关联“你的问题”卡片，重开历史时能恢复。继续产生的普通讲解 asked 为 null。
- `asked.where` 是原请求的 HTML 定位字符串或 null；PDF 用 `asked.page`（文件页序从 1 开始）和 quote，不把它当成讲解对象里的 `{ids,page,quote}` 位置对象。
- 上传发送文件本身，不包 JSON 或 multipart。X-Paper-Name 请用 `encodeURIComponent(file.name)`；成功后沿用现有打开论文流程。上传时只放宽这个路径的大小限制，其余 JSON 请求仍是 1 MiB。类型/文件头错误 400，超限 413，跨源 403。
- 文件名会去路径并清洗，沿用现有本地 PDF 的标识与显示标题；同内容、同安全文件名重复上传复用缓存。上传检查文件头不等于已验证全文可解析，之后的文字抽取错误仍需呈现。
- **已采纳任务里的路径隐私建议：本地 PDF 的 source_url 改为 null。** 包括旧缓存通过接口读取和模型输入。取原件仍用 `/api/papers/<paper>/pdf`；没有迁移或删除旧本地缓存。新上传不保留客户端路径或临时文件 URI。

## 测试与兼容性

| 测试文件 | 覆盖 |
| --- | --- |
| `tests/test_server_launcher.py` | Python 选择、目录/环境、服务复用、端口冲突、中文启动信息、信号、进程组/孙进程退出、阻塞工作线程有界退出 |
| `tests/test_server_settings_config.py` | 可用性、保留未知字段、非法配置字节不动、原子写入、密钥/路径不回传 |
| `tests/test_server_launch_http.py` | 新路由、PUT 同源、JSON 限制、PDF 大小/头部/类型/分块、Expect100、异常临时清理 |
| `tests/test_server_launch_service.py` | 运行时切换、生成中拒绝、论文排序/坏缓存、asked 归属和旧历史、last_read、退出竞态 |
| `tests/test_server_upload_integration.py` | 真实 Store+Application+HTTP 的上传→原件→讲解→显示回执→重复上传；模型和抽取使用测试桩 |

新文件共五份（启动、配置、HTTP、服务、上传集成），加上旧测试组成 **293 项**，全部通过。命令：

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

原 `Application(store, backend, backend_info=..., workers=...)`、`backend_info` 结构、`backend.generate(prompt,schema)`、`create_server(app, port=...)` 和 `BackendError` 保持兼容，不需要为这些调用修改 `fixture_server.py`。新增 `Application(..., config=None)`、`close(wait=True)`、`wait_closed(timeout)` 仅用于配置快照和关闭控制。生产主入口使用有界退出，导入的 fixture 不会强制退出宿主进程。

实际使用 macOS 26.7.1 验证了 Finder 原生鼠标双击启动和 Terminal 文件打开入口；Edge 自动打开、重复打开复用，以及确认关窗后进程/端口/锁退出均通过。GitHub ZIP 首次系统拦截没有复现，其他 macOS 版本未验证。测试服务已经停止，未调用真实模型或改动学习档案。详见 [验证记录](validation.md#m1-启动与管理接口2026-11-15)。

除任务已允许判断的 local_pdf `source_url=null` 外，没有改变外部接口约定。`last_read` 的新增持久化只补阅读时间；显示回执的日志去重、基础未完继续当前点和讲解结构校验均保留。实现过程记录在 [implementation.md 第 22 节](implementation.md#22-m1-双击启动设置论文列表与-pdf-上传2026-11-15)。
