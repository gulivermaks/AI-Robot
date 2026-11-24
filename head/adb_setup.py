"""Connect Android head phone over ADB, set adb reverse tcp:8765, keep screen awake, and launch Chrome."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import env_setup

env_setup.configure_env()


def find_adb() -> str:
    adb = shutil.which("adb")
    if adb:
        return adb
    exe_name = "adb.exe" if os.name == "nt" else "adb"
    fallback = ROOT_DIR / "platform-tools" / exe_name
    if fallback.exists():
        return str(fallback)
    raise RuntimeError(
        f"{exe_name} не найден в PATH и не найден в {ROOT_DIR / 'platform-tools'}. "
        "Установите Android SDK Platform-Tools или распакуйте их в папку platform-tools/."
    )


def run_adb(adb: str, args: list[str], timeout: float = 8.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [adb, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def parse_devices(output: str) -> list[tuple[str, str]]:
    devices: list[tuple[str, str]] = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            devices.append((parts[0], parts[1]))
    return devices


def discover_ip_by_mac(mac: str) -> str | None:
    """Find current IPv4 address for a known MAC address via Windows ARP table."""
    target_mac = mac.strip().lower().replace(":", "-")
    if not target_mac:
        return None
    try:
        out = subprocess.check_output(
            ["arp", "-a"],
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except Exception:
        return None
    for line in out.splitlines():
        norm_line = line.lower().replace(":", "-")
        if target_mac in norm_line:
            m = re.search(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b", line)
            if m:
                return m.group(1)
    return None


def setup_head(open_chrome: bool = True, volume: int | None = None) -> dict[str, Any]:
    cfg = env_setup.load_config()
    hcfg = cfg.get("head", {})
    target = str(hcfg.get("adb_target") or "").strip()
    mac = str(hcfg.get("adb_mac") or "").strip()
    port = int(hcfg.get("bridge_port") or 8765)
    media_vol = int(volume if volume is not None else hcfg.get("media_volume") or 12)

    adb = find_adb()
    devices = parse_devices(run_adb(adb, ["devices"]).stdout)
    ready = [d for d, st in devices if st == "device"]

    if not ready:
        conn_out = ""
        if target:
            conn = run_adb(adb, ["connect", target], timeout=6.0)
            conn_out = (conn.stdout + conn.stderr).strip()
            devices = parse_devices(run_adb(adb, ["devices"]).stdout)
            ready = [d for d, st in devices if st == "device"]
        if not ready and mac:
            found_ip = discover_ip_by_mac(mac)
            if found_ip:
                new_target = f"{found_ip}:5555"
                if new_target != target:
                    conn = run_adb(adb, ["connect", new_target], timeout=6.0)
                    conn_out = (conn.stdout + conn.stderr).strip()
                    devices = parse_devices(run_adb(adb, ["devices"]).stdout)
                    ready = [d for d, st in devices if st == "device"]
                    if ready:
                        target = new_target
                        cfg.setdefault("head", {})["adb_target"] = new_target
                        try:
                            env_setup.save_config(cfg)
                        except Exception:
                            pass
        if not ready:
            if target:
                err_msg = f"Android-телефон (голова робота, {target}) недоступен по ADB: {conn_out}"
            else:
                err_msg = (
                    "Android-телефон (голова робота) не найден по ADB. "
                    "Подключите телефон по USB или укажите head.adb_target (IP:5555) в config.json."
                )
            return {
                "ok": False,
                "adb": adb,
                "target": target,
                "devices": devices,
                "error": err_msg,
            }

    serial = target if target in ready else ready[0]
    steps: dict[str, Any] = {"serial": serial}

    rev = run_adb(adb, ["-s", serial, "reverse", f"tcp:{port}", f"tcp:{port}"])
    steps["reverse"] = rev.returncode == 0
    if rev.returncode != 0:
        raise RuntimeError(f"adb reverse не удался: {rev.stderr.strip()}")

    run_adb(adb, ["-s", serial, "shell", "svc", "power", "stayon", "true"])
    run_adb(adb, ["-s", serial, "shell", "input", "keyevent", "KEYCODE_WAKEUP"])
    run_adb(adb, ["-s", serial, "shell", "wm", "dismiss-keyguard"])

    vol_res = run_adb(
        adb,
        ["-s", serial, "shell", "cmd", "media_session", "volume", "--stream", "3", "--set", str(media_vol)],
    )
    if vol_res.returncode != 0:
        vol_res = run_adb(
            adb, ["-s", serial, "shell", "media", "volume", "--stream", "3", "--set", str(media_vol)]
        )
    steps["volume_set"] = vol_res.returncode == 0

    url = f"http://localhost:{port}"
    if open_chrome:
        am_res = run_adb(
            adb,
            [
                "-s",
                serial,
                "shell",
                "am",
                "start",
                "-n",
                "com.android.chrome/com.google.android.apps.chrome.Main",
                "-a",
                "android.intent.action.VIEW",
                "--es",
                "com.android.browser.application_id",
                "com.android.chrome",
                "-d",
                url,
            ],
        )
        if am_res.returncode != 0:
            am_res = run_adb(
                adb,
                ["-s", serial, "shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", url],
            )
        steps["chrome_opened"] = am_res.returncode == 0
        time.sleep(0.5)

    steps["ok"] = True
    steps["url"] = url
    return steps


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Настройка ADB-туннеля к Android-телефону (голова робота)")
    parser.add_argument("--no-open", action="store_true", help="Не открывать Chrome на телефоне")
    parser.add_argument("--volume", type=int, default=None, help="Громкость медиа (0..15)")
    args = parser.parse_args(argv)

    res = setup_head(open_chrome=not args.no_open, volume=args.volume)
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
