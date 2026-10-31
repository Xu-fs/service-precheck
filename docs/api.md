# 办事人问答接口

工作人员后台第一至四批已实现JWT、范围权限、知识发布、提示词及授权审计接口，见 [工作人员后台运行说明](工作人员后台第一批运行说明.md)及本文末尾接口表。后台凭证与办事人凭证隔离；第四批工单已提供，接口见本文末尾。

## 状态与身份

已实现教学问答入口，不包含材料识别、预检、人工工单或真实政务对接。`session_id` 对应 `conversations.id`，与登录会话 `auth_sessions.id` 不同，也不是预检编号、工单编号或正式办件编号。

匿名用户通过HttpOnly访问Cookie隔离；登录用户通过HttpOnly登录Cookie取得身份。所有会话读取、提问、确认事项均检查当前所有者；登录失效返回401，不回落匿名身份。匿名历史不自动归入登录账号。客户端不能指定user_id。

## 入口与辅助接口

| 方法及路径 | 请求／结果 |
| --- | --- |
| GET `/` | 聊天页面，设置匿名访问Cookie及页面CSRF令牌 |
| GET `/bootstrap` | 返回csrf_token、identity、username及教学事项选项；不返回访问Cookie正文 |
| POST `/chat` | 问答流，见下文 |
| GET `/sessions/{session_id}/messages` | 当前所有者的消息，before为上页游标，limit默认40、最大100；返回items、next_cursor及当前事项 |
| POST `/sessions/{session_id}/matter` | `{matter_id, scenario_id, confirmation_token}`，令牌10分钟有效且一次消费 |
| POST `/auth/login` | `{username,password}`，独立账号登录；不预置账号 |
| POST `/auth/logout` | 撤销当前登录凭证；过期凭证也可经CSRF校验后退出 |

写请求均需 `X-CSRF-Token`；浏览器同源发送。Cookie生产环境应通过HTTPS并启用Secure。登录会话有效期8小时；匿名Cookie有效期30天，此期限不是业务数据留存期限。

事项与情形：`residence-permit`／`rental-demo`；`sole-trader-registration`／`standard-demo`。两事项均支持后台发布教学指南。产品无默认个体工商户指南，未配置时正常依据不足。居住证在首次后台发布前兼容原本地教学指南；首次发布后只使用适用公开版本，即使全部停用也不恢复旧文件。

## POST /chat

请求头：`Content-Type: application/json`、`Accept: text/event-stream`、`X-CSRF-Token`、UUID格式的 `Idempotency-Key`。

```json
{"session_id": null, "question": "教学申请表有哪些字段？"}
```

首次传null，后续传会话UUID。问题去除首尾空白后1至2000字符。服务端拒绝额外字段。同一身份的幂等键不可复用于不同问题或会话；已完成请求重放结果，不重复写库或调用模型。进行中请求、失败后复用旧标识均返回409，失败后主动重试使用新标识。每会话同时一个在途请求。

响应：`text/event-stream; charset=utf-8`，`Cache-Control: no-store`，`X-Accel-Buffering: no`。事件按空行分隔，data为单行JSON。等待期间每15秒发送 `: ping` 注释心跳，不计事件序号。客户端使用POST fetch与ReadableStream解析，不使用原生EventSource。

每个业务事件携带：`request_id, session_id, message_id, user_message_id, seq`。seq从1递增；本期无事件断点续传。重放使用原请求及消息ID，序号重新从1开始。

| event | data额外字段／含义 |
| --- | --- |
| session | replayed：是否重放持久化结果 |
| status | stage、text；retrieving、generating、validating三个阶段 |
| matter_required | text、options、confirmation_token；要求确认事项及教学情形 |
| answer_delta | text：已校验答案的连续片段 |
| citations | items：实际支持答案的引用 |
| fallback | reason、text、actions；无充分依据的正常业务结果 |
| done | outcome、persisted=true；业务成功终态 |
| error | code、message、retryable；建立流后的技术故障终态 |

一次流只以done或error之一结束。outcome为answered、needs_matter_confirmation或insufficient_evidence。没有终态的EOF视为中断，前端查询已保存记录，不自动重复POST。

引用字段：`citation_id, filename, matter, rule_version, chunk_id, quote, is_teaching`；后台发布知识另携带 `version_id, page_no, chunk_no`（Markdown页码为null）。引用按服务端片段元数据生成，不信任模型提供的来源名。只展示完整句或完整行边界的原文，拒绝从否定语句中截取相反结论；切片截断边界不能视为句子边界。机械校验仍不代表语义充分性已经得到业务验收。

```text
event: answer_delta
data: {"request_id":"...","session_id":"...","message_id":"...","user_message_id":"...","seq":4,"text":"教学申请表包含姓名、联系电话"}

event: citations
data: {"request_id":"...","session_id":"...","message_id":"...","user_message_id":"...","seq":6,"items":[{"citation_id":"c1","filename":"居住证申领_教学指南.md","matter":"居住证申领","rule_version":"DEMO-2026-01","chunk_id":"C002","quote":"教学申请表包含姓名、联系电话和居住地址三个示例字段。","is_teaching":true}]}

event: done
data: {"request_id":"...","session_id":"...","message_id":"...","user_message_id":"...","seq":7,"outcome":"answered","persisted":true}

```

