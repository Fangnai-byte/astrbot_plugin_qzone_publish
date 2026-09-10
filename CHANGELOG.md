# 更新日志

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
