"""日志和配置输出使用的密钥脱敏。"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


SENSITIVE_KEYS = {
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "smtp_password",
    "webhook",
    "key",
    "access_token",
    "qmt_account",
}
ACCOUNT_KEYS = {
    "account",
    "account_id",
    "资金账号",
    "qmt_account",
    "jqdata_user",
}

_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_KEY_VALUE = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization)"
    r"\s*[=:]\s*([^\s,;]+)"
)


def mask_secret(value, *, visible: int = 4) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= visible * 2:
        return "*" * len(text)
    return f"{text[:visible]}...{text[-visible:]}"


def mask_account(value) -> str:
    text = str(value or "")
    if len(text) <= 4:
        return "*" * len(text)
    return f"{text[:2]}***{text[-2:]}"


def redact(value, *, key: str | None = None, account: bool = False):
    """递归脱敏 dict/list/标量；保持结构不变。"""
    normalized_key = str(key or "").strip().lower()
    if isinstance(value, dict):
        return {
            item_key: redact(
                item_value,
                key=str(item_key),
                account=account or str(item_key).lower() in ACCOUNT_KEYS,
            )
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item, key=key, account=account) for item in value]
    if normalized_key in SENSITIVE_KEYS:
        if normalized_key == "webhook":
            return redact_url(value)
        if normalized_key in ACCOUNT_KEYS:
            return mask_account(value)
        return mask_secret(value)
    if account and normalized_key in ACCOUNT_KEYS:
        return mask_account(value)
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_text(value) -> str:
    text = str(value or "")
    text = _BEARER.sub("Bearer ***", text)
    text = _KEY_VALUE.sub(lambda match: f"{match.group(1)}=***", text)
    if text.startswith(("http://", "https://")):
        return redact_url(text)
    return text


def redact_url(value) -> str:
    text = str(value or "")
    try:
        parts = urlsplit(text)
        query = []
        for key, item in parse_qsl(parts.query, keep_blank_values=True):
            query.append((
                key,
                mask_secret(item) if key.lower() in SENSITIVE_KEYS else item,
            ))
        return urlunsplit((
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(query),
            parts.fragment,
        ))
    except ValueError:
        return text
