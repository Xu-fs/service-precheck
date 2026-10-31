"""岗位工作台界面回归；仅使用虚构账号访问各自获授权的页面。"""
from html import unescape
from pathlib import Path
import re
import sys
import tempfile
import unittest

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from zhengwu_precheck.main import create_app
from zhengwu_precheck.settings import Settings
from test_chat import FakeQA


EXPECTED = {
    "window": ("窗口受理工作台", "窗口工单受理", {"knowledge", "rules", "feedback", "tickets"}),
    "reviewer": ("业务审核工作台", "专业审核与分派", {"knowledge", "rules", "tickets"}),
    "owner": ("业务维护工作台", None, {"knowledge", "rules", "reviews", "feedback"}),
    "supervisor": ("监督审核工作台", None, {"knowledge", "rules", "reviews", "prompts", "audits"}),
    "hotline": ("热线服务工作台", "工单进度查询", {"knowledge", "rules", "feedback", "tickets"}),
    "engineer": ("平台运维工作台", None, {"system"}),
    "director": ("主任工作台", "升级工单处理", {"tickets"}),
}


def navigation(html):
    return re.search(r'<nav aria-label="后台导航">(.*?)</nav>', html, re.S)[1]


class StaffWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(Settings(runtime_dir=Path(self.temp.name),
                                      staff_jwt_secret="workspace-fixture-secret-" + "x9" * 24), qa=FakeQA())
        self.lifecycle = self.app.router.lifespan_context(self.app)
        await self.lifecycle.__aenter__()
        self.clients = []

    async def asyncTearDown(self):
        for client in self.clients:
            await client.aclose()
        await self.lifecycle.__aexit__(None, None, None)
        self.temp.cleanup()

    async def login(self, role, extra_roles=()):
        username = "workspace-" + role
        await self.app.state.repo.create_user(username, "workspace-fixture-password")
        for granted in (role, *extra_roles):
            for matter in ("residence-permit", "sole-trader-registration"):
                await self.app.state.staff_repo.grant_role(username, granted, matter, actor="workspace-fixture")
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app, client=(role, 1234)),
                                   base_url="http://testserver", follow_redirects=True)
        self.clients.append(client)
        page = await client.get("/staff/login")
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text)[1]
        response = await client.post("/staff/api/auth/login", data={"username": username,
                                     "password": "workspace-fixture-password", "csrf_token": csrf})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.url.path, "/staff")
        return client, response

    async def test_each_role_lands_in_its_own_workspace(self):
        for role, (title, ticket_name, slugs) in EXPECTED.items():
            with self.subTest(role=role):
                _, home = await self.login(role)
                self.assertIn(f"<h1>{title}</h1>", home.text)
                links = set(re.findall(r'href="(/staff[^"]*)"', navigation(home.text)))
                self.assertEqual(links, {"/staff", *("/staff/" + slug for slug in slugs)})
                for other_role, (other_title, _, _) in EXPECTED.items():
                    if other_role != role:
                        self.assertNotIn(other_title, home.text)
                self.assertNotIn("业务功能安排", home.text)
                self.assertNotIn("第2批", home.text)
                self.assertNotIn("材料识别与自动预检尚未实现", home.text)
                self.assertEqual(home.text.count('class="workspace-card"'), len(slugs))
                if ticket_name:
                    self.assertIn(ticket_name, navigation(home.text))

    async def test_role_navigation_stays_consistent_inside_authorized_modules(self):
        for role, (title, _, slugs) in EXPECTED.items():
            with self.subTest(role=role):
                client, home = await self.login(role)
                for slug in sorted(slugs):
                    page = await client.get("/staff/" + slug)
                    self.assertEqual(page.status_code, 200, (role, slug))
                    self.assertEqual(navigation(page.text), navigation(home.text).replace(
                        ' aria-current="page"', '').replace(
                        f'href="/staff/{slug}"', f'href="/staff/{slug}" aria-current="page"'), (role, slug))
                    self.assertIn(title, page.text)

    async def test_ticket_page_explains_role_and_shows_only_relevant_scopes(self):
        for role in ("window", "reviewer", "hotline", "director"):
            with self.subTest(role=role):
                client, _ = await self.login(role)
                page = await client.get("/staff/tickets")
                self.assertIn(f"<h1>{EXPECTED[role][1]}</h1>", page.text)
                options = re.search(r'<select name="scope">(.*?)</select>', page.text, re.S)[1]
                scopes = set(re.findall(r'<option value="([^"]+)"', options))
                expected = {"all"} if role == "hotline" else {"all", "mine", "pending"}
                if role == "window":
                    expected.add("queue")
                self.assertEqual(scopes, expected)
                self.assertIn("居住证申领", unescape(page.text))

    async def test_multiple_authorized_roles_have_explicit_combined_workspace(self):
        client, home = await self.login("window", extra_roles=("reviewer",))
        self.assertIn("<h1>多岗位工作台</h1>", home.text)
        self.assertIn("窗口受理工作台", home.text)
        self.assertIn("业务审核工作台", home.text)
        self.assertEqual(home.text.count('class="workspace-card"'), 4)
        self.assertEqual((await client.get("/staff/tickets")).status_code, 200)


if __name__ == "__main__":
    unittest.main()
