import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from uuid import uuid4

import aiohttp
from yarl import URL

from . import channels
from .config import Adapter, Settings


class Status(Enum):
    ONLINE = "QQ 在线"
    OFFLINE = "QQ 已掉线"
    UNKNOWN = "QQ 在线状态未知"


async def probe(
    session: aiohttp.ClientSession, adapter: Adapter, timeout: int
) -> tuple[Status, str]:
    """探测包含握手、发送、等待本次 echo 的一个总超时。"""
    try:
        url = URL(adapter.ws_url.strip())
        if url.scheme not in {"ws", "wss"} or not url.host:
            return Status.UNKNOWN, "请核对 OneBot 正向 WebSocket 地址"
        headers = {}
        if adapter.token:
            url = url.update_query(access_token=adapter.token)
            headers["Authorization"] = f"Bearer {adapter.token}"
        async with (
            asyncio.timeout(timeout),
            session.ws_connect(url, headers=headers) as ws,
        ):
            echo = uuid4().hex
            await ws.send_json({"action": "get_status", "params": {}, "echo": echo})
            async for frame in ws:
                if frame.type != aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    reply = frame.json()
                except ValueError:
                    continue
                if not isinstance(reply, dict) or reply.get("echo") != echo:
                    continue
                code = reply.get("retcode")
                if reply.get("status") != "ok" or type(code) is not int or code != 0:
                    return Status.UNKNOWN, "OneBot get_status 返回失败"
                data = reply.get("data")
                online = data.get("online") if isinstance(data, dict) else None
                if online is True:
                    return Status.ONLINE, "OneBot 报告 QQ 账号在线"
                if online is False:
                    return Status.OFFLINE, "OneBot 报告 QQ 账号离线"
                return Status.UNKNOWN, "OneBot 未给出确定的 data.online"
            if ws.close_code == 1008:
                # SnowLuma 在升级后才校验令牌与路径，用 1008 关闭连接。
                return (
                    Status.UNKNOWN,
                    "OneBot 拒绝连接（代码 1008，请核对访问令牌和路径）",
                )
            return Status.UNKNOWN, f"OneBot 关闭了连接（代码 {ws.close_code}）"
    except TimeoutError:
        return Status.UNKNOWN, "OneBot 状态探测超时"
    except aiohttp.WSServerHandshakeError as exc:
        if exc.status in {401, 403}:
            return (
                Status.UNKNOWN,
                f"OneBot 拒绝连接（HTTP {exc.status}，请核对访问令牌）",
            )
        return Status.UNKNOWN, f"WebSocket 握手失败（HTTP {exc.status}）"
    except aiohttp.ClientConnectorError:
        # NapCat 被踢下线后会重启 Worker，期间端口不可连接。
        return (
            Status.UNKNOWN,
            "无法连接 OneBot（QQ 客户端未运行、正在重启或地址有误）",
        )
    except (aiohttp.ClientError, OSError, ValueError) as exc:
        return Status.UNKNOWN, f"WebSocket 探测失败（{type(exc).__name__}）"


@dataclass
class Incident:
    failures: int = 0
    same_count: int = 0
    last_sample: Status = Status.ONLINE
    status: Status = Status.ONLINE
    delivered: dict[str, Status] = field(default_factory=dict)
    recovery_due: dict[str, Status] = field(default_factory=dict)

    def observe(self, sample: Status, settings: Settings) -> set[str]:
        self.same_count = self.same_count + 1 if sample is self.last_sample else 1
        self.last_sample = sample
        if sample is Status.ONLINE:
            self.failures = 0
            if self.status is not Status.ONLINE:
                self.recovery_due, self.delivered = self.delivered, {}
                self.status = Status.ONLINE
            if not settings.plugin.notify_recovery:
                self.recovery_due.clear()
            return set(self.recovery_due)

        self.failures += 1
        threshold = settings.plugin.failure_threshold
        if self.failures < threshold:
            return set()
        if self.same_count >= threshold and not (
            # 已确认掉线后客户端常会重启，探测随之变为不可达，不必再报一次「未知」。
            self.status is Status.OFFLINE and sample is Status.UNKNOWN
        ):
            self.status = sample
        elif self.status is Status.ONLINE:
            # 连续异常但原因不同也要告警，只有连续明确的 false 才判定账号掉线。
            self.status = Status.UNKNOWN
        # 本轮样本与告警状态不同也照常补发失败渠道，正文的详情取自本轮探测。
        self.recovery_due.clear()
        return {
            ch
            for ch in settings.destinations()
            if self.delivered.get(ch) is not self.status
        }


async def check(
    session: aiohttp.ClientSession,
    settings: Settings,
    adapter: Adapter,
    incident: Incident,
    logger: logging.Logger,
) -> None:
    sample, detail = await probe(session, adapter, settings.plugin.timeout_seconds)
    previous = incident.status
    pending = incident.observe(sample, settings)
    if previous is not incident.status:
        logger.info(
            "麦麦掉线铃 · %s %s（%s）", adapter.name, incident.status.value, detail
        )
    if not pending:
        return

    stamp = datetime.now().astimezone().isoformat(sep=" ", timespec="seconds")

    async def deliver(channel: str) -> None:
        status = incident.status.value
        if incident.status is Status.ONLINE:
            status = (
                "QQ 已恢复上线"
                if incident.recovery_due[channel] is Status.OFFLINE
                else "探测已恢复，QQ 在线"
            )
        title = f"【{settings.plugin.bot_name}】{adapter.name} {status}"
        body = f"麦麦掉线铃\n机器人：{settings.plugin.bot_name}\n适配器：{adapter.name}\n状态：{status}\n时间：{stamp}\n详情：{detail}"
        try:
            await channels.send(
                session,
                channel,
                settings,
                title,
                body,
                urgent=incident.status is not Status.ONLINE,
            )
        except channels.PushError as exc:
            logger.warning(
                "麦麦掉线铃 · %s %s推送失败（%s），下轮重试", channel, status, exc
            )
            return
        if incident.status is Status.ONLINE:
            incident.recovery_due.pop(channel, None)
        else:
            incident.delivered[channel] = incident.status

    await asyncio.gather(*(deliver(channel) for channel in pending))
