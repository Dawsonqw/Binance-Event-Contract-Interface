"""Private storage for Binance session credentials and browser state.

No credentials are stored inside the Git repository.
"""
import json
import os
import stat
import tempfile
from pathlib import Path


def state_dir():
    path = Path(os.environ.get(
        "BINANCE_EVENT_STATE_DIR",
        str(Path.home() / ".local" / "state" / "binance-event-contract"),
    )).expanduser()
    if path.is_symlink():
        raise RuntimeError("Refusing a symlinked state directory")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError("Invalid state directory")
    path.chmod(0o700)
    return path


def save_token(data):
    """Atomically store credentials as mode 0600; never log them."""
    directory = state_dir()
    target = directory / "token.json"
    if target.is_symlink():
        raise RuntimeError("Refusing a symlinked token file")
    fd, temp_path = tempfile.mkstemp(prefix=".token-", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, target)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def load_token():
    """Refuse token files accessible to another user."""
    path = state_dir() / "token.json"
    if path.is_symlink():
        raise RuntimeError("Refusing a symlinked token file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        mode = stat.S_IMODE(os.fstat(stream.fileno()).st_mode)
        if mode & 0o077:
            raise PermissionError("Token file must be owner-only (chmod 600)")
        data = json.load(stream)
    if not isinstance(data, dict) or not all(
        isinstance(data.get(field), str) and data[field]
        for field in ("csrftoken", "p20t")
    ):
        raise ValueError("Invalid token file")
    return data
