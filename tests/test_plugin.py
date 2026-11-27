import asyncio
import importlib.util
import logging
import sys
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
# 按 MaiBot Runner 的包入口规则导入，覆盖带连字符的安装目录。
spec = importlib.util.spec_from_file_location(
    "bell_test_plugin", ROOT / "plugin.py", submodule_search_locations=[str(ROOT)]
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class TestPlugin(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.plugin = module.create_plugin()
        self.plugin._set_context(SimpleNamespace(logger=logging.getLogger("test_bell")))
        self.config = tomllib.loads((ROOT / "config.example.toml").read_text())
        self.plugin.set_plugin_config(self.config)
        self.addAsyncCleanup(self.plugin.on_unload)

    async def test_example_matches_sdk_and_default_is_idle(self):
        self.assertEqual(self.config, self.plugin.build_default_config())
        schema = self.plugin.build_config_schema()["sections"]
        secrets = {
            ("feishu", "webhook"),
            ("feishu", "secret"),
            ("dingtalk", "webhook"),
            ("dingtalk", "secret"),
            ("pushplus", "token"),
            ("serverchan", "sendkey"),
            ("bark", "device_key"),
        }
        for section_name, section in schema.items():
            for name, field in section["fields"].items():
                if section_name == "plugin" and name in {"enabled", "config_version"}:
                    continue  # WebUI 隐藏宿主保留的开关与版本号
                # WebUI 的标题取 label，缺省时直接显示英文字段名。
                self.assertNotEqual(field["label"], name, (section_name, name))
                widget = "password" if (section_name, name) in secrets else None
                self.assertEqual(
                    field["ui_type"] == "password", widget == "password", name
                )
                for item, item_field in (field.get("item_fields") or {}).items():
                    self.assertNotEqual(item_field["label"], item, item)
        await self.plugin.on_load()
        self.assertIsNone(self.plugin._task)

    async def test_reload_retains_incident_and_cancels_old_worker(self):
        self.config["monitor"]["adapters"][0]["enabled"] = True
        self.config["pushplus"]["token"] = "demo"
        self.plugin.set_plugin_config(self.config)
        sessions = []
        started = asyncio.Event()

        async def hold(session, settings, adapter, state, logger):
            sessions.append(session)
            started.set()
            await asyncio.Future()

        with patch.object(module, "check", side_effect=hold):
            await self.plugin.on_load()
            await asyncio.wait_for(started.wait(), 1)
            old_task = self.plugin._task
            state = next(iter(self.plugin._states.values()))
            status_type = type(state.status)
            state.status = status_type.OFFLINE
            state.delivered["pushplus"] = status_type.OFFLINE
            await self.plugin.on_config_update("model", {}, "")
            self.assertIs(self.plugin._task, old_task)
            started.clear()
            self.config["plugin"]["interval_seconds"] = 5
            self.plugin.set_plugin_config(self.config)
            await self.plugin.on_config_update("self", self.config, "v2")
            await asyncio.wait_for(started.wait(), 1)
            self.assertTrue(old_task.cancelled())
            self.assertTrue(sessions[0].closed)
            self.assertEqual(
                next(iter(self.plugin._states.values())).delivered,
                {"pushplus": status_type.OFFLINE},
            )
            self.config["pushplus"]["topic"] = "changed"
            self.plugin.set_plugin_config(self.config)
            await self.plugin.on_config_update("self", self.config, "v3")
            self.assertEqual(state.delivered, {})
            await self.plugin.on_unload()
            self.assertTrue(all(s.closed for s in sessions))
            self.assertIsNone(self.plugin._task)
