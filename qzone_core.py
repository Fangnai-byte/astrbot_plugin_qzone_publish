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


def build_payload(uin: str, content: str) -> Dict[str, str]:
    return {
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
