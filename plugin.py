"""麦麦掉线铃的 MaiBot 插件入口。"""

import asyncio
from contextlib import suppress
from typing import cast

import aiohttp
from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF, MaiBotPlugin

from .bell.config import Adapter, Settings
from .bell.monitor import Incident, check


class MaiOfflineBell(MaiBotPlugin):
    config_model = Settings

    def __init__(self) -> None:
        super().__init__()
        self._task: asyncio.Task | None = None
        self._states: dict[tuple[str, str], Incident] = {}
        self._settings: Settings | None = None
        self._loaded = False
        self._lock = asyncio.Lock()

    async def on_load(self) -> None:
        async with self._lock:
            self._loaded = True
            await self._configure()

    async def on_config_update(
        self, scope: str, config_data: dict, version: str
    ) -> None:
        async with self._lock:
            if self._loaded and scope == CONFIG_RELOAD_SCOPE_SELF:
                await self._configure()

    async def on_unload(self) -> None:
        async with self._lock:
            self._loaded = False
            await self._stop()
            self._settings = None
            self._states.clear()

    async def _stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _configure(self) -> None:
        await self._stop()
        settings = cast(Settings, self.config)
        previous, self._settings = self._settings, settings
        targets = {
            (a.name, a.ws_url): a for a in settings.monitor.adapters if a.enabled
        }
        destinations = settings.destinations()
        if not settings.plugin.enabled or not targets or not destinations:
            self._states.clear()
            self.ctx.logger.info(
                "麦麦掉线铃已待命。启用监控需要至少一个适配器和一个推送渠道。"
            )
            return

        self._states = {key: self._states.get(key, Incident()) for key in targets}
        # 配置热更新时只沿用目标未变的投递记录（PushPlus 群组、Bark 服务器也算目标）。
        unchanged = set()
        if previous:
            old = previous.destinations()
            unchanged = {
                name for name in destinations if old.get(name) == destinations[name]
            }
        for state in self._states.values():
            for recipients in (state.delivered, state.recovery_due):
                for channel in recipients.keys() - unchanged:
                    del recipients[channel]

        self._task = asyncio.create_task(
            self._run(settings, targets), name="mai-offline-bell"
        )
        self.ctx.logger.info(
            "麦麦掉线铃监控 %d 个适配器，渠道 %s", len(targets), ", ".join(destinations)
        )

    async def _run(
        self, settings: Settings, targets: dict[tuple[str, str], Adapter]
    ) -> None:
        timeout = aiohttp.ClientTimeout(total=settings.plugin.timeout_seconds)
        async with aiohttp.ClientSession(
            timeout=timeout, cookie_jar=aiohttp.DummyCookieJar()
        ) as session:

            async def watch(adapter: Adapter, state: Incident) -> None:
                while True:
                    try:
                        await check(session, settings, adapter, state, self.ctx.logger)
                    except Exception as exc:  # noqa: BLE001
                        # 保持其它适配器及后续轮次运行。URL 和服务端响应可能含有密钥。
                        self.ctx.logger.error(
                            "麦麦掉线铃本轮检查异常（%s）", type(exc).__name__
                        )
                    await asyncio.sleep(settings.plugin.interval_seconds)

            async with asyncio.TaskGroup() as group:
                for key, adapter in targets.items():
                    group.create_task(watch(adapter, self._states[key]))


def create_plugin() -> MaiOfflineBell:
    return MaiOfflineBell()
