"""补充明确标注的教学模拟单案，不把设计数值写回原始采访事实。"""
from datetime import datetime
from pathlib import Path
import hashlib
import json
import shutil

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / '作业提交'
EVENT = OUT / 'docs/03_典型事件采访'
MARKER = '\n## 单案量化补充 教学模拟\n'


def make_case():
    # All timestamps are designed values. They are not measured browser timings.
    raw = [
        (1,'咨询','窗口岗','11-02 09:00','11-02 09:14'),
        (1,'首次材料提交','窗口岗','11-02 09:14','11-02 09:22'),
        (1,'首次完整性预检','窗口岗','11-02 09:22','11-02 09:32'),
        (1,'首次专业核对','审核岗','11-02 10:00','11-02 10:15'),
        (1,'第一次补正告知','窗口岗','11-02 10:15','11-02 10:21'),
        (2,'第一次补正材料提交','窗口岗','11-03 09:00','11-03 09:10'),
        (2,'第二次完整性预检','窗口岗','11-03 09:10','11-03 09:18'),
        (2,'第二次专业核对','审核岗','11-03 10:00','11-03 10:12'),
        (2,'第二次补正告知','窗口岗','11-03 10:12','11-03 10:17'),
        (3,'第二次补正材料提交','窗口岗','11-04 09:00','11-04 09:08'),
        (3,'第三次完整性预检','窗口岗','11-04 09:08','11-04 09:14'),
        (3,'第三次专业核对','审核岗','11-04 09:30','11-04 09:40'),
        (3,'核对结果通知','热线岗','11-04 09:40','11-04 09:45'),
    ]
    segments=[]
    for visit, stage, role, start, end in raw:
        a=datetime.strptime('2026-'+start,'%Y-%m-%d %H:%M')
        b=datetime.strptime('2026-'+end,'%Y-%m-%d %H:%M')
        segments.append(dict(visit=visit, stage=stage, role=role, start=a.isoformat(timespec='minutes'),end=b.isoformat(timespec='minutes'),minutes=int((b-a).total_seconds()/60)))
    parse=datetime.fromisoformat
    processing=sum(s['minutes'] for s in segments)
    elapsed=int((parse(segments[-1]['end'])-parse(segments[0]['start'])).total_seconds()/60)
    onsite=sum(int((max(parse(s['end']) for s in segments if s['visit']==v)-min(parse(s['start']) for s in segments if s['visit']==v)).total_seconds()/60) for v in (1,2,3))
    roles={r:sum(s['minutes'] for s in segments if s['role']==r) for r in ('窗口岗','审核岗','热线岗')}
    assert processing==117 and elapsed==2925 and onsite==203
    assert roles=={'窗口岗':75,'审核岗':37,'热线岗':5}
    return dict(case_id='DEMO-EVENT-001', provenance='teaching_simulation', real_business_verified=False, observed_interview_quote=False,
                note='所有个案次数、日期和耗时由编制者设计，仅作教学模拟。f00和f01仅提供总体目标与流程背景，不提供这些个案数字。',
                scope='居住证申领材料准备与辅助核对示例，不判断资格或审批结果',
                visits=3, correction_rounds=2, staff_roles=3, segments=segments,
                metrics=dict(staff_processing_minutes=processing, role_minutes=roles, onsite_minutes=onsite,
                             onsite_wait_minutes=onsite-processing, offsite_interval_minutes=elapsed-onsite,
                             total_nonprocessing_minutes=elapsed-processing, elapsed_minutes=elapsed))


