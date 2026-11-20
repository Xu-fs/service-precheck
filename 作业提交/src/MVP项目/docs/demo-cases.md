# 两个完整流程预设案例（示例）

两个案例使用虚构资料，沿用现有咨询工单、岗位转派和确认结案流程；这里的初审、复核和主任审核是咨询处理意见流转，正式政务申请审批接口尚未接入。

网页同时提供两种方式：[手动逐步办理](http://127.0.0.1:8000/my/tickets)使用实际岗位账号操作预设工单；[自动演示](http://127.0.0.1:8000/examples/workflow)逐步执行同样的预设动作，并自动切换演示视角。两种方式分别保存记录，互不推进对方进度。

| 案例 | 用户问题 | 补充信息 |
| --- | --- | --- |
| 居住证材料补正（示例） | 居住说明遗漏地址 | 补充虚构路一号及资料日期 |
| 个体工商户经营地址核对（示例） | 两份资料中的地址不一致 | 澄清为虚构路二号，并说明更正内容 |

## 准备待办案例

在项目根目录执行：

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts/create_demo_cases.py
```

脚本复用[示例账号](demo-accounts.md)，创建两个待窗口接手的工单，并分别保存一条办事人消息。重复执行复用原案例，不清空已有消息或进度。账号、案例与网页必须使用同一个 `ZHENGWU_RUNTIME_DIR`；默认使用系统临时目录下的 `zhengwu-web`。

也可在网页[我的人工工单](http://127.0.0.1:8000/my/tickets)的“预设案例（示例）”中选择任一案例，核对填写内容后点击“提交工单”，直接进入窗口待接手队列。选择案例本身不会创建记录。

## 在网页逐步办理

密码均为本地预设密码 `123456`；若此前自行修改过密码，初始化会保留修改后的密码。

| 顺序 | 登录账号 | 页面操作 |
| --- | --- | --- |
| 1 | `demo_citizen` | 首页个人登录；进入“我的人工工单”，查看初始化案例，或选择预设并确认提交、发送消息 |
| 2 | `demo_window` | 工作人员登录 → 工单处理 → 接手案例；发送预填的“请求补充信息” |
| 3 | `demo_citizen` | 打开原工单，发送预填的地址补充或更正说明 |
| 4 | `demo_window` | 刷新工单，将预填的初审意见转派给 `demo_reviewer` |
| 5 | `demo_reviewer` | 确认接收转派；发送预填的业务复核结果，再转派给 `demo_director` |
| 6 | `demo_director` | 确认接收转派，发送预填的主任审核意见 |
| 7 | `demo_citizen` | 核对最终答复，点击“确认问题已解决”，工单结案 |

工作人员入口为 [后台登录](http://127.0.0.1:8000/staff/login)。同一浏览器可在工作人员登录页更换岗位账号；办事人与工作人员登录分别保存。工单详情显示下一步账号、操作和示例文字，答复与转派表单自动预填相关内容，仍需本人确认发送。每次切换角色或完成操作后刷新详情。

完整演示时请在主任给出最终意见后再由办事人确认结案。现有通用咨询工单允许办事人在工作人员答复后确认解决，不额外强制所有工单经过这三个岗位。

## 自动执行及重新演示

### 网页自动演示

使用办事人账号登录首页后，进入“我的人工工单”，点击案例旁的“自动演示”。也可直接打开[流程演示（示例）](http://127.0.0.1:8000/examples/workflow)。

- “开始自动演示”依次执行11步，画面随办事人、窗口、审核员、主任和办事人的处理动作切换。
- “暂停”在当前正在提交的步骤保存后停止；“继续自动演示”接着执行。
- “下一步”每次执行一步，便于逐环节讲解；同一请求重试不会多推进一步。
- “新建一轮示例”另建一份示例，原轮次保留在“历史演示记录”中。
- 刷新页面后读取已有进度，点击继续即可续办。播放速度提供正常、详细和快速三档。

自动切换的是演示视角，当前实际登录账号不变。每一步仍通过现有工单服务保存消息、岗位转派与接收、答复及确认结案。网页演示数据位于运行目录的 `workflow-demo/workflow-examples.sqlite3`，与手动工单的 `chat.sqlite3` 分开；不能操作普通工单或真实材料，也不生成工作人员登录凭证。每个登录用户只能打开自己创建的演示记录。

### 命令行自动执行

下面保留原有脚本方式，用于处理工作库中的预设账号案例；它不会操作网页自动演示库。

```powershell
# 将默认的一对案例按真实服务动作依次执行到用户确认结案
.\.venv\Scripts\python.exe -X utf8 scripts/create_demo_cases.py --complete

# 保留已有案例，另建一对待办案例
.\.venv\Scripts\python.exe -X utf8 scripts/create_demo_cases.py --new

# 保留已有案例，另建一对并完整执行到结案
.\.venv\Scripts\python.exe -X utf8 scripts/create_demo_cases.py --new --complete
```

自动执行会逐条保存用户消息、补充请求、两次转派及接收、复核和主任答复、用户确认记录，不直接修改状态来伪造流转。重复执行会复用已执行步骤；默认案例若已手动处理，自动流程遇到版本变化会停止，保留现有记录，此时使用 `--new` 开始另一轮演示。

无需配置外部模型即可办理这两个工单案例；指南智能问答仍按其原有配置运行。

## 验证与文件归属

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -p test_demo_cases.py -v
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -p test_workflow_demo.py -v
.\.venv\Scripts\python.exe -X utf8 tests/run_browser_check.py --suite cases
.\.venv\Scripts\python.exe -X utf8 scripts/check_structure.py
```

本次案例相关文件位于既有目录内：

```text
zhengwu/
├── data/
│   └── workflow-cases.json
├── docs/
│   └── demo-cases.md
├── scripts/
│   └── create_demo_cases.py
├── src/zhengwu_precheck/
│   ├── api/routes/tickets.py
│   ├── api/routes/workflow_demo.py
│   ├── core/services/demo_cases.py
│   ├── core/services/workflow_demo.py
│   ├── main.py
│   ├── static/
│   │   ├── css/workflow_demo.css
│   │   └── js/workflow_demo.js
│   └── templates/
│       ├── example_workflow_hint.html
│       ├── tickets.html
│       ├── workflow_demo.html
│       └── staff/tickets_detail.html
└── tests/
    ├── browser_demo_cases.py
    ├── run_browser_check.py
    ├── test_demo_cases.py
    └── test_workflow_demo.py
```
