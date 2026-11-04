"""在单个事件循环内合并同权限的只读查询，缓存只保存已验证输出。"""
import asyncio
import json
import math
import time
from collections import OrderedDict

from .errors import Fault


class ReadCache:
    def __init__(self, settings):
        if not isinstance(settings, dict) or set(settings) - {"tools", "max_entries"}:
            raise ValueError("只读缓存配置字段无效")
        self.capacity = settings.get("max_entries", 128)
        if type(self.capacity) is not int or not 1 <= self.capacity <= 1024:
            raise ValueError("只读缓存容量应为1至1024")
        self.policies = settings.get("tools", {})
        if not isinstance(self.policies, dict) or len(self.policies) > 128:
            raise ValueError("只读缓存工具配置无效")
        for name, policy in self.policies.items():
            if not isinstance(name, str) or len(name) > 256 or not isinstance(policy, dict):
                raise ValueError("只读缓存工具配置无效")
            if set(policy) != {"ttl_s", "freshness"}:
                raise ValueError("只读缓存必须声明TTL及数据版本")
            ttl = policy["ttl_s"]
            if type(ttl) not in (int, float) or not math.isfinite(ttl) or not 0 < ttl <= 60:
                raise ValueError("只读缓存TTL应大于0且不超过60秒")
            if not isinstance(policy["freshness"], str) or not 0 < len(policy["freshness"]) <= 256:
                raise ValueError("只读缓存数据版本无效")
        self.entries = OrderedDict()
        self.flights = {}

    async def run(self, key, ttl, participant, operation):
        """每个等待者独立鉴权；同键查询共享物理执行，但不共享取消和返回对象。"""
        participant["guard"]()
        now = time.monotonic()
        for entry_key in list(self.entries):
            expires_at, _ = self.entries[entry_key]
            if expires_at <= now:
                del self.entries[entry_key]
        if key in self.entries:
            self.entries.move_to_end(key)
            return json.loads(self.entries[key][1])

        flight = self.flights.get(key)
        if flight is None:
            if len(self.flights) >= self.capacity:
                raise Fault("READ_CACHE_BUSY", "只读查询队列已满", 429)
            flight = {"participants": {}}
            self.flights[key] = flight

            def select_participant(charge=False):
                # 发起者退出后，从剩余等待者中选择仍获授权的预算承担者。
                last_error = None
                for candidate in list(flight["participants"].values()):
                    if "error" in candidate:
                        last_error = candidate["error"]
                        continue
                    try:
                        candidate["guard"]()
                        if charge:
                            candidate["charge"]()
                        return candidate["value"]
                    except Fault as exc:
                        candidate["error"] = exc
                        last_error = exc
                if last_error is not None:
                    raise last_error
                raise asyncio.CancelledError()

            async def execute_shared_query():
                try:
                    output = await operation(select_participant)
                    select_participant()
                    encoded = json.dumps(output, ensure_ascii=False, allow_nan=False).encode("utf-8")
                    # 超大输出仍完整返回，只是不留在进程共享缓存中。
                    if len(encoded) <= 1048576:
                        self.entries[key] = (time.monotonic() + ttl, encoded)
                        while len(self.entries) > self.capacity:
                            self.entries.popitem(last=False)
                    return output
                finally:
                    if self.flights.get(key) is flight:
                        self.flights.pop(key, None)

            flight["task"] = asyncio.create_task(execute_shared_query())
        participant_token = object()
        flight["participants"][participant_token] = participant
        shared_task = flight["task"]
        try:
            while not shared_task.done():
                if "error" in participant:
                    raise participant["error"]
                participant["guard"]()
                # wait不会把当前等待者的取消传播给共享查询。
                await asyncio.wait({shared_task}, timeout=0.05)
            if "error" in participant:
                raise participant["error"]
            participant["guard"]()
            # JSON往返使等待者不共享可变对象；执行ID由外层任务单独生成。
            serialized = json.dumps(shared_task.result(), ensure_ascii=False, allow_nan=False)
            return json.loads(serialized)
        finally:
            flight["participants"].pop(participant_token, None)
            # 只由最后一个离开的等待者回收查询，并等待资源释放完成。
            if not flight["participants"]:
                if not shared_task.done():
                    shared_task.cancel()
                await asyncio.gather(shared_task, return_exceptions=True)

    async def close(self):
        tasks = [flight["task"] for flight in self.flights.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.entries.clear()