def section():
    case=make_case()
    text=MARKER+'''
本节于2026年11月8日补充，类型为教学模拟单案。它与前文两轮原始采访分开：原受访者没有提供可核实的个案次数和节点计时，以下数字均为课程设计值，不是顾明岚或其他被访谈者的新陈述，也不是系统运行实测。

### 来源与新增内容的区别

government-service-f00只提供总体补正率36%及目标18%、平均清单解释12分钟及目标5分钟；government-service-f01只提供主流程并说明节点尚未分别计时。两份附件均自标为合成业务资料。它们能支持选择“材料反复补正”这个主题，不能证明本节单案的次数、日期和耗时。附件原件副本位于本目录原始事实附件中。

本节编号DEMO-EVENT-001，不复用government-service-f00或f01作为新增事实编号。模拟时间轴安排为2026年11月2日至4日，仅方便检查先后关系；这些不是实际办事日期。

### 典型事件与处理结果

模拟办事人准备居住证申领材料，第一次到窗口时，居住说明缺少完整地址，需要补充；第二次提交时，两份材料的地址表达不一致，需要由办事人确认并补充说明；第三次提交后，经窗口核对、审核岗复核及热线通知，材料问题得到澄清。设计假定其间线上清单未明确列出这两项核对提示，所以问题在后续环节分两次暴露。

此处“结果完成”仅指材料核对与咨询反馈完成，可进入原有法定办理流程；不表示已批准居住证，不表示已形成真实正式办件。

### 单案量化概览

| 指标 | 模拟值 | 统计定义 |
| --- | --- | --- |
| 实际到场次数 | 3趟 | 三个不同日期各到场一次，首次提交计入 |
| 材料补正次数 | 2轮 | 两次发出补正要求并分别再次提交，不按材料份数累加 |
| 涉及工作人员岗位 | 3类 | 窗口岗、审核岗、热线岗；办事人另计，不算工作人员岗位 |
| 窗口岗处理工时 | 75分钟 | 本案窗口岗各次直接处理时间之和 |
| 审核岗处理工时 | 37分钟 | 三次专业核对15加12加10分钟 |
| 热线岗处理工时 | 5分钟 | 一次核对结果通知 |
| 工作人员总处理工时 | 117分钟 | 三岗工时相加，模拟中各节点没有并行重叠 |
| 办事人在场总时长 | 203分钟 | 三趟分别81、77、45分钟，含处理及现场等待 |
| 现场等待合计 | 86分钟 | 三次等待28、42、16分钟 |
| 跨次到场间隔合计 | 2722分钟 | 含非工作时间和材料准备间隔，不全部归责于工作人员 |
| 端到端自然历时 | 2925分钟 | 48小时45分钟，从首次咨询至结果通知 |

本案咨询耗时设为14分钟，与f00中的总体平均12分钟分开。总体平均值不应直接填成某一单的实测值。单个模拟案例不能计算或验证总体补正率、99%解释一致率、每周发生频次或真实提速比例。

### 逐节点时间账

下表全部为模拟设计值。每段均含明确起止时间、责任岗位及直接处理分钟，等待从相邻记录的时间差计算。第1、2、3天分别对应模拟时间轴中的11月2、3、4日。

| 环节 | 模拟起止时间 | 岗位 | 处理分钟 |
| --- | --- | --- | --- |
'''
    for s in case['segments']:
        text+=f"| {s['stage']} | 第{s['visit']}天 {s['start'][11:]}至{s['end'][11:]} | {s['role']} | {s['minutes']} |\n"
    text+='''
### 等待和工时的核算

首次现场等待为09:32至10:00，共28分钟；第二次为09:18至10:00，共42分钟；第三次为09:14至09:30，共16分钟，合计86分钟。两次离场至下次到场间隔分别为1359和1363分钟，合计2722分钟。

工作人员处理工时117分钟加现场等待86分钟，等于办事人在场203分钟。在场203分钟加跨次到场间隔2722分钟，等于自然历时2925分钟。跨日间隔包含夜间、本人准备及其他未细分时间，不把它全部称为系统延误，也不与三岗工时重复相加。

### 补充访谈演练 三轮模拟问答

以下为根据本节模拟时间账编写的访谈演练，不是原平台采访导出，不是原六位受访者的原话。原始采访TXT及三个原始导出文件保持不变。

第一轮 访谈者：这一单具体哪里出了问题，群众跑了几趟、补了几次材料？

模拟窗口岗：本例第一次提交缺完整地址，第二次提交的两份材料地址表达不一致，需要再次说明。到第三次提交才把问题核对清楚，一共到场3趟、补正2轮。这里按补正要求和再次提交的轮次统计，不把每份文件各算一次。

第二轮 访谈者：你本人花了多久，涉及哪些岗位，等待和处理怎样区分？

模拟窗口岗：我直接处理合计75分钟，审核岗三次核对合计37分钟，热线通知5分钟，三岗处理合计117分钟。办事人三趟在场203分钟，其中86分钟为现场等待。跨次到场间隔2722分钟含夜间与准备时间，不全部计为窗口耗时。这些都是本教学案例设定的数据。

第三轮 访谈者：最终结果是什么，可以据此说改造已经减少了补正吗？

模拟窗口岗：本例材料问题已澄清并通知办事人，后续正式申请仍按原法定流程走。本例没有实施前后配对数据，不能据此声称AI已降低补正率。改造建议是把完整地址和跨材料一致性提示前移，统一规则版本，并在发补正意见前做人工复核；效果需另行采样。

### 与业务工作流的对应及后续采样

本例可映射到提交工单、窗口补正、用户补充、专业复核、答复和用户确认解决。该链路可使用现有非模型业务工作流演示。实际浏览器测试通过只能证明系统动作可流转，不证明本节设定的75分钟或2925分钟来自系统测量。

如需换成真实或平台确认的个案，应追加脱敏业务编号、材料版本、到场或提交凭据、两次补正通知、各岗位起止记录、最终结果及数据授权。届时按相同公式重算。原采访缺失的真实性证据仍保留为待核实，本节只补足课程模拟案例的具体描述和可复核时间账。
'''
    return text


