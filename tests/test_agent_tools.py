import unittest
import asyncio

from zhengwu_precheck.core.services.agent_tools import dispatch, ticket_status_handler


class AgentToolBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_tool_rejected(self):
        result = await dispatch('delete_everything', {}, {'user_id': 'u'})
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['error']['code'], 'UNKNOWN_TOOL')

    async def test_identity_cannot_be_supplied_by_model(self):
        result = await dispatch('get_ticket_status', {'ticket_id': 'TK-1', 'user_id': 'other'}, {'user_id': 'u'})
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['error']['code'], 'INVALID_ARGUMENTS')

    async def test_unconnected_tool_does_not_claim_success(self):
        result = await dispatch('get_ticket_status', {'ticket_id': 'TK-1'}, {'user_id': 'u'})
        self.assertEqual(result['status'], 'temporarily_unavailable')

    async def test_non_object_arguments_are_rejected(self):
        result = await dispatch('get_ticket_status', [], {'user_id': 'u'})
        self.assertEqual(result['status'], 'blocked')

    async def test_handler_receives_server_identity(self):
        async def status(args, principal):
            return {'ticket_id': args.ticket_id, 'owner': principal['user_id']}
        result = await dispatch('get_ticket_status', {'ticket_id': 'TK-1'},
                                {'user_id': 'server-user'}, handlers={'get_ticket_status': status})
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['data']['owner'], 'server-user')

    async def test_handler_timeout_does_not_report_success(self):
        async def slow(args, principal):
            await asyncio.sleep(1)
        result = await dispatch('get_ticket_status', {'ticket_id': 'TK-1'}, {'user_id': 'u'},
                                handlers={'get_ticket_status': slow}, timeout=0.01)
        self.assertEqual(result['status'], 'temporarily_unavailable')

    async def test_handler_failure_does_not_leak_details(self):
        async def broken(args, principal):
            raise RuntimeError('sensitive secret')
        result = await dispatch('get_ticket_status', {'ticket_id': 'TK-1'}, {'user_id': 'u'},
                                handlers={'get_ticket_status': broken})
        self.assertNotIn('sensitive secret', str(result))
        self.assertNotEqual(result['status'], 'ok')

    async def test_status_uses_repository_authorization_and_minimizes_output(self):
        class Repo:
            async def citizen_detail(self, user_id, ticket_id):
                self.called = (user_id, ticket_id)
                return {'ticket': {'id': ticket_id, 'status': 'queued', 'version': 1,
                                   'created_at': 5, 'content': 'private'},
                        'messages': [], 'transfers': [{'reason': 'internal'}]}
        repo = Repo()
        result = await dispatch('get_ticket_status', {'ticket_id': 'TK-1'}, {'user_id': 'owner'},
                                handlers={'get_ticket_status': ticket_status_handler(repo)})
        self.assertEqual(repo.called, ('owner', 'TK-1'))
        self.assertEqual(result['data']['status'], 'queued')
        self.assertNotIn('internal', str(result))
        self.assertNotIn('private', str(result))

    async def test_status_requires_login(self):
        result = await dispatch('get_ticket_status', {'ticket_id': 'TK-1'}, {'user_id': None},
                                handlers={'get_ticket_status': ticket_status_handler(object())})
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['error']['code'], 'LOGIN_REQUIRED')


if __name__ == '__main__':
    unittest.main()
