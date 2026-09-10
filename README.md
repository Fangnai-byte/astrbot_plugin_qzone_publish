# Qzone Publish（QQ空间说说）

让宁宁自己发说说：`/说说 晚风溜进部室……`。

## 用法

| 命令 | 说明 |
| --- | --- |
| `/说说 内容` | 发一条说说（别名 `/发说说`、`/qzone`） |
| `/检查说说登录` | 检查 cookie 是否够用、目标 QQ 是哪个 |

## 配置

- `enabled`：总开关。
- `dry_run`：**默认开**。开着的时候只校验和打印，不会真发；确认流程没问题后再关掉。
- `cookies`：含 `p_skey` 的完整 cookie 串（从浏览器 F12 或协议端取）。
- `uin`：要发说说的 QQ 号；留空则读 cookie 里的 `uin`。
- `auto_fetch_cookie`：先向协议端（NapCat）请求 `get_cookies`，失败再用 `cookies`。
- `allow_user_ids`：允许用命令的 QQ 号，留空=所有人。
- `max_length` / `timeout_sec`：内容长度上限和请求超时。

## 原理

向 `user.qzone.qq.com/proxy/.../emotion_cgi_publish_v6?g_tk=xxx` POST 表单，
`g_tk` 由 `p_skey` 用经典 djb2 变体算出，cookie 里必须带登录态（`p_skey`/`skey` + `uin`）。

## 注意

cookie 会过期，失效时重新获取即可。仓库里默认不含任何真实 QQ 号与 cookie，
若你本地填了 `uin`、`cookies`、`allow_user_ids`，推送前记得清掉。
