# 办事人问答接口

工作人员后台第一至四批已实现JWT、范围权限、知识发布、提示词及授权审计接口，见 [工作人员后台运行说明](工作人员后台第一批运行说明.md)及本文末尾接口表。后台凭证与办事人凭证隔离；第四批工单已提供，接口见本文末尾。

## 状态与身份

已实现示例问答、有限工具助手、私有示例材料与辅助预检、确认后人工工单；真实政务对接仍未开放。`session_id` 对应 `conversations.id`，与登录会话 `auth_sessions.id` 不同，也不是预检编号、工单编号或正式办件编号。

匿名用户通过HttpOnly访问Cookie隔离；登录用户通过HttpOnly登录Cookie取得身份。所有会话读取、提问、确认事项均检查当前所有者；登录失效返回401，不回落匿名身份。匿名历史不自动归入登录账号。客户端不能指定user_id。

## 入口与辅助接口

| 方法及路径 | 请求／结果 |
| --- | --- |
| GET `/` | 聊天页面，右侧显示登录用户的站内消息；窄屏折叠显示。设置匿名访问Cookie及页面CSRF令牌 |
| GET `/services/id-card-replacement` | 无需登录的身份证补办全国通用指南，附官方来源；省略具体地点和联系电话 |
| GET `/services/public-venues` | 无需登录的便民场馆开放全国通用指南，附官方来源；省略具体地点和联系电话 |
| GET `/bootstrap` | 返回csrf_token、identity、username及示例事项选项；不返回访问Cookie正文 |
| POST `/chat` | 问答流，见下文 |
| GET `/sessions/{session_id}/messages` | 当前所有者的消息，before为上页游标，limit默认40、最大100；返回items、next_cursor及当前事项 |
| POST `/sessions/{session_id}/matter` | `{matter_id, scenario_id, confirmation_token}`，令牌10分钟有效且一次消费 |
| POST `/auth/login` | `{username,password}`，独立账号登录；不预置账号 |
| POST `/auth/logout` | 撤销当前登录凭证；过期凭证也可经CSRF校验后退出 |

写请求均需 `X-CSRF-Token`；浏览器同源发送。Cookie生产环境应通过HTTPS并启用Secure。登录会话有效期8小时；匿名Cookie有效期30天，此期限不是业务数据留存期限。

事项与情形：`residence-permit`／`rental-demo`；`sole-trader-registration`／`standard-demo`。两事项均支持后台发布示例指南；候选资料在`data/materials/`，须审核发布后启用，未发布时正常依据不足。居住证在首次后台发布前兼容原本地示例指南；首次发布后只使用适用公开版本，即使全部停用也不恢复旧文件。

## POST /chat

请求头：`Content-Type: application/json`、`Accept: text/event-stream`、`X-CSRF-Token`、UUID格式的 `Idempotency-Key`。

```json
{"session_id": null, "question": "示例申请表有哪些字段？"}
```

首次传null，后续传会话UUID。问题去除首尾空白后1至2000字符。服务端拒绝额外字段。同一身份的幂等键不可复用于不同问题或会话；已完成请求重放结果，不重复写库或调用模型。进行中请求、失败后复用旧标识均返回409，失败后主动重试使用新标识。每会话同时一个在途请求。

响应：`text/event-stream; charset=utf-8`，`Cache-Control: no-store`，`X-Accel-Buffering: no`。事件按空行分隔，data为单行JSON。等待期间每15秒发送 `: ping` 注释心跳，不计事件序号。客户端使用POST fetch与ReadableStream解析，不使用原生EventSource。

每个业务事件携带：`request_id, session_id, message_id, user_message_id, seq`。seq从1递增；本期无事件断点续传。重放使用原请求及消息ID，序号重新从1开始。

| event | data额外字段／含义 |
| --- | --- |
| session | replayed：是否重放持久化结果 |
| status | stage、text；retrieving、generating、validating三个阶段 |
| matter_required | text、options、confirmation_token；要求确认事项及申请情形 |
| answer_delta | text：已校验答案的连续片段 |
| citations | items：实际支持答案的引用 |
| actions | items：回答中的可点击入口；便民指南项含 type=`open_service_guide`、label、href、enabled |
| fallback | reason、text、actions；无充分依据的正常业务结果 |
| done | outcome、persisted=true；业务成功终态 |
| error | code、message、retryable；建立流后的技术故障终态 |

