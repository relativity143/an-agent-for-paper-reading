# Claude 长请求超时诊断

2026-11-15。承接 [M1 第二轮真实试跑](m1-round2-live-trial.md) 中两次 600 秒超时，在用户已授权使用额度的范围内追加 **3 次真实调用**。所有请求显式固定 `--effort high`，未修改用户认证、代理、默认模型或供应商配置。

## 结论与边界

本轮观察到的显著延迟发生在 **Claude CLI 等待上游响应流完成** 的阶段：内容块已经完整到达后，连接继续发送 ping，最终 `message_delta`、`message_stop` 与 `result` 才一起出现。CLI debug 对两次长请求分别报告 **90.7 秒和 51.2 秒的 streaming stall**。结构化校验本身只用了 2 毫秒；收到流完成后，CLI 的 engine 结束只需约 14–16 毫秒。未发现请求重试、退避、认证错误或超时错误。

本机继承环境与 Claude 用户设置都配置了**自定义 Anthropic 兼容端点，并非 `api.anthropic.com`**。因此不能把 debug 中的 `firstParty` 路由标签当作直连官方服务的证明。日志支持将本轮延迟定位到 CLI 所见的上游流完成阶段；没有服务端或链路抓包证据，**不能进一步断言是兼容网关、其模型供应商，还是中间链路造成**。

本轮三次调用均成功退出，**没有复现之前的 600 秒超时，也没有证明之前两次必然由同一原因造成**。旧记录只保存了本机 `effortLevel=xhigh` 配置，没有逐次有效 effort；本轮固定 high，不能视为完全同条件性能对照。没有重新试默认 Opus，也没有宣称所有模型均已恢复稳定。

## 有界试验

使用已安装的 Claude Code **2.1.322**。请求模型固定为 `sonnet`，CLI 初始化和响应均报告 `claude-sonnet-5-5`；这属于客户端/响应报告，不是对兼容端点底层模型的独立核验。

调用沿用 `CLIBackend._claude_command` 的隔离参数：空临时工作目录、关闭普通工具、MCP、hooks、skills、Chrome 与会话持久化；保留现有本机认证。诊断时仅将输出改为 `stream-json` 并增加 verbose、partial events 和本地 debug，以记录阶段时间。生产配置仍使用原有 JSON 输出；这不是完整浏览器联调，也不是对生产 `--output-format json` 路径的新增实测。

