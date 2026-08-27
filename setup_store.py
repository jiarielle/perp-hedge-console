"""
.env 安全读写:网页设置向导的唯一持久化通道。

- 写入时保持 600 权限,只覆盖已有键值或追加新键,不动其他行
- 读取永远返回脱敏值(仅前 6 后 4 位),私钥明文只进不出
- clear() 一键抹除全部密钥
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Dict, Optional

ENV_PATH = Path(__file__).parent / ".env"

SECRET_KEYS = {"LIGHTER_API_PRIVATE_KEY", "POPDEX_SIGNER_PRIVATE_KEY"}
MODE_KEYS = {"DRY_RUN", "LIVE_TRADING_ACK"}


def read_raw() -> Dict[str, str]:
    """解析 .env 为 dict(不含默认值,只看文件里实际写了什么)。"""
    values: Dict[str, str] = {}
    if not ENV_PATH.exists():
        return values
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip()
    return values


def update(updates: Dict[str, Optional[str]]) -> None:
    """批量写入键值(None 表示删除该键);保留原有注释与其他行。"""
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() \
        if ENV_PATH.exists() else []
    pending = {k: v for k, v in updates.items()}
    out = []
    for line in lines:
        key, sep, _ = line.partition("=")
        k = key.strip()
        if sep and k in pending:
            v = pending.pop(k)
            if v is not None:
                out.append(f"{k}={v}")
            # v is None → 删除该行
        else:
            out.append(line)
    for k, v in pending.items():
        if v is not None:
            out.append(f"{k}={v}")
    ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")
    ENV_PATH.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 600


def mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 12:
        return value[:3] + "***"
    return f"{value[:6]}…{value[-4:]}"


def get_masked(keys: list[str]) -> Dict[str, str]:
    raw = read_raw()
    result = {}
    for k in keys:
        v = raw.get(k, "")
        result[k] = {"configured": bool(v), "preview": mask(v) if k in SECRET_KEYS else v}
    return result


def clear_secrets() -> None:
    update({k: "" for k in SECRET_KEYS})


def env_bool(key: str, default: str = "true") -> bool:
    return os.environ.get(key, read_raw().get(key, default)).lower() == "true"
