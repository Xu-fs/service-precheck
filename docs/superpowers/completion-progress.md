# 三批补齐执行记录

日期：2026-10-31。分支：`codex/completion-batches`。依据为[已授权路线](specs/2026-10-31-completion-roadmap-design.md)和[实施计划](plans/2026-10-31-completion-execution.md)。用户已批准三批实施，本轮从中断点继续；没有提交、推送、合并或部署。

## 当前交付状态

本地教学闭环及运维基础已实现，集成修复和独立代码复核已完成；普通集成回归已通过。生产放行仍未完成。本文替代上一版“交接停止点”作为当前状态，原阶段过程见各模块说明及项目工作历史。

| 批次 | 已落地及验收内容 | 边界 |
| --- | --- | --- |
| 第一批 | 案例与规则确认；版本草稿及共享预览；所有建单入口共用确认消费事务；原键重试返回原工单；六工具有限循环；四类异常独立转人工；未知事项显式授权；SSR和助手入口 | 模型HTTP使用替身；正式办件未接入 |
| 第二批 | 两事项候选指南及审核发布链；私有材料授权/上传/字段转录/撤回；真实本地PNG与扫描PDF中文OCR；可解释预检；共享材料、异常和历史预检摘要到实际双端页面 | 指南须经审核发布；默认关闭不可信材料解析；独立预检审计总览未接入 |
| 第三批 | 同机多进程共享限流；四维持久模型预算；知识任务操作者归属；外发内容最小化；独立记录签名留存处置；构建、TLS预检、数据库及文件备份/隔离恢复 | 不清理业务库/材料/备份；跨机器协调、生产容量和实际版本回退未验收 |

## 中断问题处理

1. 正式启动改为存在的`create_app`工厂，保留TLS参数；CLI契约测试通过。
2. 原生表单重试沿用首次成功确认指纹并返回同一工单；不同内容/草稿复用操作键仍被拒绝。
3. 助手首次启动不再提前读取CSRF；Chrome完成初始化、规则确认、刷新恢复、材料和草稿流程。
4. 备份/恢复核对源文件、索引和当前授权材料引用及哈希；缺失引用拒绝完整快照。TLS文件不得放源码或业务运行目录；发布域名须属于允许列表。
5. 草稿预览、办事人及工作人员详情实际接入共享材料、选定异常和历史预检摘要；热线和材料岗位权限保留。实际HTTP页面断言通过。
6. 编辑正文保留并复验原选异常，用户可显式移除；保存修改使旧确认失效。
7. 预算原子检查全局、操作者、服务及组合；知识嵌入归属上传者并恢复上下文；预检计算后复验案例、规则和材料集合。
8. Windows浏览器测试直接管理实际解释器，并保留虚拟环境依赖路径，结束后回收本任务服务进程。
9. 恢复演练升级为实际应用schema：虚构办事人、独立窗口账户、共享材料、确认消费、工单和2项持久任务；恢复后新应用启动，核验健康与权限、原件相同、原提交重试只保留1张工单，重复执行到期任务不重复留痕；正常审核发布后停用规则、创建恢复版本重新审核发布；提示词恢复追加第3版；恢复后核对当前及历史规则快照、真实FAISS索引与提示词历史。嵌入为明确离线替身，不调用真实模型。

适当修复先观察失败，再做聚焦回归。最初两个多进程错误来自Windows无法重新加载标准输入测试入口；现改用有主入口保护的文件，不把原错误记为通过。

## 最终验证

- Python普通回归：179项通过，0失败，529.150秒；日志`.venv/tmp/resume-regression-current.log`。新恢复用例在该次发现之后加入，另行聚焦通过；CLI两项包含1个已在179项中的启动契约，不将重复项相加。当前有效普通用例合计180项。两个既有PDF解析压力用例未执行，相关边界只作静态复核；不声称完整发现套件全部通过。
- 浏览器：分组累计73项通过（办事人14、工作人员19、工单16、助手7、知识内容17）；实际Chrome运行。前四组同一次通过，内容组更新过期文本断言后单独重跑17项通过。不是一次不中断的全组结果。
- SSE：6项通过，0失败；日志`.venv/tmp/resume-sse-current.log`。
- 新真实应用恢复CLI：2项通过，3.698秒；日志`.venv/tmp/resume-recovery-app-tests.log`。
- 实际恢复命令：1份材料、1张工单、2项到期任务和4个引用文件；备份0.191304秒，恢复及应用核验0.383451秒；日志`.venv/tmp/resume-recovery-actual.log`。只测本地虚构资料，不是生产RPO/RTO或真实通知投递承诺。
- 配额替身容量：60次本地台账尝试，15获准、45受限，150 token，1.405261秒、42.6967次尝试/秒；没有模型调用，不能当业务吞吐。
- 构建、依赖元数据一致性、源码/测试/脚本编译及结构检查通过；8个项目目录、23个必需文件。`git diff --check`通过，仅有Windows行尾转换提示。未跟踪文本另行检查归属和格式。
- 独立代码审查：指定范围未发现新重要缺陷；主业务链及最后新增恢复函数均完成独立阅读，后者另有8项静态通过；明确通过项及边界见[独立审查](completion-independent-review.md)，审查者另行执行结构检查通过。