长请求复用上一轮的公开 [2203.00107v5 PDF](https://arxiv.org/pdf/2203.00107v5)，共 17 页。导读提示在本次读者说明功能修改前冻结，保留完整正文、工作流说明与 overview schema，不是缩短的论文摘要。对照只移除 `--json-schema`，把同一 schema 附在文本提示中；所以两条提示长度稍有不同。

| 请求 | 提示字符数 | 上限（秒） | 首个模型事件（秒） | 内容完成/内部验收（秒） | 最终 result（秒） | 进程结束（秒） |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 短布尔 schema | 47 | 100 | 5.590 | 5.696 | 5.696 | 6.216 |
| 真实 PDF + schema | 121,720 | 180 | 8.057 | 63.164 | 153.829 | 154.451 |
| 同 PDF + 文本 JSON 对照 | 125,579 | 180 | 7.051 | 45.954 | 97.060 | 97.578 |

时间由同一有界本地 runner 的单调时钟记录，包含 CLI 启动；“首个模型事件”是 `message_start`，不是最终可用结果。短请求只验证认证、连接和最小结构返回，不能代替真实论文试跑。

## 分阶段证据

真实 schema 请求：

- 8.057 秒：收到模型 `message_start` 与 thinking 事件，启动和认证阶段已通过。
- 54.043 秒：thinking 内容块完成。
- 62.953–63.164 秒：`StructuredOutput` 内容块到达，内部工具回执成功。debug 记 `outcome=ok`、`durationMs=2`。
- 此后只看到 9 个 ping，没有新的模型请求或结构重试。
- 153.829 秒：`message_delta`、`message_stop`、最终成功 result 到达。debug 报 1 次 streaming stall，共 90.7 秒；约 16 毫秒后 engine 结束。

文本 JSON 对照：

- 7.051 秒收到 `message_start`；34.889 秒开始文本输出，45.954 秒完整文本块到达。
- 后续出现 5 个 ping；97.060 秒才收到最终结束与成功 result。
- debug 报 1 次 streaming stall，共 51.2 秒；约 14 毫秒后 engine 结束。没有启用 `StructuredOutput` 工具，也没有结构重试。

因此，结构化输出机制**不是尾部等待出现的必要条件**。两次推理量不同，样本各只有一次，不能把总耗时差直接解释成 schema 的固定成本。两份 debug 日志均未出现 retry/backoff、error、timeout 或 abort 记录。

收到完整 result 后，两份长结果都经过现有 `CLIBackend._parse_output`、`validate_explanation` 和 `Application._check_result`，字段与 PDF 定位校验通过。没有写入用户阅读历史，也没有因此确认物理内容正确、逐字核对每个 quote，或证明当前新增读者说明流程的学习效果。

## 处理决定

**本轮仅新增诊断记录，不猜测性修改后端、时限或校验器。** 已有 600 秒仍是有界配置，不是所有请求的完成保证；这次用 180 秒诊断上限已经拿到完整结果。

保留“最终 result 成功后才能交付”的判定，不把 `StructuredOutput` 中间工具块提前当作完成。Claude 官方文档明确说明应读取最终 `structured_output`，中间结构化结果还可能被重试或模型回退撤回：[结构化输出说明](https://code.claude.com/docs/en/agent-sdk/structured-outputs)。流事件、结构化输出和 debug 参数参见[官方 CLI 参数说明](https://code.claude.com/docs/en/cli-reference)。

如再次出现 600 秒超时，按以下顺序复核，不能仅增加等待时间：

1. 固定同一模型、high 和提示/schema 哈希，沿用现有 600 秒总时限；临时启用 `--output-format stream-json --verbose --include-partial-messages --debug-file`，debug 文件放在访问受限目录。
2. 只汇总首字节、最后内容块、ping 数、最终 `message_stop/result` 与 engine 结束时间，以及 retry/error 次数；不要把原始提示或认证字段带入报告。
3. 将同一请求的 UTC 时间范围与兼容端点的服务端请求结束记录对齐，判断结束信号何时离开上游、何时到达 CLI。没有该证据前，不把客户端观察到的 stall 具体归责于某家服务或本机代理。

本轮获得两条尾部延迟证据后已停止新增真实调用，未切换供应商、绕过认证或修改代理。

## 复核与收尾

- 三个 Claude 进程均单独建立会话/进程组，结束后执行本任务所属组清理。随后按具体 PID/PGID 核验：**本次 runner 与模型进程无残留**。用户的 Claude Desktop、现有阅读服务及其他共享服务未停止。
- 本次专属临时诊断目录（原始流、debug、冻结提示复制件及摘要）已整体移入用户的可恢复清理目录，权限保持仅本人可访问；运行位置的缓存已清空，未永久删除或加入仓库。原试验样本和用户文件未动。仓库不保存凭据、自定义端点 URL、完整提示或原始模型回复。
- schema SHA-256：`57fca5e50b5074f4c1dcb73aa4b0d598fc5e73a02e3c6b105be6c925c9fc4cae`。
- 真实 schema 提示 SHA-256：`6f12087a5488fe71f9f52087e6d3caac67d4270af8d678712dd81b8852f64eac`。
- 文本 JSON 对照提示 SHA-256：`22832d8700f303ef6953302f82e133ec9692ef307f762851112318cfa8b1389d`。
- 三次 CLI 合计报告 input_tokens=12、cache_creation_input_tokens=89,840、output_tokens=29,729、thinking_tokens=22,631。保留各原字段，不把缓存与输入重复合并，不将 CLI 估价视为实际账单。
