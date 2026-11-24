"""Environment bootstrap and shared paths/config for AI-Robot.

Enforces the strict rule that ALL caches, temporary files, models, and runtime
artifacts live inside the project directory (<project_root>/.cache and runtime).
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent
CACHE_DIR = ROOT_DIR / ".cache"
RUNTIME_DIR = ROOT_DIR / "runtime"
FRAMES_DIR = RUNTIME_DIR / "frames"
CONFIG_PATH = ROOT_DIR / "config.json"
INBOX_PATH = RUNTIME_DIR / "inbox.jsonl"
DIARY_PATH = RUNTIME_DIR / "diary.md"

_DLL_HANDLES: list[Any] = []
_ENV_CONFIGURED = False


def to_short_path(path: Path | str) -> str:
    """Return Windows 8.3 short path (ASCII-safe) when available, else str(path)."""
    p_str = str(path)
    if os.name != "nt":
        return p_str
    try:
        kernel32 = ctypes.windll.kernel32
        buf_len = kernel32.GetShortPathNameW(p_str, None, 0)
        if buf_len > 0:
            buf = ctypes.create_unicode_buffer(buf_len)
            res = kernel32.GetShortPathNameW(p_str, buf, buf_len)
            if res > 0:
                return buf.value
    except Exception:
        pass
    return p_str


def _ensure_directories() -> None:
    for d in (
        CACHE_DIR,
        CACHE_DIR / "pip",
        CACHE_DIR / "hf",
        CACHE_DIR / "hf" / "hub",
        CACHE_DIR / "torch",
        CACHE_DIR / "xdg",
        CACHE_DIR / "tmp",
        CACHE_DIR / "whisper",
        RUNTIME_DIR,
        FRAMES_DIR,
    ):
        d.mkdir(parents=True, exist_ok=True)


def _register_cuda_dll_dirs() -> None:
    if os.name != "nt":
        return
    site_packages = Path(sys.prefix) / "Lib" / "site-packages"
    candidates = [
        site_packages / "nvidia" / "cublas" / "bin",
        site_packages / "nvidia" / "cudnn" / "bin",
        site_packages / "nvidia" / "cuda_runtime" / "bin",
        site_packages / "torch" / "lib",
        site_packages / "ctranslate2",
    ]
    current_path = os.environ.get("PATH", "")
    path_parts = [p for p in current_path.split(os.pathsep) if p]

    for folder in candidates:
        if not folder.is_dir():
            continue
        folder_str = str(folder)
        if hasattr(os, "add_dll_directory"):
            try:
                # Keep handles alive for the lifetime of the process.
                handle = os.add_dll_directory(folder_str)
                _DLL_HANDLES.append(handle)
            except OSError:
                pass
        if folder_str not in path_parts:
            path_parts.insert(0, folder_str)

    os.environ["PATH"] = os.pathsep.join(path_parts)


def configure_env(force: bool = False) -> None:
    """Redirect all caches/temp dirs inside project .cache/ and expose CUDA DLL directories."""
    global _ENV_CONFIGURED
    if _ENV_CONFIGURED and not force:
        return

    _ensure_directories()

    tmp_str = str(CACHE_DIR / "tmp")
    hf_str = str(CACHE_DIR / "hf")
    hf_hub_str = str(CACHE_DIR / "hf" / "hub")
    torch_str = str(CACHE_DIR / "torch")
    pip_str = str(CACHE_DIR / "pip")
    xdg_str = str(CACHE_DIR / "xdg")

    env_map = {
        "PIP_CACHE_DIR": pip_str,
        "HF_HOME": hf_str,
        "HUGGINGFACE_HUB_CACHE": hf_hub_str,
        "TORCH_HOME": torch_str,
        "XDG_CACHE_HOME": xdg_str,
        "TEMP": tmp_str,
        "TMP": tmp_str,
        "TMPDIR": tmp_str,
        "HF_HUB_DISABLE_SYMLINKS_WARNING": "1",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    for k, v in env_map.items():
        os.environ[k] = v

    tempfile.tempdir = tmp_str
    _register_cuda_dll_dirs()
    _ENV_CONFIGURED = True


def load_config() -> dict[str, Any]:
    """Load config.json as UTF-8 dict."""
    if not CONFIG_PATH.exists():
        return {}
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def save_config(cfg: dict[str, Any]) -> None:
    """Atomically write config.json."""
    _ensure_directories()
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".config_", suffix=".tmp", dir=str(ROOT_DIR)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp_name, CONFIG_PATH)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
