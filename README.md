# 政务易办

办事材料预检与服务协同平台。仓库名称建议：`zhengwu-precheck`；Python 包名：`zhengwu_precheck`。

## 当前状态

已实现命令行示例指南问答原型及办事人网页问答入口：FastAPI POST SSE、SQLite会话与消息、匿名身份隔离、独立账号登录、事项确认、引用展示和手机适配页面。指南问答共用BM25、FAISS及可配置模型API。

工作人员后台第一至四批已接入：独立JWT登录、七类岗位与事项权限、显式记录授权和SSR工作台；Markdown/PDF知识处理、不可变草稿、审核发布/停用、规则反馈；每事项提示词版本/恢复，以及真实对话与拒绝事件的授权审计、风险标记和人工复核；窗口工单接收、确认转派、回复、站内提醒与完成。入口 `/staff/login`，账号与操作说明见 [工作人员后台运行说明](docs/工作人员后台第一批运行说明.md)。

公开问答使用接纳事务冻结的适用发布包、知识版本及提示词，按BM25/FAISS检索并校验完整原文引用。两类事项均可配置示例资料；首次发布后停用不会恢复旧本地指南，内部流程不进入公开问答。审计没有伪造预检记录。

办事人通过 `/assistant` 确认示例事项与规则、上传并主动转录虚构材料、辅助预检及选择共享范围；通过草稿预览明确确认后创建人工工单。`/my/tickets` 提供普通咨询和本人进度，工作人员在 `/staff/tickets` 处理。传统入口和六工具共用草稿确认与幂等创建服务，未知事项进入显式授权的人工分流。详见 [智能体运行与验证](docs/智能体运行与验证说明.md)。

材料私有存储、逐份授权、字段转录、两事项示例预检及工单资料共享已接入。仓库提供两事项候选示例指南，须经现有审核发布流程启用；不自动发布。可显式初始化八类虚构示例账号，见 [示例预设账号](docs/demo-accounts.md)；正常启动不自动创建账号。真实本地中文图片及扫描PDF OCR已验证，仅在可信虚构示例模式启用。Windows任意不可信文件的权限及网络隔离尚未完成，默认不开放此类解析。详见 [材料实现与验证](docs/材料基础实现与验证.md)。

同机共享限流、模型预算、获批独立记录留存、构建及一致性备份恢复工具已接入，见 [运维部署与恢复](docs/运维部署与恢复.md)。真实政策、客户授权、政务接口、获批准模型服务和生产负载仍待联调；不声明正式上线或达到99%一致率。

## 环境与运行

Windows 已准备好下述 Python 环境和依赖后，可直接双击根目录的 `启动网页.bat`。脚本会在服务就绪后打开浏览器；若本机 8000 端口上的本项目服务已经启动，会直接打开网页。运行期间保持启动窗口打开，按 `Ctrl+C` 停止本次启动的服务；报错时窗口会保留错误信息。实际指南问答仍需要配置模型服务。

需要 Python 3.11 或更高版本。在仓库根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:PYTHONPATH = (Resolve-Path src).Path
.\.venv\Scripts\python.exe -m zhengwu_precheck.main
```

启动后访问 http://127.0.0.1:8000 。上述 PYTHONPATH 设置只影响当前终端。网页可先进行匿名事项确认；实际指南问答需要配置获批准的LLM及嵌入服务。

模型配置沿用阶段1环境变量：`LLM_PROVIDER`、`LLM_BASE_URL`、`LLM_MODEL`、`LLM_API_KEY`、`EMBEDDING_BASE_URL`、`EMBEDDING_MODEL`、`EMBEDDING_API_KEY`。运行数据库和索引默认保存在仓库外系统临时目录的 `zhengwu-web/`，需要稳定保存时设置仓库外的 `ZHENGWU_RUNTIME_DIR`。

完整启动、账号创建、部署限制和实际目录树见 [办事人问答入口运行说明](docs/办事人问答入口运行说明.md)。

## 检查和示例

两个可完整流转的预设案例：居住证材料补正、个体工商户经营地址核对，均标注“（示例）”。可在 `/my/tickets` 选用预设手动逐步办理，在 `/examples/workflow` 自动演示并跟随各岗位视角，或执行 `scripts/create_demo_cases.py` 准备待办。两种网页模式分别保留记录，均按窗口初审、业务复核、主任审核、办事人确认依次结案。操作说明见 [完整流程案例](docs/demo-cases.md)。

```powershell
python scripts/check_structure.py
python examples/demo.py
```

`examples/demo.py` 只检查依赖，不启动常驻服务器。命令行问答仍使用 `examples/guide_qa.py`，配置及验证问题见 [阶段1指南问答原型](docs/阶段1指南问答原型.md)；核心逻辑现已提取到源码包内供网页复用。

自动测试：`.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -v`。外部模型响应在测试中使用替身，不等于真实模型效果验证。

前端流解析测试：`node --test tests/test_sse.mjs`。浏览器回归：额外安装 `playwright` 并准备本机Chrome后，执行 `.\.venv\Scripts\python.exe -X utf8 tests/run_browser_check.py --suite all`；问答、工作人员、工单、助手和内容流程分别使用独立临时数据库，模型HTTP使用替身，退出时停止测试服务。当前验证结果与限制统一记录于 [补齐执行记录](docs/superpowers/completion-progress.md)，历史测试数字不作为当前回归结论。

## 目录约定

后续新增文件必须遵守根目录 [AGENTS.md](AGENTS.md) 中的底层边界。设计与接口分别见 [设计框架](docs/设计框架.md) 和 [接口说明](docs/api.md)。

## 许可证

本骨架采用 MIT 许可证，见 LICENSE。第三方材料的授权由其原始权利人决定。