一次流只以done或error之一结束。outcome为answered、needs_matter_confirmation、insufficient_evidence或public_service_guidance。没有终态的EOF视为中断，前端查询已保存记录，不自动重复POST。

身份证补办和便民场馆开放问题返回 `public_service_guidance`，先显示简要答复，再通过 `actions` 提供对应的站内指南入口；同一问题涉及两项时同时提供两个入口。该路径不要求先选择原有示例事项，也不调用模型；它使用已核对的公开资料说明，不确认正式办理状态。入口随答复保存，刷新恢复及请求重放后仍可使用；此类通用指引不进入原事项的模型上下文。

引用字段：`citation_id, filename, matter, rule_version, chunk_id, quote, is_teaching`；后台发布知识另携带 `version_id, page_no, chunk_no`（Markdown页码为null）。引用按服务端片段元数据生成，不信任模型提供的来源名。只展示完整句或完整行边界的原文，拒绝从否定语句中截取相反结论；切片截断边界不能视为句子边界。机械校验仍不代表语义充分性已经得到业务验收。

```text
event: answer_delta
data: {"request_id":"...","session_id":"...","message_id":"...","user_message_id":"...","seq":4,"text":"示例申请表包含姓名、联系电话"}

event: citations
data: {"request_id":"...","session_id":"...","message_id":"...","user_message_id":"...","seq":6,"items":[{"citation_id":"c1","filename":"居住证申领_示例指南.md","matter":"居住证申领","rule_version":"DEMO-2026-01","chunk_id":"C002","quote":"示例申请表包含姓名、联系电话和居住地址三个示例字段。","is_teaching":true}]}

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

个人材料、预检及人工工单通过下列独立受控接口访问；公开问答不自动读取私人材料。工作人员不因角色名称获得全局权限。

## 工作人员知识、模板与审计接口

工作人员登录后进入 `GET /staff`，首页与各业务页的导航按当前岗位授权生成，只展示相关功能。窗口与审核员分别显示“窗口工单受理”和“专业审核与分派”；工程师通过 `GET /staff/system` 查看技术状态页面，该页面及现有 `GET /staff/api/system/status` 均要求 `system.health`。岗位页面分工见[岗位工作台调整说明](岗位工作台调整说明.md)。

| 方法与路径（均以`/staff`开头） | 用途及关键约束 |
| --- | --- |
| GET `/knowledge`、`/rules`、`/knowledge/new`、`/knowledge/versions/{id}` | 知识/规则SSR列表、上传及详情，按岗位和事项校验 |
| GET `/api/knowledge`、`/api/jobs/{id}` | 版本列表及任务状态，内部租约令牌不返回 |
| POST `/api/knowledge/uploads` | multipart示例Markdown/PDF；20MB文件，标题/事项/情形/类型/来源/版本/示例确认；workflow仅staff可见 |
| POST `/api/rules` | JSON或表单创建rule/checklist示例正文，最多50000字符 |
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

文档处理为SQLite持久队列，租约恢复且旧worker不能覆盖重新接手结果；正文提取在独立可终止子进程内，30秒超时，PDF最多20页、每页内容2MB、全文50000字符，解压/解析前应用pypdf预算。空白页为needs_text，逐页人工补充生成新草稿，不假装OCR成功。PDF含Form XObject等复杂嵌入内容时整体拒绝处理，避免库跳过资源超限的必要正文后误将部分文字视为ready；需将示例资料转换为简单PDF或人工补充。Windows仍需生产级OS内存/文件/网络沙箱与配额治理；此实现不是完整恶意PDF隔离。

发布流程draft→submitted→approved→published，驳回后新版本包重新提交，停用保留retired历史。生效区间采用[start,end)，同事项/情形重叠发布409；异常数据库出现多个适用包则公开问答转人工，不自行选版本。嵌入模型变化须重建草稿并重新审核发布，不能原地修改已发布向量。技术故障MODEL_RESPONSE_INVALID、KNOWLEDGE_INDEX_INVALID/INCOMPATIBLE不与正常依据不足混用。


## 第四批人工工单

GET `/my/tickets` 为办事人页面；GET `/staff/tickets` 和 `/staff/tickets/{id}` 为SSR后台，原生表单提交成功303。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET/POST | `/tickets` | 本人列表/创建，创建使用服务端登录身份 |
| POST | `/tickets/submit` | 普通咨询直接提交，接收事项、情形、标题、正文及操作标识；支持JSON和原生表单 |
| GET | `/tickets/{id}` | 本人详情 |
| POST | `/tickets/{id}/messages` | 本人补充并取消本轮关闭/未发送提醒 |
| POST | `/tickets/{id}/confirm-resolved` | 本人确认完成 |
| GET | `/notifications` | 本人未删除的站内消息，包含对应工单标题 `ticket_title`；已读不等于解决 |
| POST | `/notifications/{id}/read` | 本人消息标记已读，重复标记不改变首次已读时间 |
| POST | `/notifications/{id}/delete` | 删除本人消息，刷新后不再显示；不影响工单及处理记录，重复删除返回成功 |
| GET | `/staff/api/tickets`、`/staff/api/tickets/{id}` | 授权列表/详情 |
| POST | `/staff/api/tickets/{id}/accept` | 窗口抢接手 |
| POST | `/staff/api/tickets/{id}/transfers` | 当前责任人发起转派 |
| POST | `/staff/api/tickets/{id}/transfers/{transfer_id}/accept`、`/reject`、`/cancel` | 接收/拒绝/原责任人撤回 |
| POST | `/staff/api/tickets/{id}/replies` | 责任人确认答复，kind=supplement/result |
| GET | `/staff/api/tickets/{id}/transitions` | 授权流转时间线 |
| GET | `/staff/api/assignees?ticket_id=...&matter_id=...` | 本次转派允许的接收职务，每项含role_code、role_name，同岗合并 |

普通工单页面使用`POST /tickets/submit`直接建单，必填`matter_id`、`scenario_id`、`title`（1—120字符）、`content`（1—4000字符）、`idempotency_key`（8—128字符），可选关联本人的`conversation_id`和`message_id`。只提交填写的问题，不共享材料或预检；创建、通知、任务及幂等记录在同一事务保存。原生表单成功后跳转工单详情，JSON返回工单记录。

材料协作及智能体草稿仍使用`POST /tickets`，只接收`draft_id`、`draft_version`、`submission_confirmation_id`、`idempotency_key`。先创建及预览本人草稿，再通过独立确认接口取得凭证。`POST /tickets/drafts`准备草稿，`POST /tickets/drafts/{id}/submit`校验预览版本和哈希后签发确认并进入同一创建服务。所有后续工单动作要求 expected_version 与 idempotency_key；转派另需 target_role、reason（1—1000字符），默认进入该职务的待接收队列，target_user_id为空；保留可选target_user_id以兼容原指定人员接口及历史转派。页面只显示中文职务、不显示人员姓名或账号，同职务且具备该事项处理权限的在职人员均可查看并确认接收，首次成功接收后绑定实际责任人，其他人不能重复接收。拒收/撤回也需reason。通知已读的页面适配层要求操作标识与版本占位0，本身按对象幂等。

公开咨询凭证不能提交个人工单；办事人写请求沿用X-CSRF-Token，后台沿用独立JWT和CSRF。列表先在SQL筛权限、事项和处理关系再分页，status/matter_id/page/page_size/scope（all/queue/mine/pending）可选。热线只获取问题、进度和答复，不展示内部转派原因与原会话链接；工程师无工单访问权限。责任转移仅在接收人明确确认事务中发生。

错误沿用统一JSON：未登录401、权限/CSRF403、对象不可见404、状态/版本或幂等内容冲突409、字段422。重复请求使用同一操作标识和完全相同载荷；旧责任人已失去处理权限时操作重试拒绝，先刷新确认状态。

人工工单编号为TK-UUID；历史表precheck_id与formal_case_id保持null。新增材料、预检和异常关联从草稿及agent_submissions映射读取，详情的`shared_context`展示授权材料元数据、历史预检摘要及选中异常。正式办件未接入。时钟和示例工作日历见[工单运行说明](第四批工单运行说明.md)。

## 助手、草稿和私有示例材料

以下写接口沿用办事人CSRF，身份由服务端取得；上传、草稿、预检和个人记录要求登录。工具结果使用`status/data/error/trace_id`封装，HTTP 200不等于工具执行成功，须检查status；直接校验失败仍可返回对应4xx。

| 方法及路径 | 关键请求或返回 |
| --- | --- |
| GET `/assistant` | 材料准备与人工协作页面 |
| POST `/agent/cases`，GET `/agent/cases/{id}` | 建立本人/匿名案例，读取当前选择与版本 |
| GET `/agent/cases/{id}/rules` | 查询参数matter_id/scenario_id；返回snapshot_id、指南、示例规则批准状态 |
| POST `/agent/cases/{id}/selection` | matter_id、scenario_id、snapshot_id；用户直接确认规则 |
| POST `/agent/cases/{id}/ask` | question；执行有限六工具循环 |
| POST `/agent/cases/{id}/exceptions` | kind、detail；记录待人工核实的异常，不自动建单 |
| POST `/agent/cases/{id}/exception-draft` | exception_id、summary、expected_case_version；受控准备异常草稿 |
| POST `/agent/drafts` | title/content/matter_id/scenario_id，可选case_id、本人conversation_id/message_id、shared_material_ids、有效precheck_id；异常从受控操作关联 |
| GET/PUT `/agent/drafts/{id}` | 读取草稿；更新须expected_version和data，原异常可保留或明确移除，不能任意新增关联 |
| POST `/agent/drafts/{id}/confirm` | draft_version；签发当前版本提交确认，模型无此工具 |
| GET `/my/ticket-drafts/{id}` | 正文、共享材料版本、预检摘要和异常预览 |
| POST `/tickets/drafts/{id}/edit` | 原生表单title/content/draft_version、操作标识及可选remove_materials/remove_precheck/remove_exceptions=yes |
| GET/POST `/agent/cases/{id}/materials` | 本人列表；multipart file及consent=true，10MB/份、最多10份有效材料 |
| POST `/agent/materials/{id}/process` | 默认明确不可用；可信示例模式执行真实本地提取/OCR |
| PUT `/agent/materials/{id}/fields` | expected_version、kind、fields；来源为本人主动转录 |
| DELETE `/agent/materials/{id}` | 撤回授权并删除原件，使旧集合失效 |
| GET `/agent/materials/{id}/download` | 本人当前授权原件，校验哈希 |
| POST `/agent/cases/{id}/material-sets` | material_ids；冻结当前材料版本 |
| POST `/agent/cases/{id}/precheck` | material_set_id、rule_snapshot_id、expected_case_version；计算完成后再次核对规则及材料集合 |
| GET `/agent/prechecks/{id}` | 本人有效示例预检记录，重新核对材料和规则 |
| GET `/staff/api/tickets/{ticket}/materials/{material}` | 工单处理关系、material.view权限、明确共享及当前授权均成立时下载 |

预检不是真伪、资格或审批判断。更改材料、撤回授权或切换规则后旧结果不能作为当前结论。模型不接收材料正文、草稿全文或提交确认；公开知识与私有材料分开保存。工单详情只共享选中材料及预检历史摘要，热线保持摘要边界。

## 留存及运行控制

`POST /staff/api/retention/dispose`要求工作人员JWT、独立CSRF及同一条全事项supervisor授权；请求为signed_policy、approved（ID到SHA256）、dry_run（默认true）、quiesced（默认false）。实际处置仅限获批独立评价/诊断记录，须外部签名配置和明确静默写入，不清理业务库、材料或备份。

同机SQLite共享限流与模型预算覆盖全局、操作者、服务及操作者与服务组合；未知usage保守保留预算，超限返回MODEL_QUOTA_EXCEEDED。本地OCR另有处理配额。配置、凭证、HTTPS域名、备份及恢复契约见[运维说明](运维部署与恢复.md)。
