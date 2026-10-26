# 更新日志

## v0.4.0
- 新增：**每个 bot 各发一条**（`daily_per_bot`，默认开）。遍历所有启用的 aiocqhttp 实例，
  每个 bot 用自己的 `get_cookies` 取自己的 cookie、从 cookie 里读自己的 `uin`，各发一条；
  去重键按 bot 分开，互不吞掉对方的定时。关掉则退回全局 `uin`/`cookies` 单账号模式。
- 新增：`daily_prompt_map`（每行 `平台id,提示词`）可以按 bot 覆盖提示词。
- 新增：**私聊上下文**——`daily_context_friends` 指定 QQ 号，读 AstrBot 会话历史
  （`data/data_v4.db` 的 `conversations`），自动剥掉 `<system_reminder>` 之类的注入块；
  分 bot 模式下只取**该 bot 自己**的会话，不会把另一个 bot 的私聊当素材。
- 改：**聊天素材改成插件自己记**（`daily_record`），不再依赖 `astrbot_plugin_group_log_archive`。
  只记 `daily_context_groups` / `daily_context_friends` 里列出的会话，落盘到
  `data/plugin_data/astrbot_plugin_qzone_publish/context.json`；带昵称、按天过期（`daily_record_keep_days`）、
  每会话限长（`daily_record_per_session`）。群聊素材因此不再需要别的插件，日志格式变了也不受影响。
- 改：`oneshot_on_load` 的正文与定时说说共用同一套生成逻辑（`oneshot_content` 留空就由模型现写）。
- 改：定时说说默认提示词改成「只讲一个瞬间、别写汇报体、别引用聊天原文」，并加了两道兜底：
  写出来像流水账（「然后/接着/后来/最后」≥3 次或分点 ≥2 条）、或正文与素材有连续 8 字重合
  （在照抄原话）时自动重写一次；仍不行则照发不空转。
- 新增：`daily_context_paths`——**外部聊天记录**引用路径。每项可以是文件、目录或通配符，
  按修改时间取最新的那个，读最后若干行**原样**当素材（不解析格式），
  其它插件导出的、手动存的记录都能直接接上。
- 改：聊天素材**原样保留**——只处理 AstrBot 核心自己的注入格式（`<system_reminder>`、引用标记），
  其它插件或 persona 的标记（`[[ja]]`、`【档案/记忆】` 之类）不做剥离。那是各家自己的约定，
  通用插件替别人做决定只会误伤。
- 修：读会话库时加 `pragma temp_store = memory` 并改成「先排 rowid 再取 content」的两步查询——
  排序大字段时 SQLite 要建临时文件，个别环境建不出来会报 `unable to open database file`，
  导致私聊素材静默取不到。
- 改：`publish()` 支持 `cookie_override` / `uin_override`，供按实例发帖。

## v0.3.0
- 新增：定时发说说 `daily_at`（多个 `HH:MM`，每个时刻每天只发一次），不需要任何人发命令。
  正文两个来源：`daily_content` 固定文案；**留空则由模型按 `daily_prompt` 现写**
  （支持 `{date}`/`{time}`/`{weekday}`），写不出来时用 `daily_fallback` 兜底。
- 新增：定时说说可以**带群聊上下文**——`daily_context_groups` 指定群号，读
  `group_log_archive` 导出的归档日志（今天没有就取昨天），按 `daily_context_lines` 取最近若干条、
  `daily_context_max_chars` 限长，当作素材塞进提示词（要求别逐条复述）。
- 修：`terminate` 以前是空的，重载后定时/一次性任务不会停；现在会取消定时任务。
- 改：文档与配置说明里不再把协议端写死成某一家实现（改为 SnowLuma 等 OneBot 实现）。

## v0.2.2
- 改：向协议端取 cookie 的域名顺序改为 `qzone.qq.com` 优先（它在 QQ 的 pskey 白名单里；
  `user.qzone.qq.com` 必然拿不到 p_skey，原来排第一，白打一条误导性 warning）。
- 改：`/检查说说登录` 会逐域列出结果（`retcode` / 拿到多少字），自动取失败时直接给出下一步提示。
- 修：手填裸值（只贴 `p_skey` 的值）时提示语明确要求 `p_skey=…; skey=…; uin=o…` 整串。
- 文档：README 说明 SnowLuma 也实现了 `get_cookies`，以及“某些域拿不到 p_skey 属正常”。

## v0.2.1
- 新增：LLM 工具 `qzone_publish_say`，机器人可以在对话里自主发说说（受 `tool_enabled` 控制）。
- 新增：发布逻辑与命令解耦，新增无 `event` 依赖的 `publish()` 入口，命令行与工具共用。
- 新增：一次性自主发布（`oneshot_on_load` / `oneshot_delay` / `oneshot_content`），靠 flag 文件去重，加载后延迟自动发一条。
- 新增：一次性发布结果写入 `oneshot_result_path`，方便外部判定成功与否。
- 新增：插件卸载（`terminate`）时清理后台任务，避免重载后残留。
- 说明：原 `zz_qzone_oneshot` 临时插件的能力已全部并入本插件，临时插件已移除。

## v0.2.0
- 新增：说说带图。支持本地路径、图片直链，单条最多 9 张，自动上传后拼 `richval`。
- 新增：消息里直接发图片 + `/说说 文字`，图片随消息一起带上。
- 新增：引用带图消息取图，`reply` 段递归解析。
- 新增：引用**合并转发聊天记录**取图，解析 `forward` 段的 `content` 节点，兼容 `message` 列表与 `raw_message` 字符串。
- 新增：`file=` 短名图片自动经协议端 `get_image` 换成可下载链接。
- 新增：配置项 `enable_image`，可单独关掉带图。
- 修复：非法 f-string 格式串导致的切片报错，改用 `%` 格式化。
- 修复：上传 / 发布异常补 `traceback` 落盘，便于排查。
- 新增：独立调试日志 `qzone_debug.log`（本地，已在 `.gitignore` 排除）。

## v0.1.1
- 新增：插件 Logo（logo.png，512x512），metadata 按星尘手账同款写法声明 `logo` 字段。
- 修复：发布请求补上 `con` 字段，解决 QQ 返回 -10005「您未输入内容」的问题。
- 修复：`get_cookies` 返回结构兼容扁平 / 嵌套两种格式。
- 修复：热重载时强制 reload `qzone_core` 子模块，避免改动不生效。

## v0.1.0
- 新增：`/说说 内容` 发布 QQ 空间说说，别名 `/发说说`、`/qzone`。
- 新增：`dry_run` 演习模式，默认开启，确认无误后再真发。
- 新增：`/检查说说登录` 检查 cookie 与目标 QQ。
- 新增：cookie 支持手填，或开启 `auto_fetch_cookie` 向协议端请求 `get_cookies`。
- 新增：用户白名单、长度上限、超时配置。
