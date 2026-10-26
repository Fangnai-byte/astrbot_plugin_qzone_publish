# Qzone Publish（QQ空间说说）

![logo](logo.png)

让机器人自己发说说：`/说说 晚风溜进部室……`，还能带图；也可以让机器人在对话里自己决定发一条。

## 用法

| 命令 | 说明 |
| --- | --- |
| `/说说 内容` | 发一条说说（别名 `/发说说`、`/qzone`） |
| `/检查说说登录` | 检查 cookie 是否够用、目标 QQ 是哪个 |

## 让机器人自己发（LLM 工具）

开启 `tool_enabled` 后，插件会注册 LLM 工具 `qzone_publish_say`。机器人在对话里想发说说时，自己调用它即可，不需要任何人敲命令。

- `content`：说说正文。
- `image_urls`：配图，http 直链或本地路径，多张用英文逗号隔开。
- 是否真发同样受 `dry_run`、`allow_user_ids`、`enable_image`、长度上限约束。

另外还有「加载即发一条」的自检通道：`oneshot_on_load` + `oneshot_content`，靠 flag 文件去重只跑一次，结果写到 `oneshot_result_path`。主要用来验证部署是否正常，平时保持关闭。

## 带图

三种取图方式，可以混用，单条说说最多 9 张：

1. **跟图一起发**：图片 + `/说说 文字` 同一条消息里发出。
2. **引用带图的消息**：`/说说 文字` 的同时引用一条图片消息；引用**合并转发聊天记录**也行，会自动把里面所有图片都取出来。
3. **文字里写图源**：直接写图片直链或本地绝对路径，例如 `/说说 今天的天空 ~/pic.jpg https://example.com/a.png`。

图片会先上传到 QQ 空间相册，再用 richval 拼进说说正文，顺序与消息里出现的顺序一致。想关掉带图，把 `enable_image` 设为 `false`。

## 配置

- `enabled`：总开关。
- `dry_run`：**默认开**。开着的时候只校验和打印，不会真发；确认流程没问题后再关掉。
- `enable_image`：是否允许带图。
- `cookies`：**完整的** cookie 串，形如 `p_skey=xxx; skey=xxx; uin=o123456`（从浏览器 F12 或协议端 `get_cookies` 取）。只贴值会被当成空。
- `uin`：要发说说的 QQ 号；留空则读 cookie 里的 `uin`。
- `auto_fetch_cookie`：先向协议端（SnowLuma 等 OneBot 实现）请求 `get_cookies`，失败再用 `cookies`。
  插件按 `qzone.qq.com → user.qzone.qq.com → qq.com` 的顺序试；**前一个之外的域名拿不到 p_skey 是正常现象**
  （QQ 的 pskey 白名单里只有 `qzone.qq.com`），日志里的 `跳过 user.qzone.qq.com` 不是故障。
- `allow_user_ids`：允许用命令的 QQ 号，留空=所有人。
- `max_length` / `timeout_sec`：内容长度上限和请求超时。
- `tool_enabled`：是否注册 LLM 工具 `qzone_publish_say`，让机器人能自主发说说。
- `oneshot_on_load` / `oneshot_delay` / `oneshot_content`：加载后延迟自动发一条（一次性，flag 去重），平时保持关闭。
- `oneshot_flag_path` / `oneshot_result_path`：上面的去重标记与结果文件路径。
- `daily_at`：**每天定点发说说**，填 `HH:MM`（可多个，如 `20:00`、`08:30`）。每个时刻当天只发一次；留空=不定时。
- `daily_content`：固定正文。**留空就由模型现写**，这样每天内容都不一样。
- `daily_prompt`：让模型写说说用的提示词，支持 `{date}` / `{time}` / `{weekday}` 占位符。
- `daily_fallback`：模型超时、报错、返回空时的兜底文案；留空则这一次直接跳过。
- `daily_context_groups` / `daily_context_friends`：给模型当素材的**群号 / 私聊 QQ 号**列表。
  插件自己记这些会话的「谁说了什么」（`data/plugin_data/astrbot_plugin_qzone_publish/context.json`），
  **不依赖任何别的插件**；私聊刚装上还没攒到记录时，会回落到 AstrBot 会话历史（`data/data_v4.db`）。
- `daily_record` / `daily_record_keep_days` / `daily_record_per_session`：是否记录、保留几天、每会话最多多少条。
- `daily_context_file` / `daily_context_db`：素材文件与会话库路径，一般留空。
- `daily_context_paths`：**外部聊天记录**（可选）。每项可以是文件、目录或通配符，插件按时间取最新的那个，
  把最后若干行**原样**当素材——不解析格式，别的插件导出的、自己手动存的都能直接指。
  例：`data/workspaces/group_logs/astrbot_1108558028_*.log`。
- `daily_context_lines` / `daily_context_max_chars`：取多少条、上下文总共多少字（默认 40 条 / 1500 字）。
- `daily_per_bot`：**每个 bot 各发一条**（默认开）。遍历所有启用的 aiocqhttp 实例，各用各的 cookie/uin、
  各取自己的会话上下文；去重按 bot 分开。关掉则用全局 `uin`/`cookies` 只发一条。
- `daily_prompt_map`：按 bot 覆盖提示词，每行 `平台id,提示词`（平台 id 是面板里那个 bot 的名字）。

> 模型走 AstrBot 当前默认的对话模型（`context.get_using_provider()`）。没有可用模型时只会发 `daily_fallback`。
> 带上下文的写法是「把这些聊天当素材，别逐条复述」，模型仍会按 `daily_prompt` 的风格要求落笔。
> 素材**原样**喂给模型：只去掉 AstrBot 核心的 `<system_reminder>` 注入块与引用标记，
> 别的插件 / persona 加的标记（`[[ja]]`、`【档案】` 等）一概不动——那是各家自己的约定。
> 写出来像流水账（「然后/接着/后来/最后」串时间线，或分点罗列）会重写一次；
> 正文里有连续 8 个字和素材一模一样（在照抄原话）也会重写一次。只重试一次，还不行就照发。

## 原理

向 `user.qzone.qq.com/proxy/.../emotion_cgi_publish_v6?g_tk=xxx` POST 表单，
`g_tk` 由 `p_skey` 用经典 djb2 变体算出，cookie 里必须带登录态（`p_skey`/`skey` + `uin`）。

带图时先请求上传接口拿图片信息，再拼 `richval`（每条 `<相册id>,<lloc>,<sloc>,<type>,<height>,<width>,<size>,<name>,<desc>,<url>`，用制表符分隔、多条用 `,` 连接）。

取图兼容：图片直链、本地路径、`file=` 短名（走协议端 `get_image` 换链接）、引用消息（`reply` 递归）、合并转发（`forward` 段的 `content` 节点，必要时回退 `get_forward_msg`）。

## 注意

cookie 会过期，失效时重新获取即可。仓库里默认不含任何真实 QQ 号与 cookie，
若你本地填了 `uin`、`cookies`、`allow_user_ids`，推送前记得清掉。
调试日志 `qzone_debug.log` 只落本地，已在 `.gitignore` 里排除。
