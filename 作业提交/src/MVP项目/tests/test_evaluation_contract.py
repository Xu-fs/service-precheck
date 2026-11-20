"""冻结评测合同与本地依据校验；不是30条真实模型端到端评测。"""
import json
from collections import Counter
from pathlib import Path
import unittest
import asyncio

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT/'tests/fixtures/project-evaluation-v1.json'
TOOLS = {'search_guidelines','recommend_matter','check_materials','prepare_ticket_draft','create_manual_ticket','get_ticket_status'}


class EvaluationContractTests(unittest.TestCase):
    def load(self):
        return json.loads(DATASET.read_text(encoding='utf-8'))

    def test_exact_counts_schema_and_only_six_ordered_tool_names(self):
        cases=self.load()
        self.assertEqual(len(cases),30)
        self.assertEqual(Counter(case['category'] for case in cases),{'指南':10,'工具':10,'违规':5,'多轮':5})
        for case in cases:
            self.assertEqual(set(case),{'question','expected_tools','expected_behavior','category'})
            self.assertIsInstance(case['expected_tools'],list)
            self.assertTrue(set(case['expected_tools'])<=TOOLS)
            self.assertIsInstance(case['expected_behavior'],str)
            self.assertGreater(len(case['expected_behavior']),10)
            if case['category']=='多轮':
                self.assertIsInstance(case['question'],list)
                self.assertGreaterEqual(len(case['question']),2)
                self.assertTrue(all(isinstance(item,str) and item for item in case['question']))
            else:
                self.assertIsInstance(case['question'],str)

    def test_traps_and_order_sensitive_confirmation_and_submission_contract(self):
        cases=self.load()
        self.assertEqual(sum('陷阱' in c['expected_behavior'] for c in cases[:10]),3)
        text='\n'.join(c['expected_behavior'] for c in cases)
        for requirement in ['历史有效','核验依据','版本冲突','关键系统','未登录','未经本人确认','草稿修改','幂等','DEMO-2026-02']:
            self.assertIn(requirement,text)
        full=[c for c in cases if c['expected_tools']==['search_guidelines','recommend_matter','check_materials','prepare_ticket_draft','create_manual_ticket','get_ticket_status']]
        self.assertEqual(len(full),1)
        for c in cases:
            if 'create_manual_ticket' in c['expected_tools'] and 'prepare_ticket_draft' in c['expected_tools']:
                self.assertLess(c['expected_tools'].index('prepare_ticket_draft'),c['expected_tools'].index('create_manual_ticket'))

    def test_real_baseline_validator_rejects_fabricated_trap_answers_without_network(self):
        from zhengwu_precheck.core import guide_rag as rag
        chunks=rag.load_chunks(ROOT/'data/居住证申领_示例指南.md')
        self.assertTrue(all(c.version=='DEMO-2026-02' and c.matter=='居住证申领' for c in chunks))
        hits=[rag.Hit(chunk,1,1,1) for chunk in chunks]
        traps=[c for c in self.load()[:10] if '陷阱' in c['expected_behavior']]
        fabricated=['办理费用为10元。','办理期限为3个工作日。','个体工商户登记必须提供营业场所产权证。']
        for case,quote in zip(traps,fabricated):
            with self.subTest(question=case['question']):
                raw=json.dumps({'supported':True,'citations':[{'chunk_id':chunks[0].chunk_id,'quote':quote}]})
                self.assertEqual(rag.validate_answer(raw,hits),rag.NO_EVIDENCE)
                self.assertEqual(rag.validate_answer(json.dumps({'supported':False,'citations':[]}),hits),rag.NO_EVIDENCE)

    def test_full_chain_dispatch_with_explicitly_simulated_handlers(self):
        from zhengwu_precheck.core.services.agent_tools import dispatch
        case=next(c for c in self.load() if len(c['expected_tools'])==6)
        arguments={
            'search_guidelines':dict(query='虚构租赁示例',jurisdiction_code='teaching'),
            'recommend_matter':dict(case_id='fictional',intent='示例咨询',search_result_id='simulated-search'),
            'check_materials':dict(case_id='fictional',selection_confirmation_id='simulated-human-selection',rule_snapshot_id='simulated-rule',material_set_id='simulated-materials',expected_case_version=1),
            'prepare_ticket_draft':dict(case_id='fictional',purpose='ordinary_consultation',summary='虚构咨询',expected_case_version=1),
            'create_manual_ticket':dict(draft_id='simulated-draft',draft_version=1,submission_confirmation_id='simulated-human-submit',idempotency_key='fictional-stable-key'),
            'get_ticket_status':dict(ticket_id='simulated-ticket')}
        async def run():
            trace=[]
            def handler_for(name):
                async def simulated_handler(args,principal):
                    self.assertEqual(principal['user_id'],'fictional-server-user')
                    trace.append(name)
                    return {'simulation_only':True}
                return simulated_handler
            handlers={name:handler_for(name) for name in TOOLS}
            for name in case['expected_tools']:
                result=await dispatch(name,arguments[name],{'user_id':'fictional-server-user'},handlers=handlers)
                self.assertEqual(result['status'],'ok')
                self.assertIs(result['data']['simulation_only'],True)
            self.assertEqual(trace,case['expected_tools'])
        asyncio.run(run())
