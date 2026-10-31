import asyncio
import multiprocessing
from pathlib import Path
import tempfile
import unittest
from sqlalchemy.ext.asyncio import create_async_engine
from zhengwu_precheck.core.errors import AppError


def rate_worker(path,queue):
    from zhengwu_precheck.infrastructure.request_limiter import SharedRateLimiter
    async def run():
        engine=create_async_engine('sqlite+aiosqlite:///'+path,connect_args={'timeout':20})
        try:
            await SharedRateLimiter(engine).check('fictional-ip',count=3,window=600)
            queue.put('accepted')
        except AppError as error:
            queue.put(error.code)
        finally:
            await engine.dispose()
    asyncio.run(run())


class SharedRateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=(Path(self.temp.name)/'rate.db').as_posix()

    def test_exact_expiry_and_safe_error(self):
        from zhengwu_precheck.infrastructure.request_limiter import SharedRateLimiter
        async def run():
            engine=create_async_engine('sqlite+aiosqlite:///'+self.path)
            now=[1000]
            limiter=SharedRateLimiter(engine,clock=lambda:now[0])
            await limiter.initialize()
            await limiter.check('fictional',count=1,window=600)
            now[0]=1599.999
            with self.assertRaises(AppError) as caught:
                await limiter.check('fictional',count=1,window=600)
            self.assertEqual(caught.exception.code,'RATE_LIMITED')
            self.assertEqual(caught.exception.status,429)
            self.assertTrue(caught.exception.retryable)
            now[0]=1600
            await limiter.check('fictional',count=1,window=600)
            await engine.dispose()
        asyncio.run(run())

    def test_six_processes_share_three_admissions(self):
        from zhengwu_precheck.infrastructure.request_limiter import SharedRateLimiter
        async def initialize():
            engine=create_async_engine('sqlite+aiosqlite:///'+self.path)
            await SharedRateLimiter(engine).initialize()
            await engine.dispose()
        asyncio.run(initialize())
        ctx=multiprocessing.get_context('spawn')
        queue=ctx.Queue()
        workers=[ctx.Process(target=rate_worker,args=(self.path,queue)) for _ in range(6)]
        for worker in workers:worker.start()
        for worker in workers:
            worker.join(20)
            if worker.is_alive():
                worker.terminate()
                worker.join()
            self.assertEqual(worker.exitcode,0)
        results=[queue.get(timeout=5) for _ in workers]
        queue.close()
        self.assertEqual(results.count('accepted'),3)
        self.assertEqual(results.count('RATE_LIMITED'),3)
