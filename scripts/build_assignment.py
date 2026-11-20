"""按课程清单生成独立提交副本，保留原始来源、真实测试结果及未满足项。"""
from pathlib import Path
import csv
import hashlib
import io
import json
import re
import shutil
import textwrap
from datetime import datetime
from zipfile import ZipFile, ZIP_DEFLATED

from docx import Document
from docx.shared import Cm, Pt, RGBColor
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / '作业提交'
DOCS = OUT / 'docs'
DATE = '2026年11月7日'
FONT = 'Microsoft YaHei'


def exportable(path):
    return not any(part in {'.venv', '.git', '__pycache__', '.pytest_cache'} for part in path.relative_to(OUT).parts)


def put(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.strip() + '\n', encoding='utf-8')


def plain(s):
    s = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'\1（\2）', s)
    return s.replace('**', '').replace('`', '')


def document(path, content):
    doc = Document()
    sec = doc.sections[0]
    sec.page_height, sec.page_width = Cm(29.7), Cm(21)
    sec.top_margin = sec.bottom_margin = Cm(1.9)
    sec.left_margin = sec.right_margin = Cm(2)
    for name in ('Normal', 'Title', 'Subtitle', 'Heading 1', 'Heading 2', 'Heading 3'):
        style = doc.styles[name]
        style.font.name = FONT
        style._element.get_or_add_rPr().rFonts.set(qn('w:eastAsia'), FONT)
        style.font.color.rgb = RGBColor(0, 0, 0)
    doc.styles['Normal'].font.size = Pt(10.5)
    doc.styles['Normal'].paragraph_format.line_spacing = 1.28
    doc.styles['Normal'].paragraph_format.space_after = Pt(6)
    doc.styles['Title'].font.size = Pt(23)
    doc.styles['Title'].paragraph_format.space_after = Pt(12)
    for name, size in [('Heading 1', 15), ('Heading 2', 12), ('Heading 3', 11)]:
        doc.styles[name].font.size = Pt(size)
        doc.styles[name].paragraph_format.space_before = Pt(10)
        doc.styles[name].paragraph_format.space_after = Pt(5)
    doc.core_properties.title = content.splitlines()[0].lstrip('# ')
    doc.core_properties.author = '政务易办项目组'
    doc.core_properties.subject = '工程技术与创新应用课程作业'
    lines = content.strip().splitlines()
    i = 0
    in_code = False
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith('```'):
            in_code = not in_code
            i += 1
            continue
        if not line:
            i += 1
            continue
        if line.startswith('|') and i + 1 < len(lines) and re.match(r'^\|[\s:|\-]+\|$', lines[i + 1].strip()):
            rows = []
            while i < len(lines) and lines[i].strip().startswith('|'):
                row = [plain(v.strip()) for v in lines[i].strip().strip('|').split('|')]
                if not all(re.fullmatch(r'[:\-\s]+', cell) for cell in row):
                    rows.append(row)
                i += 1
            cols = len(rows[0])
            table = doc.add_table(rows=0, cols=cols)
            table.autofit = False
            widths = [17 / cols] * cols
            if cols == 4:
                widths = [2.3, 4.9, 5.4, 4.4]
            elif cols == 3:
                widths = [3.1, 6.9, 7]
            elif cols == 2:
                widths = [4, 13]
            if '作业提交清单' in doc.core_properties.title and cols == 3:
                widths = [4.8, 6.1, 6.1]
            if '报价' in doc.core_properties.title and cols == 4:
                widths = [6.8, 3.4, 3.4, 3.4]
            for col, width in zip(table.columns, widths):
                col.width = Cm(width)
            for n, row in enumerate(rows):
                cells = table.add_row().cells
                for cell, value, width in zip(cells, row, widths):
                    cell.width = Cm(width)
                    cell.text = value
                    tcPr = cell._tc.get_or_add_tcPr()
                    borders = OxmlElement('w:tcBorders')
                    for side in ('top', 'left', 'bottom', 'right'):
                        edge = OxmlElement('w:' + side)
                        for k, v in [('val', 'single'), ('sz', '5'), ('color', 'D9D9D9')]:
                            edge.set(qn('w:' + k), v)
                        borders.append(edge)
                    tcPr.append(borders)
                    margins = OxmlElement('w:tcMar')
                    for side in ('top', 'left', 'bottom', 'right'):
                        edge = OxmlElement('w:' + side)
                        edge.set(qn('w:w'), '85')
                        edge.set(qn('w:type'), 'dxa')
                        margins.append(edge)
                    tcPr.append(margins)
                    if n == 0:
                        shade = OxmlElement('w:shd')
                        shade.set(qn('w:fill'), 'DFEAF3')
                        tcPr.append(shade)
                    for p in cell.paragraphs:
                        p.paragraph_format.space_after = Pt(3)
                        p.paragraph_format.line_spacing = 1.12
                        if n == 0:
                            p.paragraph_format.keep_with_next = True
                        if '报价' in doc.core_properties.title and cols == 4 and cell != cells[0]:
                            p.alignment = 1
                        for r in p.runs:
                            r.font.size = Pt(9)
                            r.bold = n == 0
                trPr = table.rows[-1]._tr.get_or_add_trPr()
                no_split = OxmlElement('w:cantSplit')
                trPr.append(no_split)
                if n == 0:
                    trPr.append(OxmlElement('w:tblHeader'))
            doc.add_paragraph().paragraph_format.space_after = Pt(0)
            continue
        if line.startswith('!['):
            match = re.match(r'!\[(.*?)\]\((.*?)\)', line)
            if match:
                img = path.parent / match[2]
                if img.is_file():
                    with Image.open(img) as im:
                        w, h = im.size
                    max_height = 17 if '场景测试过程' in doc.core_properties.title else 19
                    width = min(17, max_height * w / h)
                    doc.add_picture(str(img), width=Cm(width))
                    doc.paragraphs[-1].paragraph_format.keep_with_next = True
                    p = doc.add_paragraph(match[1])
                    p.paragraph_format.space_after = Pt(8)
                    for run in p.runs:
                        run.font.size = Pt(9)
                else:
                    raise FileNotFoundError(img)
        elif line.startswith('# '):
            doc.add_paragraph(plain(line[2:]), 'Title')
        elif line.startswith('## '):
            doc.add_paragraph(plain(line[3:]), 'Heading 1')
        elif line.startswith('### '):
            doc.add_paragraph(plain(line[4:]), 'Heading 2')
        elif line.startswith('- '):
            doc.add_paragraph(plain(line[2:]), 'List Bullet')
        else:
            p = doc.add_paragraph(plain(line))
            if in_code:
                p.paragraph_format.line_spacing = 1.05
                p.paragraph_format.space_after = Pt(3)
                for run in p.runs:
                    run.font.size = Pt(9)
        i += 1
    # Page numbers aid assessment of long reports without adding decorative headers.
    footer = sec.footer.paragraphs[0]
    footer.alignment = 2
    field = OxmlElement('w:fldSimple')
    field.set(qn('w:instr'), 'PAGE')
    footer._p.append(field)
    # Remove the managed default template's inherited blue rules from new reports.
    for parent in (doc.styles.element, doc.element):
        for border in list(parent.iter(qn('w:pBdr'))):
            border.getparent().remove(border)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)


def both(folder, name, content):
    path = folder / name
    put(path.with_suffix('.md'), content)
    document(path.with_suffix('.docx'), content)


