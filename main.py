#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QQ空间说说发布 —— AstrBot 插件

用命令 `/说说 内容` 直接发一条说说，可以顺带带图：
    • 消息里直接发图片 + `/说说 文字`（可多张）
    • 引用一条带图的消息（含合并转发聊天记录）再 `/说说 文字`
    • 文字里写图片链接或本地路径，最多 9 张

登录态走 cookie（p_skey 那一串），也可以让插件自动向协议端要 cookie。

配置要点：
    • enabled         总开关
    • dry_run         演习模式，只打印请求内容不真的发（默认开）
    • enable_image    是否允许带图（默认开）
    • cookies         手工填的 cookie 串，不想自动取就填这里
    • uin             要发说说的 QQ 号
    • auto_fetch_cookie  为真时优先向协议端要 cookie
    • allow_user_ids  允许使用命令的 QQ 号白名单
"""
from __future__ import annotations

import asyncio
import base64
import importlib
import json
import os
import re
import time
import traceback
from typing import List, Tuple

import aiohttp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

try:  # 兼容包 / 非包两种加载方式
    from . import qzone_core as _qc
except ImportError:  # pragma: no cover
    import qzone_core as _qc

_qc = importlib.reload(_qc)  # 插件热重载时强制刷新子模块
DEFAULT_HEADERS = _qc.DEFAULT_HEADERS
PUBLISH_URL = _qc.PUBLISH_URL
UPLOAD_URL = _qc.UPLOAD_URL
UPLOAD_HEADERS = _qc.UPLOAD_HEADERS
MAX_IMAGES = _qc.MAX_IMAGES
build_payload = _qc.build_payload
build_richval = _qc.build_richval
build_upload_body = _qc.build_upload_body
check_cookie = _qc.check_cookie
get_gtk = _qc.get_gtk
join_richvals = _qc.join_richvals
parse_cookie = _qc.parse_cookie
parse_upload_response = _qc.parse_upload_response
pick_skey = _qc.pick_skey
prepare_image_bytes = _qc.prepare_image_bytes

DBG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qzone_debug.log")


def _dbg(msg: str) -> None:
    """独立调试日志，避开主日志轮转。"""
    try:
        with open(DBG_PATH, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:  # noqa: BLE001
        pass


COMMANDS = ("说说", "发说说", "qzone", "空间")
ONESHOT_FLAG = "/root/qzone_oneshot.flag"
ONESHOT_RESULT = "/root/qzone_oneshot_result.json"
IMG_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".jfif")
URL_RE = re.compile(r"https?://[^\s'\"<>（）()【】]+")


def _looks_like_image_url(url: str) -> bool:
    clean = url.split("?")[0].split("#")[0].lower()
    return clean.endswith(IMG_EXT)


def split_text_and_images(text: str) -> Tuple[str, List[str]]:
    """把文字里的图片链接 / 本地路径抠出来，返回 (剩余文字, 图片来源列表)。"""
    srcs: List[str] = []
    rest = text or ""
    for url in URL_RE.findall(rest):
        if _looks_like_image_url(url):
            srcs.append(url)
            rest = rest.replace(url, " ")
    for token in rest.split():
        if token.startswith(("/", "~")):
            path = os.path.expanduser(token)
            if os.path.isfile(path):
                srcs.append(path)
                rest = rest.replace(token, " ")
    return " ".join(rest.split()), srcs


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

    def _timeout(self):
        return aiohttp.ClientTimeout(total=int(self._conf("timeout_sec", 30)))

    # ---------------- 一次性自主发布（不需要任何人发命令） ----------------
    async def initialize(self):
        """插件加载后，若开了 oneshot_on_load 就自己发一条。"""
        if not self._conf("oneshot_on_load", False):
            return
        flag = str(self._conf("oneshot_flag_path", ONESHOT_FLAG) or ONESHOT_FLAG)
        if os.path.exists(flag):
            logger.info("[Qzone] 一次性任务跑过了，跳过；想再来一次就删掉 flag 文件")
            return
        try:
            with open(flag, "w", encoding="utf-8") as f:
                f.write(str(time.time()))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[Qzone] 落 flag 失败: {e}")
        asyncio.create_task(self._oneshot_job())
        logger.info("[Qzone] 自主发布任务已排入后台，等协议端就绪后动手")

    async def _oneshot_job(self):
        await asyncio.sleep(max(0, int(self._conf("oneshot_delay", 8))))
        content = str(self._conf("oneshot_content", "") or "")
        ok, msg = False, "没执行"
        try:
            ok, msg = await self.publish(content)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[Qzone] 自主发布异常: {e}")
            msg = f"异常：{e}"
        path = str(
            self._conf("oneshot_result_path", ONESHOT_RESULT) or ONESHOT_RESULT
        )
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "ok": ok,
                        "message": msg,
                        "content": content,
                        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[Qzone] 写结果失败: {e}")
        logger.info(f"[Qzone] [OneShot] ok={ok} result={msg}")

    async def terminate(self):
        pass

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

    @staticmethod
    def _extract_cookie(ret) -> str:
        if isinstance(ret, dict):
            inner = ret.get("data")
            data = inner if isinstance(inner, dict) else ret
            return str(data.get("cookies") or data.get("cookie") or "")
        if isinstance(ret, str):
            return ret
        return ""

    def _platform_call_action(self):
        """没有 event 的时候，直接从平台实例里摸出 call_action。"""
        pm = getattr(self.context, "platform_manager", None)
        insts = pm.get_insts() if pm else []
        for inst in insts or []:
            name = ""
            try:
                name = inst.meta().name
            except Exception:  # noqa: BLE001
                name = type(inst).__name__
            if name not in ("aiocqhttp", type(inst).__name__):
                continue
            client = None
            getter = getattr(inst, "get_client", None)
            if callable(getter):
                try:
                    client = getter()
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[Qzone] get_client 失败: {e}")
            for obj in (client, getattr(inst, "bot", None), getattr(inst, "api", None)):
                act = getattr(obj, "call_action", None)
                if callable(act):
                    return act
        logger.warning("[Qzone] 没找到可用的 aiocqhttp 实例，取不到 cookie")
        return None

    async def _fetch_cookie_from_platform(self) -> str:
        act = self._platform_call_action()
        if act is None:
            return ""
        for domain in ("user.qzone.qq.com", "qzone.qq.com", "qq.com"):
            try:
                ret = await act("get_cookies", domain=domain)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[Qzone] 平台取 cookie 失败({domain}): {e}")
                continue
            cookie = self._extract_cookie(ret)
            if cookie:
                logger.info(f"[Qzone] 从平台实例拿到 cookie({domain}) 长度={len(cookie)}")
                return cookie
        return ""

    async def _resolve_cookie_any(self, event: AstrMessageEvent | None = None) -> str:
        cookie = ""
        if self._conf("auto_fetch_cookie", False):
            if event is not None:
                cookie = await self._fetch_cookie_from_bot(event)
            if not cookie:
                cookie = await self._fetch_cookie_from_platform()
        if not cookie:
            cookie = str(self._conf("cookies", "") or "")
        return cookie.strip()

    async def _resolve_cookie(self, event: AstrMessageEvent) -> str:
        return await self._resolve_cookie_any(event)

    def _resolve_uin(self, cookie: str) -> str:
        uin = str(self._conf("uin", "") or "").strip()
        if uin:
            return uin
        jar = parse_cookie(cookie)
        raw = jar.get("uin") or jar.get("pt2gguin") or ""
        return raw.lstrip("o")

    # ---------------- 收图 ----------------
    def _call_action(self, event: AstrMessageEvent):
        bot = getattr(event, "bot", None)
        return getattr(bot, "call_action", None) or getattr(
            getattr(bot, "api", None), "call_action", None
        )

    async def _get_msg(self, event: AstrMessageEvent, mid):
        act = self._call_action(event)
        if act is None:
            return None
        try:
            ret = await act("get_msg", message_id=int(mid))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[Qzone] get_msg({mid}) 失败: {e}")
            return None
        return ret if isinstance(ret, dict) else None

    @staticmethod
    def _segs_of(ret) -> list:
        if not isinstance(ret, dict):
            return []
        for key in ("message", "data"):
            val = ret.get(key)
            if isinstance(val, list):
                return val
            if isinstance(val, dict) and isinstance(val.get("message"), list):
                return val["message"]
        return []

    @staticmethod
    def _cq_images(text: str) -> List[str]:
        return re.findall(r"\[CQ:image,[^\]]*?(?:url|file)=([^,\]]+)\]", text or "")

    async def _forward_images(self, event: AstrMessageEvent, fid: str, depth: int = 0) -> List[str]:
        """合并转发：把节点里的图片全掏出来。"""
        act = self._call_action(event)
        if act is None:
            return []
        try:
            ret = await act("get_forward_msg", message_id=str(fid))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[Qzone] get_forward_msg({fid}) 失败: {e}")
            return []
        nodes = self._segs_of(ret)
        if not nodes and isinstance(ret, dict):
            data = ret.get("data")
            if isinstance(data, dict):
                nodes = data.get("messages") or data.get("nodeList") or []
        _dbg("forward %s 节点数=%d" % (fid, len(nodes or [])))
        srcs: List[str] = []
        for node in nodes or []:
            if not isinstance(node, dict):
                continue
            segs = node.get("message")
            if segs is None:
                segs = node.get("content")
            if isinstance(segs, str):
                srcs.extend(self._cq_images(segs))
                continue
            if isinstance(segs, list):
                srcs.extend(await self._images_from_segs(event, segs, depth + 1))
        return srcs

    async def _resolve_src(self, event: AstrMessageEvent, val: str) -> str:
        """file= 之类的短名换成可用链接。"""
        if not val:
            return ""
        if val.startswith(("http://", "https://", "file://", "/")):
            return val
        act = self._call_action(event)
        if act is None:
            return ""
        try:
            ret = await act("get_image", file=val)
        except Exception as e:  # noqa: BLE001
            _dbg("get_image(%s) 失败: %s" % (val[:50], e))
            return ""
        if isinstance(ret, dict):
            data = ret.get("data") if isinstance(ret.get("data"), dict) else ret
            u = data.get("url") or ""
            if u:
                return u
            fp = data.get("file") or ""
            if fp.startswith("/"):
                return fp
        return ""

    async def _node_images(self, event: AstrMessageEvent, node, depth: int = 0) -> List[str]:
        """合并转发里的单个节点。"""
        if not isinstance(node, dict):
            return []
        segs = node.get("message")
        if isinstance(segs, list):
            return await self._images_from_segs(event, segs, depth + 1)
        raw = node.get("raw_message") or node.get("content")
        if isinstance(raw, str):
            return self._cq_images(raw)
        return []

    async def _images_from_segs(self, event: AstrMessageEvent, segs, depth: int = 0) -> List[str]:
        srcs: List[str] = []
        for seg in segs or []:
            if not isinstance(seg, dict):
                continue
            stype = seg.get("type")
            info = seg.get("data") or {}
            if stype == "image":
                src = info.get("url") or info.get("file") or ""
                if src:
                    srcs.append(await self._resolve_src(event, src))
            elif stype == "reply" and depth < 2:
                rid = str(info.get("id") or "")
                if rid.isdigit():
                    srcs.extend(await self._images_from_msg(event, rid, depth + 1))
            elif stype in ("forward", "node") and depth < 2:
                nodes = info.get("content") or []
                got: List[str] = []
                if isinstance(nodes, list):
                    for node in nodes:
                        got.extend(await self._node_images(event, node, depth))
                if not got:
                    fid = str(info.get("id") or info.get("message_id") or "")
                    if fid:
                        got.extend(await self._forward_images(event, fid, depth + 1))
                srcs.extend([x for x in got if x])
        return [x for x in srcs if x]

    async def _images_from_msg(self, event: AstrMessageEvent, mid, depth: int = 0) -> List[str]:
        """从某条消息里抠图片链接，并跟进它引用的消息 / 合并转发。"""
        ret = await self._get_msg(event, mid)
        if ret is None:
            return []
        segs = self._segs_of(ret)
        _dbg("get_msg %s 段落=%s" % (mid, str(segs)[:400]))
        logger.info(f"[Qzone] get_msg({mid}) 段落={segs}")
        srcs = await self._images_from_segs(event, segs, depth)
        if not segs:
            raw = ret.get("raw_message")
            if isinstance(raw, str):
                srcs.extend(self._cq_images(raw))
        return srcs

    async def _collect_images(self, event: AstrMessageEvent) -> List[str]:
        """把消息里的图片（含被引用消息里的）都收集起来。"""
        comps = list(getattr(event, "message", []) or [])
        if not comps:
            try:
                comps = list(event.get_messages() or [])
            except Exception:  # noqa: BLE001
                comps = []
        obj = getattr(event, "message_obj", None)
        raw = str(getattr(obj, "raw_message", "") or "")
        logger.info("[Qzone] 组件数=%d 原文前200=%s" % (len(comps), raw[:200]))
        _dbg("组件数=%d 类型=%s 原文=%s" % (
            len(comps),
            [str(getattr(getattr(c, "type", ""), "value", getattr(c, "type", ""))) for c in comps],
            raw[:300],
        ))
        srcs: List[str] = []
        reply_ids: List[str] = []
        for comp in comps:
            ctype = getattr(comp, "type", "")
            ctype = str(getattr(ctype, "value", ctype))
            if ctype == "Reply":
                rid = str(getattr(comp, "id", "") or "")
                if rid.isdigit():
                    reply_ids.append(rid)
            elif ctype == "Image":
                try:
                    path = await comp.convert_to_file_path()
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[Qzone] 图片落地失败: {e}")
                    continue
                if path:
                    srcs.append(path)
        for rid in reply_ids:
            srcs.extend(await self._images_from_msg(event, rid))
        if not srcs and not reply_ids:
            mid = getattr(obj, "message_id", None)
            if mid:
                srcs.extend(await self._images_from_msg(event, mid))
        logger.info(f"[Qzone] 收集到图片 {len(srcs)} 张")
        _dbg("收集到图片 %d 张 reply_ids=%s srcs=%s" % (len(srcs), reply_ids, srcs))
        return srcs

    async def _read_source(self, sess: aiohttp.ClientSession, src: str) -> bytes:
        if src.startswith("file://"):
            src = src[len("file://"):]
            if src.startswith("localhost/"):
                src = src[len("localhost"):]
        if src.startswith(("http://", "https://")):
            async with sess.get(src) as resp:
                if resp.status != 200:
                    raise ValueError(f"下载图片失败 HTTP {resp.status}")
                return await resp.read()
        with open(os.path.expanduser(src), "rb") as f:
            return f.read()

    # ---------------- 上传图片 ----------------
    async def _upload_image(self, cookie: str, uin: str, raw: bytes) -> dict:
        jar = parse_cookie(cookie)
        skey = jar.get("skey") or jar.get("p_skey") or ""
        pskey = jar.get("p_skey") or ""
        gtk = get_gtk(skey)
        body = build_upload_body(
            uin, skey, pskey, gtk, base64.b64encode(raw).decode()
        )
        headers = dict(UPLOAD_HEADERS)
        headers["Cookie"] = cookie
        url = f"{UPLOAD_URL}?g_tk={gtk}"
        logger.info(f"[Qzone] 开始上传图片，压缩后大小={len(raw)} 字节")
        async with aiohttp.ClientSession(timeout=self._timeout()) as sess:
            async with sess.post(url, headers=headers, data=body) as resp:
                text = await resp.text()
        logger.info(f"[Qzone] 上传响应前120字: {text[:120]!r}")
        data = parse_upload_response(text)
        logger.info(
            f"[Qzone] 上传成功 albumid={data.get('albumid')} "
            f"lloc={data.get('lloc')} {data.get('width')}x{data.get('height')}"
        )
        return data

    async def _upload_all(self, cookie: str, uin: str, srcs: List[str]) -> List[str]:
        """把图片逐个传上去，返回 richval 列表。"""
        richvals: List[str] = []
        async with aiohttp.ClientSession(timeout=self._timeout()) as sess:
            for src in srcs:
                try:
                    raw = await self._read_source(sess, src)
                except Exception as e:  # noqa: BLE001
                    raise ValueError(f"读不到图片 {src}：{e}") from e
                packed = prepare_image_bytes(raw)
                data = await self._upload_image(cookie, uin, packed)
                richvals.append(build_richval(data))
        return richvals

    # ---------------- 真正发出去 ----------------
    async def _publish(self, cookie: str, uin: str, content: str, richval: str = ""):
        skey = pick_skey(parse_cookie(cookie))
        gtk = get_gtk(skey)
        url = f"{PUBLISH_URL}?g_tk={gtk}"
        headers = dict(DEFAULT_HEADERS)
        headers["Cookie"] = cookie
        headers["Referer"] = f"https://user.qzone.qq.com/{uin}"
        payload = build_payload(uin, content, richval)
        logger.info(
            f"[Qzone] 发布 payload: uin={uin} con长度={len(payload.get('con', ''))} "
            f"gtk={gtk} 图片数={len([r for r in richval.split(chr(9)) if r])} "
            f"内容前20={content[:20]!r}"
        )
        _dbg("发布 payload 图片数=%d 内容=%r" % (
            len([r for r in richval.split("\t") if r]), content[:80]))
        async with aiohttp.ClientSession(timeout=self._timeout()) as sess:
            async with sess.post(url, headers=headers, data=payload) as resp:
                text = await resp.text()
                _dbg("发布返回 status=%s body=%s" % (resp.status, text[:200]))
                return resp.status, text[:400]

    # ---------------- 发布核心：命令、工具、自主任务都走这里 ----------------
    async def publish(
        self,
        content: str,
        image_srcs: List[str] | None = None,
        event: AstrMessageEvent | None = None,
        dry_run: bool | None = None,
    ) -> Tuple[bool, str]:
        """发一条说说，返回 (是否成功, 给人看的话)。event 可以为空。"""
        text = (content or "").strip()
        seen, pics = set(), []
        for s in image_srcs or []:
            s = str(s or "").strip()
            if s and s not in seen:
                seen.add(s)
                pics.append(s)
        if len(pics) > MAX_IMAGES:
            logger.info(f"[Qzone] 图片超过 {MAX_IMAGES} 张，只带前 {MAX_IMAGES} 张")
            pics = pics[:MAX_IMAGES]
        if not text and not pics:
            return False, "要发什么呢？内容不能是空的。"
        max_len = int(self._conf("max_length", 900))
        if len(text) > max_len:
            return False, f"太长啦，说说最多 {max_len} 字。"

        cookie = await self._resolve_cookie_any(event)
        ok, msg = check_cookie(cookie)
        if not ok:
            return False, f"登录态不行：{msg}"
        uin = self._resolve_uin(cookie)
        if not uin:
            return False, "没找到要发说说的 QQ 号，请在配置里填 uin。"

        richval = ""
        if pics:
            try:
                richvals = await self._upload_all(cookie, uin, pics)
                richval = join_richvals(richvals)
            except Exception as e:  # noqa: BLE001
                logger.error(f"[Qzone] 图片上传失败: {e}")
                _dbg("图片上传异常:\n" + traceback.format_exc())
                return False, f"图片没传上去：{e}"

        is_dry = self._conf("dry_run", True) if dry_run is None else dry_run
        if is_dry:
            logger.info(
                f"[Qzone] 演习模式，不发送。uin={uin} 图片数={len(pics)} "
                f"richval长度={len(richval)} 内容={text}"
            )
            return False, (
                f"演习模式：本来会发「{text}」，带 {len(pics)} 张图，"
                "配置里关掉 dry_run 就真发。"
            )

        try:
            status, body = await self._publish(cookie, uin, text, richval)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[Qzone] 发布异常: {e}")
            _dbg("发布异常: %s\n%s" % (e, traceback.format_exc()))
            return False, f"发送出错了：{e}"
        if status == 200 and ('"code":0' in body.replace(" ", "") or '"tid"' in body):
            logger.info(f"[Qzone] 发布成功: {body[:200]}")
            tip = (
                f"发出去啦，带了 {len(pics)} 张图，去空间看看吧"
                if pics
                else "发出去啦，去空间看看吧"
            )
            return True, tip
        logger.warning(f"[Qzone] 发布失败 http={status} body={body}")
        return False, f"好像失败了（HTTP {status}）：{body[:120]}"

    # ---------------- 命令 ----------------
    @filter.command(
        "说说",
        alias={"/说说", "／说说", "发说说", "/发说说", "qzone", "/qzone"},
    )
    async def publish_say(self, event: AstrMessageEvent, content: str = ""):
        """发一条 QQ 空间说说：/说说 今天天气不错（可带图）"""
        if not self._conf("enabled", True):
            return
        if not self._is_allowed(event):
            return
        text = (content or "").strip() or event.message_str
        for cmd in COMMANDS:
            if text.startswith(cmd):
                text = text[len(cmd):].strip()
                break

        srcs: List[str] = []
        if self._conf("enable_image", True):
            srcs = await self._collect_images(event)
            text, from_text = split_text_and_images(text)
            srcs.extend(from_text)
        try:
            ok, msg = await self.publish(text, srcs, event)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[Qzone] 命令发布异常: {e}")
            _dbg("命令发布异常:\n" + traceback.format_exc())
            msg = f"出错了：{e}"
        yield event.plain_result(msg)

    # ---------------- LLM 工具：机器人自己想发就发 ----------------
    @filter.llm_tool(name="qzone_publish_say")
    async def tool_publish_say(
        self,
        event: AstrMessageEvent,
        content: str,
        image_urls: str = "",
    ):
        """发一条 QQ 空间说说，把此刻的心情写进自己的空间。

        Args:
            content(string): 说说的正文，别超过配置的字数上限，也别用颜文字和表情
            image_urls(string): 可选配图，http 直链或本地路径，多张用英文逗号隔开
        """
        if not self._conf("enabled", True):
            return "说说功能没开，发不了。"
        if not self._conf("tool_enabled", True):
            return "自主发布没开，要发的话得先让共犯在配置里打开。"
        if not self._is_allowed(event):
            return "不在白名单里，不能替你发说说。"
        srcs: List[str] = []
        if self._conf("enable_image", True):
            try:
                srcs.extend(await self._collect_images(event))
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[Qzone] 工具收图失败: {e}")
        for tok in re.split(r"[,，\s]+", image_urls or ""):
            if tok.strip():
                srcs.append(tok.strip())
        text = content or ""
        if self._conf("enable_image", True):
            text, from_text = split_text_and_images(text)
            srcs.extend(from_text)
        try:
            ok, msg = await self.publish(text, srcs, event)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[Qzone] 工具发布异常: {e}")
            return f"出错了：{e}"
        return msg

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
