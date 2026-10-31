import sys
from pathlib import Path
import tempfile
import unittest
import httpx
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from zhengwu_precheck.infrastructure.database import ChatRepository
from zhengwu_precheck.infrastructure.quota_repository import QuotaRepository,QuotaLimits
from zhengwu_precheck.core.errors import AppError


class ModelBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_service_and_operator_usage_are_recorded_separately(self):
        from zhengwu_precheck.core.services.model_budget import ModelBudget, model_principal
        with tempfile.TemporaryDirectory() as directory:
            repo=ChatRepository(Path(directory)/'budget.db')
            await repo.initialize()
            quota=QuotaRepository(repo.engine)
            await quota.initialize()
            budget=ModelBudget(quota,QuotaLimits(10,10000,2))
            identity=model_principal.set('fictional-operator')
            try:
                async with httpx.AsyncClient(transport=httpx.MockTransport(
                    lambda request: httpx.Response(200,json={'usage':{'total_tokens':2}}))) as client:
                    await budget.send(client,'http://localhost/v1/embeddings',{'input':['教学资料']},{})
                    await budget.send(client,'http://localhost/v1/chat/completions',{'messages':[]},{})
                self.assertEqual((await quota.usage('model-service:embedding'))['requests'],1)
                self.assertEqual((await quota.usage('model-service:llm'))['requests'],1)
                self.assertEqual((await quota.usage('model-owner:fictional-operator'))['requests'],2)
                self.assertEqual((await quota.usage('model-owner-service:fictional-operator:embedding'))['tokens'],2)
            finally:
                model_principal.reset(identity)
                await repo.close()

    async def test_llm_and_embedding_share_budget_and_payload_is_minimized(self):
        from zhengwu_precheck.core.services.model_budget import ModelBudget
        with tempfile.TemporaryDirectory() as directory:
            repo=ChatRepository(Path(directory)/'budget.db')
            await repo.initialize()
            quota=QuotaRepository(repo.engine)
            await quota.initialize()
            seen=[]
            def server(request):
                seen.append(request.content.decode())
                return httpx.Response(200,json={'usage':{'total_tokens':2},'ok':True})
            async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
                budget=ModelBudget(quota,QuotaLimits(1,10000,1))
                await budget.send(client,'http://localhost/v1/chat/completions',{'messages':[{'content':'电话13812345678'}],'max_tokens':1024},{})
                self.assertNotIn('13812345678',seen[0])
                self.assertIn('1024',seen[0])
                with self.assertRaises(AppError) as error:
                    await budget.send(client,'http://localhost/v1/embeddings',{'input':['教学资料']},{})
                self.assertEqual(error.exception.code,'MODEL_QUOTA_EXCEEDED')
                self.assertEqual(len(seen),1)
            await repo.close()
