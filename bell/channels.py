"""四个渠道的出站协议。异常内容可以直接写日志，避免暴露 URL 或响应正文。"""

import base64
import hashlib
import hmac
import re
import time

import aiohttp
from yarl import URL

from .config import Settings


class PushError(Exception):
    """仅包含可公开的诊断信息。"""


def _signature(key: str, message: str) -> str:
    digest = hmac.new(key.encode(), message.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def _webhook(value: str, host: str, path: str) -> URL:
    url = URL(value.strip())
    if (
        url.scheme != "https"
        or url.host != host
        or url.port != 443
        or url.user is not None
        or url.fragment
        or not re.fullmatch(path, url.path)
    ):
        raise PushError("请核对自定义机器人的 HTTPS Webhook 地址")
    return url


def _bark(settings: Settings) -> tuple[URL, str]:
    """返回 /push 接口地址和 Device Key。"""
    key = settings.bark.device_key.strip()
    server = URL(settings.bark.server.strip() or "https://api.day.app")
    if key.lower().startswith(("http://", "https://")):
        # App 里复制的示例地址形如 https://api.day.app/<key>/推送内容。
        pasted = URL(key)
        segments = [part for part in pasted.path.split("/") if part]
        key = segments[0] if segments else ""
        server = pasted.with_path("/").with_query(None).with_fragment(None)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", key):
        raise PushError("请核对 Bark Device Key")
    if server.scheme not in {"http", "https"} or not server.host or server.query_string:
        raise PushError("请核对 Bark 服务器地址")
    push = server.with_fragment(None).with_path(server.path.rstrip("/") + "/push")
    return push, key


def _request(
    channel: str, settings: Settings, title: str, body: str, urgent: bool
) -> tuple[URL, dict, tuple[str, ...], int]:
    text = f"{title}\n{body}"
    if channel == "serverchan":
        key = settings.serverchan.sendkey.strip()
        if match := re.fullmatch(r"sctp([0-9]+)t[A-Za-z0-9_-]+", key):
            url = URL(f"https://{match[1]}.push.ft07.com/send/{key}.send")
        elif re.fullmatch(r"SCT[A-Za-z0-9_-]+", key, flags=re.IGNORECASE):
            url = URL(f"https://sctapi.ftqq.com/{key}.send")
        else:
            raise PushError("请核对 Server 酱 SendKey 格式")
        return url, {"title": title[:32], "desp": body}, ("code",), 0

    if channel == "pushplus":
        data = {
            "token": settings.pushplus.token.strip(),
            "title": title,
            "content": body,
            "template": "txt",
        }
        if topic := settings.pushplus.topic.strip():
            data["topic"] = topic
        return URL("https://www.pushplus.plus/send"), data, ("code",), 200

    if channel == "feishu":
        url = _webhook(
            settings.feishu.webhook,
            "open.feishu.cn",
            r"/open-apis/bot/v2/hook/[A-Za-z0-9_-]+",
        )
        data = {"msg_type": "text", "content": {"text": text}}
        if secret := settings.feishu.secret.strip():
            stamp = str(int(time.time()))
            # 飞书将时间戳和密钥一起作为 HMAC 的 key，消息体为空。
            data.update(timestamp=stamp, sign=_signature(f"{stamp}\n{secret}", ""))
        return url, data, ("code", "StatusCode"), 0

    if channel == "dingtalk":
        url = _webhook(settings.dingtalk.webhook, "oapi.dingtalk.com", r"/robot/send")
        if not url.query.get("access_token"):
            raise PushError("钉钉 Webhook 缺少 access_token")
        if secret := settings.dingtalk.secret.strip():
            stamp = str(time.time_ns() // 1_000_000)
            url = url.update_query(
                timestamp=stamp, sign=_signature(secret, f"{stamp}\n{secret}")
            )
        return url, {"msgtype": "text", "text": {"content": text}}, ("errcode",), 0

    if channel == "bark":
        url, key = _bark(settings)
        data = {
            "device_key": key,
            "title": title,
            "body": body,
            "group": "麦麦掉线铃",
            "level": settings.bark.level if urgent else "active",
        }
        return url, data, ("code",), 200

    raise PushError("未知推送渠道")


async def send(
    session: aiohttp.ClientSession,
    channel: str,
    settings: Settings,
    title: str,
    body: str,
    *,
    urgent: bool = True,
) -> None:
    """urgent 为 False 表示恢复通知，Bark 会降为普通级别。"""
    try:
        url, data, code_fields, success = _request(
            channel, settings, title, body, urgent
        )
        async with session.post(url, json=data, allow_redirects=False) as response:
            if not 200 <= response.status < 300:
                raise PushError(f"HTTP {response.status}")
            result = await response.json(content_type=None)
    except (aiohttp.ClientError, OSError, ValueError) as exc:
        raise PushError(f"请求或响应异常（{type(exc).__name__}）") from None

    if not isinstance(result, dict):
        raise PushError("响应不是 JSON 对象")
    code = next((result[key] for key in code_fields if key in result), None)
    if type(code) is not int or code != success:
        # 平台返回的 errmsg、URL 或其他字符串都可能包含令牌。
        reason = f"业务码 {code}" if type(code) is int else "缺少有效的业务码"
        raise PushError(reason)
