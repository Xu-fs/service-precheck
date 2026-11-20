"""便民入口跳转到公开指南，内容阅读不依赖登录或模型服务。"""
from html.parser import HTMLParser
from pathlib import Path
import sys
import tempfile
import unittest
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from zhengwu_precheck.main import create_app
from zhengwu_precheck.settings import Settings
from test_chat import FakeQA, events


class PageLinks(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.links = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.links.append(dict(attrs))


class PublicServicePageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.qa = FakeQA()
        self.app = create_app(Settings(runtime_dir=Path(self.temp.name)), qa=self.qa)
        self.lifecycle = self.app.router.lifespan_context(self.app)
        await self.lifecycle.__aenter__()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://testserver')

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.lifecycle.__aexit__(None, None, None)

    async def test_home_shortcuts_send_questions_before_navigation(self):
        response = await self.client.get('/')
        links = {link.get('href') for link in PageLinks(response.text).links}
        self.assertIn('data-question="我想咨询身份证补办', response.text)
        self.assertIn('data-question="我想查询便民场馆', response.text)
        self.assertNotIn('data-prefill=', response.text)
        self.assertNotIn('/services/id-card-replacement', links)
        self.assertNotIn('/services/public-venues', links)
        self.assertIn('/my/tickets#create-title', links)

    async def test_answers_offer_guide_buttons_and_preserve_them_in_history_and_replay(self):
        await self.client.get('/')
        token = (await self.client.get('/bootstrap')).json()['csrf_token']
        session_id = None
        for question, path in [
            ('身份证丢了怎么补办？', '/services/id-card-replacement'),
            ('便民场馆开放时间和预约方式是什么？', '/services/public-venues'),
            ('身份证补办和公共图书馆开放都想了解', None),
        ]:
            headers = {'X-CSRF-Token': token, 'Idempotency-Key': str(uuid4())}
            body = {'question': question, 'session_id': session_id}
            response = await self.client.post('/chat', json=body, headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            streamed = events(response)
            self.assertNotIn('matter_required', [name for name, _ in streamed])
            links = next(data['items'] for name, data in streamed if name == 'actions')
            expected = {path} if path else {'/services/id-card-replacement', '/services/public-venues'}
            self.assertEqual({link['href'] for link in links}, expected)
            self.assertEqual(streamed[-1][1]['outcome'], 'public_service_guidance')
            session_id = streamed[0][1]['session_id']
            history = (await self.client.get(f'/sessions/{session_id}/messages')).json()['items']
            self.assertEqual(history[-1]['result']['actions'], links)
            replayed = events(await self.client.post('/chat', json=body, headers=headers))
            self.assertTrue(replayed[0][1]['replayed'])
            self.assertEqual(next(data['items'] for name, data in replayed if name == 'actions'), links)
        self.assertEqual(self.qa.calls, [])

    async def test_both_guides_are_public_and_have_official_sources(self):
        for path, title, topics in [
            ('/services/id-card-replacement', '身份证补办', ['申请材料', '办理流程', '费用与时限']),
            ('/services/public-venues', '便民场馆开放', ['公共图书馆', '文化馆', '公共体育场馆', '预约']),
        ]:
            with self.subTest(path=path):
                response = await self.client.get(path)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIn(f'<h1>{title}</h1>', response.text)
                self.assertIn('全国通用', response.text)
                self.assertIn('信息来源', response.text)
                self.assertIn('2026-10-31', response.text)
                for topic in topics:
                    self.assertIn(topic, response.text)
                links = PageLinks(response.text).links
                self.assertTrue(any('.gov.cn/' in link.get('href', '') for link in links))
                self.assertTrue(any(link.get('href') == '/' for link in links))
        self.assertEqual(self.qa.calls, [])

    async def test_public_guide_does_not_change_confirmed_matter_or_model_history(self):
        await self.client.get('/')
        self.client.headers['X-CSRF-Token'] = (await self.client.get('/bootstrap')).json()['csrf_token']

        async def ask(question, session_id=None):
            response = await self.client.post('/chat', json={'question': question, 'session_id': session_id},
                                              headers={'Idempotency-Key': str(uuid4())})
            self.assertEqual(response.status_code, 200, response.text)
            return events(response)

        initial = await ask('示例申请表有哪些字段？')
        sid = initial[0][1]['session_id']
        prompt = next(data for name, data in initial if name == 'matter_required')
        confirmed = await self.client.post(f'/sessions/{sid}/matter', json={
            'matter_id': 'residence-permit', 'scenario_id': 'rental-demo',
            'confirmation_token': prompt['confirmation_token'],
        })
        self.assertEqual(confirmed.status_code, 200)
        self.assertEqual((await ask('示例申请表有哪些字段？', sid))[-1][1]['outcome'], 'answered')
        self.assertEqual((await ask('身份证丢了怎么补办？', sid))[-1][1]['outcome'], 'public_service_guidance')
        self.assertEqual((await ask('文化馆单位出具的居住证明可以用于居住证申领吗？', sid))[-1][1]['outcome'], 'answered')
        self.assertEqual((await ask('再介绍一下示例申请表字段', sid))[-1][1]['outcome'], 'answered')
        self.assertEqual(len(self.qa.calls), 3)
        self.assertNotIn('身份证丢了', str(self.qa.calls[-1][1]))
        history = (await self.client.get(f'/sessions/{sid}/messages')).json()
        self.assertEqual(history['matter_id'], 'residence-permit')


if __name__ == '__main__':
    unittest.main()