def apply_to_submission():
    from build_assignment import both, document, put
    case=make_case()
    EVENT.mkdir(parents=True,exist_ok=True)
    attachments=EVENT/'原始事实附件'
    attachments.mkdir(exist_ok=True)
    for name in ('government-service-f00.txt','government-service-f01.txt'):
        source=OUT/name
        shutil.copy2(source, attachments/name)
        assert source.read_bytes()==(attachments/name).read_bytes()
    path=EVENT/'典型事件采访整理.md'
    original=path.read_text(encoding='utf-8').split(MARKER,1)[0].rstrip()
    combined=original+'\n'+section()
    put(path,combined)
    document(path.with_suffix('.docx'),combined)
    put(EVENT/'单案量化补充_教学模拟.md','# 材料反复补正单案量化补充\n\n'+section().lstrip())
    put(EVENT/'单案量化数据_教学模拟.json',json.dumps(case,ensure_ascii=False,indent=2))
    interview=section().split('### 补充访谈演练 三轮模拟问答',1)[1].split('### 与业务工作流',1)[0]
    put(EVENT/'典型事件补充访谈_教学模拟.txt','教学模拟访谈演练 非原始采访\n'+interview)
    index=OUT/'00_作业提交清单.md'
    value=index.read_text(encoding='utf-8')
    old='1. 典型事件已有两轮，但“具体到一单且量化”的原始信息不足。补充一个脱敏个案的趟次、补正次数、节点时长和结果；本包没有编造这些数字。'
    new='1. 已补教学模拟单案DEMO-EVENT-001：3趟到场、2轮补正、3类岗位、117分钟处理工时，含逐节点账和三轮模拟访谈。原始采访仍无可核实单案数据；新增数字全部为课程设计值，不标为原受访者确认或系统实测。'
    value=value.replace(old,new)
    put(index,value)
    document(index.with_suffix('.docx'),value)
    print(json.dumps(case['metrics'],ensure_ascii=False))


if __name__=='__main__':
    apply_to_submission()
