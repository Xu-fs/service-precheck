# 贡献指南

修改前阅读 README.md 和 AGENTS.md，保持文件在规定框架内。

提交问题时说明预期行为、实际行为和复现步骤，附件应脱敏。

提交 PR 时描述变更目的、影响范围与实际验证结果。代码放在 src/，对应测试放在 tests/；功能变化同步更新 docs/ 和 CHANGELOG.md。

提交前执行 `python scripts/check_structure.py`，并运行与改动有关的测试。业务尚未实现时，不以占位测试代表业务验证通过。

## 环境和统一检查

使用 Python 3.12（当前自动检查版本）和 Node.js 22。在已有运行依赖之外安装开发检查工具：

```powershell
python -m pip install -r requirements.txt -r config/requirements-dev.txt
python scripts/verify.py
```

上述 `python` 应为项目虚拟环境解释器。脚本自动设置源码路径和 UTF-8，依次执行目录边界、Ruff 静态检查、新检查工具的格式检查、依赖入口、Python 全量测试和前端流解析；任何一步失败即停止，缺失工具也视为失败，不自动安装或跳过。

界面或浏览器交互有变化时，额外安装 Playwright 并准备本机 Google Chrome，执行 `python scripts/verify.py --full`。该模式在基础检查后运行全部现有浏览器回归，使用临时教学运行库和模型替身。它不能证明真实外部模型或政务接口效果。

Ruff 配置统一位于 `config/ruff.toml`。静态检查覆盖 `src/`、`scripts/`、`tests/`、`examples/`；首批启用语法、无效控制流、未定义名称和未使用导入检查。兼容导出用显式同名别名说明用途。新增 Python 代码采用四空格、双引号、100 列目标宽度；历史文件随必要修改逐步格式化，避免与业务修改混成大面积差异。

```powershell
python -m ruff check --config config/ruff.toml src scripts tests examples
python -m ruff format --config config/ruff.toml scripts/verify.py tests/test_verify.py
```

## 提交和评审

- 一次提交围绕一个可解释的目的，避免只写 `v1.0`、`demo` 或 `docs2.0`。
- 标题使用 `类型: 具体变化`，类型可用 `feat`、`fix`、`docs`、`test`、`chore`、`refactor`。例如 `chore: 统一工程检查并增加 Windows 自动验证`。
- 正文说明原因、影响范围、实际执行的验证和未验证内容。不能以测试替身通过宣称真实服务联调成功。
- 功能或接口有变化时同步 README、对应使用/API 文档和 CHANGELOG；不编造历史日期、测试数字或发布状态。
- 修复缺陷应提供可复现步骤及相关回归验证；PR 按仓库模板说明目的、变更和结果。
- 提交前检查差异，排除真实资料、凭证、运行数据库、依赖和生成缓存，遵守 AGENTS.md 的文件归属规则。
