# AegisFlow 电商售后多智能体决策工作台

面向客服的售后建议系统。LangGraph Supervisor 按计划逐步调度 Planner、Tool、Memory、Reviewer。模型负责拆解、归纳和生成建议，业务规则负责资格、金额、证据和权限审核。系统不实际执行退款或赔付。

## 运行

使用 Python 3.11 或更高版本：

```powershell
python -m pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

按 `.env.example` 配置 `.env` 中的 OpenAI 兼容模型服务。模型需支持强制 Function Calling；生成失败会记录原因并转人工，不静默切回文本解析。工作台位于 http://127.0.0.1:8000 ，API 文档位于 /docs。

默认 DEMO_AUTH=true，浏览器获得随机、服务端签发的演示客服身份，权限等级固定为 1。刷新后仍使用同一 HttpOnly Cookie。演示数据是模拟订单、规则与已核验证据，历史案例也明确标记为模拟来源。

生产模式设 DEMO_AUTH=false，并在服务端配置 API_USERS：

```json
{"secret-agent-token":{"actor_id":"staff-1","tenant":"demo","level":1,"allowed_ticket_ids":["T20260203002"]},"secret-reviewer-token":{"actor_id":"reviewer-1","tenant":"demo","level":3}}
```

工作台“客服身份”入口用配置的 API 凭据登录。服务端决定身份、租户、工单范围和等级，客户端不能设置权限。高级客服只能处理同租户人工队列。外部接口也可使用 Authorization: Bearer 凭据。HTTPS 部署应启用 COOKIE_SECURE；跨域来源只按 ALLOWED_ORIGINS 白名单开放。

## 客服会话与工单管理

首页是客服聊天界面，支持直接描述售后问题、粘贴订单号或选择关联订单。信息不足时先追问；诉求齐全后由 Intake 用结构化函数调用归纳意图，创建工单并启动现有 Supervisor-Worker 图。处理进度和最终答复通过会话 SSE 返回。客户答复只展示已通过审核的处理建议；未通过时明确转人工，不把草稿金额当成处理结果。

会话和用户消息持久保存，刷新可恢复历史；消息幂等键防止重复建单，同会话同时只能处理一个诉求。用户可以停止处理中诉求，服务重启后的中断消息可以继续处理，已提交的工单和图检查点复用。每个会话关联一笔订单，后续诉求可以生成新的关联工单，询问已有结果不会重复建单。

工单管理支持新建、修改未处理工单、删除和回收站恢复。已运行工单不能改写原始内容，补充诉求可通过会话提交。删除保留处理审计记录，并撤回尚未领取的人工任务；正在执行、暂停或已被人工领取的工单需先停止或完成复核。演示工单的修改与删除仅影响当前客服，不会改动全局模拟数据或其他用户的工单。

新工单客户 ID 来自订单，创建时间来自服务端。客户端不能设置已核验证据、客户身份或回填时间。自建工单以每次运行的快照传递给本地工具；独立 MCP 服务通过经过鉴权的协议元数据接收同一快照，不需要共享平台数据库。用户陈述始终不等于已核验证据。

```text
GET    /v1/orders                          可访问订单
POST   /v1/tickets                         新建工单
PATCH  /v1/tickets/{ticket_id}              修改未处理工单
DELETE /v1/tickets/{ticket_id}              移入回收站
GET    /v1/tickets?deleted=true             工单回收站
POST   /v1/tickets/{ticket_id}/restore      恢复工单
POST   /v1/conversations                   新建客服会话
GET    /v1/conversations                   历史会话
GET    /v1/conversations/{id}              对话与关联任务
POST   /v1/conversations/{id}/messages     发送诉求，立即返回 request_id
GET    /v1/conversations/{id}/events       会话消息与进度 SSE
POST   /v1/conversations/{id}/cancel       停止处理
POST   /v1/conversations/{id}/requests/{request_id}/retry 继续中断诉求
DELETE /v1/conversations/{id}              删除会话，关联工单保留
```

## 编排与审核

所有 Worker 执行后返回 Supervisor。Supervisor 根据 plan_cursor 和 depends_on 派发下一步；MAX_STEPS 统计 Worker 调用次数（包含 Planner、每次工具、Memory、Reviewer，不包含调度节点），派发前检查预算。预算耗尽转人工，MAX_REPLANS 最多为 2。

Planner 对全部步骤、依赖、工具名、参数及访问范围进行校验；遗漏订单、物流和申请动作资格校验时自动补齐。审核前安排证据归纳。Memory 保留按重要性排序的前 N 条事实，Planner 与 Reviewer 使用有预算的摘要和精简证据；完整调用保留在事件及证据档案中。摘要不能作为资格成立的依据。

Reviewer 独立核验签收期限、物流停滞、已确认的质量或破损事实、规则适用性、调用成功与证据 ID、金额范围、补偿计算结果、动作互斥及累计金额。高额、累计高频售后、已关闭、超期或证据不足进入人工复核。查询结果先按领域模型及工单归属验证，再更新 State；审核使用工具刚返回的订单和物流。工单时间固定，回放不会随着当前日期变化。原路退款和未发货取消按实付金额校验。决策动作使用固定枚举，FAQ 和案例不能作为资格授权规则；后续跟进与决策项分开。模型只生成 ProposalDraft，完整证据由服务端附加。

模型解析或校验失败最多重试 3 次；网络请求没有 SDK 隐式重试，并设置超时。所有终态为 completed、manual_review、failed 或 cancelled。paused 表示服务停止或重启后等待恢复的任务。

## 会话与 SSE

```text
POST /v1/auth/session                  建立客服身份，生产模式传 api_token
POST /v1/sessions                      建立会话，可传 ticket_id
POST /v1/runs                          启动任务，传 ticket_id/thread_id/idempotency_key
GET  /v1/runs/{run_id}/events           订阅或重放 SSE
GET  /v1/runs/{run_id}                  查询状态与结果
POST /v1/runs/{run_id}/cancel           取消活动任务
POST /v1/runs/{run_id}/resume           恢复 paused 任务
POST /v1/sessions/{thread_id}/supplements 保存客户补充材料
POST /v1/runs/{run_id}/feedback         记录采纳或问题反馈
GET  /v1/manual-reviews                人工复核队列
POST /v1/manual-reviews/{run_id}/claim  高级客服领取
POST /v1/manual-reviews/{run_id}/decision 提交 outcome/note
```

SSE 事件包含递增 id 和 started/update/done/paused 类型。update 的数据统一为：

```json
{"stage":"tool_agent","delta":{"tool_results":[],"step_count":2}}
```

Last-Event-ID 或 after 查询参数用于重放未接收的事件。连接断开只停止订阅，后台任务继续执行。同会话活动任务唯一，幂等键重复请求返回原任务。GET 不启动任务。旧 /v1/agents/{thread_id}/stream 仅订阅该会话最新任务；同步 /v1/agents/run 仍可使用，但需要先建立客服身份。

SQLite 持久化会话、事件、人工队列、反馈和完整证据；AsyncSqliteSaver 持久化每个 run_id 的图 checkpoint。重启后未完成任务标记 paused，由用户显式恢复。恢复从最近已提交节点继续；进程在工具返回与 checkpoint 提交之间崩溃时，当前只读工具可能重试，不承诺外部写操作 exactly-once。

当前 SQLite 部署只支持一个执行进程；数据库文件锁会拒绝第二个 worker。Redis 可选用于热点知识缓存、会话状态镜像、限流和消息总线，不替代持久化 checkpoint。数据库是恢复与会话状态的权威来源。知识缓存键包含内容版本，消息消费具有去重标记和运行隔离。

## 标准 MCP 工具服务

本地默认使用严格类型的工具执行器；也可以独立启动同一工具集合的标准 MCP Streamable HTTP 服务：

```powershell
# 先配置非空 MCP_API_KEY
python -m app.mcp_server
```

平台配置 MCP_URL=http://127.0.0.1:8001/mcp 和相同 MCP_API_KEY。客户端启动时执行 initialize 和 tools/list 并验证参数契约，工具通过 tools/call 调用。工具服务注册的新工具会自动被客户端发现，无需在平台增加 handler；新参数由 JSON Schema 校验，权限等级来自服务端元数据，order_id/user_id 仍限制在当前工单范围。平台先检查客服权限及工单范围；MCP 服务凭据授予可信后台服务访问权，不发送到浏览器。服务端再次严格校验原始参数，并进行工具限流和超时控制。工具服务当前仍使用模拟领域数据。

## Docker

```text
docker compose up --build
```

默认启动平台和 Redis，包含静态页面，数据库写入 aegisflow-data 命名卷。启用独立 MCP 时配置 MCP_API_KEY、MCP_URL=http://mcp:8001/mcp 并运行 docker compose --profile mcp up --build；若工具服务尚未就绪，应重启平台完成连接初始化。

## 回归与可观测

```text
pip install -r requirements-dev.txt
python -m pytest -m "not browser" -q
python -m playwright install chromium
python -m pytest -m browser -q
```

回归覆盖业务审核、熔断、严格工具参数、会话归属、人工流程、幂等及事件重放、取消、跨重启 checkpoint 恢复、真实 MCP 协议和桌面/移动浏览器工作流。GitHub Actions 配置离线回归及浏览器检查，不需要真实模型密钥。

```text
python -m eval.evaluate --limit 6
pip install -r requirements-eval.txt
python -m eval.evaluate --limit 6 --judge --ragas
```

普通评测输出证据覆盖、规则引用有效性和审核结果等诊断。--judge 使用原始事实与 SOP，并验证评分结构；JUDGE_MODEL 可指定独立评审模型。--ragas 运行 Ragas Faithfulness.ascore，失败使评测失败，未启用的评审标注 not_requested。报告包含模型及 Prompt 源码哈希。

默认 JSON 追踪记录 run、Worker、工具、LLM usage、每次模型尝试的耗时与失败类型。TRACE_PROVIDER=langfuse 使用 Langfuse v4 的父子 Observation 和 Generation，配置 LANGFUSE_PUBLIC_KEY、LANGFUSE_SECRET_KEY、LANGFUSE_BASE_URL 后启用远程追踪。

Qwen 的强制 Function Calling 默认设置 enable_thinking=false，避免思考模式与指定 tool_choice 的冲突。LLM_EXTRA_BODY 可以覆盖供应商参数，详见 [阿里云工具调用文档](https://help.aliyun.com/zh/model-studio/qwen-function-calling)。
