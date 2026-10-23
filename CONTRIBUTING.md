# 贡献指南

修改前阅读 README.md 和 AGENTS.md，保持文件在规定框架内。

提交问题时说明预期行为、实际行为和复现步骤，附件应脱敏。

提交 PR 时描述变更目的、影响范围与实际验证结果。代码放在 src/，对应测试放在 tests/；功能变化同步更新 docs/ 和 CHANGELOG.md。

提交前执行 `python scripts/check_structure.py`，并运行与改动有关的测试。业务尚未实现时，不以占位测试代表业务验证通过。
