"""发布命令入口契约；不启动真实服务或加载生产凭证。"""
import importlib
import asyncio
from contextlib import closing
import json
from pathlib import Path
import runpy
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


class OperationsCLITests(unittest.TestCase):
    def test_recovery_demo_restores_real_application_and_replays_original_submission(self):
        namespace = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/operations.py'))
        with tempfile.TemporaryDirectory() as folder:
            result = asyncio.run(namespace['recovery_demo'](Path(folder)))
            self.assertTrue(result['application_restart_verified'])
            self.assertTrue(result['original_submission_replayed'])
            self.assertTrue(result['tasks_not_duplicated'])
            self.assertTrue(result['knowledge_index_restored'])
            self.assertTrue(result['rule_restore_verified'])
            self.assertTrue(result['prompt_restore_verified'])
            self.assertFalse(result['production_rpo_rto_verified'])
            work = Path(result['artifact_directory'])
            with closing(sqlite3.connect(work / 'restored/chat.sqlite3')) as database:
                self.assertEqual(database.execute('SELECT count(*) FROM tickets').fetchone()[0], 1)
                self.assertEqual(database.execute("SELECT count(*) FROM ticket_jobs WHERE status='done'").fetchone()[0], 2)
                self.assertEqual(database.execute('SELECT count(*) FROM agent_submissions').fetchone()[0], 1)
                material_id = database.execute('SELECT id FROM private_materials').fetchone()[0]
            self.assertEqual((work / f'restored/private-materials/{material_id}.bin').read_bytes(),
                             (Path(__file__).resolve().parents[1] / 'data/materials/fictional-identity.png').read_bytes())
            self.assertEqual(json.loads((work / 'report.json').read_text(encoding='utf-8')), result)

    def test_serve_resolves_application_factory_and_preserves_tls(self):
        namespace = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/operations.py'))
        main = namespace['main']
        with patch.dict(main.__globals__, release_preflight=lambda *a, **k: {}), \
             patch('sys.argv', ['operations.py', 'serve']), \
             patch.dict('os.environ', ZHENGWU_TLS_CERT='fictional-cert.pem', ZHENGWU_TLS_KEY='fictional-key.pem'), \
             patch('uvicorn.run') as launch:
            main()
        target = launch.call_args.args[0]
        module, attribute = target.split(':')
        self.assertTrue(callable(getattr(importlib.import_module(module), attribute, None)), target)
        self.assertTrue(launch.call_args.kwargs.get('factory'))
        self.assertEqual(launch.call_args.kwargs['ssl_certfile'], 'fictional-cert.pem')
        self.assertEqual(launch.call_args.kwargs['ssl_keyfile'], 'fictional-key.pem')
