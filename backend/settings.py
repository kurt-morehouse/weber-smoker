"""Backend settings, from environment variables (all optional).

    SMOKER_DATA_DIR             cook CSVs to list and replay (default ../data)
    SMOKER_PI_HOST              the Pi running control/main.py (default 192.168.1.229)
    SMOKER_PI_PORT              its live WebSocket port (default 8765)
    SMOKER_LIVE                 0 turns the live connection off (default 1)
    SMOKER_COMMAND_TOKEN        token for cook commands; default: the contents
                                of ../control/command_token (the same file
                                deploy.sh pushes to the Pi)
    SMOKER_FRONTEND_DIR         the dashboard to serve at / (default ../frontend)
    SMOKER_RETRY_MAX_S          longest wait between reconnects (default 30)
"""
import os
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent


class Reader:
    def __init__(self, env):
        self.env = env

    def text(self, key, default):
        v = self.env.get(key)
        return v if v not in (None, "") else default

    def number(self, key, default, cast=float):
        v = self.env.get(key)
        if v in (None, ""):
            return default
        try:
            return cast(v)
        except ValueError:
            raise ValueError(f"{key}={v!r} is not a number")

    def flag(self, key, default):
        v = self.env.get(key)
        if v in (None, ""):
            return default
        return v.strip().lower() not in ("0", "false", "no", "off")


@dataclass
class Settings:
    data_dir: Path
    pi_host: str
    pi_port: int
    live: bool
    command_token: str | None
    frontend_dir: Path
    retry_max_s: float

    @property
    def pi_url(self):
        return f"ws://{self.pi_host}:{self.pi_port}"

    @property
    def journal_dir(self):
        return self.data_dir / "journal"


def _token(r):
    t = r.text("SMOKER_COMMAND_TOKEN", None)
    if t:
        return t.strip()
    p = HERE.parent / "control" / "command_token"
    try:
        return p.read_text().strip() or None
    except OSError:
        return None


def load(env=None):
    r = Reader(os.environ if env is None else env)
    return Settings(
        data_dir=Path(r.text("SMOKER_DATA_DIR", str(HERE.parent / "data"))).expanduser().resolve(),
        pi_host=r.text("SMOKER_PI_HOST", "192.168.1.229"),
        pi_port=r.number("SMOKER_PI_PORT", 8765, int),
        live=r.flag("SMOKER_LIVE", True),
        command_token=_token(r),
        frontend_dir=Path(r.text("SMOKER_FRONTEND_DIR", str(HERE.parent / "frontend"))).resolve(),
        retry_max_s=r.number("SMOKER_RETRY_MAX_S", 30.0),
    )
