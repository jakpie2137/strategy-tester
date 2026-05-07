# -*- coding: utf-8 -*-
import os
from pathlib import Path
from urllib.parse import quote, unquote
from typing import Union

def load_env(path: Union[str, Path] = ".env", override: bool = True) -> None:
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        key = k.strip()
        val = v.strip().strip('"').strip("'")
        if not override and key in os.environ:
            continue
        os.environ[key] = val

def mask_dsn(dsn: str) -> str:
    if "://" not in dsn:
        return dsn
    scheme, rest = dsn.split("://", 1)
    if "@" not in rest:
        return dsn
    creds, hostpart = rest.split("@", 1)
    if ":" in creds:
        user, _pw = creds.split(":", 1)
        return f"{scheme}://{user}:***@{hostpart}"
    return dsn

def get_dsn() -> str:
    dsn = os.getenv("PG_DSN")
    if dsn:
        return dsn
    user = os.getenv("PG_USER", "postgres")
    password = os.getenv("PG_PASSWORD", "")
    password_norm = quote(unquote(password), safe="")
    host = os.getenv("PG_HOST", "127.0.0.1")
    port = os.getenv("PG_PORT", "2137")
    database = os.getenv("PG_DATABASE", "live_data")
    connect_timeout = os.getenv("PG_CONNECT_TIMEOUT", "3")
    return f"postgresql://{user}:{password_norm}@{host}:{port}/{database}?connect_timeout={connect_timeout}"
