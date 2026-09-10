#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QQ空间说说发布的核心逻辑（与 AstrBot 解耦，便于单测）。

发布走的接口是移动/网页版通用的 emotion_cgi_publish_v6：
    POST https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6?g_tk=<gtk>
Cookie 里必须带 p_skey（或 uin+skey），g_tk 由 p_skey 推算。
"""
from __future__ import annotations

import random
import time
from typing import Dict, Optional, Tuple

PUBLISH_URL = (
    "https://user.qzone.qq.com/proxy/domain/"
    "taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6"
)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
    ),
    "Origin": "https://user.qzone.qq.com",
    "Referer": "https://user.qzone.qq.com/",
    "Content-Type": "application/x-www-form-urlencoded",
}


def get_gtk(skey: str) -> str:
    """由 skey / p_skey 计算 g_tk（Qzone 经典的 djb2 变体）。"""
    h = 5381
    for ch in skey or "":
        h += (h << 5) + ord(ch)
        h &= 0x7FFFFFFF
    return str(h & 0x7FFFFFFF)


def parse_cookie(cookie: str) -> Dict[str, str]:
    """把 "a=1; b=2" 形式的 cookie 串解析成字典。"""
    jar: Dict[str, str] = {}
    for part in (cookie or "").replace("\n", ";").split(";"):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k, v = k.strip(), v.strip()
        if k:
            jar[k] = v
    return jar


def pick_skey(jar: Dict[str, str]) -> str:
    """优先 p_skey，其次 skey，再其次 rv2/today 之类。"""
    for key in ("p_skey", "skey", "rv2", "s_key"):
        if jar.get(key):
            return jar[key]
    return ""


def build_payload(uin: str, content: str, richval: Optional[str] = None) -> Dict[str, str]:
    payload = {
        "syn_tweet_verson": "1",
        "paramstr": "1",
        "who": "1",
        "conformat": "1",
        "r": f"{random.random():.16f}",
        "con": content,
        "content": content,
        "feedversion": "1",
        "ver": "1",
        "ugc_right": "1",
        "to_sign": "0",
        "hostuin": str(uin),
        "code_version": "1",
        "format": "json",
        "qzreferrer": f"https://user.qzone.qq.com/{uin}",
        "t": str(int(time.time() * 1000)),
    }
    if richval:
        payload["richtype"] = "1"
        payload["richval"] = richval
    return payload


def check_cookie(cookie: str) -> Tuple[bool, str]:
    """粗略校验 cookie 是否够用。"""
    jar = parse_cookie(cookie)
    if not jar:
        return False, "cookie 是空的，先填上 p_skey 那一串吧"
    missing = [k for k in ("p_skey", "skey") if not jar.get(k)]
    if len(missing) == 2:
        return False, "cookie 里找不到 p_skey 或 skey，登录态可能过期了"
    if not jar.get("uin") and not jar.get("pt2gguin"):
        return False, "cookie 里没有 uin，无法确定要发到哪个空间"
    return True, "ok"


# ---------------- 带图说说：先把图片传上空间图床 ----------------
UPLOAD_URL = "https://up.qzone.qq.com/cgi-bin/upload/cgi_upload_image"

UPLOAD_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
    ),
    "Origin": "https://user.qzone.qq.com",
    "Referer": "https://user.qzone.qq.com/",
    "Content-Type": "application/x-www-form-urlencoded",
}

# 空间一条说说最多带 9 张图
MAX_IMAGES = 9


def build_upload_body(
    uin: str, skey: str, pskey: str, g_tk: str, b64: str
) -> Dict[str, str]:
    """upload/cgi_upload_image 的表单字段（走 x-www-form-urlencoded）。"""
    back_urls = (
        "http://upbak.photo.qzone.qq.com/cgi-bin/upload/cgi_upload_image,"
        "http://119.147.64.75/cgi-bin/upload/cgi_upload_image"
        f"&url={UPLOAD_URL}?g_tk={g_tk}"
    )
    return {
        "filename": "filename",
        "uin": str(uin),
        "skey": skey,
        "zzpaneluin": str(uin),
        "p_uin": str(uin),
        "p_skey": pskey,
        "uploadtype": "1",
        "albumtype": "7",
        "exttype": "0",
        "refer": "shuoshuo",
        "output_type": "jsonhtml",
        "charset": "utf-8",
        "output_charset": "utf-8",
        "upload_hd": "1",
        "hd_width": "2048",
        "hd_height": "10000",
        "hd_quality": "96",
        "backUrls": back_urls,
        "base64": "1",
        "jsonhtml_callback": "callback",
        "picfile": b64,
        "qzreferrer": f"https://user.qzone.qq.com/{uin}/main",
    }


def parse_upload_response(text: str) -> Dict[str, str]:
    """把 frameElement.callback({...}); 之类的响应解析成 data 字典。"""
    body = text or ""
    key = ""
    idx = -1
    for cand in ("frameElement.callback", "_Callback", "callback"):
        idx = body.find(cand)
        if idx != -1:
            key = cand
            break
    if idx == -1:
        raise ValueError(f"上传响应不认识：{body[:200]!r}")
    seg = body[idx + len(key):]
    start, end = seg.find("{"), seg.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"上传响应里没有 JSON：{body[:200]!r}")
    import json as _json

    obj = _json.loads(seg[start : end + 1])
    data = obj.get("data") or {}
    sub = obj.get("subcode", obj.get("code", 0))
    if not data or (sub not in (0, None, "0")):
        raise ValueError(
            "上传失败："
            + str(obj.get("message") or obj.get("msg") or obj.get("subcode") or obj)[:200]
        )
    if not (data.get("lloc") or data.get("url")):
        raise ValueError(f"上传返回没有图片地址：{str(data)[:200]}")
    return data


def build_richval(data: Dict[str, str]) -> str:
    """按空间要的格式拼 richval：,albumid,lloc,sloc,type,height,width,,height,width"""
    albumid = data.get("albumid", "")
    lloc = data.get("lloc", "")
    sloc = data.get("sloc") or lloc
    typ = data.get("type", "1")
    height = data.get("height", "0")
    width = data.get("width", "0")
    return f",{albumid},{lloc},{sloc},{typ},{height},{width},,{height},{width}"


def join_richvals(richvals) -> str:
    """多张图用制表符拼在一起，没图就返回空串。"""
    return "\t".join([r for r in richvals if r])


def prepare_image_bytes(raw: bytes, max_side: int = 1600, quality: int = 88) -> bytes:
    """压缩一下再传，省流量也少被接口嫌弃；没有 PIL 就原样传。"""
    try:
        import io

        from PIL import Image

        im = Image.open(io.BytesIO(raw))
        im.load()
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGB")
        w, h = im.size
        biggest = max(w, h) or 1
        if biggest > max_side:
            scale = max_side / biggest
            im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=quality, optimize=True)
        out = buf.getvalue()
        return out if 0 < len(out) < len(raw) else raw
    except Exception:  # noqa: BLE001
        return raw
