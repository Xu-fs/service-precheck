"""问答状态协调：事项确认、业务兜底与统一结果；人工工单使用独立入口。"""
import re

from zhengwu_precheck.core.guide_rag import NO_EVIDENCE

MATTERS = [
    {"matter_id": "residence-permit", "name": "居住证申领", "scenario_id": "rental-demo", "scenario_name": "租赁居住（示例）"},
    {"matter_id": "sole-trader-registration", "name": "个体工商户设立登记", "scenario_id": "standard-demo", "scenario_name": "普通设立（示例）"},
]


def outside_preset_reply(conversation, question):
    """未知问题直接给出工单入口；明确的示例咨询和上下文追问继续原流程。"""
    if any(word in question for word in (
        "居住证", "个体工商户", "营业执照", "申请表", "字段", "回执", "示例清单",
        "换个事项", "切换事项", "重新选择事项",
    )):
        return None
    generic = re.fullmatch(
        r"(?:请问|请|我想了解|想咨询)?(?:这个事项|该事项)?"
        r"(?:需要什么材料|要什么材料|材料有哪些|有哪些材料|需要哪些材料|办理条件是什么|多久办好|费用多少)[？?。！!]*",
        question.strip(),
    )
    if generic or (conversation and conversation.get("confirmed_at") and any(
        word in question for word in ("租赁合同", "居住证明", "身份证明", "申请情形", "补充材料", "材料清单")
    )):
        return None
    return {"outcome":"insufficient_evidence", "reason":"outside_presets", "citations":[],
            "text":"您可以提交工单，补充说明您的问题，由工作人员协助处理。",
            "actions":[{"type":"create_ticket", "label":"提交工单", "enabled":True}]}


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
