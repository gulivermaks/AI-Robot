"""Compact CLI for the AI agent to control body, camera, voice, and hearing."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import httpx

ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import env_setup

env_setup.configure_env()

from body.vacuum_driver import VacuumDriver


def _base_url() -> str:
    hcfg = env_setup.load_config().get("head", {})
    host = hcfg.get("bridge_host") or "127.0.0.1"
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    port = int(hcfg.get("bridge_port") or 8765)
    return f"http://{host}:{port}"


def _api(method: str, path: str, payload: dict[str, Any] | None = None, timeout: float = 40.0) -> dict[str, Any]:
    url = f"{_base_url()}{path}"
    with httpx.Client(timeout=timeout) as client:
        if method == "GET":
            r = client.get(url, params=payload)
        else:
            r = client.post(url, json=payload or {})
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", r.text)
        except Exception:
            detail = r.text
        raise RuntimeError(f"HTTP {r.status_code}: {detail}")
    return r.json()


def _try_api_or_direct(path: str, direct_fn) -> dict[str, Any]:
    try:
        return _api("POST", path, timeout=15.0)
    except httpx.ConnectError:
        return direct_fn()


def _print_compact(data: dict[str, Any]) -> None:
    print(json.dumps(data, ensure_ascii=False))


def main(argv: list[str]) -> int:
    if not argv:
        print(
            "Команды:\n"
            "  step <f1.5 l0.3 ...> [--say 'текст'] [--gap 0.35] [--hd] [--no-frame]\n"
            "  listen [сек] [--no-frame]\n"
            "  say 'текст' [--no-wait] [--engine edge|silero]\n"
            "  frame [hd|<ширина>]\n"
            "  status | stop | exit | dock | find | cancel-return | inbox | health"
        )
        return 1

    cmd = argv[0]
    rest = argv[1:]

    if cmd == "health":
        _print_compact(_api("GET", "/health", timeout=5.0))
        return 0

    if cmd == "status":
        _print_compact(_try_api_or_direct("/vacuum/status", lambda: VacuumDriver().status()))
        return 0

    if cmd == "stop":
        _print_compact(_try_api_or_direct("/vacuum/stop", lambda: VacuumDriver().emergency_stop()))
        return 0

    if cmd == "exit":
        _print_compact(_try_api_or_direct("/vacuum/exit", lambda: VacuumDriver().exit_manual()))
        return 0

    if cmd == "dock":
        _print_compact(_try_api_or_direct("/vacuum/dock", lambda: VacuumDriver().dock()))
        return 0

    if cmd == "find":
        _print_compact(_try_api_or_direct("/vacuum/find", lambda: VacuumDriver().find()))
        return 0

    if cmd == "cancel-return":
        _print_compact(_try_api_or_direct("/vacuum/cancel-return", lambda: VacuumDriver().cancel_return()))
        return 0

    if cmd == "inbox":
        p = argparse.ArgumentParser()
        p.add_argument("--consume", action="store_true")
        p.add_argument("--limit", type=int, default=20)
        args = p.parse_args(rest)
        _print_compact(_api("GET", "/inbox", {"consume": str(args.consume).lower(), "limit": args.limit}, timeout=5.0))
        return 0

    if cmd == "frame":
        width = None
        if rest:
            if rest[0].lower() == "hd":
                width = int(env_setup.load_config().get("head", {}).get("hd_frame_width") or 1280)
            else:
                width = int(rest[0])
        res = _api("POST", "/frame", {"width": width}, timeout=10.0)
        _print_compact(res)
        return 0

    if cmd == "think":
        p = argparse.ArgumentParser()
        p.add_argument("text")
        p.add_argument("--model", type=str, default=None)
        args = p.parse_args(rest)
        res = _api("POST", "/think", {"text": args.text, "model": args.model}, timeout=10.0)
        _print_compact(res)
        return 0

    if cmd == "say":
        p = argparse.ArgumentParser()
        p.add_argument("text")
        p.add_argument("--thought", type=str, default=None)
        p.add_argument("--no-wait", action="store_true")
        p.add_argument("--engine", choices=["edge", "silero"], default=None)
        args = p.parse_args(rest)
        res = _api(
            "POST",
            "/say",
            {"text": args.text, "thought": args.thought, "wait": not args.no_wait, "engine": args.engine},
            timeout=60.0,
        )
        _print_compact(res)
        return 0

    if cmd == "listen":
        p = argparse.ArgumentParser()
        p.add_argument("seconds", nargs="?", type=float, default=15.0)
        p.add_argument("--no-frame", action="store_true")
        p.add_argument("--hd", action="store_true")
        args = p.parse_args(rest)
        fw = int(env_setup.load_config().get("head", {}).get("hd_frame_width") or 1280) if args.hd else None
        res = _api(
            "POST",
            "/listen",
            {"timeout": args.seconds, "with_frame": not args.no_frame, "frame_width": fw},
            timeout=args.seconds + 15.0,
        )
        _print_compact(res)
        return 0

    if cmd == "step":
        p = argparse.ArgumentParser()
        p.add_argument("moves", nargs="*")
        p.add_argument("--say", type=str, default=None)
        p.add_argument("--thought", type=str, default=None)
        p.add_argument("--gap", type=float, default=None)
        p.add_argument("--hd", action="store_true")
        p.add_argument("--no-frame", action="store_true")
        args = p.parse_args(rest)
        if not args.moves and not args.say:
            raise RuntimeError("Укажите ходы (например f1.5 l0.3) и/или --say 'текст'")
        fw = int(env_setup.load_config().get("head", {}).get("hd_frame_width") or 1280) if args.hd else None
        payload = {
            "moves": args.moves,
            "say": args.say,
            "thought": args.thought,
            "gap": args.gap,
            "frame_width": fw,
            "no_frame": args.no_frame,
        }
        try:
            res = _api("POST", "/step", payload, timeout=60.0)
        except httpx.ConnectError:
            if args.say or not args.no_frame:
                raise
            res = {"ok": True, "move": VacuumDriver().move(args.moves, gap=args.gap)}
        _print_compact(res)
        return 0

    # Shorthand: python robot_cli.py f1.5 l0.3
    _print_compact(_try_api_or_direct("/vacuum/move", lambda: VacuumDriver().move(argv)))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
