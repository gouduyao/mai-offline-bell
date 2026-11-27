# 麦麦掉线铃

定时查询 NapCat、SnowLuma 等 OneBot 11 客户端报告的 QQ 在线状态。QQ 掉线或客户端持续不可达时，通过 Server 酱、PushPlus、飞书、钉钉或 Bark 提醒号主。五个渠道可以单独使用，也可以同时开启。

## 安装与配置

1. 将安装包解压到 **MaiBot 实例的 `plugins` 目录**。解压后 `mai-offline-bell` 文件夹内应直接看到 `plugin.py` 和 `_manifest.json`。
2. 重启 MaiBot，打开 WebUI 的「麦麦掉线铃」插件设置。也可以在首次启动前，把插件目录中的 `config.example.toml` 复制为同目录的 `config.toml` 后编辑。
3. 在「监控项 → OneBot 账号」里启用一项，填入 QQ 客户端的 **OneBot 11 正向 WebSocket 地址**和访问令牌，与 OneBot 适配器配置里 `[client]` 的 `server`、`port`、`token` 保持一致。官方适配器默认连接 `ws://127.0.0.1:3001`。容器部署时填写从 MaiBot 容器可达的地址。监控多个账号时，每个账号单独加一项，并取不同的名称。[官方接入说明](https://docs.maibot.chat/manual/adapters/qq-local-client)
4. 至少配置下表中的一个渠道后保存。清空该渠道的令牌或 Webhook 即可关闭它。

| 渠道 | WebUI 分组 · 必填项 | 可选项 |
| --- | --- | --- |
| Server 酱 | 「Server 酱」· SendKey（`serverchan.sendkey`），支持 Turbo 的 `SCT…` 和 Server 酱³ 的 `sctp…` | 按 SendKey 自动选择接口 |
| PushPlus | 「PushPlus」· 用户令牌（`pushplus.token`） | 群组编码（`pushplus.topic`），留空只推送给自己 |
| 飞书群自定义机器人 | 「飞书群机器人」· Webhook 地址（`feishu.webhook`），使用 `open.feishu.cn` 的完整地址 | 启用签名校验时填写签名密钥（`feishu.secret`） |
| 钉钉群自定义机器人 | 「钉钉群机器人」· Webhook 地址（`dingtalk.webhook`），保留完整地址中的 `access_token` | 启用加签时填写签名密钥（`dingtalk.secret`） |
| Bark（iOS） | 「Bark」· Device Key（`bark.device_key`），可以直接粘贴 App 里的示例地址 `https://api.day.app/<Key>/…` | 自建服务器时修改服务器地址（`bark.server`，可带路径前缀；需要基本认证时写成 `https://用户:密码@主机`，该字段明文显示）。告警级别（`bark.level`）默认「时效性通知」，选「重要警告」时静音也会响铃 |

若群机器人设置了关键词，可填 **麦麦掉线铃**，所有通知的正文都含有这个词。签名使用运行 MaiBot 的机器时间，时间偏差过大时平台会拒收。

插件不申请任何 MaiBot 能力，不读取聊天记录，也不向任何聊天会话发消息。它只连接你填写的 OneBot 地址和上表各渠道的接口（Bark 为你配置的服务器）。通知内容只有机器人名称、监控项名称、状态、时间和探测详情，不含 QQ 号。

## 掉线与恢复

- **判定**　默认每轮结束后等 60 秒，每个监控项独立计数。插件每次临时连接一次 OneBot，发送带 echo 的 `get_status`，读取回包里的 `data.online`。连续两次明确返回 `false`，报「QQ 已掉线」。连续两次检查异常但没有明确的 `false`（连接失败、超时、鉴权失败、`online: null`，或几种情况混合），报「QQ 在线状态未知」。收到 `true` 后清零。[OneBot 11 接口定义](https://github.com/botuniverse/onebot-11/blob/master/api/public.md#get_status-获取运行状态)
- **常见掉线场景**　NapCat 账号被踢下线后，通常几秒内就会重启 Worker。重启期间端口无法连接，所以这种掉线多半表现为「QQ 在线状态未知」，详情显示「无法连接 OneBot」。已经报过「QQ 已掉线」时，客户端随后重启不会再追加一条「未知」。
- **推送与重试**　同一次掉线中，渠道确认受理后不再重复推送。遇到网络超时、HTTP 错误或平台返回错误码时，只在下一轮重试失败的渠道。如果平台已经受理但响应在途中丢失，重试可能产生重复消息。
- **恢复通知**　收到「QQ 已掉线」的渠道，恢复后收到「QQ 已恢复上线」。收到「状态未知」的渠道，恢复后收到「探测已恢复，QQ 在线」。没收到告警的渠道不会收到恢复通知。Bark 的恢复通知固定为普通级别，不会突破专注模式。可在「监控设置」中关闭恢复通知。投递记录只保存在内存中，插件重载或 MaiBot 重启后重新计数。
- **检测范围**　插件检查两件事：本插件能否连上 OneBot，以及 QQ 客户端自报的账号状态。MaiBot 的消息路由由官方适配器单独上报。比如适配器进入「仅身份查询」模式后，Host 会撤销收发路由，但 QQ 仍然在线，本插件此时依然显示在线。插件 SDK 目前没有查询路由状态的接口。MaiBot 自身或整机停机时，插件随之停止，需要外部监控。[MaiBot 消息网关](https://docs.maibot.chat/develop/plugin/message-gateway) · [官方 OneBot 适配器](https://github.com/Mai-with-u/MaiBot-SnowLuma-Adapter)

## 常见问题

- **一直报「QQ 在线状态未知」**　看通知里的详情：
  - 「无法连接 OneBot」：地址或端口不对，或者 QQ 客户端没有开启正向 WebSocket 服务器。
  - 「拒绝连接（HTTP 401/403）」或「代码 1008」：访问令牌或路径不对。
  - 「探测超时」：客户端卡住，或者网络不通。
- **收不到推送**　MaiBot 日志里搜「麦麦掉线铃」，推送失败时会记录渠道和平台错误码。钉钉 `310000` 是安全设置未通过（关键词、加签或 IP），飞书 `19021` 是签名或时间戳校验失败，PushPlus 返回非 `200` 时先核对令牌和群组编码。Bark 返回 `400` 多为 Device Key 无效，自建服务器返回 HTTP 418 是基本认证未通过。
- **通知太频繁或太慢**　调整「监控设置」里的检查间隔和连续失败次数。从掉线到收到提醒，最长约为 连续失败次数 ×（检查间隔 + 超时）。
- **监控项里的令牌明文显示**　WebUI 的列表编辑器不支持密码输入框。渠道凭据单独成组，以密码形式显示。

## 验收方法

在测试实例中，把检查间隔设为 5 秒，连续失败次数保持 2：

1. 确认账号在线，停止 QQ 客户端，保持 MaiBot 运行。约 10 秒后应收到「QQ 在线状态未知」，详情为「无法连接 OneBot」。
2. 重启客户端并登录，应收到「探测已恢复，QQ 在线」。
3. 把监控项的令牌改错，应收到「状态未知」，详情提示核对访问令牌。改回后恢复。
4. 在另一台设备上登录同一 QQ 号，把机器人挤下线。NapCat 通常几秒内重启 Worker，所以多半收到的是「状态未知」，这是预期行为。只有客户端保持可连接、`get_status` 又连续返回 `online: false` 时，才会报「QQ 已掉线」。

首次接入新的 QQ 客户端或升级客户端版本后，请留意启动后几分钟内有没有误报「QQ 已掉线」。NapCat 在第一次收到 QQ 状态回调之前，`get_status` 会返回 `online: false`。

## 开发与验证

本版针对 MaiBot 1.4.8、Plugin SDK 2.23.0 开发，宿主已经自带 aiohttp 依赖。单独运行测试时，在 Python 3.12 或更新的环境中执行：

```sh
python -m pip install 'maibot-plugin-sdk==2.23.0' -r requirements.txt
python -m unittest discover -s tests -v
```

测试用本地 OneBot WebSocket 服务和 HTTP 服务模拟五个渠道，覆盖签名、业务码、Bark 级别与自建服务器地址、部分渠道失败、去重、恢复、客户端重启、连接被拒、WebUI 字段渲染、配置热更新和任务取消。