一般依据不足时text为“现有办事指南中未找到相关依据”；版本冲突时reason为`version_conflict`，text明确暂停建议并转人工。actions提供补充描述、已配置的人工咨询方式及“登录后提交人工工单”入口。不创建假工单，也不输出审批结论。

## 上下文及事务

当前问题之外最多最近6个完整问答轮次，历史最多6000字符，检索片段最多1800字符，总输入最多12000字符。超限移除最早整轮，不截断必要依据。失败、取消和不同事项的回答不进入历史。模型输出预算1024token；运营方还须按实际模型能力配置输入预算，字符数不等于token数。

用户问题与pending回答先在短事务中建立，网络调用不持写事务。完整答案、引用与完成状态提交后才发送answer_delta。取消只影响pending记录；已完成结果不因客户端停止显示而撤销。处理租约180秒、心跳续租；过期请求在初始化、消息读取或新请求接纳时恢复为失败。事项快照在接纳事务内比较，发生多标签页变更返回409，不按旧事项作答。接纳事务同时冻结提示词正文/版本与适用公开发布包/不可变知识版本，排队过程中模板更新或旧包停用不覆盖这次请求；内部`_snapshot`不进入SSE或办事人历史。结果保存`prompt_version_id, prompt_version, release_ids, knowledge_version_ids`，不保存到公开结果中的模板正文；原始快照供授权审计核验。发布索引校验checksum和嵌入provider/model/url/revision，异常暂停建议。

## 统一错误

流前JSON结构：

```json
{"error":{"code":"SESSION_NOT_FOUND","message":"会话不存在或无权访问","request_id":"...","retryable":false}}
```

| HTTP | 错误码示例 |
| --- | --- |
| 400 | INVALID_JSON、INVALID_IDEMPOTENCY_KEY、INVALID_HOST |
| 401 | VISITOR_CREDENTIAL_REQUIRED、AUTH_EXPIRED、INVALID_CREDENTIALS |
| 403 | CSRF_FAILED |
| 404 | SESSION_NOT_FOUND、NOT_FOUND |
| 409 | SESSION_BUSY、SESSION_CHANGED、IDEMPOTENCY_CONFLICT、REQUEST_IN_PROGRESS、REQUEST_RETRY_REQUIRED、CONFIRMATION_EXPIRED |
| 413 | REQUEST_TOO_LARGE |
| 422 | INVALID_QUESTION、INVALID_REQUEST、INVALID_MATTER |
| 429 | RATE_LIMITED |
| 503 | SERVICE_UNAVAILABLE、STORAGE_UNAVAILABLE |

流内技术错误：MODEL_TIMEOUT、MODEL_SERVICE_ERROR、MODEL_RESPONSE_INVALID、CONTEXT_TOO_LONG、REQUEST_INTERRUPTED、CHAT_FAILED。HTTP状态保持200，通过error事件传达；外部响应正文、口令、密钥和堆栈不返回浏览器。

个人材料、预检记录和工单尚未开放，不通过问答模型代理查询这些数据。工作人员已开放以下第二三批接口；不获得角色名称之外的全局权限。

## 工作人员知识、模板与审计接口

| 方法与路径（均以`/staff`开头） | 用途及关键约束 |
| --- | --- |
| GET `/knowledge`、`/rules`、`/knowledge/new`、`/knowledge/versions/{id}` | 知识/规则SSR列表、上传及详情，按岗位和事项校验 |
| GET `/api/knowledge`、`/api/jobs/{id}` | 版本列表及任务状态，内部租约令牌不返回 |
| POST `/api/knowledge/uploads` | multipart教学Markdown/PDF；20MB文件，标题/事项/情形/类型/来源/版本/教学确认；workflow仅staff可见 |
| POST `/api/rules` | JSON或表单创建rule/checklist教学正文，最多50000字符 |
| POST `/api/knowledge/{doc}/versions`、`/api/knowledge/{doc}/versions/{ver}/text` | 指定base_version_id或路径版本，正文、rule_version及必填expected_latest；PDF指定现有page；总是新版本 |
| POST `/api/knowledge/{doc}/versions/{ver}/reprocess`、`/api/knowledge/{doc}/archive` | 重试草稿任务或归档；父文档与版本必须一致，发布/审核中不可归档 |
| GET `/reviews`、`/reviews/{id}` | 仅owner/supervisor对应事项的rule.release.read权限 |
| POST `/api/releases` | title、version_ids(1..10同事项情形)、effective_from及可选effective_to；SSR时间默认北京时间，带时区保留偏移 |
| POST `/api/releases/{id}/{action}` | submit/approve/reject/publish/retire，必填expected_revision和comment；实际提交人及内容维护者不能自审 |
| GET `/feedback`，POST `/api/feedback`、`/api/feedback/{id}/resolve` | 窗口/热线反馈，负责人追加处理说明，已处理再次提交409 |
| GET `/prompts`、`/api/prompts/{matter}/versions` | 每事项模板及不可变历史，仅prompt.manage |
| POST `/api/prompts/{matter}/versions`、`/api/prompts/{matter}/restore` | content或历史version、必填expected_version/comment；恢复追加，不覆盖旧版本 |
| GET `/audits`、`/audits/{id}`、`/api/audits`、`/api/audits/{id}` | audit.review+事项+显式body记录授权，先SQL限制再筛选计数/分页；kind=precheck明确未接入 |
| POST `/api/audits/{id}/flags`、`/api/audits/{id}/reviews` | kind/reason或decision/comment，追加标记与复核，不覆盖原始记录 |

