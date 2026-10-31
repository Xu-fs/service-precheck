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
MATTER_NAMES = {"residence-permit": "居住证申领", "sole-trader-registration": "个体工商户设立登记", "*": "全部事项（显式授权）"}
MODULES = [
    {"slug": "knowledge", "name": "办事知识库", "permission": "knowledge.read", "batch": 2},
    {"slug": "rules", "name": "规则与清单", "permission": "rule.read", "batch": 2},
    {"slug": "reviews", "name": "审核发布", "permission": "rule.release.read", "batch": 2},
    {"slug": "feedback", "name": "规则问题反馈", "permission": "knowledge.feedback", "batch": 2},
    {"slug": "prompts", "name": "提示词模板", "permission": "prompt.manage", "batch": 3},
    {"slug": "audits", "name": "对话与预检审计", "permission": "audit.review", "batch": 3},
    {"slug": "tickets", "name": "工单处理", "permission": "ticket.list", "batch": 4},
]


def allows(grants, permission, matter_id=None):
    """不能将A岗位的动作权限与B岗位的事项范围交叉组合。"""
    return any(permission in grant.get("permissions", ROLE_PERMISSIONS.get(grant["role_code"], set()))
               and (matter_id is None or grant["matter_id"] in {"*", matter_id}) for grant in grants)


def public_identity(identity):
    permissions = sorted({permission for grant in identity["grants"] for permission in grant["permissions"]})
    return {"user_id": identity["user_id"], "username": identity["username"], "grants": identity["grants"], "permissions": permissions}
