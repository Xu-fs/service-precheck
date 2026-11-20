"""核对课程副本的来源、文件格式、证据和哈希，并制作不含递归副本的压缩包。"""
from pathlib import Path
import hashlib
import json
import re
import subprocess
import sys
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / '作业提交'
checks = []


def exportable(path):
    return not any(part in {'.venv', '.git', '__pycache__', '.pytest_cache'} for part in path.relative_to(OUT).parts)


def require(value, message):
    if not value:
        raise AssertionError(message)
    checks.append('通过 ' + message)


def main():
    pairs = [(ROOT / 'docs/discovery-report.md', OUT / 'docs/01_六人访谈记录/六人访谈记录.md'),
             (ROOT / 'docs/需求分析与设计框架.docx', OUT / 'docs/07_详细项目设计/需求分析与设计框架_用户原件.docx')]
    for name in ('discovery-report.md', 'discovery-report.json', 'discovery-requirements.csv'):
        pairs.append((Path('C:/Users/26381/Desktop') / name, OUT / 'docs/03_典型事件采访' / name))
    require(all(a.read_bytes() == b.read_bytes() for a, b in pairs), '五个指定来源副本与原文件按字节一致')
    require(all((OUT / name).is_dir() for name in ('docs', 'src', 'test')), '提交目录包含docs src test三组')
    result = subprocess.run([sys.executable, '-X', 'utf8', str(ROOT / 'scripts/check_structure.py')], cwd=ROOT, capture_output=True, text=True, encoding='utf-8')
    require(result.returncode == 0, '主仓库目录边界检查通过 九个项目目录')
    docs = [p for p in OUT.rglob('*.docx') if exportable(p) and 'MVP项目' not in p.parts]
    require(len(docs) == 20, '二十份独立Word交付文档齐全 不含源码内重复原件')
    for p in docs:
        with ZipFile(p) as z:
            require(z.testzip() is None, p.relative_to(OUT).as_posix() + ' OOXML包可读')
    data = json.loads((OUT / 'test/证据/本次分项测试结果.json').read_text(encoding='utf-8'))
    require(len(data) == 5 and all(len(rows) == 3 and all(x['result'] == '通过' for x in rows) for rows in data.values()), '五类各三项代表性测试共十五项实际通过')
    for category in data:
        require((OUT / 'test' / (category + '场景测试过程.docx')).is_file() and (OUT / 'test/证据' / (category + '-执行记录.png')).is_file(), category + '场景Word报告与可视化真实日志齐全')
    rendered = json.loads((ROOT / '.venv/assignment-qa/render/render-index.json').read_text(encoding='utf-8-sig'))
    require(len(rendered) == len(docs) and all(Path(x['pdf']).is_file() for x in rendered), '二十份Word均完成实际渲染 PDF及页面图保存在忽略目录')
    suspicious = [p for p in OUT.rglob('*') if p.is_file() and exportable(p) and (p.suffix in {'.key', '.pem', '.sqlite', '.sqlite3', '.db', '.pyc'} or p.name == '.env')]
    require(not suspicious, '提交副本未包含虚拟环境 Git历史 数据库及密钥文件类型')
    # No check can turn the missing individual event data or public deployment into completed work.
    note = '# 作业交付核对记录\n\n核对日期 2026年11月8日。核对结果限于下列文件和测试，不表示真实客户验收或正式政务生产上线。\n\n'
    note += '\n'.join('- ' + item for item in checks)
    event_note = '典型事件原始两轮采访没有可核实单案量化数据。'
    simulation = OUT / 'docs/03_典型事件采访/单案量化数据_教学模拟.json'
    if simulation.is_file():
        case = json.loads(simulation.read_text(encoding='utf-8'))
        require(case['provenance'] == 'teaching_simulation' and case['real_business_verified'] is False, '新增个案明确标注教学模拟 不冒充已核实事实')
        require(sum(s['minutes'] for s in case['segments']) == case['metrics']['staff_processing_minutes'], '模拟单案逐节点处理工时与汇总相符')
        event_note += '已补带独立编号、逐节点时间账和三轮模拟访谈的教学单案；模拟数字不是原受访者确认或系统实测。'
    public_note = '公网网站地址尚未取得。'
    public_evidence = OUT / 'test/证据/公网免登录验证.json'
    if public_evidence.is_file():
        evidence = json.loads(public_evidence.read_text(encoding='utf-8'))
        require(len(evidence['checks']) >= 5 and len(evidence['cases']) == 2 and
                all(case['completed_steps'] == 11 and case['close_reason'] == 'confirmed_resolved'
                    for case in evidence['cases']), '公网免登录两案例各11步确认结案 有独立外网证据')
        public_note = '公网教学演示已于' + evidence['checked_at'] + '核验：' + evidence['base_url'] + '/examples/workflow，两个虚构案例免登录完成11步并结案；HTTP教学环境，不代表生产上线。'
    note += '\n\n## 公网状态与尚未满足的条件\n\n' + public_note + event_note + '问答和知识内容旧浏览器套件失败，相关日志已保留。全量单元回归因耗时主动中止，未取得完整结束结果；五类代表性测试十五项独立通过，欢迎页、两完整案例、助手及岗位工作台浏览器验证通过。\n'
    (OUT / '交付核对记录.md').write_text(note, encoding='utf-8')
    archive = OUT / '政务易办_作业提交.zip'
    tree = ['作业提交/']
    paths = {p for p in OUT.rglob('*') if exportable(p)} | {archive}
    for p in sorted(paths):
        relative = p.relative_to(OUT)
        if len(relative.parts) <= 3:
            tree.append('  ' * len(relative.parts) + p.name + ('/' if p.is_dir() else ''))
    tree.append('\n目录树展示至三级，MVP项目保留完整源码，证据子目录保留完整截图。')
    (OUT / '目录树.txt').write_text('\n'.join(tree) + '\n', encoding='utf-8')
    entries = []
    for p in sorted(OUT.rglob('*')):
        if p.is_file() and exportable(p) and p not in (archive, OUT / '文件校验清单.json'):
            entries.append({'path': p.relative_to(OUT).as_posix(), 'bytes': p.stat().st_size, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()})
    (OUT / '文件校验清单.json').write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding='utf-8')
    with ZipFile(archive, 'w', ZIP_DEFLATED) as z:
        for p in sorted(OUT.rglob('*')):
            if p.is_file() and exportable(p) and p != archive:
                z.write(p, '作业提交/' + p.relative_to(OUT).as_posix())
    with ZipFile(archive) as z:
        require(z.testzip() is None, '压缩包完整且不递归包含自身')
    print('\n'.join(checks))
    print('ARCHIVE_BYTES', archive.stat().st_size)


if __name__ == '__main__':
    main()