def flow(path, after=False):
    font_path = 'C:/Windows/Fonts/msyh.ttc'
    font = ImageFont.truetype(font_path, 25)
    small = ImageFont.truetype(font_path, 21)
    title = ImageFont.truetype(font_path, 40)
    im = Image.new('RGB', (1600, 1190), 'white')
    d = ImageDraw.Draw(im)
    d.text((65, 35), '政务易办改造后业务流程' if after else '政务易办改造前业务流程', fill='#142c41', font=title)
    d.text((65, 95), '教学设计  典型问题：清单不一致与材料反复补正  不代表真实行政审批流程已获批准', fill='#526578', font=small)
    before = [
        '办事人\n查阅线上材料清单', '办事人\n咨询并准备材料', '窗口工作人员\n接收材料并人工核对',
        '窗口工作人员\n发现清单差异或缺件', '窗口与办事人\n人工解释并要求补正', '办事人\n补充后再次提交',
        '业务审核员\n核对有效期与专业问题', '业务审核员\n异常退回并记录原因', '窗口与办事人\n继续补正或解释',
        '窗口与审核员\n跨岗交接核对状态', '热线服务人员\n查原业务并解释进度', '法定办理人员\n按原系统办理并通知']
    revised = [
        '办事人\n选择事项与办理情形', '办事人确认点一\n核对事项与规则版本', '办事人确认点二\n授权材料并核对字段',
        '系统辅助\n按版本检索与材料预检', '系统辅助\n展示引用 问题及风险', '办事人\n补充材料或选择转人工',
        '系统辅助\n整理草稿及共享范围', '办事人确认点三\n预览后明确确认提交', '窗口工作人员\n接手并发送补正意见',
        '审核员确认点四\n专业复核及接收转派', '工作人员\n答复并保留审计记录', '办事人确认点五\n确认解决并关闭工单']
    steps = revised if after else before
    centers = []
    for i, step in enumerate(steps):
        row, pos = divmod(i, 3)
        col = pos if row % 2 == 0 else 2 - pos
        x, y = 65 + col * 505, 175 + row * 220
        centers.append((x, y))
        fill = '#E6F0F8' if after else '#F2F3F5'
        if '确认点' in step:
            fill = '#EAF4E6'
        d.rounded_rectangle((x, y, x + 450, y + 145), radius=13, fill=fill, outline='#9AAFBF', width=2)
        d.text((x + 16, y + 13), f'{i + 1:02}', fill='#35536C', font=small)
        d.multiline_text((x + 25, y + 49), step, fill='#172F43', font=font, spacing=9)
    for i in range(11):
        x, y = centers[i]
        nx, ny = centers[i + 1]
        if y == ny:
            direction = 1 if nx > x else -1
            a = (x + 450, y + 72) if direction == 1 else (x, y + 72)
            b = (nx, ny + 72) if direction == 1 else (nx + 450, ny + 72)
            d.line([a, b], fill='#526F85', width=4)
            d.polygon([b, (b[0] - direction * 15, b[1] - 8), (b[0] - direction * 15, b[1] + 8)], fill='#526F85')
        else:
            a, b = (x + 225, y + 145), (nx + 225, ny)
            d.line([a, b], fill='#526F85', width=4)
            d.polygon([b, (b[0] - 8, b[1] - 15), (b[0] + 8, b[1] - 15)], fill='#526F85')
    note = '异常分支：依据不足 有效期疑问 版本冲突 服务不可用 → 停止自动建议并进入第7步人工草稿。' if after else '返工分支：第4或第7步发现问题 → 第5至第9步补正后重新核验；具体次数和耗时尚无个案记录。'
    d.text((65, 1080), note, fill='#9C3F29', font=small)
    d.text((65, 1123), '工单关闭不等于正式办件批准或办结。来源：六人访谈与典型事件采访；流程为课程设计整理。', fill='#526578', font=small)
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path)


