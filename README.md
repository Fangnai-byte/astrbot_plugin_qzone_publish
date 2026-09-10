# Qzone Publish（QQ空间说说）

![logo](logo.png)

让宁宁自己发说说：`/说说 晚风溜进部室……`，还能带图。

## 用法

| 命令 | 说明 |
| --- | --- |
| `/说说 内容` | 发一条说说（别名 `/发说说`、`/qzone`） |
| `/检查说说登录` | 检查 cookie 是否够用、目标 QQ 是哪个 |

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
- `cookies`：含 `p_skey` 的完整 cookie 串（从浏览器 F12 或协议端取）。
- `uin`：要发说说的 QQ 号；留空则读 cookie 里的 `uin`。
- `auto_fetch_cookie`：先向协议端（NapCat）请求 `get_cookies`，失败再用 `cookies`。
- `allow_user_ids`：允许用命令的 QQ 号，留空=所有人。
- `max_length` / `timeout_sec`：内容长度上限和请求超时。

## 原理

向 `user.qzone.qq.com/proxy/.../emotion_cgi_publish_v6?g_tk=xxx` POST 表单，
`g_tk` 由 `p_skey` 用经典 djb2 变体算出，cookie 里必须带登录态（`p_skey`/`skey` + `uin`）。

带图时先请求上传接口拿图片信息，再拼 `richval`（每条 `<相册id>,<lloc>,<sloc>,<type>,<height>,<width>,<size>,<name>,<desc>,<url>`，用制表符分隔、多条用 `,` 连接）。

取图兼容：图片直链、本地路径、`file=` 短名（走协议端 `get_image` 换链接）、引用消息（`reply` 递归）、合并转发（`forward` 段的 `content` 节点，必要时回退 `get_forward_msg`）。

## 注意

cookie 会过期，失效时重新获取即可。仓库里默认不含任何真实 QQ 号与 cookie，
若你本地填了 `uin`、`cookies`、`allow_user_ids`，推送前记得清掉。
调试日志 `qzone_debug.log` 只落本地，已在 `.gitignore` 里排除。
