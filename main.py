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
import ast
import base64
import importlib
import json
import os
import re
import sqlite3
import time
import traceback
from pathlib import Path
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
# 向协议端要 cookie 时按这个顺序试：qzone.qq.com 在 QQ 的 pskey 白名单里，
# user.qzone.qq.com 不在（要它必然报 “QQ did not return a p_skey”，属正常现象）。
COOKIE_DOMAINS = ("qzone.qq.com", "user.qzone.qq.com", "qq.com")
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
        # 最近一次向协议端取 cookie 的逐域诊断，供 /检查说说登录 展示
        self._last_cookie_diag: List[Tuple[str, str, str]] = []
        # 定时发说说：后台任务句柄 + 已发过的「日期 + 时刻」去重键
        self._daily_task = None
        self._daily_sent: set = set()
        # 自己记的聊天上下文（定时说说素材），懒加载
        self._ctx_cache = None
        self._ctx_dirty = False
        self._ctx_writes = 0

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
        """插件加载后：先排定时发说说，再看 oneshot_on_load 要不要自己发一条。"""
        self._load_context()  # 把上次记的聊天素材读回内存
        self._start_daily_loop()
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
        # 与定时说说共用同一套「写什么」的逻辑：oneshot_content 留空就由模型现写
        content = str(self._conf("oneshot_content", "") or "").strip() or await self._daily_text()
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
        task = getattr(self, "_daily_task", None)
        if task is not None and not task.done():
            task.cancel()
            logger.info("[Qzone] 已停止定时发说说任务")
        if getattr(self, "_ctx_dirty", False):
            self._save_context()

    # ---------------- 定时发说说 ----------------
    def _daily_times(self) -> List[str]:
        """把 daily_at 配置整理成 ["HH:MM", ...]，非法项丢掉。"""
        raw = self._conf("daily_at", []) or []
        if isinstance(raw, str):
            raw = [x for x in re.split(r"[,;\s]+", raw) if x]
        out = []
        for item in raw:
            m = re.match(r"^(\d{1,2}):(\d{2})$", str(item).strip())
            if not m:
                continue
            h, mi = int(m.group(1)), int(m.group(2))
            if 0 <= h < 24 and 0 <= mi < 60:
                out.append(f"{h:02d}:{mi:02d}")
        return sorted(set(out))

    def _daily_should_fire(self, now: float, tag: str = "") -> str:
        """到点且这个时刻（tag 相同）当天还没发过 → 返回去重键（同时占位），否则空串。

        `tag` 用来区分 bot：多 bot 时每个 bot 各自记一次，互不吞掉对方的定时。
        """
        lt = time.localtime(now)
        hhmm = f"{lt.tm_hour:02d}:{lt.tm_min:02d}"
        if hhmm not in self._daily_times():
            return ""
        key = time.strftime("%Y-%m-%d ", lt) + hhmm + (f"#{tag}" if tag else "")
        if key in self._daily_sent:
            return ""
        self._daily_sent.add(key)
        # 只留当天的键，避免长期跑下去无限增长
        today = time.strftime("%Y-%m-%d ", lt)
        self._daily_sent = {k for k in self._daily_sent if k.startswith(today)}
        return key

    # ---------------- 每个 bot 各发一条 ----------------
    def _instance_call_action(self, inst):
        """从平台实例里摸出 OneBot 的 call_action。"""
        client = None
        getter = getattr(inst, "get_client", None)
        if callable(getter):
            try:
                client = getter()
            except Exception:  # noqa: BLE001
                client = None
        for obj in (client, getattr(inst, "bot", None), getattr(inst, "api", None)):
            act = getattr(obj, "call_action", None)
            if callable(act):
                return act
        return None

    def _bot_targets(self) -> List[Tuple[str, str, object]]:
        """启用的 aiocqhttp 实例 → [(platform_id, 显示名, call_action)]。"""
        pm = getattr(self.context, "platform_manager", None)
        getter = getattr(pm, "get_insts", None)
        try:
            insts = getter() if callable(getter) else []
        except Exception:  # noqa: BLE001
            insts = []
        out: List[Tuple[str, str, object]] = []
        for inst in insts or []:
            try:
                meta = inst.meta()
            except Exception:  # noqa: BLE001
                meta = None
            ptype = str(getattr(meta, "name", "") or "").lower()
            if "aiocqhttp" not in ptype and "aiocqhttp" not in type(inst).__name__.lower():
                continue
            pid = str(getattr(meta, "id", "") or getattr(meta, "description", "") or "")
            if not pid:
                continue
            act = self._instance_call_action(inst)
            if act is None:
                logger.warning(f"[Qzone] 实例「{pid}」拿不到 call_action，跳过")
                continue
            out.append((pid, pid, act))
        return out

    async def _daily_post_bot(self, platform_id: str, label: str, act) -> Tuple[bool, str]:
        """给某个 bot 发一条定时说说：用它自己的 cookie/uin 和它自己的会话上下文。"""
        cookie, diag = await self._try_get_cookies(act, label)
        self._last_cookie_diag = diag
        ok, msg = check_cookie(cookie)
        if not ok:
            logger.warning(f"[Qzone] 「{label}」cookie 不可用（{msg}），这个 bot 跳过")
            return False, msg
        jar = parse_cookie(cookie)
        uin = str(jar.get("p_uin") or jar.get("uin") or "").lstrip("o")
        if not uin:
            logger.warning(f"[Qzone] 「{label}」cookie 里没有 uin，跳过")
            return False, "cookie 里没有 uin"
        text = await self._daily_text(platform_id)
        if not text:
            logger.warning(f"[Qzone] 「{label}」没有正文，跳过")
            return False, "没有正文"
        ok, msg = await self.publish(text, cookie_override=cookie, uin_override=uin)
        logger.info(f"[Qzone] 「{label}」定时说说 ok={ok} uin={uin} result={msg}")
        return ok, msg

    def _start_daily_loop(self) -> None:
        times = self._daily_times()
        if not times:
            return
        content = str(self._conf("daily_content", "") or "").strip()
        prompt = str(self._conf("daily_prompt", "") or "").strip()
        if not content and not prompt:
            logger.warning(
                "[Qzone] daily_at 配了，但 daily_content 和 daily_prompt 都是空的，定时发说说不会生效"
            )
            return
        self._daily_task = asyncio.create_task(self._daily_loop())
        mode = "固定文案" if content else "模型现写"
        logger.info(f"[Qzone] 定时发说说已排入后台：{', '.join(times)}（{mode}）")

    @staticmethod
    def _clean_llm_text(text: str) -> str:
        """把模型输出收拾成能直接发的正文：去代码块、去首尾引号、压掉多余空行。"""
        s = (text or "").strip()
        s = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", s).strip()
        s = re.sub(r"^(说说|正文|内容)[:：]\s*", "", s).strip()
        for pair in ('""', "''", "「」", "“”", "‘’"):
            if len(s) >= 2 and s[0] == pair[0] and s[-1] == pair[1]:
                s = s[1:-1].strip()
        s = re.sub(r"\n{3,}", "\n\n", s)
        return s.strip()

    # ---------------- 定时说说：自己记的聊天上下文（不依赖任何别的插件） ----------------
    def _context_file(self) -> Path:
        raw = str(self._conf("daily_context_file", "") or "").strip()
        if raw:
            return Path(raw)
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_root

            base = Path(get_astrbot_root()) / "data" / "plugin_data" / "astrbot_plugin_qzone_publish"
        except Exception:  # noqa: BLE001
            base = Path("data") / "plugin_data" / "astrbot_plugin_qzone_publish"
        return base / "context.json"

    def _list_conf(self, key: str) -> List[str]:
        raw = self._conf(key, []) or []
        if isinstance(raw, str):
            raw = [x for x in re.split(r"[,;\s]+", raw) if x]
        return [str(x).strip() for x in raw if str(x).strip()]

    def _context_wanted(self, umo: str) -> bool:
        """这条会话要不要记：群号/QQ 号在配置里才记。"""
        parts = str(umo).split(":", 2)
        if len(parts) != 3:
            return False
        _pid, kind, sid = parts
        if "Group" in kind:
            return sid in self._list_conf("daily_context_groups")
        if "Friend" in kind:
            return sid.lstrip("o") in [q.lstrip("o") for q in self._list_conf("daily_context_friends")]
        return False

    def _load_context(self) -> dict:
        if getattr(self, "_ctx_cache", None) is not None:
            return self._ctx_cache
        data = {}
        path = self._context_file()
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8")) or {}
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[Qzone] 读上下文缓存失败: {e}")
            data = {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault("sessions", {})
        self._ctx_cache = data
        return data

    def _save_context(self) -> None:
        data = getattr(self, "_ctx_cache", None)
        if not data:
            return
        path = self._context_file()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            self._ctx_dirty = False
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[Qzone] 写上下文缓存失败: {e}")

    def _record_line(self, umo: str, name: str, text: str) -> None:
        """把一条聊天记进内存（脏了攒着，定期落盘）。"""
        text = (text or "").strip()
        if not text:
            return
        keep_days = int(self._conf("daily_record_keep_days", 7) or 7)
        cap = int(self._conf("daily_record_per_session", 200) or 200)
        data = self._load_context()
        bucket = data["sessions"].setdefault(umo, [])
        bucket.append({"t": time.time(), "n": name[:24], "x": text[:300]})
        if len(bucket) > cap:
            del bucket[:-cap]
        if keep_days > 0:
            cutoff = time.time() - keep_days * 86400
            data["sessions"][umo] = [r for r in bucket if float(r.get("t") or 0) >= cutoff]
        self._ctx_dirty = True
        self._ctx_writes = getattr(self, "_ctx_writes", 0) + 1
        if self._ctx_writes % 20 == 0:
            self._save_context()

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def _record_context(self, event: AstrMessageEvent):
        """记下群聊/私聊里的一句话，供定时说说当素材（原名原话，不依赖别的插件）。"""
        if not self._conf("enabled", True) or not self._conf("daily_record", True):
            return
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if not umo or not self._context_wanted(umo):
            return
        try:
            name = event.get_sender_name() or ""
        except Exception:  # noqa: BLE001
            name = ""
        text = str(getattr(event, "message_str", "") or "")
        self._record_line(umo, name, text)

    def _recent_from_records(self, kind: str, sid: str, limit: int, platform_id: str = "") -> List[str]:
        """从自己记的上下文里取某个会话最近若干条。"""
        data = self._load_context()
        out: List[str] = []
        for umo, bucket in (data.get("sessions") or {}).items():
            parts = str(umo).split(":", 2)
            if len(parts) != 3:
                continue
            pid, k, s = parts
            if kind not in k:
                continue
            if s.lstrip("o") != str(sid).lstrip("o"):
                continue
            if platform_id and pid != platform_id:
                continue
            for rec in bucket or []:
                name, text = str(rec.get("n") or ""), str(rec.get("x") or "").strip()
                if not text:
                    continue
                out.append(f"[{name or '某人'}] {text}")
        return out[-limit:] if limit > 0 else out

    def _resolve_external_targets(self, raw: str) -> List[Path]:
        """把一条配置项展开成要读的文件列表（旧 → 新，便于最后截尾时留下最新的）。"""
        max_files = max(1, int(self._conf("daily_context_paths_max_files", 1) or 1))
        p = Path(raw)
        try:
            if "*" in raw or "?" in raw:
                hits = [x for x in p.parent.glob(p.name) if x.is_file()]
            elif p.is_dir():
                hits = [x for x in p.iterdir() if x.is_file()]
            elif p.is_file():
                return [p]
            else:
                logger.info(f"[Qzone] 外部记录没找到，跳过：{raw}")
                return []
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[Qzone] 解析外部记录路径失败 {raw}: {e}")
            return []
        if not hits:
            logger.info(f"[Qzone] 外部记录没找到，跳过：{raw}")
            return []
        # 旧的在前、新的在后：合并后统一取尾部时，留下的就是最新的对话
        hits.sort(key=lambda x: x.stat().st_mtime)
        return hits[-max_files:]

    def _external_context_lines(self, limit: int) -> List[str]:
        """从外部聊天记录里取最后若干行。

        支持三种写法，格式**不做任何解析**（原样当素材，别人导出成什么样都行）：
        - 文件路径
        - 目录
        - 通配符（如 `.../astrbot_*_2026-10-26.log`）

        目录/通配符匹配到多个文件时，读几个由 `daily_context_paths_max_files` 决定
        （默认 1 = 只要最新的那个）。
        """
        out: List[str] = []
        seen: set = set()
        for raw in self._list_conf("daily_context_paths"):
            for target in self._resolve_external_targets(raw):
                try:
                    key = str(target.resolve()).lower()
                except Exception:  # noqa: BLE001
                    key = str(target).lower()
                if key in seen:  # 多个配置项指到同一个文件时别重复读
                    continue
                seen.add(key)
                try:
                    with open(target, encoding="utf-8", errors="replace") as f:
                        lines = [ln.strip() for ln in f if ln.strip()]
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[Qzone] 读外部记录失败 {target}: {e}")
                    continue
                logger.info(f"[Qzone] 外部记录 {target.name}：{len(lines)} 行")
                out.extend(lines[-limit:] if limit > 0 else lines)
        return out

    def _daily_context(self, platform_id: str = "") -> str:
        """把最近的聊天记录拼成素材。

        来源三处：自己记的会话 → 私聊没记录时回落会话库 → `daily_context_paths` 里的外部记录。
        `platform_id` 非空时只取**这个 bot 自己**的会话，多 bot 不串素材。
        """
        lines_n = int(self._conf("daily_context_lines", 40) or 40)
        max_chars = int(self._conf("daily_context_max_chars", 1500) or 1500)
        chunks: List[str] = []
        for gid in self._list_conf("daily_context_groups"):
            chunks.extend(self._recent_from_records("Group", gid, lines_n, platform_id))
        for qq in [q.lstrip("o") for q in self._list_conf("daily_context_friends")]:
            got = self._recent_from_records("Friend", qq, lines_n, platform_id)
            if not got:
                # 刚装上还没攒到记录：退回 AstrBot 已有的会话历史（私聊内容干净，没有注入块）
                got = self._daily_context_friend(qq, lines_n, platform_id)
            chunks.extend(got)
        chunks.extend(self._external_context_lines(lines_n))
        if not chunks:
            return ""
        text = "\n".join(chunks[-lines_n:])
        if len(text) > max_chars:
            text = text[-max_chars:]
        return text

    # ---------------- 定时说说：从会话历史取私聊 ----------------
    def _db_path(self) -> Path | None:
        db = str(self._conf("daily_context_db", "") or "").strip()
        if db:
            p = Path(db)
            return p if p.exists() else None
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_root

            p = Path(get_astrbot_root()) / "data" / "data_v4.db"
        except Exception:  # noqa: BLE001
            p = Path("data") / "data_v4.db"
        return p if p.exists() else None

    @staticmethod
    def _history_text(raw) -> str:
        """把会话历史里的一条 content 还原成纯文本（去掉 system_reminder 之类的注入块）。"""
        if isinstance(raw, str):
            try:
                parts = ast.literal_eval(raw)
            except (ValueError, SyntaxError):
                return raw.strip()
        else:
            parts = raw
        if not isinstance(parts, (list, tuple)):
            return ""
        texts = [
            str(p.get("text", ""))
            for p in parts
            if isinstance(p, dict) and p.get("type") == "text"
        ]
        s = "".join(texts)
        # 注入块要整段去掉（里面是 User ID / 群规这类东西）；引用标记只去标记本身，
        # 被引用的原话属于聊天内容，留着当素材没问题。
        s = re.sub(r"<system_reminder>.*?</system_reminder>", "", s, flags=re.S)
        s = re.sub(r"<system_reminder>.*$", "", s, flags=re.S)
        s = s.replace("<Quoted Message>", "").replace("<quoted_message>", "")
        # 只处理 AstrBot 核心自己的注入格式（system_reminder / 引用标记）。
        # 其它插件或 persona 的标记（比如 [[ja]]、【档案】这类）不在这层剥离：
        # 那是各家自己的约定，通用插件替别人做决定只会误伤。
        return re.sub(r"\s+", " ", s).strip()

    def _daily_context_friend(self, qq: str, limit: int, platform_id: str = "") -> List[str]:
        """从 data_v4.db 的 conversations 里取某个私聊最近的对话。

        `platform_id` 非空时只取 `<platform_id>:FriendMessage:<qq>`，避免多 bot 串素材。
        """
        db = self._db_path()
        if db is None:
            return []
        pattern = (
            f"{platform_id}:FriendMessage:{qq}"
            if platform_id
            else f"%FriendMessage:{qq}"
        )
        try:
            con = sqlite3.connect(str(db), timeout=5)
            # 只读打开，别把 AstrBot 的库写坏
            con.execute("pragma query_only = 1")
            # 排序大字段时 SQLite 会建临时文件，个别环境（TEMP 落在不可写目录）建不出来，
            # 会报 "unable to open database file"——强制走内存临时表
            con.execute("pragma temp_store = memory")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[Qzone] 打开会话库失败: {e}")
            return []
        try:
            # 先只按 rowid/时间排序（小字段），再单独取那条的大字段，避免整表排序时落临时文件
            row = con.execute(
                "select content from conversations where rowid = ("
                "  select rowid from conversations where user_id like ?"
                "  order by updated_at desc limit 1)",
                (pattern,),
            ).fetchone()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[Qzone] 读会话库失败: {e}")
            return []
        finally:
            con.close()
        if not row or not row[0]:
            return []
        try:
            msgs = json.loads(row[0])
        except Exception:  # noqa: BLE001
            return []
        out: List[str] = []
        for m in msgs if isinstance(msgs, list) else []:
            if not isinstance(m, dict):
                continue
            text = self._history_text(m.get("content"))
            if not text:
                continue
            who = "我" if str(m.get("role")) == "assistant" else "对方"
            out.append(f"[私聊/{who}] {text[:200]}")
        return out[-limit:] if limit > 0 else out

    def _daily_prompt_for(self, platform_id: str = "") -> str:
        """取提示词：`daily_prompt_map` 里有这个 bot 就用它，否则用 `daily_prompt`。"""
        base = str(self._conf("daily_prompt", "") or "").strip()
        if not platform_id:
            return base
        raw = self._conf("daily_prompt_map", []) or []
        if isinstance(raw, str):
            raw = [x for x in raw.splitlines() if x.strip()]
        for item in raw:
            s = str(item)
            if "," not in s:
                continue
            pid, val = s.split(",", 1)
            if pid.strip() == platform_id and val.strip():
                return val.strip()
        return base

    async def _daily_text(self, platform_id: str = "") -> str:
        """定时说说的正文：优先用固定文案；留空就让模型按 daily_prompt 现写。

        `platform_id` 用于按 bot 取提示词与上下文。
        """
        fixed = str(self._conf("daily_content", "") or "").strip()
        if fixed:
            return fixed
        prompt = self._daily_prompt_for(platform_id)
        if not prompt:
            return ""
        now = time.localtime()
        prompt = (
            prompt.replace("{date}", time.strftime("%Y-%m-%d", now))
            .replace("{time}", time.strftime("%H:%M", now))
            .replace("{weekday}", "一二三四五六日"[now.tm_wday])
        )
        context = self._daily_context(platform_id)
        if context:
            prompt = (
                f"{prompt}\n\n下面是最近的聊天记录（只当素材用，别逐条复述、别照抄）：\n"
                f"{context}"
            )
        else:
            logger.info("[Qzone] 定时说说没有可用的上下文（daily_context_groups/friends 没配或为空）")
        fallback = str(self._conf("daily_fallback", "") or "").strip()
        try:
            provider = self.context.get_using_provider()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[Qzone] 取对话模型失败: {e}")
            provider = None
        if provider is None:
            logger.warning("[Qzone] 没有可用的对话模型，定时发说说这次跳过")
            return fallback
        try:
            resp = await provider.text_chat(prompt=prompt)
            text = self._clean_llm_text(str(getattr(resp, "completion_text", "") or ""))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[Qzone] 模型写说说失败: {e}")
            return fallback
        if not text:
            logger.info("[Qzone] 模型没写出内容，改用 daily_fallback")
            return fallback
        # 写得像流水账、或直接引用了素材原话，就重写一次（只重试一次，避免死循环）
        problems = []
        if self._looks_like_report(text):
            problems.append("流水账")
        if context and self._quotes_source(text, context):
            problems.append("引用了聊天原文")
        if problems:
            logger.info(f"[Qzone] 这条{'、'.join(problems)}，重写一次：{text[:40]}…")
            retry_prompt = (
                f"{prompt}\n\n注意：上一次写成了{'、'.join(problems)}。"
                "这次只讲一个瞬间或一件事，别用「然后/接着/后来/最后」串时间线，不要分点罗列，"
                "也不要引用、复述聊天里的原话——用自己的话说。"
            )
            try:
                resp = await provider.text_chat(prompt=retry_prompt)
                again = self._clean_llm_text(str(getattr(resp, "completion_text", "") or ""))
                if again:
                    text = again
                    still = []
                    if self._looks_like_report(text):
                        still.append("流水账")
                    if context and self._quotes_source(text, context):
                        still.append("仍引用原文")
                    if still:
                        logger.warning(f"[Qzone] 重写后{'、'.join(still)}，就这样发")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[Qzone] 重写失败，保留第一版: {e}")
        logger.info(f"[Qzone] 模型写好了定时说说：{text[:40]}…（{len(text)} 字）")
        return text

    @staticmethod
    def _quotes_source(text: str, context: str, n: int = 8) -> bool:
        """正文里有没有跟素材重合的长片段（说明它在照抄原话）。

        n=8：连续 8 个字符和素材一致才算照抄。中文里正常改写几乎不会连续重合同样的 8 个字，
        而「明明都说了那里不行」这种顺口照抄会被抓到。
        """
        t = re.sub(r"\s+", "", text or "")
        if len(t) < n:
            return False
        step = max(1, n // 2)
        for line in (context or "").splitlines():
            body = re.sub(r"^\[[^\]]*\]\s*", "", line).strip()
            body = re.sub(r"\s+", "", body)
            if len(body) < n:
                continue
            for i in range(0, len(body) - n + 1, step):
                if body[i:i + n] in t:
                    return True
        return False

    @staticmethod
    def _looks_like_report(text: str) -> bool:
        """粗略判断是不是「流水账/汇报」：时间连接词太多，或者分点罗列。"""
        s = text or ""
        marks = ("然后", "接着", "后来", "最后", "先是", "之后", "再后来", "紧接着")
        hits = sum(s.count(m) for m in marks)
        bullets = len(re.findall(r"(?m)^\s*(?:[-*·]|\d+[.、)]|[一二三四五六七八九十]+[、.])", s))
        return hits >= 3 or bullets >= 2

    async def _daily_loop(self) -> None:
        """每 20 秒看一次钟，命中就发；每天每个时刻（每个 bot）只发一次。"""
        while True:
            try:
                await asyncio.sleep(20)
                if not self._conf("enabled", True):
                    continue
                now = time.time()
                per_bot = bool(self._conf("daily_per_bot", True))
                targets = self._bot_targets() if per_bot else []
                if targets:
                    for pid, label, act in targets:
                        key = self._daily_should_fire(now, pid)
                        if not key:
                            continue
                        logger.info(f"[Qzone] 到点了，给「{label}」发定时说说")
                        await self._daily_post_bot(pid, label, act)
                    continue
                # 没分 bot（或拿不到实例）：沿用单账号逻辑
                key = self._daily_should_fire(now)
                if not key:
                    continue
                content = await self._daily_text()
                if not content:
                    logger.warning(f"[Qzone] 定时发说说（{key.strip()}）没有正文，跳过")
                    continue
                ok, msg = await self.publish(content)
                logger.info(f"[Qzone] 定时发说说（{key.strip()}）ok={ok} result={msg}")
            except asyncio.CancelledError:
                return
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[Qzone] 定时发说说异常: {e}")

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
        cookie, diag = await self._try_get_cookies(call_action, "事件")
        self._last_cookie_diag = diag
        return cookie

    async def _try_get_cookies(self, call_action, tag: str) -> Tuple[str, List[Tuple[str, str, str]]]:
        """按 COOKIE_DOMAINS 顺序向协议端要 cookie，返回 (cookie, 每个域的诊断记录)。

        `qzone.qq.com` 排第一：它在 QQ 的 pskey 白名单里，而 `user.qzone.qq.com` 不在
        （协议端会直接报 "QQ did not return a p_skey for ..."，属于正常现象，不是故障）。
        """
        diag: List[Tuple[str, str, str]] = []
        for domain in COOKIE_DOMAINS:
            try:
                ret = await call_action("get_cookies", domain=domain)
            except Exception as e:  # noqa: BLE001
                why = str(e) or type(e).__name__
                diag.append((domain, "跳过", why[:90]))
                logger.info(f"[Qzone] 取 cookie 跳过 {domain}（{tag}）: {why[:160]}")
                continue
            cookie = self._extract_cookie(ret)
            code = ""
            if isinstance(ret, dict):
                # 注意 retcode=0 是「成功」，不能用 `or ""` 兜底（0 会被吃掉）
                raw_code = ret.get("retcode", "")
                code = "" if raw_code is None else str(raw_code)
            diag.append((domain, f"retcode={code}" if code else "ok", f"{len(cookie)} 字"))
            logger.info(
                f"[Qzone] get_cookies({domain}) {tag}: "
                f"retcode={code or '-'} cookie 长度={len(cookie)}"
            )
            if cookie:
                logger.info(f"[Qzone] 通过协议端拿到 cookie({domain}) {tag}")
                return cookie, diag
        return "", diag

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
        cookie, diag = await self._try_get_cookies(act, "平台实例")
        self._last_cookie_diag = diag
        return cookie

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
        cookie_override: str = "",
        uin_override: str = "",
    ) -> Tuple[bool, str]:
        """发一条说说，返回 (是否成功, 给人看的话)。event 可以为空。

        `cookie_override` / `uin_override` 供「每个 bot 各发一条」时按实例指定账号。
        """
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

        cookie = (cookie_override or "").strip() or await self._resolve_cookie_any(event)
        ok, msg = check_cookie(cookie)
        if not ok:
            return False, f"登录态不行：{msg}"
        uin = (uin_override or "").strip() or self._resolve_uin(cookie)
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
            lines = [f"检查没过：{msg}"]
            lines += self._cookie_diag_lines()
            auto = bool(self._conf("auto_fetch_cookie", False))
            if not auto:
                lines.append("auto_fetch_cookie 关着，只用了手填的 cookies")
            elif not cookie:
                lines.append("手填的 cookies 也要写成 p_skey=…; skey=…; uin=o…，只贴值会被忽略")
            return "\n".join(lines)
        jar = parse_cookie(cookie)
        keys = ",".join(sorted(k for k in jar if k in ("p_skey", "skey", "uin", "pt2gguin")))
        lines = [f"cookie 看起来还行，关键字段：{keys}；目标 QQ：{self._resolve_uin(cookie)}"]
        lines += self._cookie_diag_lines()
        return "\n".join(lines)

    def _cookie_diag_lines(self) -> List[str]:
        """把最近一次向协议端取 cookie 的逐域结果整理成几行。"""
        diag = getattr(self, "_last_cookie_diag", []) or []
        if not diag:
            return ["（这次没有向协议端取 cookie）"]
        lines = ["协议端取 cookie（按域）："]
        for domain, state, info in diag:
            lines.append(f"  {domain}: {state} {info}".rstrip())
        return lines

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