def main():
    OUT.mkdir(exist_ok=True)
    for name in ('docs', 'src', 'test'):
        (OUT / name).mkdir(exist_ok=True)
    # User-supplied interview and design are distinct sources and remain byte-for-byte copies.
    six = DOCS / '01_六人访谈记录'
    six.mkdir(parents=True, exist_ok=True)
    report = (ROOT / 'docs/discovery-report.md').read_text(encoding='utf-8-sig')
    shutil.copy2(ROOT / 'docs/discovery-report.md', six / '六人访谈记录.md')
    mapping = {
        'government-service-r0': ('顾明岚', '服务中心主任'), 'government-service-r1': ('林亦舟', '窗口工作人员'),
        'government-service-r2': ('唐映秋', '业务审核员'), 'government-service-r3': ('秦芮', '政务平台工程师'),
        'government-service-r4': ('傅景', '数据与流程监督员'), 'government-service-r5': ('叶书', '热线服务负责人')}
    transcript = []
    original_transcript = report.split('## 访谈记录\n', 1)[1].split('\n## ', 1)[0]
    for m in re.finditer(r'^- (\S+) (访谈者|government-service-r\d)：(.*)$', original_transcript, re.M):
        at, speaker, value = m.groups()
        transcript.append(dict(at=at, speaker=speaker, text=value))
    facts = []
    for m in re.finditer(r'^- \[(government-service-f\d+)\] ([^：]+)：(.*)$', report, re.M):
        facts.append(dict(id=m[1], character=m[2], text=m[3]))
    payload = dict(title='六人访谈记录', source='原仓库docs/discovery-report.md', note='合成场景访谈；本JSON由用户指定的Markdown解析重建，不冒充平台原始JSON导出。', roles=mapping, facts=facts, transcript=transcript)
    put(six / '六人访谈记录_解析重建.json', json.dumps(payload, ensure_ascii=False, indent=2))
    with (six / '六人访谈证据_解析重建.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['id', 'character', 'text'])
        writer.writeheader()
        writer.writerows(facts)
    put(six / '来源说明.md', '# 六人访谈来源说明\n\n六人访谈来源为用户明确指定的仓库 docs/discovery-report.md。Markdown为原文件副本；JSON与CSV依据该Markdown解析生成，保留事实编号和角色。不得将其标为平台原始导出。原报告为合成案例，不是真实政务客户调查。')
    event = DOCS / '03_典型事件采访'
    event.mkdir(parents=True, exist_ok=True)
    for name in ('discovery-report.md', 'discovery-report.json', 'discovery-requirements.csv'):
        shutil.copy2(Path('C:/Users/26381/Desktop') / name, event / name)
    source = json.loads((event / 'discovery-report.json').read_text(encoding='utf-8-sig'))
    rounds = sum(t['speaker'] == 'user' for t in source['transcript'])
    raw = '典型事件采访 原始导出文本提取\n来源 discovery-report.json\n合成场景访谈，非真实政务客户记录。保留原文，不补写未提供的数字。\n\n'
    for turn in source['transcript']:
        raw += turn['at'] + ' ' + ('访谈者' if turn['speaker'] == 'user' else '受访者 顾明岚') + '\n' + turn['text'] + '\n\n'
    put(event / '典型事件采访原始记录.txt', raw)
    event_text = '''# 材料反复补正典型事件采访整理

本稿将用户提供的三份原始导出文件整理为课程采访材料，保留两轮完整问答，并将总体指标与个案数据分开。来源为2026年11月6日的合成场景访谈。当前受访者未提供某一办事人的完整个案记录，因此不据此声称已完成个案量化。

## 采访对象与问题

受访者为服务中心主任顾明岚。主题是线上清单与窗口要求不一致时，材料补正与跨岗交接如何造成重复办理。采访在两轮问答中分别追问亲历个案、跑动次数、补正次数、处理耗时和日常高频不便。

## 原始问答
'''
    for n, turn in enumerate(source['transcript']):
        if turn['speaker'] == 'user':
            event_text += f'\n### 第{n // 2 + 1}轮提问\n\n' + turn['text'] + '\n'
        else:
            event_text += '\n受访者原文：' + turn['text'] + '\n'
    event_text += '''
## 可确认的数字与口径

| 项目 | 当前口径 | 解释 |
| --- | --- | --- |
| 补正率 | 36% 目标18% | 合成场景的总体基线与目标，不是单个案例补正次数 |
| 清单解释时间 | 平均12分钟 目标5分钟 | 总体口径；不能写成该受访者某次实际耗时 |
| 采访轮数 | 2轮完整问答 | 两次提问与两次回答，原文见TXT |
| 单案跑动次数与补正次数 | 尚未核实 | 受访者明确没有可核对记录 |
| 各节点等待和处理时间 | 未分别计量 | 无法据此断言瓶颈在某一节点 |

## 事件链与改造建议

现状事件链为咨询、材料提交、完整性预检、专业审核、补正和办理通知。线上要求与窗口要求不一致可能使办事人在提交后才发现问题；预检与审核之间的等待是另一项需要验证的假设。先统一规则版本和问题记录，再通过辅助预检提示具体差异；需要进一步判断的事项保留人工复核和接手确认。

在系统中，办事人先确认事项和规则版本，上传并核对材料，看到有引用的准备性提示；未解决的问题经草稿预览和共享范围确认后进入窗口工单。专业异常由审核员核对，接收方确认接手后才完成责任转移。该链条是课程设计，不是本次采访中已完成的真实改造成效。

## 补充采样与验收方式

由窗口工作人员林亦舟提供一个脱敏的典型工单，业务审核员唐映秋补审核交接时间，监督员核对授权及记录完整性。为同一业务记录咨询起止、提交、预检起止、审核接收、补正通知、再次提交和最终结果；分别统计等待与处理时长。建议连续一周覆盖两类事项，样本数、排除规则和异常分类在采样前确定。

每个个案至少补齐：匿名业务编号、事项与清单版本、群众实际到场次数、材料补正次数、涉及岗位数、各节点起止时间、最后结果及证据位置。补齐前不填推测数值。两轮访谈数量已满足最低轮数，但“具体到一单且量化”的要求仍需这些原始记录支撑。
'''
    both(event, '典型事件采访整理', event_text)
    brief = '''# 政务易办项目简报

政务易办是一套面向材料准备环节的指南问答、辅助预检和人工工单协同教学系统。项目目标是让办事人在正式提交前发现清单问题，并让无法自动解释的问题有明确的人工接手人。本简报根据六人访谈汇总；访谈来自合成案例，数字用于提出验证目标。

## 项目背景

线上材料清单与窗口要求不一致、事项编码不同步及材料有效期字段不足，导致重复解释和补正。窗口、业务科室和热线分别将“材料完整”“专业审核有效”“答复已送达”视为完成，容易形成交接责任和统计口径差异。系统应首先保存统一的事项及版本依据，并区分工单关闭、问题解决与正式办件办结。

## 六个访谈角色

| 人员 | 职责 | 主要诉求 |
| --- | --- | --- |
| 顾明岚 | 服务中心主任 | 确认目标与试点范围，控制投入和风险 |
| 林亦舟 | 窗口工作人员 | 在受理前发现缺项，保留原记录及待确认字段 |
| 唐映秋 | 业务审核员 | 明确节点责任、退回原因与接手确认 |
| 秦芮 | 政务平台工程师 | 对齐主编号与字段口径，保持只读接口边界 |
| 傅景 | 数据与流程监督员 | 控制权限、人工兜底、审核发布与留痕 |
| 叶书 | 热线服务负责人 | 统一解释口径，按业务编号追溯投诉和反馈 |

## 首期范围

教学示例选择居住证申领及个体工商户设立登记，服务办事人、窗口、审核员、业务负责人、监督员、热线、工程师和主任等角色。匿名用户可以公开咨询；上传、个人记录和工单需要登录。工作人员按岗位、事项和记录授权查看资料。

系统提供指南引用、材料辅助预检、本人确认与人工工单。AI不作行政审批、不判断证件真伪、不保证法律效力，不将外地指南当作云州市正式政策。真实政务系统接口、生产数据、正式政策和获准模型服务属于真实试点前置条件。

## 目标与度量

| 指标 | 案例基线及目标 | 核验方法 |
| --- | --- | --- |
| 补正率 | 36%至18% | 同事项同口径比较；实际业务尚未验收 |
| 清单解释平均耗时 | 12分钟至5分钟 | 逐单计时，分开等待与处理时间 |
| 清单解释一致率 | 目标不低于99% | 业务人员签核标准答案，再计算真实模型结果 |
| 工作量 | 600件咨询每天 8人时核对 | 六人访谈合成案例口径；不等于单件解释耗时 |
| 预算 | 案例首期18万元 | 约束教学估算，非已签订单 |

## 交付与验证路径

先以虚构资料回放，验证规则、引用、身份、本人确认和工单交接；然后在取得授权后开展小范围并行试点。提交包提供采访归档、前后流程、需求及设计、三档估算、运行手册、源码和五类测试材料。程序测试通过只证明相应实现，不证明真实客户指标已达到。
'''
    both(DOCS / '02_项目简报', '政务易办项目简报', brief)
    industry = DOCS / '04_行业分析'
    industry.mkdir(parents=True, exist_ok=True)
    for ext in ('md', 'docx'):
        shutil.copy2(ROOT / 'docs' / ('行业分析.' + ext), industry / ('行业分析.' + ext))
    flows = DOCS / '05_业务流程图'
    flow(flows / '改造前业务流程图.png')
    flow(flows / '改造后业务流程图.png', True)
    both(flows, '改造前后流程说明', '''# 政务易办改造前后业务流程

两图围绕清单差异引发反复补正的典型问题展开。现状依据六人访谈，改造方案依据已有需求设计；本次典型事件采访没有完整单案计时，不能把设计流程写成已观察到的真实改造成果。

## 改造前流程

![改造前业务流程图](改造前业务流程图.png)

## 改造后流程

![改造后业务流程图](改造后业务流程图.png)

## 对应的改造点

清单差异通过事项及规则快照提前暴露；字段差异通过引用材料和本人核对处理；跨岗丢失说明通过冻结共享范围、关联预检和确认接手处理。有效期疑问、依据不足、版本冲突或服务不可用时停止自动建议，经草稿预览后转人工；补充资料后重新确认及预检。

图中的正常路径不代表每个咨询必须转工单。可自助解决时结束咨询；只有未解决问题进入人工链。工作人员答复后，用户可以继续补充、确认解决，或按既有14天等待规则处理；默认关闭需单列，不当作实际问题解决。正式申请和行政审批仍在原政务系统完成。
''')
    req = '''# 政务易办需求定义与验收规格

本文件把六人访谈、典型事件采访和用户指定的设计框架转为可追溯的课程需求。目标是在材料提交前发现问题、减少重复解释，并保留人工决定权。需求编号沿用原仓库需求追溯总表，具体实现状态以配套原表与本次测试结果为准。

## 需求来源及证据边界

六人访谈见01目录，典型事件两轮采访见03目录；设计原件是需求分析与设计框架.docx，已原样归档。六人访谈是合成案例，典型采访仅采集2条事实；它们支持提出假设，不证明真实客户已验收。补正率36%至18%、12分钟至5分钟及99%一致率是案例目标；本次程序测试没有验证这些业务改善。

## 用户和业务对象

办事人持有自己的会话、材料、预检与工单。业务工作人员按岗位及事项处理，记录正文还需显式授权。事项具有情形和生效规则版本；材料具有所有者、授权及不可变版本；预检保留当时规则与证据；工单承接待解决问题；办件是正式行政申请，未与本系统对接。

## 优先级与功能规格

P0为正确性、权限和可运行主链的前提；P1为提升效率和维护能力；P2为依赖外部条件的扩展。优先级是本次交付建议，不冒充客户批准。

| 编号及级别 | 触发及输入 | 预期行为与约束 | 验收证据 |
| --- | --- | --- | --- |
| REQ01 P0 | 匿名用户公开咨询 | 会话隔离 手机可读 刷新按当前规则重置 | test_chat与欢迎页浏览器 |
| REQ02 P0 | 查询已发布指南 | 保留原文与版本依据 无依据拒答 | test_guide_qa及发布测试 |
| REQ03 P0 | 选择事项与情形 | 本人确认绑定快照 规则变更后重确认 | test_agent_routes |
| REQ04 P0 | 登录并逐份授权材料 | 所有者及共享范围均校验 不公开存储路径 | test_materials |
| REQ05 P0 | 图片或PDF及字段 | 可信示例识别或主动转录 人工确认字段 | 材料测试与助手浏览器 |
| REQ06 P0 | 已确认规则及材料集 | 缺项和冲突有依据 不判断真伪或批准 | 两事项预检测试 |
| REQ07 P0 | 草稿及共享范围 | 预览 本人确认 幂等创建 修改使旧确认失效 | confirmed_tickets与agent_routes |
| REQ08 P0 | 四类异常 | 暂停自动建议 可独立准备人工草稿 | 异常草稿与规则变更测试 |
| REQ09 P0 | 工作人员访问业务 | 岗位 事项 记录授权同时成立 | test_staff与security_boundaries |
| REQ10 P1 | 接手 转派 答复 | 接收方确认转派才完成交接 保留审计 | test_tickets与两案例浏览器 |
| REQ11 P1 | 规则草稿和发布 | 业务负责人维护 监督员审核版本 | test_knowledge与content浏览器 |
| REQ12 P1 | 模型或工具请求 | 服务端身份 工具白名单 调用限额和超时 | test_agent_loop与model_budget |
| REQ20 P2 | 真实政务系统接入 | 授权后对齐接口 编码和更新口径 | 当前待客户联调 |
| REQ21 P2 | 真实模型效果评估 | 签核样本后计算解释一致率 | 当前未验收99% |

本表中的REQ01等是展示缩写；与原需求追溯总表的REQ-01等同。为避免误映射，完整逐条原始需求、代码及测试对应见附带需求追溯总表.md，本节为按能力汇总，REQ05至REQ12的分组不替代原表的逐条映射。

## 人工确认与权限矩阵

| 主体 | 可以执行 | 限制 |
| --- | --- | --- |
| 匿名访客 | 公开指南咨询 选择事项 | 不上传材料 不读个人工单 |
| 登录办事人 | 本人材料 授权确认 工单提交与解决确认 | 不读取其他办事人资料 |
| 窗口人员 | 接手授权事项 工单回复与发起转派 | 不自行发布规则或替代专业审批 |
| 业务审核员 | 专业异常复核及任务分派 | 不替代监督员规则签核 |
| 业务负责人 | 维护及提交规则草稿 | 不自行跳过独立审核 |
| 监督员 | 审核发布及明确授权审计 | 不因角色名称获取所有材料 |
| 平台工程师 | 技术状态 运维诊断 | 不默认读取业务正文 |
| 热线及主任 | 授权摘要与明确升级的工单 | 不因通知已送达宣称办件办结 |

至少五处明确人工点：事项与规则确认、材料授权及字段核对、草稿和共享范围确认、转派接收确认、用户解决确认；另有规则审核发布。模型不得代签，技术岗位不得跨越业务授权。

## 非功能规格及失败控制

确认信封绑定用户、草稿版本、内容哈希、共享范围及有效期；并发重试不重复建单。模型每轮工具调用默认6次、配置最大12次，支持总时限和单工具超时。限额实际值以源码设置及环境模板为准，不能由浏览器身份字段覆盖。

本机SQLite用于教学单机部署，容量、真实模型延迟和跨主机运行未获本次验收。授权撤回后旧材料不可下载。任意不可信材料隔离未完成时默认关闭自动解析，使用主动转录；真实证件不进入课程仓库。

## 验收清单

1. 两个示例事项均能区分完整和缺项，并保留版本及证据。
2. 没有确认信封、信封过期、内容变化或跨用户使用时拒绝建单。
3. 匿名、跨用户及跨岗位越权被拒绝，所有者合法操作不受影响。
4. 伪造引用及未知工具不会制造执行成功或批准结论。
5. OCR不可用、模型超时、规则变化和无依据时有明确失败状态及人工路径。
6. 两个完整示例工单可完成窗口、审核员、主任及用户确认链。
7. 五类测试方案、实际日志、可视化过程与失败记录可逐项核对。
8. 真实上线前另验HTTPS、域名、政策、数据授权、模型服务、恢复和效果样本。

## 变更与追溯

需求变更先记录来源、编号、影响范围和验收方式，再调整实现及测试；规则发布生成新版本，历史引用不覆盖。原需求追溯总表随包附带，准确列出本地实现、部分实现和真实接入前待完成项。典型事件的待补个案字段不得在需求文档中自动变成已确认事实。
'''
    # Own capability IDs avoid falsely assigning legacy REQ-xx semantics.
    req = req.replace('REQ01', 'CAP01').replace('REQ02', 'CAP02').replace('REQ03', 'CAP03').replace('REQ04', 'CAP04').replace('REQ05', 'CAP05').replace('REQ06', 'CAP06').replace('REQ07', 'CAP07').replace('REQ08', 'CAP08').replace('REQ09', 'CAP09').replace('REQ10', 'CAP10').replace('REQ11', 'CAP11').replace('REQ12', 'CAP12').replace('REQ20', 'CAP20').replace('REQ21', 'CAP21')
    req = re.sub(r'本表中的CAP01等是展示缩写.*?逐条映射。', '本表CAP编号是课程能力分组，不是原需求编号；逐条原始需求、代码及测试对应见附带需求追溯总表.md，其中沿用REQ-01等编号。两套编号分别用于能力概览与原需求定位，不互相替代。', req)
    both(DOCS / '06_需求定义', '需求定义与验收规格', req)
    trace = (ROOT / 'docs/需求追溯总表.md').read_text(encoding='utf-8')
    def relocate(match):
        label, target = match.groups()
        if target.startswith(('http:', 'https:', '#')):
            return match[0]
        target = target[3:] if target.startswith('../') else 'docs/' + target
        return '[' + label + '](../../src/MVP项目/' + target + ')'
    trace = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', relocate, trace)
    put(DOCS / '06_需求定义/需求追溯总表.md', trace)
    compact = '# 政务易办需求追溯矩阵\n\n本矩阵沿用原仓库REQ编号，列出来源、能力和验证入口。完整代码链接及实现限制见同目录需求追溯总表.md；本次执行结果另见test/本次测试结果汇总，不以历史测试总数替代。\n\n| 编号 | 来源与需求 | 实现与验证 | 状态边界 |\n| --- | --- | --- | --- |\n'
    trace_rows = [
        ('01','产品确认 公开咨询与隔离','chat及test_chat','本地示例'),
        ('02','f15 f24 f32 f52 有依据问答','guide_qa及test_guide_qa','真实模型效果待验收'),
        ('03','产品确认 事项与版本本人确认','agent及test_agent_routes','本地示例'),
        ('04','f40 f41 f45 材料授权','materials及test_materials','真实数据授权待落实'),
        ('05','设计预检深度 识别及转录','material_extract及test_materials','任意文件隔离仍有边界'),
        ('06','f02 f15 f21 两事项辅助预检','materials及test_teaching_workflow','虚构规则'),
        ('07','f15 f44 四类异常转人工','agent及test_agent_routes','本地异常路径'),
        ('08','产品确认 预览和幂等建单','confirmed_tickets及其测试','本地示例'),
        ('09','f22 f40 f41 岗位事项记录授权','staff及test_staff','本地示例'),
        ('10','f24 负责人维护监督员审核','knowledge及test_knowledge','本次content浏览器失败见日志'),
        ('11','f20 f23 f25 接收确认与转派','tickets及test_tickets','两示例浏览器通过'),
        ('12','产品确认 接手答复与等待时限','tickets及test_tickets','关闭不等于问题解决'),
        ('13','产品确认 站内消息与详情','notifications及test_tickets_routes','本地示例'),
        ('14','f15 f45 引用与人工复核留痕','audits prompts及对应测试','不覆盖历史'),
        ('15','工程与数据边界 共享配额','model_budget及对应测试','非真实模型费用'),
        ('16','运维规范 预检备份恢复','operations及test_operations','跨版本切换未验收'),
        ('17','f43 f45 分类留存','retention_policy及对应测试','真实期限待确认'),
        ('18','工程规范 技术日志权限','system_observability测试','技术日志不含业务正文'),
        ('19','AGENTS与贡献规范 统一检查','scripts/verify.py及test_verify','远端Actions未核验'),
        ('20','f30至f35 真实系统只读接入','现有编号和快照机制','真实接口待落实'),
        ('21','f02 解释一致率99%','评测合同与引用校验','真实签核样本待验收'),
        ('22','f00 f50 f51 f53 业务改善','时间记录与独立完成口径','36%至18%未实测'),
        ('23','客户环境授权与商业交付','非机密配置及估算报价','真实客户条件待核实')]
    for number, source_item, implementation, status in trace_rows:
        compact += f'| REQ-{number} | {source_item} | {implementation} | {status} |\n'
    both(DOCS / '06_需求定义', '需求追溯矩阵', compact)
    framework = DOCS / '07_详细项目设计'
    framework.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / 'docs/需求分析与设计框架.docx', framework / '需求分析与设计框架_用户原件.docx')
    design = '''# 政务易办详细项目设计方案

本方案说明现有MVP的工程架构、Agent流程、知识库、权限、数据和异常控制，供课程评阅、演示和后续接入使用。设计承接用户提供的需求分析与设计框架，原件同时归档。系统使用虚构事项和材料验证链路，真实政策、外部接口、生产环境与模型效果仍须另行验收。

## 工程架构与部署单元

单机进程以Python FastAPI提供HTTP接口及服务端渲染页面，SQLite通过SQLAlchemy保存业务对象。前端使用HTML模板、CSS和JavaScript，流式问答采用POST SSE。知识检索使用BM25和FAISS，LLM及嵌入服务通过环境配置接入。源代码统一在src/zhengwu_precheck下。

请求由API层解析并取得服务端身份，业务服务执行授权及确定性校验，再访问仓储。模型只提出工具调用，不能持有数据库写入权或替代本人确认。运行数据库、原始材料、备份和密钥保存到仓库外私有目录。

| 模块 | 所在位置 | 职责 |
| --- | --- | --- |
| 应用装配 | main.py settings.py | 配置校验 生命周期和依赖注入 |
| 请求边界 | api/routes及身份依赖 | 解析CSRF 登录 岗位与事项授权 |
| 业务服务 | core/services | 指南 材料 Agent 工单及配额 |
| 数据持久化 | infrastructure | 会话 材料 草稿 工单 审计及备份 |
| 通用工具 | utils | 文档解析 脱敏 安全与流解析 |
| 页面 | templates static | 办事人页面及按岗位生成的工作台 |

## Agent状态与工具编排

案例从事项选择进入规则查看和本人确认；材料提交后建立材料集合及版本快照，预检只作用于当前已确认集合。问题未解决时整理草稿、显示共享范围、本人签发提交确认，再由统一服务原子消费确认并建单。规则、材料或草稿变化将旧确认置为不可用，不能静默沿用。

| 工具 | 输入目的 | 服务端约束 |
| --- | --- | --- |
| search_guidelines | 按事项问题检索 | 仅使用当前适用已发布知识 |
| recommend_matter | 推荐事项情形 | 推荐不能代替本人确认 |
| check_materials | 当前材料集辅助核验 | 检查案例 规则及材料集合版本 |
| prepare_ticket_draft | 整理问题与共享范围 | 生成草稿 不直接形成工单 |
| create_manual_ticket | 提交已确认草稿 | 必须带真实确认且原子幂等 |
| get_ticket_status | 查询工单状态 | 服务端身份及记录归属授权 |

工具白名单固定，默认每轮最多6次调用、可配置范围1至12次，另设总时限与单工具时限。未知工具、伪造成功文本或工具循环超限必须停止并给出受控结果，不将模型生成的字段当作身份与权限。

## 知识库与引用链

业务负责人上传Markdown或受控PDF，抽取后生成带来源和版本的分块，构建BM25及FAISS索引；待监督员审核发布后才可供公开问答使用。新草稿不覆盖已发布正文。检索结果进入模型后，输出引文仍由服务端验证是否来自真实块，伪造引用、未知编号及断章引用不能通过。

知识版本包含适用事项、来源、生效时间和状态。规则冲突时保留双方版本供人工判断，不自动选择宽松规则。历史问答保存当时引用，停用影响新请求，不抹去历史证据。

## 材料处理设计

材料先校验登录、归属、逐份同意、扩展名及文件特征，再以私有引用保存。可信虚构示例可使用本地OCR；任意不可信文件隔离不可用时默认停止解析，保留主动转录。OCR失败不表示材料缺失，低置信和主动转录字段均由本人核对。

预检分别判断清单齐全性、信息可读性及跨材料一致性；有效期疑问仅触发人工核验，不判断真伪、法定资格或批准。计算完成后再次核对规则及材料版本，防止计算期间修改造成陈旧结果。

## 数据对象与一致性

| 对象 | 关键关联 | 一致性规则 |
| --- | --- | --- |
| 用户与会话 | 用户 标识 事项 历史消息 | 匿名会话不随登录自动认领 |
| 知识及规则 | 来源 生效版本 发布签核 | 不可变发布快照与历史引用 |
| 材料及字段 | 所有者 授权 版本 来源 | 撤回或修改使旧共享失效 |
| 案例与预检 | 事项 情形 规则 材料集 | 版本改变后重确认 重计算 |
| 草稿与确认 | 用户 版本 哈希 共享 有效期 | 一次性消费 并发和重试幂等 |
| 工单及转派 | 问题 消息 接手方 状态 | 对方确认接收后责任转移 |
| 审计及配额 | 操作者 事件 时间及范围 | 不记录口令 额度持久保存 |

正式办件尚未接入，不制造办件编号。工单共享冻结材料版本，工作人员须同时满足岗位、事项及处理关系。提交响应丢失时先查询原操作结果，不能更换标识盲目新建。

## 权限与人工审核设计

至少区分公开访客、办事人、窗口、审核员、负责人、监督员、热线、工程师和主任。工程师只管理技术状态，规则负责人提交草稿后由监督员审核发布；无明确记录授权不能读取全部业务正文。未知事项需要独立unassigned授权，通配事项不自动包含。

人工点包括事项规则确认、材料授权字段确认、提交预览确认、工作人员发送答复、转派接收、监督员发布以及办事人确认解决。转派与资产变更均不能仅凭模型意图执行。

## 异常路径与可观测性

四类核心异常为过期证件历史有效、缺少依据、版本冲突和关键服务不可用。记录触发原因、接手人及确认时间，原始记录不覆盖。解析或模型故障提供受控错误，区别“系统暂不可用”与“指南无依据”；授权运维查看结构化健康和错误日志，业务正文不进入技术日志。

模型调用以全局、操作者、服务及操作者和服务组合原子预留配额；未知usage保守计入，过期并发租约不退回未知token。配额不包含虚构货币账单。备份需静默窗口，核验数据库和附件校验和后隔离恢复；不能覆盖已有运行目录。

## 工单时限与业务口径

窗口接手时限和首次答复分别按已有配置及实现执行，首次答复以提交后连续48小时计。工作人员答复后进入14天等待，7天和14天提醒；默认关闭与本人确认解决分别统计。是否升级主任由工作人员明确选择，不能由超时自动推断行政处理结论。

## 部署与交付边界

提交包提供源码快照、依赖声明、非机密配置模板、测试和运行说明。开发可本机启动；正式发布需独立域名、可信TLS、随机签名密钥、私有目录和客户授权模型服务。公网地址尚未配置时只能报告本地MVP，不能标记上线验收完成。

## 验证映射与后续接入

本次五类代表性测试与浏览器记录位于test目录；原需求追溯表和补充实现文档保留在源码快照docs目录。部署前还需真实政策签核、真实接口字段映射、模型效果样本、真实负载与恢复目标。课程示例的通过结果不替代真实业务验证。
'''
    both(framework, '详细项目设计方案', design)
    pricing = '''# 政务易办三档报价与投入产出测算

本文件提供课程设计用预算估算，金额为人民币万元，按工作量假设计算，不是供应商正式承诺或市场实时价格。原报价单作为历史来源保留；本次新增低、中、高档范围及可复核的成本收益公式。案例18万元只作为中档首年预算约束。

## 报价范围与计价假设

低档用于单机教学与少量示例验证；中档用于两事项的授权并行试点准备；高档预算用于扩大事项和跨系统联调，外部系统许可及对接可行性仍须客户确认。实施成本采用内部综合人日成本0.10万元的教学假设，运维及资源费用为预留估算。

| 成本项目 | 低档 | 中档 | 高档 |
| --- | --- | --- | --- |
| 需求与业务梳理 | 1.0 | 1.5 | 2.5 |
| 知识与规则配置 | 1.2 | 3.5 | 5.0 |
| 问答及Agent集成 | 1.5 | 3.5 | 6.0 |
| 工单 权限与审计 | 1.0 | 2.5 | 4.0 |
| 测试 培训与交付 | 1.3 | 1.8 | 2.5 |
| 已授权接口联调预留 | 0.0 | 2.0 | 4.0 |
| 一次实施合计 | 6.0 | 14.8 | 24.0 |
| 年度应用运维 | 1.0 | 2.0 | 3.5 |
| 年度模型及计算资源预留 | 0.5 | 0.8 | 1.5 |
| 首年总投入 | 7.5 | 17.6 | 29.0 |
| 三年总投入 | 10.5 | 23.2 | 39.0 |

实施分别折合60、148、240人日；综合成本用于课程估算，不能据此推断项目已消耗这些工时。三年投入等于一次实施加3倍年度运维与资源费，不重复累计实施成本。若不需要接口联调，按实际确认范围调整，不能收取不存在的对接工作。

## 三档交付范围

低档含两事项虚构资料、问答、基础预检、本人确认、核心工单及本地演示；不包含真实接口或生产SLA。中档增加完整岗位流程、规则审核发布、审计、备份恢复、培训及约定接口联调预留。高档在中档基础上预留更多事项、专门联调和扩容验证；不意味着现有代码已支持多机生产部署。

年度费用包括应用维护及估计资源消耗，不含客户内部人工复核、专线、第三方许可、真实模型超量及外部审批费用。税费及采购合同条件在正式报价阶段确认，本表不作为含税正式报价。

## 投入产出测算口径

以六人访谈合成口径600次咨询每天、8人时每天清单核对为起点，教学假设每年250个工作日、综合人工成本60元每小时。8人时表示清单核对总量，与12分钟单件解释耗时属于不同统计范围；不以600乘12分钟重复计算。

低中高档分别假设节省该核对工时的20%、35%、50%，新增人工复核分别0.2、0.3、0.5小时每天。年度净节省工时分别为350、625、875小时；年度机会价值分别为2.10、3.75、5.25万元。机会价值不是现金收入，不计算未验证的群众出行收益或行政审批提速收益。

年度净节省工时 = （8 × 节省比例 − 新增复核小时每天）× 250。
年度机会价值万元 = 年度净节省工时 × 60 ÷ 10000。
首年机会收益ROI = （年度机会价值 − 首年总投入）÷ 首年总投入。

| 测算项目 | 低档 | 中档 | 高档 |
| --- | --- | --- | --- |
| 年度净工时节省 | 350小时 | 625小时 | 875小时 |
| 年度机会价值 | 2.10万元 | 3.75万元 | 5.25万元 |
| 首年机会收益ROI | -72.0% | -78.7% | -81.9% |
| 三年机会净值 | -4.20万元 | -11.95万元 | -23.25万元 |

三年机会净值等于三年机会价值减三年投入，未计折现。上述结果说明仅靠已给出的核对工时基线不能论证商业回本；采购价值还需真实补正减少、服务可用性和投诉成本数据支撑，不人为把结果改为盈利。

## 敏感性与建议

中档按节省比例20%、35%、50%测算，新增复核保持0.3小时每天，年度净工时分别325、625、925小时，对应机会价值1.95、3.75、5.55万元。需记录实际模型成本、规则维护工时及节省比例；未形成真实基线前只建议小范围回放和并行验证，不建议直接扩大采购。
'''
    both(DOCS / '08_报价与投入产出', '三档报价与投入产出测算', pricing)
    shutil.copy2(ROOT / 'docs/报价单.doc', DOCS / '08_报价与投入产出/历史报价单_原件.doc')
    manual = '''# 政务易办交付运行手册

本手册面向课程评阅者、演示人员和维护人员，说明如何在Windows本机启动MVP、完成两事项人工工单演示、查看测试证据并准备后续发布。当前使用虚构资料，公网部署状态见上线地址说明，不将本地可运行等同于正式生产上线。

## 交付内容与环境

docs目录保存访谈、简报、行业分析、流程、需求、设计、报价和本手册；src目录提供问答助手归档与MVP项目源码快照；test目录提供五类方案、过程报告和证据。源码快照不带虚拟环境、私密配置、数据库或Git历史。

需要Python 3.12和项目requirements所列依赖，运行目录需可写。已有项目环境优先使用.venv/Scripts/python.exe。迁移到新机器时先建立环境并按依赖声明安装，外部模型与嵌入服务需要管理员在仓库外配置，不能把密钥写到提交包。

## 本机启动

在原项目根目录双击启动网页.bat；启动逻辑位于scripts/start_web.py。也可以在根目录执行：
```
$env:PYTHONPATH='src'
.venv/Scripts/python.exe -m uvicorn zhengwu_precheck.main:create_app --factory --host 127.0.0.1 --port 8000
```
正常本机入口为http://127.0.0.1:8000；工作人员入口为/staff/login，材料及人工协作为/assistant，个人工单为/my/tickets。端口占用时先核对是否已有本项目服务，不终止其他程序。只有健康检查返回且页面可访问后才判定启动成功。

源码快照首次使用须新建虚拟环境并安装requirements.txt，再按README设置。双击入口不包含云服务器或域名购买功能。公开咨询仍需已批准模型及嵌入配置才能产生真实模型答案；服务未配置时应显示不可用，不能期待自动离线模拟回答。

## 示例账号与数据准备

仅在隔离教学运行目录中按README及scripts/create_demo_accounts.py的命令行说明初始化虚构账号。正常启动不自动创建管理员；示例口令不可用于公网。示例材料位于data/materials，真实证件、电话、密钥及生产记录不得用于作业演示。

两个预设案例是居住证材料补正、个体工商户经营地址核对。公网打开http://47.117.178.247/examples/workflow，无需登录，选案例后点击“开始自动演示”。在/my/tickets登录后可手动办理；本地同样通过服务地址访问/examples/workflow。不要直接双击HTML模板文件。自动演示数据明确标示为示例，与普通工单历史分开。

## 办事人操作

1. 描述问题或选择事项，查看规则版本并本人确认。
2. 需要材料预检时登录，逐份授权上传虚构材料，转录或识别后核对字段。
3. 执行辅助预检，查看具体问题、版本和引用；识别失败不等于材料缺失。
4. 无法自助解决时选择共享材料、整理草稿、预览内容和共享范围，再明确确认提交。
5. 在我的工单查看答复、补充信息；实际问题解决后本人确认。撤回授权会使旧材料不可下载。

## 工作人员操作

窗口接收队列并接手，发送补正说明；专业异常交业务审核员。转派发起后由接收方明确确认，原岗位不能只因发送消息就视为交接完成。主任只处理明确升级的工单。业务负责人维护知识草稿，监督员负责审核发布；工程师负责技术诊断，不默认获得业务正文。

新知识先处理并构建索引，再提交审核及发布。尚未发布或已停用内容不能用于新公开回答，历史引用仍保留。两事项示例指南仅存在于仓库并不代表已发布，需要按已有知识流程启用。

## 常见问题处理

| 现象 | 检查与操作 | 边界 |
| --- | --- | --- |
| 网页打不开 | 检查启动日志 端口及/health | 不把启动命令已发出当成功 |
| 模型未配置或超时 | 核对外部环境配置及获批服务 | 不输出假答案或批准结果 |
| 指南没有依据 | 核对发布状态 事项及版本 | 无依据保持拒答 转人工 |
| 材料解析不可用 | 使用可信虚构资料或主动转录 | 不关闭隔离后上传任意真实文件 |
| 权限不足 | 核对岗位 事项及记录授权 | 不借用账号或临时扩大所有权限 |
| 重复提交或响应不明 | 查询原工单及操作标识 | 不换标识盲目重建 |
| 规则材料已变化 | 重新确认并预检 | 不复用陈旧结论 |

## 测试复现

在原仓库根目录运行目录检查和Python回归：
```
.venv/Scripts/python.exe -X utf8 scripts/check_structure.py
.venv/Scripts/python.exe -X utf8 -m unittest discover -s tests -p 'test_*.py' -v
.venv/Scripts/python.exe -X utf8 scripts/capture_assignment_evidence.py
.venv/Scripts/python.exe -X utf8 tests/run_browser_check.py --suite cases
```
浏览器测试需要本机Chrome及Playwright，使用独立临时数据库，模型HTTP为替身。本次实际结果和失败说明见test目录；不能用以前的测试数字覆盖本次失败或跳过项。

## 正式发布与运维

按config/release.env.example在仓库外设置HTTPS地址、TLS、随机签名密钥、私有目录及允许主机。先执行scripts/operations.py preflight，再按运维部署与恢复.md执行serve及反向代理配置。preflight通过不证明公网端口已开放，域名、TLS链和跨网络访问需要部署后实测。

备份前暂停写入并核对在途任务，备份数据库和关联文件；检查校验和，在隔离目录恢复验证，禁止覆盖现有运行目录。保存期限和删除动作遵守实际授权，不把评价记录的90天规则直接套到全部材料。故障期间停止自动建议并保留原人工渠道。

## 交接与验收

交接时核对版本、依赖、脱敏样例、操作文档、需求追溯和测试证据。真实试点还需确认政策、客户数据授权、模型服务、接口、效果样本、负载及恢复目标。公网地址尚未落实时如实标为待上线，不提交localhost冒充可远程访问的网站。
'''
    both(DOCS / '09_交付运行手册', '交付运行手册', manual)
    archive = '''# 政务易办问答助手归档

本归档提供可复现的问答助手工程资料，不冒充某个外部平台的助手导出文件。系统使用FastAPI、混合检索、引用校验、受控工具和人工确认，源码快照及配置模板位于MVP项目。

## 助手目标与数据范围

目标是解释当前已发布的事项指南，提示材料准备问题，并在无法处理时整理人工咨询。知识来源为data中的明确虚构示例和工作人员审核发布的文档；真实地方规则需授权和签核。不得判断证件真伪、法定资格或行政审批结论。

## 提示词与模型接入

业务提示词由事项和版本管理，相关实现见src/zhengwu_precheck/infrastructure/prompt_repository.py、api/routes/prompts.py及core/guide_rag.py。配置模板见config/release.env.example及settings.py；密钥通过外部环境传入。版本恢复产生新版本而不覆盖历史。检索内容及用户输入是不可信数据，不能授予身份或工具权限。

## 工具和人工确认

六工具是search_guidelines、recommend_matter、check_materials、prepare_ticket_draft、create_manual_ticket、get_ticket_status。每轮调用限制和白名单由服务端执行。事项、材料与字段、草稿共享范围、转派接收及问题解决均需相应人确认；模型不能调用代签工具。

## 权限及失败策略

至少包含办事人和工作人员两类身份，并细分窗口、审核员、负责人、监督员、热线、工程师及主任。匿名仅公开咨询。无依据时拒答，引用不实被校验拒绝，模型超时和材料隔离不可用分别显示故障状态并可转人工。不会将模型自由文本当作真实建单成功。

## 归档与复现入口

MVP项目中保留src、tests、data、config、scripts、docs、examples以及README和启动入口。问答与Agent验证见test_guide_qa、test_agent_loop、test_agent_routes及五类过程报告。实际外部模型效果、真实政务接口与公网地址仍按交付清单单列。
'''
    both(OUT / 'src', '问答助手归档', archive)
    build_test_materials()
    create_source_snapshot()
    create_index(rounds, len(facts), len(transcript))
    if (ROOT / 'scripts/supplement_event_case.py').is_file():
        from supplement_event_case import apply_to_submission
        apply_to_submission()


