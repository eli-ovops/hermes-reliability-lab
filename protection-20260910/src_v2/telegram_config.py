"""Telegram credential loading without source-code defaults."""
from __future__ import annotations

from collections.abc import Mapping


def load_telegram_api_credentials(env: Mapping[str, str]) -> tuple[int, str]:
    raw_api_id = env.get("TG_API_ID", "").strip()
    api_hash = env.get("TG_API_HASH", "").strip()
    if not raw_api_id:
        raise RuntimeError("缺少 TG_API_ID")
    if not api_hash:
        raise RuntimeError("缺少 TG_API_HASH")
    try:
        api_id = int(raw_api_id)
    except ValueError as exc:
        raise RuntimeError("TG_API_ID 格式无效") from exc
    return api_id, api_hash
