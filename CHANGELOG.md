# 更新日志

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
- 新增：cookie 支持手填，或开启 `auto_fetch_cookie` 向 NapCat 请求 `get_cookies`。
- 新增：用户白名单、长度上限、超时配置。