def build_test_materials():
    test_root = OUT / 'test'
    evidence = test_root / '证据'
    results = json.loads((evidence / '本次分项测试结果.json').read_text(encoding='utf-8'))
    test_plan = '''# 政务易办量化测试方案

本方案覆盖正常、边界、对抗、权限与故障五类，使用受控虚构资料和隔离数据库。选取现有测试中的15项作为提交材料的代表性验证，每类3项，保留可执行用例编号、输入、预期、实际结果与耗时。15项不是全系统覆盖率，也不是99%模型解释一致率。

## 环境与前置条件

Windows、Python 3.12、FastAPI和SQLite；浏览器测试使用本机Chrome及Playwright。指南问答的模型HTTP采用显式测试替身；检索、引用校验、授权、数据库和工单事务仍执行真实实现。测试资料全部为虚构，不使用真实证件。服务器使用独立随机端口和临时运行目录，退出后停止本次启动的测试服务。

## 判定与指标

单项通过必须全部断言满足；异常抛出、响应不符或超时为失败；因主机权限无法验证则为跳过，跳过不计通过。通过率为通过数除以执行项数，执行项数含失败和错误、不含跳过，同时单列跳过数。关键权限或确认绕过失败即停止该能力验收，不以总体通过率掩盖。

功能断言要求正确性100%；跨人读取、未经确认建单、伪造批准或伪造引用成功的容许数为0。网页手机宽390像素无横向溢出；浏览器脚本错误为0。耗时仅记录本机观察，不把它当生产SLA。真实模型一致率需另建签核样本，不用模型替身结果计算。

## 用例设计与执行顺序

先正常链，再边界和权限，随后对抗及故障。每项单独使用现有测试夹具，避免把前项数据污染带入后项。所有实际结果以配套JSON和原始日志为准。
'''
    for category, rows in results.items():
        test_plan += '\n### ' + category + '场景\n\n'
        for n, r in enumerate(rows, 1):
            test_plan += f"{n}. {r['input']}。预期：{r['expected']}。可执行编号：{r['id']}。\n\n"
    test_plan += '''
## 五类测试后的复核

执行欢迎页、两案例工单、助手、知识发布和工作台浏览器回归，保留本次真实截图。旧问答浏览器脚本可能与当前页面行为不一致，失败必须保留并核查，不能自动划为通过。测试中的替身只控制模型响应，不代表真实模型可用、生产吞吐或客户验收。

## 复现命令与证据

代表性测试使用scripts/capture_assignment_evidence.py，结果写入作业提交/test/证据。全部单元测试和各浏览器套件日志同时归档。可视化执行记录是从实际测试JSON和日志生成的证据页，不是业务系统UI；真实业务页面截图单独放入浏览器截图目录，并在报告中说明来源。
'''
    both(test_root, '量化测试方案', test_plan)
    browser_dir = evidence / '浏览器截图'
    browser_dir.mkdir(exist_ok=True)
    log_dir = evidence / '完整回归日志'
    log_dir.mkdir(exist_ok=True)
    qa = ROOT / '.venv/assignment-qa'
    for p in qa.glob('*.txt'):
        shutil.copy2(p, log_dir / p.name)
    # Only images generated in this task are included. Do not reuse stale screenshots.
    for p in (ROOT / '.venv/browser-results').glob('*.png'):
        if p.stat().st_mtime >= (qa / 'browser-results.txt').stat().st_mtime - 30:
            shutil.copy2(p, browser_dir / p.name)
    for category, rows in results.items():
        passed = sum(r['result'] == '通过' for r in rows)
        content = f'''# 政务易办{category}场景测试过程

执行日期为{DATE}。本报告记录{len(rows)}项代表性测试，通过{passed}项，失败或跳过{len(rows)-passed}项。测试使用隔离的虚构资料；模型相关行为采用替身响应。本报告不宣称覆盖该类所有情况。

## 测试方法

使用Python unittest运行原仓库中的实际用例，执行文件、类和方法编号如下。每项按夹具建立数据、设置输入或故障、调用真实业务服务或ASGI接口、断言结果，最后清理隔离数据。完整输出位于证据目录，图为本次真实输出的可视化，不是人工编造的业务页面。
'''
        for n, r in enumerate(rows, 1):
            content += f"\n## 用例{n}\n\n输入与操作：{r['input']}。\n\n执行顺序：准备独立夹具 → 注入上述输入或条件 → 调用对应实现 → 核对断言 → 清理测试数据。\n\n预期结果：{r['expected']}。\n\n实际结果：{r['result']}，运行耗时{r['seconds']}秒。\n\n用例编号：{r['id']}。源码见src/MVP项目/tests中的同名文件，完整断言及测试数据可直接核对。\n"
        content += f'\n## 实际执行记录图\n\n![{category}场景自动化执行记录可视化](证据/{category}-执行记录.png)\n'
        images = {'正常': ['residence-supplement-closed.png', 'business-address-closed.png'], '边界': ['assistant-submitted.png'], '权限': ['workspace-window.png', 'workspace-engineer.png'], '对抗': [], '故障': ['engineer-failures.png']}
        attached = 0
        for name in images.get(category, []):
            if (browser_dir / name).is_file():
                content += f'\n![本次浏览器相关流程截图 {name}](证据/浏览器截图/{name})\n'
                attached += 1
        content += '\n## 证据解释与限制\n\n可视化执行记录逐项对应原始测试日志；若附有业务页面图，仅展示本次浏览器回归中相关流程，不将它当作全部接口断言的独立证明。未附业务图的断言以真实日志、用例和结果JSON核对。故障模拟和模型替身用于可重复注入条件，不能用来证明真实供应商可用性。\n'
        both(test_root, category + '场景测试过程', content)
    summary = '# 政务易办本次测试结果汇总\n\n本次结果区分代表性测试、完整回归与浏览器套件；失败和跳过均保留，历史测试总数不替代本次证据。\n\n| 场景 | 执行 | 通过 | 失败或跳过 |\n| --- | --- | --- | --- |\n'
    for category, rows in results.items():
        passed = sum(r['result'] == '通过' for r in rows)
        summary += f'| {category} | {len(rows)} | {passed} | {len(rows)-passed} |\n'
    for p in sorted(qa.glob('*.txt')):
        if p.stem == 'selected-results':
            continue
        data = p.read_text(encoding='utf-8-sig', errors='replace')
        summary += '\n## ' + p.stem.replace('-', ' ') + '\n\n'
        if not data.strip():
            summary += '日志暂为空，不能判定通过。\n'
            continue
        chosen = [line for line in data.splitlines() if re.search(r'^Ran \d+|^OK|^FAILED|^FAIL:|^ERROR:|通过|^PASS |AssertionError:|^RuntimeError:', line)]
        summary += '\n'.join(chosen[-18:]) + '\n'
        if p.stem == 'unit-results' and not re.search(r'^Ran \d+', data, re.M):
            summary += '\n完整回归尚无结束汇总；部分执行日志保留，不据此宣称全套通过。中止原因见unit status。\n'
        if 'Traceback' in data and not any(x.startswith('FAIL') for x in chosen):
            summary += '\n本套件含异常堆栈，须查完整日志，不标为通过。\n'
    summary += '\n## 未完成条件\n\n本次旧问答浏览器脚本期待“正在检索办事指南”，当前页面在问题后引导工单事项选择，出现断言失败。完整回归的其他失败见上述本次结果。课程归档没有修改产品行为或删除失败断言；需后续核对当前产品需求与旧测试契约，再决定更新测试或修复功能。公网教学演示已在2026年11月8日独立核验，两个案例均免登录完成11步；真实模型效果与真实业务改善仍未验证。\n'
    both(test_root, '本次测试结果汇总', summary)
    deployment = '''# 政务易办MVP网站地址与部署状态

核验日期：2026年11月8日。公网教学演示已部署至用户阿里云Ubuntu服务器，以下入口已从外部网络实际验证。仅使用虚构案例，运行流程不依赖AI模型服务。

## 公网访问入口

流程演示：http://47.117.178.247/examples/workflow

使用方式：打开流程演示 → 选案例 → 点击“开始自动演示”。无需账号密码。

首页：http://47.117.178.247/

工作人员登录：http://47.117.178.247/staff/login

工作人员手动办理需要对应岗位账号。登录资料仅保存在服务器私有运行目录，不写入提交包。

## 本次外网验证

健康接口HTTP200；未登录可见案例选择和开始按钮；居住证材料补正、个体工商户经营地址核对两个案例均实际执行11步，并由办事人确认解决后结案；各有5条消息、2次转派。跨访客读取演示记录返回404，不签发业务登录身份。

Chrome直接访问公网HTTP，两案例自动播放至结案；390px宽度无横向溢出，未发现脚本错误。原始核验数据见证据/公网免登录验证.json，截图见证据/浏览器截图/公网免登录系列。

## 部署与维护

Ubuntu24.04、Python3.12、FastAPI、SQLite；Nginx对外提供80端口，应用只监听回环18080。服务由systemd管理，配置包含开机启动和异常重启；本次外网检查不单独证明服务器重启后的恢复。

服务器查看状态：systemctl status zhengwu-mvp --no-pager。查看日志：journalctl -u zhengwu-mvp -n 100 --no-pager。更新方法见src/MVP项目/docs/免登录流程演示更新说明.md。

## 范围与限制

当前为HTTP教学演示，浏览器可能显示“不安全”。HTTPS和正式域名未配置；真实政策、证件、模型效果、政务系统接口及生产负载未验收，不作为真实政务服务上线证明。

## 本地入口

Windows双击源码快照中的启动网页.bat；启动后流程演示为http://127.0.0.1:8000/examples/workflow。请通过服务地址访问，不要直接双击templates中的HTML模板。
'''
    both(test_root, 'MVP网站地址与部署状态', deployment)


