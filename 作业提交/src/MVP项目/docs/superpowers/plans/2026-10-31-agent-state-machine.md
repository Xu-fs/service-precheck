# 有限工具智能体实施计划

> 使用 executing-plans 逐项实施；每项先验证失败场景，再实现并回归。

**目标：** 在现有示例项目中提供受服务端状态和授权约束的六工具智能体与独立异常转人工路径。

**架构：** 智能体调用受限工具分发器，业务服务负责规则快照、确认和草稿，工单创建在同一数据库事务中校验并消费确认。浏览器直接完成确认，模型不能代签。

**技术：** Python、FastAPI、SQLite/SQLAlchemy、现有异步测试和浏览器测试。

**设计依据：** `docs/superpowers/specs/2026-10-31-agent-state-machine-design.md`。

## 全局约束

- 使用虚构示例数据；真实政务接口、材料识别和真实审批不作已实现声明。
- 保持现有授权按身份、岗位、事项和处理关系逐对象检查；不信任模型传入身份。
- 不自动发送正式补正或升级主任；四类异常不受预检完成条件限制。
- 保持既有数据及工单行为；数据库迁移可重复执行，避免覆盖现有记录。
- 生成文件归入现有目录，执行 `python scripts/check_structure.py`。

## 重点复核

- 未知事项异常可进入专用授权分流，不能扩大普通工单可见范围（任务 3）。
- 确认后内容变化、过期或并发消费时不能创建工单（任务 2、3）。
- 模型伪造身份、确认和额外字段均不能越权（任务 1、4）。
- 规则版本变化或冲突不沿用旧预检结论（任务 2、5）。
- 工单服务超时重试只能得到同一结果；故障不报告成功（任务 3、4）。

## 任务 1：工具分发与授权边界

**文件：** 新建 `src/zhengwu_precheck/core/services/agent_tools.py`、`tests/test_agent_tools.py`；按需修改 `src/zhengwu_precheck/main.py`。

**接口：** 定义六个工具的严格参数模型及 `async dispatch(name, arguments, principal)`，返回设计中的统一状态；principal 只能由服务端注入。

- [ ] 编写测试：未知工具、额外字段、伪造 user_id、未授权资源均拒绝；超时返回暂不可用；错误不泄露内部内容。
- [ ] 运行 `python -m unittest tests.test_agent_tools -v`，确认缺失实现导致失败。
- [ ] 实现注册表、参数校验、调用次数/超时和授权上下文；未接入的工具明确返回不可用。
- [ ] 重跑测试至通过；确认工具输出不会把数据文本提升为系统指令。

## 任务 2：确认、案例、规则快照和草稿

**文件：** 新建 `src/zhengwu_precheck/infrastructure/agent_repository.py`、`src/zhengwu_precheck/api/routes/agent.py`、`tests/test_agent_state.py`；修改 `src/zhengwu_precheck/main.py`。

**接口：** 仓储提供创建/读取案例和版本化草稿、确认事项、确认提交；确认绑定身份、对象、版本、哈希、有效期。确认路由使用现有登录及 CSRF，模型工具不暴露确认动作。

- [ ] 编写测试：事项、情形及规则快照均确认后才可预检；过期、跨用户、重放、变更草稿及规则版本均被拒绝；迁移重复执行保留历史。
- [ ] 运行 `python -m unittest tests.test_agent_state -v`，确认预期失败。
- [ ] 使用可重入迁移建立案例、确认、草稿和异常记录；事务中校验版本；接入真实办事人确认接口。
- [ ] 重跑测试至通过，检查不存储不必要的证件内容。

## 任务 3：统一工单创建及异常分流

**文件：** 修改 `src/zhengwu_precheck/infrastructure/ticket_repository.py`、`src/zhengwu_precheck/api/routes/tickets.py`、`src/zhengwu_precheck/templates/tickets.html`、`src/zhengwu_precheck/templates/staff/tickets.html` 及相关脚本；新建 `tests/test_agent_ticket.py`。