浏览器日志：`.venv/tmp/resume-browser-final.log`、`.venv/tmp/resume-browser-content-final.log`。这些是忽略目录内的本机证据，持久说明保存在本文和模块文档。

## 尚未完成与外部条件

- 真实政策、岗位授权、人工联系方式、政务接口、获批准LLM/嵌入服务和生产负载验收；未声称99%一致率。
- Windows文件访问和出站网络隔离未完成；仅可信虚构教学资料可显式开启解析。Job Object资源限制不等于权限隔离。
- 两事项指南须经审核发布。冻结30题按已保存分类重建，不是逐字恢复缺失原题，也不是30次真实模型端到端成功。
- 留存只处理逐ID批准、无业务引用的独立评价/诊断JSON；业务库、材料、索引、备份与法律保留的完整处置链未实现。
- 本地恢复覆盖真实应用工单及到期任务；不同应用版本切换、降级迁移、真实授权规则/提示词发布回退和生产在途切换仍待发布环境验收；本地演练已包含教学知识索引和规则/提示词受控恢复。
- 独立全局预检审计页面未接入；经授权共享的历史摘要已在工单详情可见。

## 文件归属

所有新增文件均在既有`src/zhengwu_precheck/`、`tests/`、`scripts/`、`docs/`、`data/`或`config/`职责内；未新增根目录配置、第二源码包或项目目录。保留既有未提交改动及`.workbuddy-ai/`。运行库、依赖和日志在已忽略`.venv/`内。

实际新增文件树见文末核对清单；原有未改动文件和工具目录省略。

```text
zhengwu/
├── config/
│   ├── release.env.example
│   └── requirements.release.lock.txt
├── data/
│   └── materials/
│       ├── fictional-identity.png
│       ├── fictional-scanned-identity.pdf
│       ├── teaching-rules-v1.json
│       ├── 两事项教学规则指南.md
│       ├── 个体工商户设立登记_教学指南_v1.md
│       └── 居住证申领_教学指南_v1.md
├── docs/
│   ├── superpowers/
│   │   ├── completion-independent-review.md
│   │   ├── completion-progress.md
│   │   ├── plans/
│   │   │   └── 2026-10-31-completion-execution.md
│   │   └── specs/
│   │       └── 2026-10-31-completion-roadmap-design.md
│   ├── 上线前检查清单与开发策略核对.md
│   ├── 智能体运行与验证说明.md
│   ├── 材料基础实现与验证.md
│   └── 运维部署与恢复.md
├── scripts/
│   ├── capacity_demo.py
│   └── operations.py
├── src/
│   └── zhengwu_precheck/
│       ├── api/
│       │   └── routes/
│       │       ├── agent.py
│       │       ├── materials.py
│       │       └── operations.py
│       ├── core/
│       │   └── services/
│       │       ├── agent.py
│       │       ├── materials.py
│       │       ├── model_budget.py
│       │       ├── operations.py
│       │       ├── retention.py
│       │       └── retention_policy.py
│       ├── infrastructure/
│       │   ├── backup.py
│       │   ├── material_repository.py
│       │   ├── quota_repository.py
│       │   └── request_limiter.py
│       ├── static/
│       │   └── js/
│       │       ├── assistant.js
│       │       └── materials.js
│       ├── templates/
│       │   ├── assistant.html
│       │   ├── shared_ticket_context.html
│       │   └── ticket_draft.html
│       └── utils/
│           ├── material_extract.py
│           └── redaction.py
└── tests/
    ├── browser_assistant.py
    ├── fixtures/
    │   └── project-evaluation-v1.json
    ├── test_agent_loop.py
    ├── test_agent_routes.py
    ├── test_confirmed_tickets.py
    ├── test_evaluation_contract.py
    ├── test_material_routes.py
    ├── test_materials.py
    ├── test_model_budget.py
    ├── test_operations.py
    ├── test_operations_cli.py
    ├── test_retention_policy.py
    ├── test_retention_routes.py
    ├── test_shared_rate.py
    └── test_teaching_workflow.py
```

根目录职责保持原状；本树列出当前工作区全部未跟踪项目文件，不包含已忽略运行产物。