def create_source_snapshot():
    target = OUT / 'src/MVP项目'
    target.mkdir(parents=True, exist_ok=True)
    ignores = shutil.ignore_patterns('__pycache__', '*.pyc', '.env', '.env.*', '*.key', '*.pem', '*.sqlite', '*.sqlite3', '*.db', '*.log', 'local.*')
    for name in ('src', 'tests', 'examples', 'scripts', 'config', 'data', 'docs', '.github'):
        shutil.copytree(ROOT / name, target / name, dirs_exist_ok=True, ignore=ignores)
    for name in ('README.md', 'LICENSE', '.gitignore', 'CHANGELOG.md', 'CONTRIBUTING.md', 'requirements.txt', 'AGENTS.md', '启动网页.bat'):
        shutil.copy2(ROOT / name, target / name)
    put(OUT / 'src/源码快照说明.md', '# 源码快照说明\n\nMVP项目是当前项目的课程快照，保留统一Python源码包及测试、脱敏样例、文档和启动脚本；不含.venv、Git历史、数据库、真实证件或本地凭证。源码仍以主仓库为准。首次运行按快照README创建环境；本次快照没有在干净电脑重新安装全部依赖，不承诺免配置启动。')


def create_index(rounds, facts, messages):
    content = f'''# 政务易办作业提交清单

整理日期为{DATE}，项目为工程技术与创新应用课程政务易办MVP。本目录按老师截图的docs、src、test三组整理。项目简报按用户补充说明提供一份，不要求另凑四个文件。所有原始资料保持原内容，整理稿、解析重建文件和估算均明确标注。

## 材料对应

| 要求 | 提交位置 | 状态 |
| --- | --- | --- |
| 六人访谈 MD JSON CSV | docs/01_六人访谈记录 | MD原件副本 JSON和CSV据MD解析重建 |
| 项目简报一份 | docs/02_项目简报 | 根据六人访谈生成 MD及DOCX |
| 典型事件采访 TXT及DOCX | docs/03_典型事件采访 | 三个用户原始导出 {rounds}轮问答 TXT及整理稿 |
| 行业分析 MD及DOCX | docs/04_行业分析 | 原有两种格式归档 |
| 改造前后流程图 | docs/05_业务流程图 | 两张PNG及说明文档 |
| 需求定义 DOCX | docs/06_需求定义 | 规格 优先级 边界 追溯及验收 |
| 详细项目设计 | docs/07_详细项目设计 | 详细方案及用户指定设计框架原件 |
| 三档报价 投入产出 DOCX | docs/08_报价与投入产出 | 新三档估算及历史报价原件 |
| 上线交付运行手册 DOCX | docs/09_交付运行手册 | 本地操作及发布运维流程 |
| 问答助手归档 | src/问答助手归档 | 说明 工具 确认与复现入口 |
| MVP源码与知识权限 | src/MVP项目 | 脱敏快照 至少三处人工点和两类权限 |
| 五类量化测试方案 | test/量化测试方案 | MD及DOCX |
| 五类可视化测试过程 | test/正常等场景测试过程 | 各一份DOCX及真实日志证据 |
| 上线网站地址文档 | test/MVP网站地址与部署状态 | 已部署 免登录 两案例公网11步结案验证通过 |

## 来源区分

六人访谈来自主仓库docs/discovery-report.md，解析得到{facts}条角色事实和{messages}条对话消息；原文件明确为合成案例。JSON和CSV是从MD重建，不是平台原始导出。用户提供的需求分析与设计框架.docx是设计原件，不当作访谈记录。

典型事件来自桌面discovery-report.md、discovery-report.json、discovery-requirements.csv三文件，按字节复制。该导出仅覆盖2条事实和两轮问答，不能替代六人访谈；受访者明确没有单案跑动次数、补正次数和节点计时。

## 提交前仍需处理

1. 典型事件已有两轮，但“具体到一单且量化”的原始信息不足。补充一个脱敏个案的趟次、补正次数、节点时长和结果；本包没有编造这些数字。
2. 公网教学演示已核验：http://47.117.178.247/examples/workflow。两案例均免登录完成11步并结案；HTTPS、真实模型及真实业务效果仍未验收。
3. 本次代表性测试与浏览器结果见test/本次测试结果汇总；失败与跳过如实保留。旧问答脚本与当前页面有行为差异，需要后续核对。
4. 行业分析保留原报告来源日期；报价为课程工作量假设，不是正式商业承诺；真实模型99%一致率及业务改善尚未验收。

## 阅读顺序

先看项目简报和前后流程，再看需求及详细设计，最后按运行手册演示并核对测试。需要追溯时从原始访谈、设计原件和需求追溯总表定位。提交包不含真实运行库、虚拟环境或模型密钥。
'''
    both(OUT, '00_作业提交清单', content)
    tree = ['作业提交/']
    for p in sorted(OUT.rglob('*')):
        if not exportable(p):
            continue
        if 'MVP项目' in p.parts and p.parent != OUT / 'src/MVP项目':
            continue
        depth = len(p.relative_to(OUT).parts)
        tree.append('  ' * depth + p.name + ('/' if p.is_dir() else ''))
    put(OUT / '目录树.txt', '\n'.join(tree))
    manifest = []
    for p in sorted(OUT.rglob('*')):
        if p.is_file() and exportable(p) and p.name not in {'文件校验清单.json', '政务易办_作业提交.zip'}:
            manifest.append({'path': p.relative_to(OUT).as_posix(), 'bytes': p.stat().st_size, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()})
    put(OUT / '文件校验清单.json', json.dumps(manifest, ensure_ascii=False, indent=2))
    print(f'提交包生成完成 {len(manifest)}个文件；DOCX {len(list(OUT.rglob("*.docx")))}份')


if __name__ == '__main__':
    main()
