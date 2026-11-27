"""真实的本地 WS/HTTP 服务，覆盖请求格式、混合投递结果与断线恢复。"""

import logging
import unittest
from collections import Counter
from unittest.mock import patch

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer
from yarl import URL

from bell.channels import PushError, send
from bell.config import Adapter, Settings
from bell.monitor import Incident, Status, check, probe


class LoopbackTransport:
    """保留生产请求的 payload/path/query，只把目标服务器切到回环地址。"""

    def __init__(self, session, server):
        self.session = session
        self.server = server
        self.destinations = []

    def post(self, url, **kwargs):
        self.destinations.append(url)
        target = self.server.make_url("/" + url.host + url.raw_path)
        return self.session.post(target.with_query(url.query), **kwargs)

    def ws_connect(self, url, **kwargs):
        return self.session.ws_connect(url, **kwargs)


class TestBell(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.reply = {"status": "ok", "retcode": 0, "data": {"online": False}}
        self.pushes = []
        self.responses = {
            "sctapi.ftqq.com": {"code": 0},
            "12.push.ft07.com": {"code": 0},
            "www.pushplus.plus": {"code": 200},
            "open.feishu.cn": {"StatusCode": 0},
            "oapi.dingtalk.com": {"errcode": 0},
            "api.day.app": {"code": 200},
            "bark.example.com": {"code": 200},
        }
        self.http_status = 200
        self.settings = Settings()
        self.settings.serverchan.sendkey = "SCTdemo"
        self.settings.pushplus.token = "demo"
        self.settings.feishu.webhook = (
            "https://open.feishu.cn/open-apis/bot/v2/hook/demo"
        )
        self.settings.dingtalk.webhook = (
            "https://oapi.dingtalk.com/robot/send?access_token=demo"
        )
        app = web.Application()
        app.router.add_get("/onebot", self.handle_ws)
        app.router.add_get("/reject", self.handle_reject)
        app.router.add_post("/{host}/{path:.*}", self.handle_post)
        self.server = TestServer(app)
        await self.server.start_server()
        self.addAsyncCleanup(self.server.close)
        self.session = ClientSession()
        self.addAsyncCleanup(self.session.close)
        self.local = LoopbackTransport(self.session, self.server)
        self.adapter = Adapter(
            enabled=True,
            ws_url=str(self.server.make_url("/onebot").with_scheme("ws")),
            token="abc+&xyz",
        )
        self.incident = Incident()
        self.logger = logging.getLogger("test_bell")

    async def handle_ws(self, request):
        if (
            request.query.get("access_token") != "abc+&xyz"
            or request.headers.get("Authorization") != "Bearer abc+&xyz"
        ):
            raise web.HTTPUnauthorized()
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        packet = await ws.receive_json()
        self.assertEqual(packet["action"], "get_status")
        self.assertEqual(packet["params"], {})
        await ws.send_json({"echo": "unrelated", "data": {"online": True}})
        if self.reply is not None:
            await ws.send_json({**self.reply, "echo": packet["echo"]})
        else:
            await ws.receive()  # 由客户端的总超时结束这次探测
        await ws.close()
        return ws

    async def handle_reject(self, request):
        # 模拟 SnowLuma：升级完成后校验令牌，失败时以 1008 关闭。
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.close(code=1008, message=b"invalid access token")
        return ws

    async def handle_post(self, request):
        host = request.match_info["host"]
        body = await request.json()
        self.pushes.append((host, body))
        if self.responses[host] is None:
            return web.Response(text="not-json: secret")
        return web.json_response(self.responses[host], status=self.http_status)

    async def tick(self, online):
        self.reply["data"]["online"] = online
        await check(self.local, self.settings, self.adapter, self.incident, self.logger)

    async def test_each_channels_wire_format_and_signatures(self):
        self.settings.pushplus.topic = "operations"
        self.settings.feishu.secret = self.settings.dingtalk.secret = "abc"
        with (
            patch("bell.channels.time.time", return_value=1700000000),
            patch("bell.channels.time.time_ns", return_value=1700000000123000000),
        ):
            for name in self.settings.destinations():
                await send(self.local, name, self.settings, "标题", "正文")
        sc, pp, fs, dt = self.local.destinations
        self.assertEqual(str(sc), "https://sctapi.ftqq.com/SCTdemo.send")
        self.assertEqual(str(pp), "https://www.pushplus.plus/send")
        self.assertEqual(str(fs), self.settings.feishu.webhook)
        self.assertEqual(
            dt.with_query(None), URL("https://oapi.dingtalk.com/robot/send")
        )
        self.assertEqual(
            dict(dt.query),
            {
                "access_token": "demo",
                "timestamp": "1700000000123",
                "sign": "4rq25dbCvDvzPu78Q13usRT9KFIb3D4WJUgnt43DuI4=",
            },
        )
        data = dict(self.pushes)
        self.assertEqual(data[sc.host], {"title": "标题", "desp": "正文"})
        self.assertEqual(
            data[pp.host],
            {
                "token": "demo",
                "topic": "operations",
                "title": "标题",
                "content": "正文",
                "template": "txt",
            },
        )
        self.assertEqual(
            data[fs.host],
            {
                "msg_type": "text",
                "content": {"text": "标题\n正文"},
                "sign": "VIS10b0EBvzzSdFnuk4tznEmK5wHaruvf/WnViv2yR4=",
                "timestamp": "1700000000",
            },
        )
        self.assertEqual(
            data[dt.host], {"msgtype": "text", "text": {"content": "标题\n正文"}}
        )

    async def test_serverchan_3_and_unsigned_bots(self):
        self.settings.serverchan.sendkey = "sctp12tabc"
        for name in self.settings.destinations():
            await send(self.local, name, self.settings, "T", "B")
        self.assertEqual(
            str(self.local.destinations[0]),
            "https://12.push.ft07.com/send/sctp12tabc.send",
        )
        self.assertNotIn("sign", dict(self.pushes)["open.feishu.cn"])
        self.assertNotIn("sign", self.local.destinations[-1].query)
        self.assertNotIn("topic", dict(self.pushes)["www.pushplus.plus"])

    async def test_outage_debounce_partial_failure_recovery_and_new_incident(self):
        await self.tick(True)
        await self.tick(False)
        await self.tick(True)
        await self.tick(False)
        self.assertEqual(self.pushes, [])
        self.responses["oapi.dingtalk.com"] = {"errcode": 310000, "errmsg": "secret"}
        with self.assertLogs(self.logger, "WARNING") as logs:
            await self.tick(False)
        self.assertNotIn("secret", str(logs.output))
        self.responses["oapi.dingtalk.com"] = {"errcode": 0}
        await self.tick(False)
        await self.tick(False)
        self.assertEqual(
            sorted(Counter(host for host, _ in self.pushes).values()), [1, 1, 1, 2]
        )
        self.pushes.clear()
        self.responses["open.feishu.cn"] = {"code": 9499}
        with self.assertLogs(self.logger, "WARNING"):
            await self.tick(True)
        self.responses["open.feishu.cn"] = {"code": 0}
        await self.tick(True)
        await self.tick(True)
        self.assertEqual(len(self.pushes), 5)
        self.assertEqual(self.pushes[-1][0], "open.feishu.cn")
        self.assertTrue(all("恢复" in str(body) for _, body in self.pushes))
        await self.tick(False)
        await self.tick(False)
        self.assertEqual(len(self.pushes), 9)

    async def test_no_recovery_to_channel_that_missed_outage(self):
        self.settings.plugin.failure_threshold = 1
        self.responses["www.pushplus.plus"] = {"code": 401}
        with self.assertLogs(self.logger, "WARNING"):
            await self.tick(False)
        self.pushes.clear()
        await self.tick(True)
        self.assertEqual(len(self.pushes), 3)
        self.assertNotIn("www.pushplus.plus", dict(self.pushes))

    async def test_disabled_recovery(self):
        self.settings.plugin.notify_recovery = False
        await self.tick(False)
        await self.tick(False)
        await self.tick(True)
        await self.tick(True)
        self.assertEqual(len(self.pushes), 4)

    async def test_malformed_or_failed_onebot_reply(self):
        for reply in (
            {"status": "failed", "retcode": 1, "data": {"online": True}},
            {"status": "ok", "retcode": 0, "data": {"online": "false"}},
            {"status": "ok", "retcode": 0},
        ):
            self.reply = reply
            self.assertIs(
                (await probe(self.session, self.adapter, 1))[0], Status.UNKNOWN
            )
        self.adapter.token = "secret"
        online, detail = await probe(self.session, self.adapter, 1)
        self.assertIs(online, Status.UNKNOWN)
        self.assertIn("401", detail)
        self.assertNotIn("secret", detail)

    async def test_rejected_and_unreachable_onebot(self):
        self.adapter.ws_url = str(self.server.make_url("/reject").with_scheme("ws"))
        status, detail = await probe(self.session, self.adapter, 2)
        self.assertIs(status, Status.UNKNOWN)
        self.assertIn("1008", detail)
        self.assertNotIn("abc", detail)
        self.adapter.ws_url = "ws://127.0.0.1:1"
        status, detail = await probe(self.session, self.adapter, 2)
        self.assertIs(status, Status.UNKNOWN)
        self.assertIn("无法连接 OneBot", detail)

    async def test_confirmed_offline_survives_client_restart(self):
        self.responses["oapi.dingtalk.com"] = {"errcode": 310000}
        with self.assertLogs(self.logger, "WARNING"):
            await self.tick(False)
            await self.tick(False)
        self.assertEqual(len(self.pushes), 4)
        # NapCat 被踢后重启，探测变为不可达：只补发失败渠道，不追加「未知」告警。
        self.responses["oapi.dingtalk.com"] = {"errcode": 0}
        self.pushes.clear()
        for _ in range(3):
            await self.tick(None)
        self.assertEqual([host for host, _ in self.pushes], ["oapi.dingtalk.com"])
        self.assertIn("QQ 已掉线", str(self.pushes[0][1]))
        self.pushes.clear()
        await self.tick(True)
        self.assertEqual(len(self.pushes), 4)
        self.assertTrue(all("QQ 已恢复上线" in str(body) for _, body in self.pushes))

    async def test_unmatched_echo_has_total_timeout(self):
        self.reply = None
        online, detail = await probe(self.session, self.adapter, 1)
        self.assertIs(online, Status.UNKNOWN)
        self.assertIn("超时", detail)

    async def test_unknown_followed_by_distinct_probe_recovery(self):
        await self.tick(None)
        await self.tick(None)
        await self.tick(None)
        self.assertEqual(len(self.pushes), 4)
        self.assertTrue(all("在线状态未知" in str(body) for _, body in self.pushes))
        self.pushes.clear()
        self.reply["data"]["good"] = False
        await self.tick(True)
        self.assertEqual(len(self.pushes), 4)
        self.assertTrue(all("探测已恢复" in str(body) for _, body in self.pushes))

    async def test_mixed_failures_before_confirmed_offline(self):
        await self.tick(None)
        await self.tick(False)
        self.assertEqual(len(self.pushes), 4)
        self.assertTrue(all("在线状态未知" in str(body) for _, body in self.pushes))
        self.pushes.clear()
        await self.tick(False)
        await self.tick(False)
        self.assertEqual(len(self.pushes), 4)
        self.assertTrue(all("QQ 已掉线" in str(body) for _, body in self.pushes))
        self.pushes.clear()
        await self.tick(True)
        self.assertEqual(len(self.pushes), 4)
        self.assertTrue(all("QQ 已恢复上线" in str(body) for _, body in self.pushes))

    async def test_http_and_business_failures(self):
        for status, body in (
            (429, {"code": 200}),
            (302, {}),
            (200, {"code": 401}),
            (200, {}),
            (200, {"code": False}),
            (200, None),
        ):
            self.http_status = status
            self.responses["www.pushplus.plus"] = body
            with self.assertRaises(PushError) as caught:
                await send(self.local, "pushplus", self.settings, "T", "B")
            self.assertNotIn("secret", str(caught.exception))
        self.settings.feishu.webhook = "http://example.com/secret"
        with self.assertRaises(PushError):
            await send(self.local, "feishu", self.settings, "T", "B")

    async def bark(self, urgent=True):
        self.pushes.clear()
        self.local.destinations.clear()
        await send(self.local, "bark", self.settings, "T", "B", urgent=urgent)
        return self.local.destinations[-1], self.pushes[-1][1]

    async def test_bark_wire_format_levels_and_servers(self):
        self.settings.bark.device_key = "AbC123"
        url, body = await self.bark()
        self.assertEqual(str(url), "https://api.day.app/push")
        self.assertEqual(
            body,
            {
                "device_key": "AbC123",
                "title": "T",
                "body": "B",
                "group": "麦麦掉线铃",
                "level": "timeSensitive",
            },
        )
        self.settings.bark.level = "critical"
        self.assertEqual((await self.bark())[1]["level"], "critical")
        self.assertEqual((await self.bark(urgent=False))[1]["level"], "active")

        # App 中复制的示例地址：取第一段作 Key，忽略后面的推送内容和参数。
        self.settings.bark.device_key = "https://api.day.app/AbC123/示例内容?sound=x"
        url, body = await self.bark()
        self.assertEqual(str(url), "https://api.day.app/push")
        self.assertEqual(body["device_key"], "AbC123")

        # 自建服务器：保留端口、路径前缀和基本认证。
        self.settings.bark.device_key = "AbC123"
        self.settings.bark.server = "https://u:p@bark.example.com:8443/pre/"
        url, _ = await self.bark()
        self.assertEqual(str(url), "https://u:p@bark.example.com:8443/pre/push")

        for server, key in (
            ("ftp://bark.example.com", "AbC123"),
            ("bark.example.com", "AbC123"),
            ("https://bark.example.com/?x=1", "AbC123"),
            ("https://api.day.app", "bad key"),
            ("https://api.day.app", "https://api.day.app/"),
        ):
            self.settings.bark.server, self.settings.bark.device_key = server, key
            with self.assertRaises(PushError):
                await send(self.local, "bark", self.settings, "T", "B")

        self.settings.bark.server, self.settings.bark.device_key = "", "AbC123"
        self.responses["api.day.app"] = {"code": 400, "message": "AbC123 invalid"}
        with self.assertRaises(PushError) as caught:
            await send(self.local, "bark", self.settings, "T", "B")
        self.assertNotIn("AbC123", str(caught.exception))

    async def test_bark_alert_then_recovery_levels(self):
        self.settings.bark.device_key = "AbC123"
        await self.tick(False)
        await self.tick(False)
        self.assertEqual(len(self.pushes), 5)
        self.assertEqual(dict(self.pushes)["api.day.app"]["level"], "timeSensitive")
        self.pushes.clear()
        await self.tick(True)
        bark = dict(self.pushes)["api.day.app"]
        self.assertEqual(bark["level"], "active")
        self.assertIn("QQ 已恢复上线", bark["title"])
