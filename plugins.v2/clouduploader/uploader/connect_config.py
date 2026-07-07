"""解析 Admin 集成页复制的 MoviePilot 对接配置。"""
from __future__ import annotations

import json
import re
from typing import Any


def parse_connect_paste(raw: object) -> dict[str, str]:
    """从粘贴块提取 api_base / api_admin_key。支持 KEY=VAL 行或 JSON。"""
    text = str(raw or "").strip()
    if not text:
        return {}

    if text.startswith("{"):
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return _normalize_keys(data)
        except json.JSONDecodeError:
            pass

    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, val = line.split("=", 1)
        elif ":" in line and not line.startswith("http"):
            key, val = line.split(":", 1)
        else:
            continue
        key = _canon_key(key.strip())
        val = val.strip().strip('"').strip("'")
        if key and val:
            out[key] = val
    return out


def _canon_key(key: str) -> str:
    k = key.lower().replace("-", "_")
    if k in ("site_url", "site", "api_base", "base_url", "stream_site"):
        return "api_base"
    if k in ("api_admin_key", "admin_key", "admin_api_key", "x_admin_key"):
        return "api_admin_key"
    return k


def _normalize_keys(data: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, val in data.items():
        canon = _canon_key(str(key))
        if canon in ("api_base", "api_admin_key") and val:
            out[canon] = str(val).strip()
    return out


def merge_connect_paste(config: dict) -> dict:
    """将 connect_paste 解析结果合并进插件 config（粘贴块优先）。"""
    parsed = parse_connect_paste(config.get("connect_paste"))
    if not parsed:
        return config
    merged = dict(config)
    for key in ("api_base", "api_admin_key"):
        if parsed.get(key):
            merged[key] = parsed[key]
    return merged


if __name__ == "__main__":
    sample = "api_base=https://example.com\napi_admin_key=gyadmin_test123"
    got = parse_connect_paste(sample)
    assert got["api_base"] == "https://example.com"
    assert got["api_admin_key"] == "gyadmin_test123"
    print("ok", got)