**接口：** `TicketRepository.create_confirmed(user_id, draft_id, draft_version, confirmation_id, idempotency_key)` 同一事务校验、消费确认并创建；所有旧新入口调用同一服务。

- [ ] 编写测试：直接 POST 绕过确认失败；修改草稿、跨用户或过期确认失败；并发同键仅产生一个工单，异载荷冲突；降权后不可重放敏感信息。
- [ ] 编写测试：四类异常可在未完成预检时创建；事项未知进入仅获授权分流岗位可见的队列；不得被自动升级主任；系统不可用时不报告已提交。
- [ ] 运行 `python -m unittest tests.test_agent_ticket -v`，确认预期失败。
- [ ] 实施工单事务、未知事项专用队列及权限过滤，并将现有页面改为展示草稿、共享范围及由办事人确认；保留现有记录与责任流转。
- [ ] 运行新测试及 `python -m unittest tests.test_tickets tests.test_tickets_routes -v`，修复回归。

## 任务 4：模型工具循环与交互

**文件：** 新建 `src/zhengwu_precheck/core/services/agent.py`、`tests/test_agent_loop.py`；修改 `src/zhengwu_precheck/api/routes/chat.py`、`src/zhengwu_precheck/static/js/chat.js`、`src/zhengwu_precheck/templates/chat.html`。

**接口：** `async run_agent(question, principal, case_id)` 通过任务 1 的分发器循环调用工具，返回答案或确认动作；系统提示词固化设计要求，服务端仍独立校验。

- [ ] 用模型替身测试工具选择、调用上限、注入文本、伪造确认、断线和超时；提交未知结果不可报告成功。
- [ ] 运行 `python -m unittest tests.test_agent_loop -v`，确认预期失败。
- [ ] 接入工具循环、受控上下文和事项/草稿确认卡片；确认由浏览器提交至任务 2 的接口。保留现有流式终态与幂等行为。
- [ ] 重跑新测试和 `python -m unittest tests.test_chat -v`。

## 任务 5：指南、预检与异常适配

**文件：** 修改 `src/zhengwu_precheck/core/services/agent_tools.py`、`src/zhengwu_precheck/core/services/guide_qa.py`；新建 `tests/test_agent_guidelines.py`。

**接口：** search_guidelines 使用发布快照和服务端引用；recommend_matter 只返回目录内候选；check_materials 要求任务 2 的确认及版本，未接入的材料能力返回明确不可用，不生成伪预检编号。

- [ ] 测试公开/内部资料授权、无依据、重复适用版本、规则更改、四类异常及不可用材料服务；异常可直接准备工单草稿。
- [ ] 运行 `python -m unittest tests.test_agent_guidelines -v`，确认预期失败。
- [ ] 实现检索和推荐适配、规则快照校验、异常记录和不可用回退；不伪装材料识别或正式审批。
- [ ] 运行新测试及 `python -m unittest tests.test_published_qa tests.test_guide_qa -v`。

## 任务 6：文档与端到端验收

**文件：** 修改 `docs/api.md`、`README.md`；新增 `docs/智能体运行与验证说明.md`；按需扩展 `tests/browser_chat.py` 和 `tests/browser_tickets.py`。

- [ ] 记录工具契约、状态、确认接口、授权、异常分流及未接入能力；明确旧工单接口的兼容方式和迁移。
- [ ] 浏览器验证正常确认创建、异常未预检转人工、未知事项分流、越权与系统故障；所有数据为虚构数据。
- [ ] 执行 `python -m unittest discover -s tests -v`、`node --test tests/test_sse.mjs`、现有浏览器检查和 `python scripts/check_structure.py`，记录结果和未验证项。
- [ ] 检查 `git diff --check`，人工复核确认事务与各入口，保留工作区既有变更。
