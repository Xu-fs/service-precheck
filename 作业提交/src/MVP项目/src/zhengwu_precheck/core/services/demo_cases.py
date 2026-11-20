"""预设案例内容和操作提示；只提供预填，不自动提交或改变岗位权限。"""
import json
from pathlib import Path

CASES_PATH = Path(__file__).resolve().parents[4] / 'data' / 'workflow-cases.json'


def workflow_cases():
    return json.loads(CASES_PATH.read_text(encoding='utf-8'))


def workflow_steps(case, accounts):
    """固定的示例步骤；脚本与网页复用相同动作，调用原工单服务执行。"""
    rows = [
        ('citizen', 'message', '办事人发送问题', {'content': case['first_message']}),
        ('window', 'accept', '窗口接手', {}),
        ('window', 'reply', '窗口初审并要求补充', {'kind': 'supplement', 'content': case['window_request']}),
        ('citizen', 'message', '办事人补充信息', {'content': case['citizen_supplement']}),
        ('window', 'transfer', '窗口提交业务复核', {'target_role': 'reviewer', 'reason': case['window_transfer']}),
        ('reviewer', 'transfer_accept', '审核员接收', {}),
        ('reviewer', 'reply', '审核员给出复核意见', {'kind': 'result', 'content': case['reviewer_reply']}),
        ('reviewer', 'transfer', '审核员提交主任审核', {'target_role': 'director', 'reason': case['reviewer_transfer']}),
        ('director', 'transfer_accept', '主任接收', {}),
        ('director', 'reply', '主任给出审核意见', {'kind': 'result', 'content': case['director_reply']}),
        ('citizen', 'confirm', '办事人确认结案', {}),
    ]
    return [dict(role=role, action=action, label=label, data=data) for role, action, label, data in rows]


def case_for_ticket(ticket):
    return next((case for case in workflow_cases() if case['title'] == ticket.get('title')
                 and case['matter_id'] == ticket.get('matter_id')
                 and case['content'] == ticket.get('content')), None)


def workflow_hint(detail, case):
    if not case:
        return None
    ticket = detail['ticket']
    if ticket['status'] == 'closed':
        return {'account': 'demo_citizen', 'action': 'done', 'label': '已结案',
                'text': '本次示例工单已结束，可查看对话、转派记录和流转时间线。'}
    pending = next((tr for tr in detail['transfers'] if tr['status'] == 'pending'), None)
    if pending:
        return {'account': 'demo_' + pending['target_role'], 'action': 'accept_transfer',
                'label': '确认接收转派', 'text': '登录对应岗位账号，打开本工单，在转派记录中点击“确认接收”。'}
    role = ticket.get('owner_role')
    if not role:
        return {'account': 'demo_window', 'action': 'accept', 'label': '窗口接手',
                'text': '登录窗口账号，在工单队列中找到本案例并接手。'}
    if role == 'window':
        if ticket['status'] == 'waiting_citizen':
            return {'account': 'demo_citizen', 'action': 'message', 'label': '补充并发送信息',
                    'text': case['citizen_supplement']}
        if not any(m['actor_type'] == 'staff' for m in detail['messages']):
            return {'account': 'demo_window', 'action': 'reply', 'kind': 'supplement',
                    'label': '窗口初审并请求补充', 'text': case['window_request']}
        return {'account': 'demo_window', 'action': 'transfer', 'target_role': 'reviewer',
                'label': '初审通过，提交业务复核', 'text': case['window_transfer']}
    if role == 'reviewer':
        replied = any(m['actor_id'] == ticket['owner_id'] and m['content'] == case['reviewer_reply']
                      for m in detail['messages'])
        if not replied:
            return {'account': 'demo_reviewer', 'action': 'reply', 'kind': 'result',
                    'label': '记录业务复核意见', 'text': case['reviewer_reply']}
        return {'account': 'demo_reviewer', 'action': 'transfer', 'target_role': 'director',
                'label': '复核通过，提交主任审核', 'text': case['reviewer_transfer']}
    if role == 'director':
        replied = any(m['actor_id'] == ticket['owner_id'] and m['content'] == case['director_reply']
                      for m in detail['messages'])
        if replied:
            return {'account': 'demo_citizen', 'action': 'confirm', 'label': '确认问题已解决并结案',
                    'text': '主任审核意见已发送。登录办事人账号，核对答复后点击“确认问题已解决”。'}
        return {'account': 'demo_director', 'action': 'reply', 'kind': 'result',
                'label': '主任审核并发送处理结果', 'text': case['director_reply']}
    return None
