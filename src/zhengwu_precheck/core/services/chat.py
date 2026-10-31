"""问答状态协调：事项确认、业务兜底与统一结果；人工工单使用独立入口。"""
from zhengwu_precheck.core.guide_rag import NO_EVIDENCE

MATTERS = [
    {"matter_id": "residence-permit", "name": "居住证申领", "scenario_id": "rental-demo", "scenario_name": "租赁居住（教学情形）"},
    {"matter_id": "sole-trader-registration", "name": "个体工商户设立登记", "scenario_id": "standard-demo", "scenario_name": "普通设立（教学情形）"},
]


def needs_confirmation(conversation, question):
    if not conversation or not conversation.get("confirmed_at"):
        return True
    mentioned = set()
    if "居住证" in question:
        mentioned.add("residence-permit")
    if any(word in question for word in ("个体工商户", "营业执照")):
        mentioned.add("sole-trader-registration")
    return (bool(mentioned - {conversation.get("matter_id")})
            or any(word in question for word in ("换个事项", "切换事项", "重新选择事项")))


def fallback(human_service):
    return {"outcome": "insufficient_evidence", "text": NO_EVIDENCE, "citations": [], "reason": "insufficient_evidence",
            "actions": [
                {"type": "clarify", "label": "补充问题描述", "enabled": True},
                {"type": "human_service", "label": "查看人工咨询方式", "enabled": True, "text": human_service},
                {"type": "create_ticket", "label": "登录后提交人工工单", "enabled": True},
            ]}
