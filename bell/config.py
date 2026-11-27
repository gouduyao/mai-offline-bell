from typing import Any, Literal

from maibot_sdk import Field, PluginConfigBase


def option(
    *default: Any,
    label: str,
    hint: str = "",
    choices: dict[str, str] | None = None,
    **kwargs: Any,
):
    """WebUI 只渲染 label 与 hint，description 留给 Schema 和代码阅读者。"""
    extra: dict[str, Any] = {"label": label}
    if hint:
        extra["hint"] = hint
    if choices:
        # WebUI 按 choice:<值> 查找选项名，任何界面语言都会回退到 zh-CN。
        extra["i18n"] = {"zh-CN": {f"choice:{k}": v for k, v in choices.items()}}
    return Field(*default, description=hint or label, json_schema_extra=extra, **kwargs)


def credential(label: str, hint: str = ""):
    extra = {"label": label, "x-widget": "password", "input_type": "password"}
    if hint:
        extra["hint"] = hint
    return Field(
        default="", repr=False, description=hint or label, json_schema_extra=extra
    )


class PluginOptions(PluginConfigBase):
    __ui_label__ = "监控设置"

    config_version: str = "1.0.0"
    enabled: bool = True
    bot_name: str = option(
        "麦麦",
        label="机器人名称",
        hint="写进通知标题，用于区分多个 MaiBot 实例",
        min_length=1,
        max_length=32,
    )
    interval_seconds: int = option(
        60, label="检查间隔（秒）", hint="每个监控项两次检查之间的等待时间", ge=1
    )
    timeout_seconds: int = option(
        10, label="超时（秒）", hint="单次探测或单次推送的总超时", ge=1, le=120
    )
    failure_threshold: int = option(
        2, label="连续失败次数", hint="连续几次检查异常后发送提醒", ge=1
    )
    notify_recovery: bool = option(
        True, label="发送恢复通知", hint="只发给已收到本次告警的渠道"
    )


class Adapter(PluginConfigBase):
    name: str = option("NapCat", label="名称", min_length=1, max_length=32)
    enabled: bool = option(False, label="启用")
    ws_url: str = option("ws://127.0.0.1:3001", label="OneBot 11 正向 WebSocket 地址")
    # 列表项编辑器不支持密码控件，令牌在此处以明文显示。
    token: str = option("", label="访问令牌（未开启鉴权留空）", repr=False)


class Monitor(PluginConfigBase):
    __ui_label__ = "监控项"
    adapters: list[Adapter] = option(
        label="OneBot 账号",
        hint="每个 QQ 账号一项。地址和令牌与 OneBot 适配器的 [client] 设置一致",
        default_factory=lambda: [Adapter()],
    )


class ServerChan(PluginConfigBase):
    __ui_label__ = "Server 酱"
    sendkey: str = credential(
        "SendKey", "支持 Turbo 版 SCT… 和 Server 酱³ 的 sctp…；留空即关闭"
    )


class PushPlus(PluginConfigBase):
    __ui_label__ = "PushPlus"
    token: str = credential("用户令牌", "留空即关闭")
    topic: str = option("", label="群组编码", hint="可选；留空则只推送给自己")


class Webhook(PluginConfigBase):
    webhook: str = credential("Webhook 地址", "自定义机器人的完整地址；留空即关闭")
    secret: str = credential("签名密钥", "机器人开启签名校验时填写")


class Feishu(Webhook):
    __ui_label__ = "飞书群机器人"


class DingTalk(Webhook):
    __ui_label__ = "钉钉群机器人"


class Bark(PluginConfigBase):
    __ui_label__ = "Bark"
    device_key: str = credential(
        "Device Key",
        "Bark App 首页的推送 Key，也可直接粘贴整条示例地址；留空即关闭",
    )
    server: str = option(
        "https://api.day.app",
        label="服务器地址",
        hint="使用自建 bark-server 时修改，可带路径前缀",
    )
    level: Literal["timeSensitive", "critical", "active", "passive"] = option(
        "timeSensitive",
        label="告警级别",
        hint="仅用于掉线和状态未知告警，恢复通知固定为普通级别",
        choices={
            "timeSensitive": "时效性通知（可突破专注模式）",
            "critical": "重要警告（静音也响铃，需在 App 中授权）",
            "active": "普通通知",
            "passive": "静默通知（只进通知列表）",
        },
    )


class Settings(PluginConfigBase):
    plugin: PluginOptions = Field(default_factory=PluginOptions)
    monitor: Monitor = Field(default_factory=Monitor)
    serverchan: ServerChan = Field(default_factory=ServerChan)
    pushplus: PushPlus = Field(default_factory=PushPlus)
    feishu: Feishu = Field(default_factory=Feishu)
    dingtalk: DingTalk = Field(default_factory=DingTalk)
    bark: Bark = Field(default_factory=Bark)

    def destinations(self) -> dict[str, str]:
        """已启用的渠道及其投递目标。目标变化时，热更新会丢弃该渠道的投递记录。"""
        targets = {
            "serverchan": (self.serverchan.sendkey,),
            "pushplus": (self.pushplus.token, self.pushplus.topic),
            "feishu": (self.feishu.webhook,),
            "dingtalk": (self.dingtalk.webhook,),
            "bark": (self.bark.device_key, self.bark.server),
        }
        return {
            name: "\n".join(part.strip() for part in parts)
            for name, parts in targets.items()
            if parts[0].strip()
        }
