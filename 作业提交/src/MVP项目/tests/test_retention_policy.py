"""获批分类策略只处置无业务引用的独立JSON运行记录。"""
import hashlib
import hmac
import json
from pathlib import Path
import tempfile
import unittest


class RetentionPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)
        self.records=self.root/'records'
        self.records.mkdir()
        self.key=b'test-only-fictional-signing-secret-32'
        self.policy={'version':'fictional-approved-v1','expires_at':1000000,'classes':{'evaluation':{'days':1,'action':'archive_delete'}}}
        body=json.dumps(self.policy,sort_keys=True,separators=(',',':')).encode()
        self.signed={'policy':self.policy,'signature':hmac.new(self.key,body,hashlib.sha256).hexdigest()}
        self.path=self.records/'fictional.json'
        self.path.write_text(json.dumps({'id':'e','category':'evaluation','created_at':0,'references':[],'standalone':True,'content':'虚构评价'}),encoding='utf-8')

    def run_disposal(self,**kwargs):
        from zhengwu_precheck.core.services.retention_policy import dispose
        options=dict(supervisor='verified-fixture',verify_supervisor=lambda actor:actor=='verified-fixture',
                     approved={'e':hashlib.sha256(self.path.read_bytes()).hexdigest()},
                     archive=self.root/'archive',audit=self.root/'audit.jsonl',now=100000,dry_run=True)
        options.update(kwargs)
        return dispose(self.records,self.signed,self.key,**options)

    def test_dry_run_no_deletion_and_execute_archives_audits_then_deletes(self):
        plan=self.run_disposal()
        self.assertEqual(plan[0]['action'],'archive_delete')
        self.assertTrue(self.path.exists())
        self.assertFalse((self.root/'archive').exists())
        self.run_disposal(dry_run=False)
        self.assertFalse(self.path.exists())
        self.assertEqual(len(list((self.root/'archive').glob('*.json'))),1)
        self.assertIn('fictional-approved-v1',(self.root/'audit.jsonl').read_text())

    def test_policy_tamper_and_unverified_supervisor_fail_closed(self):
        with self.assertRaises(PermissionError):
            self.run_disposal(verify_supervisor=lambda actor:False)
        self.signed['policy']['classes']['evaluation']['days']=0
        with self.assertRaises(ValueError):
            self.run_disposal()
        self.assertTrue(self.path.exists())

    def test_referenced_or_changed_records_not_deleted(self):
        content=json.loads(self.path.read_text())
        content['references']=['business:fictional']
        self.path.write_text(json.dumps(content))
        self.assertEqual(self.run_disposal()[0]['action'],'hold_linked_or_unapproved')
        self.assertTrue(self.path.exists())
        content['references']=[]
        self.path.write_text(json.dumps(content))
        self.assertEqual(self.run_disposal(approved={'e':'stale-hash'})[0]['action'],'hold_linked_or_unapproved')
