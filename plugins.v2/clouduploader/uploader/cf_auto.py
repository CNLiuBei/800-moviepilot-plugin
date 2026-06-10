"""
Cloudflare R2 配置自动推导

基于 Cloudflare 官方规范（https://developers.cloudflare.com/r2/api/tokens/）：
- 用一个 R2 API Token 即可推导出 S3 兼容凭证：
    Access Key ID     = API Token 的 id
    Secret Access Key = SHA-256(API Token 的明文 value)
- 通过 API 还能拿到 Account ID、列出 R2 桶

用户只需在插件里填一个 R2 API Token（在 CF 后台 R2 → Manage API Tokens 创建，
权限选 Object Read & Write 或 Admin Read & Write），其余 R2 配置自动获取。
"""
from __future__ import annotations

import hashlib
from typing import Optional

import httpx

CF_API = "https://api.cloudflare.com/client/v4"


def _cf_request(method: str, url: str, token_value: str, timeout: int = 15, **kwargs) -> httpx.Response:
    """
    调用 Cloudflare API。

    MoviePilot 所在环境可能带有 NO_PROXY=...,::1,...。httpx 在读取这类环境代理
    配置时会把裸 IPv6 ::1 误解析成端口，导致请求前抛 InvalidURL。Cloudflare API
    这里不依赖本机代理环境，显式关闭 trust_env 避免被外部环境污染。
    """
    return httpx.request(
        method,
        url,
        headers={"Authorization": f"Bearer {token_value}"},
        timeout=timeout,
        trust_env=False,
        **kwargs,
    )


def derive_access_key(token_value: str) -> str:
    """Secret Access Key = SHA-256(token 明文)。"""
    return hashlib.sha256(token_value.encode("utf-8")).hexdigest()


def verify_token(token_value: str, timeout: int = 15, log=print) -> Optional[str]:
    """
    校验 token 并返回其 id（即 Access Key ID）。失败返回 None。
    GET /user/tokens/verify
    """
    try:
        resp = _cf_request(
            "GET",
            f"{CF_API}/user/tokens/verify",
            token_value,
            timeout=timeout,
        )
        data = resp.json()
        if resp.status_code == 200 and data.get("success"):
            return data.get("result", {}).get("id")
        log(f"   ❌ CF Token 校验失败：HTTP {resp.status_code} {data.get('errors') or ''}")
    except Exception as e:
        log(f"   ❌ CF Token 校验异常：{type(e).__name__}: {e}")
    return None


def get_account_id(token_value: str, timeout: int = 15, log=print) -> Optional[str]:
    """
    获取账户 ID。优先取 token 可访问的第一个账户。
    GET /accounts
    """
    try:
        resp = _cf_request(
            "GET",
            f"{CF_API}/accounts",
            token_value,
            params={"per_page": 50},
            timeout=timeout,
        )
        data = resp.json()
        if resp.status_code == 200 and data.get("success"):
            results = data.get("result", [])
            if results:
                return results[0].get("id")
        log(f"   ❌ 获取账户 ID 失败：HTTP {resp.status_code} {data.get('errors') or ''}")
    except Exception as e:
        log(f"   ❌ 获取账户 ID 异常：{type(e).__name__}: {e}")
    return None


def list_buckets(token_value: str, account_id: str, timeout: int = 15, log=print) -> list[str]:
    """
    列出账户下的 R2 桶名。
    GET /accounts/{account_id}/r2/buckets
    """
    try:
        resp = _cf_request(
            "GET",
            f"{CF_API}/accounts/{account_id}/r2/buckets",
            token_value,
            timeout=timeout,
        )
        data = resp.json()
        if resp.status_code == 200 and data.get("success"):
            buckets = data.get("result", {}).get("buckets", [])
            return [b.get("name") for b in buckets if b.get("name")]
        log(f"   ❌ 列出 R2 桶失败：HTTP {resp.status_code} {data.get('errors') or ''}")
    except Exception as e:
        log(f"   ❌ 列出 R2 桶异常：{type(e).__name__}: {e}")
    return []


def create_bucket(token_value: str, account_id: str, name: str, timeout: int = 15, log=print) -> bool:
    """
    创建 R2 桶。已存在或创建成功均返回 True。
    POST /accounts/{account_id}/r2/buckets
    """
    try:
        resp = _cf_request(
            "POST",
            f"{CF_API}/accounts/{account_id}/r2/buckets",
            token_value,
            json={"name": name},
            timeout=timeout,
        )
        if resp.status_code in (200, 201):
            return True
        # 已存在
        data = resp.json()
        for err in data.get("errors", []):
            # 10004: bucket already exists
            if err.get("code") == 10004 or "exist" in str(err.get("message", "")).lower():
                return True
        log(f"   ❌ 创建 R2 桶失败：HTTP {resp.status_code} {data.get('errors') or ''}")
    except Exception as e:
        log(f"   ❌ 创建 R2 桶异常：{type(e).__name__}: {e}")
    return False


def auto_configure(token_value: str, prefer_bucket: str = "", create_if_missing: bool = False,
                   log=print) -> Optional[dict]:
    """
    用一个 R2 API Token 自动推导完整 R2 配置。

    Returns:
        成功返回 dict(account_id, access_key_id, secret_access_key, bucket, buckets)，
        失败返回 None。
    """
    token_value = (token_value or "").strip()
    if not token_value:
        return None

    access_key_id = verify_token(token_value, log=log)
    if not access_key_id:
        log("   ❌ CF Token 校验失败：请使用「User API Token」(cfut_ 开头)，")
        log("      在 https://dash.cloudflare.com/profile/api-tokens 创建，赋予 R2 读写权限；")
        log("      R2 后台生成的 cfat_ 令牌不适用于自动配置。")
        return None
    log("   ✅ CF Token 有效")

    account_id = get_account_id(token_value, log=log)
    if not account_id:
        log("   ❌ 无法获取账户 ID（Token 需具备账户访问权限）")
        return None
    log(f"   ✅ 账户 ID: {account_id}")

    secret = derive_access_key(token_value)

    buckets = list_buckets(token_value, account_id, log=log)
    bucket = ""
    if prefer_bucket and prefer_bucket in buckets:
        bucket = prefer_bucket
    elif prefer_bucket and create_if_missing:
        if create_bucket(token_value, account_id, prefer_bucket, log=log):
            bucket = prefer_bucket
            log(f"   ✅ 已创建桶: {prefer_bucket}")
    elif prefer_bucket:
        # 指定了目标桶但不存在且未开启自动创建：不静默改用其他桶，避免上传到错误位置
        log(f"   ⚠️ 指定桶 {prefer_bucket} 不存在（可用: {', '.join(buckets) or '无'}）；"
            f"请创建该桶或勾选自动创建")
    elif buckets:
        bucket = buckets[0]
    if bucket:
        log(f"   ✅ 使用桶: {bucket}（可用: {', '.join(buckets) or '无'}）")
    else:
        log(f"   ⚠️ 未确定桶（可用: {', '.join(buckets) or '无'}）")

    return {
        "account_id": account_id,
        "access_key_id": access_key_id,
        "secret_access_key": secret,
        "bucket": bucket,
        "buckets": buckets,
    }
