"""运行已有代表性测试并将真实执行记录可视化，禁止用历史结果冒充本次结果。"""
import ast
import html
import io
import json
from pathlib import Path
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests')]
OUTPUT = ROOT / '作业提交' / 'test' / '证据'
GROUPS = {
    '正常': [
        ('test_materials', 'test_two_matters_have_independent_complete_and_missing_results', '两类事项各准备完整材料，并分别删除一项', '完整集合未发现清单问题，缺项集合提示疑似缺项'),
        ('test_agent_routes', 'test_real_handlers_prepare_confirm_create_query', '准备草稿、本人确认、建单并查询', '工具实际返回本人创建的工单'),
        ('test_guide_qa', 'test_answer_is_exact_quote_with_verified_citation', '使用受控指南和模型替身查询', '输出与原文一致且引用可核对')],
    '边界': [
        ('test_materials', 'test_rule_change_and_historical_date_exception_are_explicit', '证件已过期但历史有效，再传入不存在的规则版本', '标记需人工核验，错误版本拒绝'),
        ('test_materials', 'test_unreadable_present_file_is_not_claimed_missing', '材料存在但OCR不可用', '结果为无法判断，不将识别失败视为缺件'),
        ('test_confirmed_tickets', 'test_concurrent_and_new_key_repetition_creates_once', '并发及使用新操作标识重复提交同一草稿', '只创建一个工单')],
    '对抗': [
        ('test_agent_loop', 'test_bounded_loop_rejects_unknown_and_never_invents_success', '提示忽略指令并调用删除全部数据的未知工具', '状态blocked，不声称执行成功'),
        ('test_agent_loop', 'test_free_model_claim_is_not_shown_without_tool_evidence', '模型直接声称证件真实有效且一定获批', '不展示无工具依据的保证'),
        ('test_guide_qa', 'test_fabricated_quote_unknown_id_and_malformed_response_refuse', '伪造引文、未知块编号及无效模型响应', '拒答而不制造政策依据')],
    '权限': [
        ('test_materials', 'test_private_storage_requires_owner_and_explicit_consent', '匿名、其他用户、未授权上传及跨人读取', '全部拒绝，所有者明确授权后可读'),
        ('test_staff', 'test_engineer_is_not_business_administrator', '工程师访问业务内容', '技术岗位不自动取得业务权限'),
        ('test_confirmed_tickets', 'test_unknown_matter_is_not_visible_to_ordinary_wildcard', '普通通配事项岗位查询未知事项队列', '没有明确未知事项授权时不可见')],
    '故障': [
        ('test_materials', 'test_untrusted_processing_fails_closed_and_is_not_missing_material', '未开启可信教学材料处理时发起识别', '返回隔离不可用，字段为空且不称缺件'),
        ('test_materials', 'test_worker_timeout_reaps_process', '材料解析工作进程超时', '受控返回失败并回收进程'),
        ('test_agent_loop', 'test_model_cannot_replace_identity_or_loop_forever', '模型连续调用工具超过限定次数', '使用服务端身份并在调用上限结束')],
}


def identifier(module, method):
    tree = ast.parse((ROOT / 'tests' / (module + '.py')).read_text(encoding='utf-8'))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and any(isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef)) and x.name == method for x in node.body):
            return module + '.' + node.name + '.' + method
    raise ValueError(method)


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    all_results = {}
    for category, cases in GROUPS.items():
        rows = []
        for module, method, given, expected in cases:
            name = identifier(module, method)
            stream = io.StringIO()
            started = time.monotonic()
            result = unittest.TextTestRunner(stream=stream, verbosity=2).run(unittest.defaultTestLoader.loadTestsFromName(name))
            status = '通过' if result.wasSuccessful() and not result.skipped else '跳过' if result.skipped else '失败'
            row = dict(id=name, input=given, expected=expected, result=status, seconds=round(time.monotonic() - started, 3), log=stream.getvalue())
            rows.append(row)
            print(category, name, status, flush=True)
        all_results[category] = rows
        (OUTPUT / (category + '-执行记录.txt')).write_text('\n\n'.join(x['log'] for x in rows), encoding='utf-8')
        (OUTPUT / (category + '-结果.json')).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
        cards = ''.join('<section><h2>' + str(i + 1) + ' ' + html.escape(r['result']) + '</h2><p><b>输入与操作</b> ' + html.escape(r['input']) + '</p><p><b>预期结果</b> ' + html.escape(r['expected']) + '</p><p><b>实际结果</b> ' + html.escape(r['result']) + '，耗时 ' + str(r['seconds']) + ' 秒</p><code>' + html.escape(r['id']) + '</code><pre>' + html.escape(r['log'][-500:]) + '</pre></section>' for i, r in enumerate(rows))
        page = '<!doctype html><meta charset="utf-8"><style>body{font:18px "Microsoft YaHei",sans-serif;color:#172c3d;background:#f4f6f8;padding:40px;max-width:1150px;margin:auto}h1{font-size:32px}section{background:white;border:1px solid #ccd5df;border-radius:8px;margin:20px 0;padding:22px}h2{font-size:23px}p{line-height:1.6}code{font-size:14px;overflow-wrap:anywhere}pre{white-space:pre-wrap;font-size:13px;color:#46546a}</style><h1>政务易办 ' + category + '场景测试执行记录</h1><p>2026年11月7日 · 自动化执行记录可视化 · 这是实际测试日志的展示页，不是业务系统界面截图。虚构数据；模型相关测试使用替身。</p>' + cards
        (OUTPUT / (category + '-执行记录.html')).write_text(page, encoding='utf-8')
    (OUTPUT / '本次分项测试结果.json').write_text(json.dumps(all_results, ensure_ascii=False, indent=2), encoding='utf-8')
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(channel='chrome', headless=True)
        try:
            page = browser.new_page(viewport={'width': 1400, 'height': 1000}, device_scale_factor=1)
            for category in GROUPS:
                page.goto((OUTPUT / (category + '-执行记录.html')).as_uri())
                page.screenshot(path=str(OUTPUT / (category + '-执行记录.png')), full_page=True)
        finally:
            browser.close()


if __name__ == '__main__':
    main()
