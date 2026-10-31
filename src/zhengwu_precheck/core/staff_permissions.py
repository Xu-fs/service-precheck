"""岗位不是管理员标志：每条授权同时限定动作和事项，记录关系另行检查。"""
ROLES = {
    "window": "窗口工作人员", "reviewer": "业务审核员", "owner": "业务负责人",
    "supervisor": "数据与流程监督员", "hotline": "热线人员", "engineer": "政务平台工程师", "director": "服务中心主任",
}
BASE = {"workspace.view"}
ROLE_PERMISSIONS = {
    "window": BASE | {"knowledge.read", "knowledge.feedback", "rule.read", "ticket.handle", "ticket.transfer", "conversation.summary", "conversation.read", "material.view"},
    "reviewer": BASE | {"knowledge.read", "rule.read", "ticket.handle", "ticket.dispatch", "conversation.summary", "conversation.read", "material.view"},
    "owner": BASE | {"knowledge.read", "knowledge.manage", "knowledge.feedback", "rule.read", "rule.edit", "rule.submit", "rule.release.read", "feedback.manage"},
    "supervisor": BASE | {"knowledge.read", "rule.read", "rule.review", "rule.publish", "rule.release.read", "prompt.manage", "audit.review", "conversation.summary", "conversation.read"},
    "hotline": BASE | {"knowledge.read", "knowledge.feedback", "rule.read", "ticket.progress", "conversation.summary"},
    "engineer": BASE | {"system.health"},
    "director": BASE | {"ticket.escalated", "conversation.summary", "conversation.read"},
}
# 只用于导航和列表入口；正文及每个动作仍检查原岗位、事项和处理关系。
for _ticket_role in ('window', 'reviewer', 'hotline', 'director'):
    ROLE_PERMISSIONS[_ticket_role].add('ticket.list')
MATTER_NAMES = {"residence-permit": "居住证申领", "sole-trader-registration": "个体工商户设立登记", "unassigned": "人工咨询分流（须单独授权）", "*": "全部已知事项（显式授权）"}
MODULES = [
    {"slug": "knowledge", "name": "办事知识库", "permission": "knowledge.read", "batch": 2},
    {"slug": "rules", "name": "规则与清单", "permission": "rule.read", "batch": 2},
    {"slug": "reviews", "name": "审核发布", "permission": "rule.release.read", "batch": 2},
    {"slug": "feedback", "name": "规则问题反馈", "permission": "knowledge.feedback", "batch": 2},
    {"slug": "prompts", "name": "提示词模板", "permission": "prompt.manage", "batch": 3},
    {"slug": "audits", "name": "对话与预检审计", "permission": "audit.review", "batch": 3},
    {"slug": "tickets", "name": "工单处理", "permission": "ticket.list", "batch": 4},
    {"slug": "system", "name": "系统运行状态", "permission": "system.health"},
]

WORKSPACES = {
    "window": {
        "title": "窗口受理工作台", "ticket_name": "窗口工单受理",
        "description": "接收本事项公共队列，处理普通咨询，将专业异常转交业务审核员。",
        "ticket_description": "接手窗口公共队列中的工单，回复办事人或转交业务审核员；接收人确认前继续承担待办责任。",
        "modules": ("tickets", "knowledge", "rules", "feedback"),
    },
    "reviewer": {
        "title": "业务审核工作台", "ticket_name": "专业审核与分派",
        "description": "接收转交的专业异常，核对业务依据，答复或分派给实际处理人。",
        "ticket_description": "确认接收转交的专业异常，核对必要材料后答复或分派；需要主任处理时手动升级。",
        "modules": ("tickets", "knowledge", "rules"),
    },
    "owner": {
        "title": "业务维护工作台",
        "description": "维护办事知识、材料清单和预检规则，处理规则反馈并提交监督员审核。",
        "modules": ("knowledge", "rules", "reviews", "feedback"),
    },
    "supervisor": {
        "title": "监督审核工作台",
        "description": "审核发布规则版本，维护提示词，复核授权范围内的对话与预检记录。",
        "modules": ("reviews", "audits", "prompts", "knowledge", "rules"),
    },
    "hotline": {
        "title": "热线服务工作台", "ticket_name": "工单进度查询",
        "description": "查询办事知识、问题进度与答复，反馈规则问题。",
        "ticket_description": "查看授权事项的问题、进度和答复摘要；材料正文及内部交接信息不在热线工作范围内。",
        "modules": ("tickets", "knowledge", "rules", "feedback"),
    },
    "engineer": {
        "title": "平台运维工作台",
        "description": "查看系统技术运行状态，开展平台维护。",
        "modules": ("system",),
    },
    "director": {
        "title": "主任工作台", "ticket_name": "升级工单处理",
        "description": "接收业务人员明确升级的工单，处理需要主任协调的问题。",
        "ticket_description": "仅处理明确升级给你的工单，确认接收后协调并答复办事人。",
        "modules": ("tickets",),
    },
}

