# 政务易办

办事材料预检与服务协同平台。仓库名称建议：`zhengwu-precheck`；Python 包名：`zhengwu_precheck`。

## 当前状态

当前为项目目录骨架，尚未实现材料预检、服务协同、接口或部署功能。

## 环境与运行

需要 Python 3.10 或更高版本。在仓库根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:PYTHONPATH = (Resolve-Path src).Path
.\.venv\Scripts\python.exe -m zhengwu_precheck.main
```

当前入口只输出骨架状态提示。上述 PYTHONPATH 设置只影响当前终端。

## 检查和示例

```powershell
python scripts/check_structure.py
python examples/demo.py
```

示例仅展示入口调用。业务测试待功能实现后补充。

## 目录约定

后续新增文件必须遵守根目录 [AGENTS.md](AGENTS.md) 中的底层边界。设计与接口分别见 [设计说明](docs/design.md) 和 [接口说明](docs/api.md)。

## 许可证

本骨架采用 MIT 许可证，见 LICENSE。第三方材料的授权由其原始权利人决定。