写接口使用独立工作人员Cookie及CSRF；JSON传X-CSRF-Token，原生表单传csrf_token；成功表单303，JSON返回状态。乐观锁过期409，越权403或隐藏对象404，无效字段422。不同岗位的动作和事项范围不能交叉组合。

文档处理为SQLite持久队列，租约恢复且旧worker不能覆盖重新接手结果；正文提取在独立可终止子进程内，30秒超时，PDF最多20页、每页内容2MB、全文50000字符，解压/解析前应用pypdf预算。空白页为needs_text，逐页人工补充生成新草稿，不假装OCR成功。PDF含Form XObject等复杂嵌入内容时整体拒绝处理，避免库跳过资源超限的必要正文后误将部分文字视为ready；需将教学资料转换为简单PDF或人工补充。Windows仍需生产级OS内存/文件/网络沙箱与配额治理；此实现不是完整恶意PDF隔离。

发布流程draft→submitted→approved→published，驳回后新版本包重新提交，停用保留retired历史。生效区间采用[start,end)，同事项/情形重叠发布409；异常数据库出现多个适用包则公开问答转人工，不自行选版本。嵌入模型变化须重建草稿并重新审核发布，不能原地修改已发布向量。技术故障MODEL_RESPONSE_INVALID、KNOWLEDGE_INDEX_INVALID/INCOMPATIBLE不与正常依据不足混用。


## 第四批人工工单

GET `/my/tickets` 为办事人页面；GET `/staff/tickets` 和 `/staff/tickets/{id}` 为SSR后台，原生表单提交成功303。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET/POST | `/tickets` | 本人列表/创建，创建使用服务端登录身份 |
| GET | `/tickets/{id}` | 本人详情 |
| POST | `/tickets/{id}/messages` | 本人补充并取消本轮关闭/未发送提醒 |
| POST | `/tickets/{id}/confirm-resolved` | 本人确认完成 |
| GET | `/notifications` | 本人站内消息，已读不等于解决 |
| POST | `/notifications/{id}/read` | 本人消息标记已读，重复标记不改变首次已读时间 |
| GET | `/staff/api/tickets`、`/staff/api/tickets/{id}` | 授权列表/详情 |
| POST | `/staff/api/tickets/{id}/accept` | 窗口抢接手 |
| POST | `/staff/api/tickets/{id}/transfers` | 当前责任人发起转派 |
| POST | `/staff/api/tickets/{id}/transfers/{transfer_id}/accept`、`/reject`、`/cancel` | 接收/拒绝/原责任人撤回 |
| POST | `/staff/api/tickets/{id}/replies` | 责任人确认答复，kind=supplement/result |
| GET | `/staff/api/tickets/{id}/transitions` | 授权流转时间线 |
| GET | `/staff/api/assignees?ticket_id=...&matter_id=...` | 本次转派允许的接收人 |

创建接收 matter_id、scenario_id、title（1—120字符）、content（1—4000字符）、idempotency_key（8—128字符），可选本人同事项conversation_id/message_id。预检和正式办件未接入，不能传入伪造编号。所有工单动作要求 expected_version 与 idempotency_key；转派另需 target_user_id、target_role、reason（1—1000字符）；拒收/撤回也需reason。通知已读的页面适配层要求操作标识与版本占位0，本身按对象幂等。

公开咨询凭证不能提交个人工单；办事人写请求沿用X-CSRF-Token，后台沿用独立JWT和CSRF。列表先在SQL筛权限、事项和处理关系再分页，status/matter_id/page/page_size/scope（all/queue/mine/pending）可选。热线只获取问题、进度和答复，不展示内部转派原因与原会话链接；工程师无工单访问权限。责任转移仅在接收人明确确认事务中发生。

错误沿用统一JSON：未登录401、权限/CSRF403、对象不可见404、状态/版本或幂等内容冲突409、字段422。重复请求使用同一操作标识和完全相同载荷；旧责任人已失去处理权限时操作重试拒绝，先刷新确认状态。

人工工单编号为TK-UUID；precheck_id与formal_case_id均为null，不生成正式办件模拟成功状态。没有个人附件接口。时钟、教学工作日历和实际验收详见[工单运行说明](第四批工单运行说明.md)。