MODULE_DESCRIPTIONS = {
    "knowledge": "查询授权事项的办事指南和业务依据。",
    "rules": "查阅授权事项的材料清单、预检规则及适用版本。",
    "reviews": "查看规则版本的提交、审核与发布进度。",
    "feedback": "提交规则问题并查看反馈处理情况。",
    "prompts": "维护授权事项的提示词版本，查看修改历史。",
    "audits": "查看获授权的记录并开展人工复核。",
    "system": "查看数据库连接及后台服务的技术状态。",
}


def allows(grants, permission, matter_id=None):
    """不能将A岗位的动作权限与B岗位的事项范围交叉组合。"""
    return any(permission in grant.get("permissions", ROLE_PERMISSIONS.get(grant["role_code"], set()))
               and (matter_id is None or grant["matter_id"] in {"*", matter_id}) for grant in grants)


def public_identity(identity):
    permissions = sorted({permission for grant in identity["grants"] for permission in grant["permissions"]})
    return {"user_id": identity["user_id"], "username": identity["username"], "grants": identity["grants"], "permissions": permissions}


def workspace_context(identity):
    """首页与业务页共用岗位导航；不改变动作、事项及记录授权。"""
    grants = identity["grants"]
    roles = [role for role in WORKSPACES if any(g["role_code"] == role for g in grants)]
    workspaces = [{"role": role, **WORKSPACES[role]} for role in roles]
    ticket_workspace = next((w for w in workspaces if "ticket_name" in w), None)
    order = list(dict.fromkeys(slug for w in workspaces for slug in w["modules"]))
    modules = []
    for module in MODULES:
        if not allows(grants, module["permission"]):
            continue
        item = {**module, "description": MODULE_DESCRIPTIONS.get(module["slug"], "")}
        if item["slug"] == "tickets" and ticket_workspace:
            item.update(name=ticket_workspace["ticket_name"], description=ticket_workspace["ticket_description"])
        if item["slug"] == "knowledge" and allows(grants, "knowledge.manage"):
            item["description"] = "上传、维护授权事项的办事指南与知识草稿。"
        if item["slug"] == "rules" and allows(grants, "rule.edit"):
            item["description"] = "维护材料清单与预检规则草稿，提交监督员审核。"
        if item["slug"] == "feedback" and allows(grants, "feedback.manage"):
            item["description"] = "核对窗口和热线提交的规则问题，记录处理结果。"
        modules.append(item)
    modules.sort(key=lambda m: order.index(m["slug"]) if m["slug"] in order else len(order))
    return {
        "identity": public_identity(identity), "modules": modules, "workspaces": workspaces,
        "workspace_title": workspaces[0]["title"] if len(workspaces) == 1 else "多岗位工作台",
        "workspace_description": workspaces[0]["description"] if len(workspaces) == 1 else "按已授予的岗位职责处理各自事项。",
        "ticket_workspace": ticket_workspace,
    }
