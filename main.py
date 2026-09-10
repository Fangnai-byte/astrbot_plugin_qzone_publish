#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QQ空间说说发布 —— AstrBot 插件

用命令 `/说说 内容` 直接发一条说说。登录态走 cookie（p_skey 那一串），
也可以让插件自动向 NapCat 要 cookie（需要协议端支持 get_cookies 接口）。

配置要点：
    • enabled         总开关
    • dry_run         演习模式，只打印请求内容不真的发（默认开）
    • cookies         手工填的 cookie 串，不想自动取就填这里
    • uin             要发说说的 QQ 号
    • auto_fetch_cookie  为真时优先向协议端要 cookie
    • allow_user_ids  允许使用命令的 QQ 号白名单
"""
from __future__ import annotations

import aiohttp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

import importlib

try:  # 兼容包 / 非包两种加载方式
    from . import qzone_core as _qc
except ImportError:  # pragma: no cover
    import qzone_core as _qc

_qc = importlib.reload(_qc)  # 插件热重载时强制刷新子模块
DEFAULT_HEADERS = _qc.DEFAULT_HEADERS
PUBLISH_URL = _qc.PUBLISH_URL
build_payload = _qc.build_payload
check_cookie = _qc.check_cookie
get_gtk = _qc.get_gtk
parse_cookie = _qc.parse_cookie
pick_skey = _qc.pick_skey

COMMANDS = ("说说", "发说说", "qzone", "空间")


class QzonePublishPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}

    # ---------------- 配置小工具 ----------------
    def _conf(self, key, default=None):
        val = self.config.get(key, default)
        return default if val is None else val

    def _is_allowed(self, event: AstrMessageEvent) -> bool:
        allow = self._conf("allow_user_ids", []) or []
        allow = [str(x).strip() for x in allow if str(x).strip()]
        if not allow:
            return True
        return str(event.get_sender_id()) in allow

    # ---------------- cookie ----------------
    async def _fetch_cookie_from_bot(self, event: AstrMessageEvent) -> str:
        bot = getattr(event, "bot", None)
        logger.info(f"[Qzone] 协议端对象: {type(bot).__name__ if bot else None}")
        call_action = getattr(bot, "call_action", None)
        if call_action is None:
            api = getattr(bot, "api", None)
            call_action = getattr(api, "call_action", None)
        if call_action is None:
            logger.warning("[Qzone] 协议端不支持 call_action，无法自动取 cookie")
            return ""
        for domain in ("user.qzone.qq.com", "qzone.qq.com", "qq.com"):
            try:
                ret = await call_action("get_cookies", domain=domain)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[Qzone] 取 cookie 失败({domain}): {e}")
                continue
            logger.info(
                f"[Qzone] get_cookies({domain}) 返回字段: "
                f"{list(ret)[:10] if isinstance(ret, dict) else type(ret).__name__}"
            )
            if isinstance(ret, dict):
                inner = ret.get("data")
                data = inner if isinstance(inner, dict) else ret
                cookie = data.get("cookies") or data.get("cookie") or ""
                logger.info(f"[Qzone] 解析后 cookie 长度={len(cookie)}")
                if cookie:
                    logger.info(f"[Qzone] 通过协议端拿到 cookie({domain})")
                    return cookie
            if isinstance(ret, str) and ret:
                return ret
        return ""

    async def _resolve_cookie(self, event: AstrMessageEvent) -> str:
        cookie = ""
        if self._conf("auto_fetch_cookie", False):
            cookie = await self._fetch_cookie_from_bot(event)
        if not cookie:
            cookie = str(self._conf("cookies", "") or "")
        return cookie.strip()

    def _resolve_uin(self, cookie: str) -> str:
        uin = str(self._conf("uin", "") or "").strip()
        if uin:
            return uin
        jar = parse_cookie(cookie)
        raw = jar.get("uin") or jar.get("pt2gguin") or ""
        return raw.lstrip("o")

    # ---------------- 真正发出去 ----------------
    async def _publish(self, cookie: str, uin: str, content: str):
        skey = pick_skey(parse_cookie(cookie))
        gtk = get_gtk(skey)
        url = f"{PUBLISH_URL}?g_tk={gtk}"
        headers = dict(DEFAULT_HEADERS)
        headers["Cookie"] = cookie
        headers["Referer"] = f"https://user.qzone.qq.com/{uin}"
        payload = build_payload(uin, content)
        logger.info(
            f"[Qzone] 发布 payload: uin={uin} con长度={len(payload.get('con',''))} "
            f"gtk={gtk} 内容前20={content[:20]!r}"
        )
        timeout = aiohttp.ClientTimeout(total=int(self._conf("timeout_sec", 30)))
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(url, headers=headers, data=payload) as resp:
                text = await resp.text()
                return resp.status, text[:400]

    # ---------------- 命令 ----------------
    @filter.command(
        "说说",
        alias={"/说说", "／说说", "发说说", "/发说说", "qzone", "/qzone"},
    )
    async def publish_say(self, event: AstrMessageEvent, content: str = ""):
        """发一条 QQ 空间说说：/说说 今天天气不错"""
        if not self._conf("enabled", True):
            return
        if not self._is_allowed(event):
            return
        text = (content or "").strip() or event.message_str
        for cmd in COMMANDS:
            if text.startswith(cmd):
                text = text[len(cmd):].strip()
                break
        if not text:
            yield event.plain_result("要发什么呢？写成 /说说 内容 就好。")
            return
        max_len = int(self._conf("max_length", 900))
        if len(text) > max_len:
            yield event.plain_result(f"太长啦，说说最多 {max_len} 字。")
            return

        cookie = await self._resolve_cookie(event)
        ok, msg = check_cookie(cookie)
        if not ok:
            yield event.plain_result(f"登录态不行：{msg}")
            return
        uin = self._resolve_uin(cookie)
        if not uin:
            yield event.plain_result("没找到要发说说的 QQ 号，请在配置里填 uin。")
            return

        if self._conf("dry_run", True):
            logger.info(f"[Qzone] 演习模式，不发送。uin={uin} 内容={text}")
            yield event.plain_result(f"演习模式：本来会发「{text}」，配置里关掉 dry_run 就真发。")
            return

        try:
            status, body = await self._publish(cookie, uin, text)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[Qzone] 发布异常: {e}")
            yield event.plain_result(f"发送出错了：{e}")
            return
        if status == 200 and ('"code":0' in body.replace(" ", "") or '"tid"' in body):
            logger.info(f"[Qzone] 发布成功: {body[:200]}")
            yield event.plain_result("发出去啦，去空间看看吧～")
        else:
            logger.warning(f"[Qzone] 发布失败 http={status} body={body}")
            yield event.plain_result(f"好像失败了（HTTP {status}）：{body[:120]}")

    async def _check_reply(self, event: AstrMessageEvent):
        cookie = await self._resolve_cookie(event)
        ok, msg = check_cookie(cookie)
        if not ok:
            return f"检查没过：{msg}"
        jar = parse_cookie(cookie)
        keys = ",".join(sorted(k for k in jar if k in ("p_skey", "skey", "uin", "pt2gguin")))
        return f"cookie 看起来还行，关键字段：{keys}；目标 QQ：{self._resolve_uin(cookie)}"

    @filter.command(
        "检查说说登录",
        alias={"/检查说说登录", "／检查说说登录", "检查说说状态", "说说登录"},
    )
    async def check_cookie_cmd(self, event: AstrMessageEvent):
        """看看 cookie 还能不能用：/检查说说登录"""
        if not self._is_allowed(event):
            return
        logger.info("[Qzone] check_cookie_cmd 命中，开始检查登录")
        reply = await self._check_reply(event)
        logger.info(f"[Qzone] check_cookie_cmd 回复内容: {reply}")
        yield event.plain_result(reply)
