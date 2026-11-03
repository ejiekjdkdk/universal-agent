"""领取并续租持久化计划步骤，执行只读工具或经审批的本地模拟写入。"""
import asyncio
import logging
import os
from pathlib import Path
import socket
import time
from uuid import uuid4

from dotenv import load_dotenv

from .catalog import Catalog
from .approvals import mock_tool
from .errors import Fault
from .plans import resolve_arguments
from .runtime import Gateway, Runtime
from .snapshots import Snapshots
from .store import Store

LOG = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]


class PlanWorker:
    def __init__(self, runtime, snapshots):
        self.runtime = runtime
        self.store = runtime.store
        self.snapshots = snapshots
        self.owner = f"{socket.gethostname()}-{os.getpid()}-{uuid4().hex[:10]}"
        self.lease_seconds = float(os.getenv("AGENT_PLAN_LEASE_SECONDS", "12"))
        if not 1 <= self.lease_seconds <= 120:
            raise ValueError("AGENT_PLAN_LEASE_SECONDS must be between 1 and 120")

    async def execute(self, claim):
        try:
            remaining_seconds = max(0, claim["deadline"] - time.time())
            async with asyncio.timeout(remaining_seconds):
                catalog = self.snapshots.open(claim["snapshot_id"], claim["business_id"])
                gateway = Gateway(self.runtime, claim["run_context_id"], catalog=catalog, lease=claim)
                step = claim["step"]
                args = resolve_arguments(step, claim["outputs"])
                ledger_tool = mock_tool(catalog, claim["business_id"], step["capability_id"])
                if ledger_tool:
                    self.store.process_mock_write(self.runtime, claim, ledger_tool, args)
                    return
                result = await gateway.execute(step["capability_id"], args, "plan." + claim["step_id"])
                gateway.context()
                self.store.settle_step(claim, result=result)
        except asyncio.CancelledError:
            # 取消异常向上传递；其他 Worker 可在租约过期后按持久化状态接管步骤。
            raise
        except Exception as exc:
            if isinstance(exc, Fault):
                fault = exc
            elif isinstance(exc, TimeoutError):
                fault = Fault("TASK_TIMEOUT", "计划执行期限已到", 504)
            else:
                LOG.error("Plan worker error (%s) for task %s", type(exc).__name__, claim["task_id"])
                fault = Fault("INTERNAL_ERROR", "计划步骤执行失败", 500)
            self.store.settle_step(claim, error=fault.as_dict())

    async def leased(self, claim):
        job = asyncio.create_task(self.execute(claim))
        try:
            while not job.done():
                done, _ = await asyncio.wait({job}, timeout=self.lease_seconds / 3)
                if done:
                    break
                if not self.store.renew_step(claim, self.lease_seconds):
                    job.cancel()
                    break
            await asyncio.gather(job, return_exceptions=False)
        finally:
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)

    async def run(self):
        jobs = set()
        try:
            while True:
                while len(jobs) < 3:
                    claim = self.store.claim_step(self.owner, self.lease_seconds)
                    if not claim:
                        break
                    jobs.add(asyncio.create_task(self.leased(claim)))
                if not jobs:
                    await asyncio.sleep(.15)
                    continue
                done, jobs = await asyncio.wait(jobs, timeout=.15, return_when=asyncio.FIRST_COMPLETED)
                for job in done:
                    if not job.cancelled():
                        job.result()
        finally:
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)


async def main():
    from .identities import check_credentials, local_principals, role_source
    from .releases import Releases
    from .skills import SkillRegistry
    from .triggers import TriggerGateway
    load_dotenv(ROOT / ".env")
    database = Path(os.getenv("AGENT_DATABASE") or ROOT / "var" / "agent.db")
    catalog = Catalog(Path(os.getenv("AGENT_SCENARIOS") or ROOT / "scenarios"))
    store = Store(database, recover_reactive=False)
    try:
        actors = local_principals(catalog, "local-worker-identity")
        check_credentials(actors, os.getenv("AGENT_ADMIN_TOKEN"), os.getenv("AGENT_SERVICE_TOKEN"))
        rt = Runtime(catalog, store, actors, {})
        rt.role_source = role_source(catalog, "local-worker-identity", None,
                                     os.getenv("AGENT_ADMIN_TOKEN"), os.getenv("AGENT_SERVICE_TOKEN"))
        snapshots = Snapshots(database, catalog)
        rt.bind_releases(Releases(store, snapshots), snapshots)
        rt.skills = SkillRegistry(Path(os.getenv("AGENT_SKILL_ROOT") or database.parent / "skills"))
        rt.triggers = TriggerGateway(rt, snapshots)
        await PlanWorker(rt, snapshots).run()
    finally:
        store.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
