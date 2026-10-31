"""真实SQLite工单状态/权限/日界线测试；仅时钟可注入，不等待真实14天。"""
import asyncio
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta

from sqlalchemy import text
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from zhengwu_precheck.infrastructure.database import ChatRepository
from zhengwu_precheck.infrastructure.staff_repository import StaffRepository
from zhengwu_precheck.infrastructure.agent_repository import AgentRepository
from zhengwu_precheck.core.errors import AppError

AVAILABLE = importlib.util.find_spec('zhengwu_precheck.infrastructure.ticket_repository') is not None
if AVAILABLE:
    from zhengwu_precheck.infrastructure.ticket_repository import TicketRepository
    from zhengwu_precheck.core.services.ticket_clock import WorkCalendar

MATTER = 'residence-permit'
TZ = timezone(timedelta(hours=8))
def stamp(day, hour=10):
    return datetime(2026, 10, day, hour, tzinfo=TZ).timestamp()


class TicketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.assertTrue(AVAILABLE, '尚未实现持久化工单仓储')
        self.tmp = tempfile.TemporaryDirectory()
        self.chat = ChatRepository(Path(self.tmp.name) / 'db.sqlite3')
        await self.chat.initialize()
        self.staff = StaffRepository(self.chat.engine)
        await self.staff.initialize()
        self.ids = {}
        for name, role, matter in [('alice',None,None),('bob',None,None),('w','window',MATTER),('w2','window',MATTER),
                                  ('r','reviewer',MATTER),('d','director',MATTER),('h','hotline',MATTER),
                                  ('e','engineer','*'),('x','window','sole-trader-registration')]:
            # 密码非本测试对象，插入显式测试占位hash，禁止用于运行账号。
            async with self.chat.engine.begin() as c:
                await c.execute(text('INSERT INTO users VALUES(:id,:name,:hash,\'active\')'), {'id': name, 'name': name, 'hash':'TEST-NOT-A-LOGIN-HASH'})
            if role:
                await self.staff.grant_role(name, role, matter, actor='test')
        self.now = stamp(30)  # 周五
        self.repo = TicketRepository(self.chat.engine, clock=lambda: self.now)
        await self.repo.initialize()
        self.agent = AgentRepository(self.chat.engine,clock=lambda:self.now)
        await self.agent.initialize()
        self.submissions = {}

    async def asyncTearDown(self):
        if hasattr(self,'chat'):
            await self.chat.close()
            self.tmp.cleanup()

    async def who(self, name):
        return {'user_id':name, 'username':name, 'grants':await self.staff.grants(name)}

    async def create(self, key='create-1'):
        if key not in self.submissions:
            payload={'matter_id':MATTER,'scenario_id':'rental-demo','title':'教学问题','content':'教学蓝色回执如何准备？'}
            draft=await self.agent.create_draft('alice',payload)
            confirmation=await self.agent.confirm_draft('alice',draft['id'],draft['version'])
            self.submissions[key]={'draft_id':draft['id'],'draft_version':draft['version'],
                                   'submission_confirmation_id':confirmation['id'],'idempotency_key':key}
        return await self.repo.create('alice',self.submissions[key])

    async def invalid_payload(self,payload):
        payload=dict(payload); key=payload.pop('idempotency_key')
        draft=await self.agent.create_draft('alice',payload)
        confirmation=await self.agent.confirm_draft('alice',draft['id'],draft['version'])
        return await self.repo.create_confirmed('alice',draft['id'],draft['version'],confirmation['id'],key)

    async def action(self, user, record, action, **kw):
        return await self.repo.staff_action(await self.who(user),record['id'],action,
            {'expected_version':record['version'],'idempotency_key':str(record['version'])+'-'+action, **kw})

    async def test_create_ownership_and_idempotency(self):
        a = await self.create()
        b = await self.create()
        self.assertEqual(a['id'], b['id'])
        self.assertTrue(a['id'].startswith('TK-'))
        self.assertIsNone(a['precheck_id']); self.assertIsNone(a['formal_case_id'])
        self.assertEqual(a['first_reply_due'], self.now+48*3600)
        self.assertEqual(a['accept_due'], datetime(2026,11,2,10,tzinfo=TZ).timestamp())
        with self.assertRaises(AppError) as denied:
            await self.repo.citizen_detail('bob',a['id'])
        self.assertEqual(denied.exception.status,404)
        self.assertEqual((await self.repo.citizen_list('bob'))['total'],0)

    async def test_racing_accept_and_pending_transfer_responsibility(self):
        t = await self.create()
        results = await asyncio.gather(self.action('w',t,'accept'),self.action('w2',t,'accept'),return_exceptions=True)
        self.assertEqual(sum(isinstance(r,dict) for r in results),1)
        t = next(r for r in results if isinstance(r,dict))
        origin = t['owner_id']
        t = await self.action(origin,t,'transfer',target_user_id='r',target_role='reviewer',reason='专业异常')
        self.assertEqual(t['owner_id'],origin)
        detail = await self.repo.staff_detail(await self.who('r'),t['id'])
        transfer = detail['transfers'][-1]
        t = await self.repo.staff_action(await self.who('r'), t['id'],'transfer_accept',
            {'expected_version':t['version'],'idempotency_key':'accept-transfer'},transfer['id'])
        self.assertEqual(t['owner_id'],'r')
        self.assertEqual(t['first_reply_due'],self.now+48*3600)
        with self.assertRaises(AppError):
            await self.action(origin,t,'reply',content='已转出不可回复',kind='result')

    async def test_scope_and_role_and_record_access(self):
        t = await self.create()
        for user in ('x','e','h','d','r'):
            with self.assertRaises(AppError):
                await self.action(user,t,'accept')
        t = await self.action('w',t,'accept')
        with self.assertRaises(AppError):
            await self.repo.staff_detail(await self.who('w2'),t['id'])
        hotline = await self.repo.staff_detail(await self.who('h'),t['id'])
        self.assertNotIn('conversation_id',hotline['ticket'])
        with self.assertRaises(AppError):
            await self.repo.staff_detail(await self.who('e'),t['id'])
        self.assertEqual((await self.repo.staff_list(await self.who('x')))['total'],0)
        targets = await self.repo.assignees(await self.who('w'), MATTER,t['id'])
        self.assertNotIn('x',{v['user_id'] for v in targets})

    async def test_round_cancellation_reminders_and_local_day_end(self):
        t = await self.action('w',await self.create(),'accept')
        t = await self.action('w',t,'reply',content='请补充教学描述',kind='supplement')
        first_round = t['round_no']
        self.now += 7*86400
        await self.repo.run_due()
        notices = await self.repo.notifications('alice')
        self.assertEqual(sum(n['kind']=='reminder7' for n in notices),1)
        await self.repo.run_due()
        self.assertEqual(len(await self.repo.notifications('alice')),len(notices))
        t = await self.repo.citizen_action('alice',t['id'],'message',{'expected_version':t['version'],'idempotency_key':'supplement','content':'已补充教学描述'})
        self.assertEqual(t['status'],'processing'); self.assertIsNone(t['close_due'])
        self.now += 9*86400
        await self.repo.run_due()
        self.assertEqual((await self.repo.citizen_detail('alice',t['id']))['ticket']['status'],'processing')
        t = await self.action('w',t,'reply',content='教学处理结果',kind='result')
        self.assertGreater(t['round_no'],first_round)
        self.now += 14*86400
        # 336小时只发送第14天提醒，仍保留当天剩余时段。
        await self.repo.run_due()
        self.assertEqual((await self.repo.citizen_detail('alice',t['id']))['ticket']['status'],'waiting_citizen')
        self.now = t['close_due']
        # 重建仓储模拟服务重启，不依赖原实例内存任务。
        recovered = TicketRepository(self.chat.engine,clock=lambda:self.now)
        await recovered.run_due(); await recovered.run_due()
        closed = (await recovered.citizen_detail('alice',t['id']))['ticket']
        self.assertEqual(closed['close_reason'],'default_complete')
        self.assertEqual(sum(n['kind']=='reminder14' for n in await recovered.notifications('alice')),1)

    async def test_confirm_and_message_race_preserves_one_result(self):
        t = await self.action('w',await self.create(),'accept')
        t = await self.action('w',t,'reply',content='教学答复',kind='result')
        results = await asyncio.gather(*[self.repo.citizen_action('alice',t['id'],a,
            {'expected_version':t['version'],'idempotency_key':a,'content':'还有问题'}) for a in ('confirm','message')],return_exceptions=True)
        self.assertEqual(sum(isinstance(r,dict) for r in results),1)
        current = (await self.repo.citizen_detail('alice',t['id']))['ticket']
        self.assertIn(current['status'],('closed','processing'))
        if current['status']=='closed': self.assertEqual(current['close_reason'],'confirmed_resolved')

    async def test_reject_cancel_and_manual_escalation(self):
        t = await self.action('w',await self.create(),'accept')
        t = await self.action('w',t,'transfer',target_user_id='d',target_role='director',reason='手动升级')
        tr = (await self.repo.staff_detail(await self.who('w'),t['id']))['transfers'][-1]
        t = await self.repo.staff_action(await self.who('d'),t['id'],'transfer_reject',{'expected_version':t['version'],'idempotency_key':'reject','reason':'需先专业核对'},tr['id'])
        self.assertEqual(t['owner_id'],'w')
        t = await self.action('w',t,'transfer',target_user_id='r',target_role='reviewer',reason='专业核对')
        tr = (await self.repo.staff_detail(await self.who('w'),t['id']))['transfers'][-1]
        t = await self.repo.staff_action(await self.who('w'),t['id'],'transfer_cancel',{'expected_version':t['version'],'idempotency_key':'cancel','reason':'撤回重新描述'},tr['id'])
        self.assertEqual(t['owner_id'],'w')

    async def test_calendar_override_and_link_validation(self):
        cal = WorkCalendar(holidays=['2026-11-02'],workdays=['2026-11-01'])
        self.assertEqual(cal.accept_due(stamp(30)),datetime(2026,11,1,10,tzinfo=TZ).timestamp())
        with self.assertRaises(AppError):
            await self.invalid_payload({'matter_id':MATTER,'scenario_id':'rental-demo','title':'教学','content':'测试',
                'idempotency_key':'bad-link','conversation_id':'other-person'})
        with self.assertRaises(AppError):
            await self.invalid_payload({'matter_id':MATTER,'scenario_id':'rental-demo','title':'教学','content':'测试',
                'idempotency_key':'bad-precheck','precheck_id':'FAKE-1'})

    async def test_hotline_cannot_borrow_unrelated_handling_role_for_detail(self):
        t=await self.action('w',await self.create(),'accept')
        t=await self.action('w',t,'transfer',target_user_id='r',target_role='reviewer',reason='内部专业核对说明')
        await self.staff.grant_role('h','reviewer',MATTER,actor='test')
        detail=await self.repo.staff_detail(await self.who('h'),t['id'])
        self.assertEqual(detail['transfers'],[])
        self.assertNotIn('conversation_id',detail['ticket'])

    async def test_downgraded_role_cannot_replay_private_operation(self):
        t=await self.create()
        data={'expected_version':1,'idempotency_key':'original-accept'}
        await self.repo.staff_action(await self.who('w'),t['id'],'accept',data)
        await self.staff.grant_role('w','hotline',MATTER,actor='test')
        await self.staff.revoke_role('w','window',MATTER,actor='test')
        with self.assertRaises(AppError) as denied:
            await self.repo.staff_action(await self.who('w'),t['id'],'accept',data)
        self.assertEqual(denied.exception.status,403)

    async def test_optional_link_types_are_rejected_before_database(self):
        for name in ('conversation_id','message_id'):
            with self.assertRaises(AppError) as error:
                await self.invalid_payload({'matter_id':MATTER,'scenario_id':'rental-demo',
                    'title':'教学类型检查','content':'测试','idempotency_key':name,'conversation_id':None,name:['非法类型']})
            self.assertEqual(error.exception.status,422)

    async def test_deadline_race_and_overdue_do_not_auto_escalate(self):
        t=await self.create()
        original_due=t['first_reply_due']
        self.now+=49*3600
        await self.repo.run_due()
        detail=await self.repo.citizen_detail('alice',t['id'])
        self.assertIsNone(detail['ticket']['owner_id'])
        self.assertIn('reply_overdue',[r['action'] for r in detail['transitions']])
        t=await self.action('w',t,'accept')
        t=await self.action('w',t,'reply',content='教学答复',kind='result')
        self.assertEqual(t['first_reply_due'],original_due)
        self.now=t['close_due']
        results=await asyncio.gather(self.repo.run_due(),self.repo.citizen_action('alice',t['id'],'confirm',
            {'expected_version':t['version'],'idempotency_key':'deadline-confirm'}),return_exceptions=True)
        self.assertTrue(any(isinstance(r,AppError) for r in results))
        final=(await self.repo.citizen_detail('alice',t['id']))['ticket']
        self.assertEqual(final['close_reason'],'default_complete')

