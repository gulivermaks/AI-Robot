"""Direct Xiaomi vacuum driver: LAN miIO (UDP 54321) with Xiaomi Cloud MIoT fallback.

Supports Xiaomi Mi Robot Vacuum-Mop 2 Pro (MJST1SHW / ijai.vacuum.v3) out of the box,
plus reference mappings for szkj.vacuum.fc01eu (S10T) and xiaomi.vacuum.c108 (E5).
"""

from __future__ import annotations

import hashlib
import json
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

try:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad, unpad
except ImportError:
    from Cryptodome.Cipher import AES
    from Cryptodome.Util.Padding import pad, unpad

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import env_setup

env_setup.configure_env()

MODELS: dict[str, dict[str, Any]] = {
    "ijai.vacuum.v3": {
        "name": "mop2pro",
        "title": "Mi Robot Vacuum-Mop 2 Pro",
        "direction": (7, 16),
        "keys": {"f": 1, "l": 2, "r": 3, "b": 4},
        "stop": 5,
        "exit": 10,
        "gap": 0.35,
        "wait_pulses": True,
        "read": {"status": (2, 1), "fault": (2, 2), "battery": (3, 1), "task": (2, 8)},
        "status": {
            0: "сон",
            1: "ожидание",
            2: "пауза",
            3: "едет на базу",
            4: "заряжается",
            5: "уборка",
            6: "уборка и мытьё",
            7: "мытьё",
            8: "обновление",
        },
        "faults": {0: "нет ошибок", 2105: "полностью заряжен"},
        "dock_action": (3, 1, []),
        "dock_alt": (7, 7, [{"piid": 43, "value": 1}]),
        "stop_action": (2, 2, []),
        "cancel_return_action": (7, 7, [{"piid": 43, "value": 0}]),
        "find_prop": (4, 1, 1),
    },
    "szkj.vacuum.fc01eu": {
        "name": "s10t",
        "title": "Xiaomi Robot Vacuum S10T",
        "direction": (7, 16),
        "keys": {"f": 1, "l": 2, "r": 3, "b": 4},
        "stop": 5,
        "exit": 10,
        "gap": 0.35,
        "wait_pulses": True,
        "read": {"status": (2, 1), "fault": (2, 2), "battery": (3, 1), "task": (2, 8)},
        "status": {
            0: "сон",
            1: "ожидание",
            2: "пауза",
            3: "едет на базу",
            4: "заряжается",
            5: "уборка",
            6: "ручной режим или уборка с мытьём",
            7: "мытьё",
            8: "обновление",
        },
        "dock_action": (7, 7, [{"piid": 43, "value": 1}]),
        "stop_action": (2, 2, []),
        "find_prop": (4, 1, 1),
    },
    "xiaomi.vacuum.c108": {
        "name": "e5",
        "title": "Xiaomi Robot Vacuum E5",
        "direction": (13, 1),
        "keys": {"l": 0, "r": 1, "f": 2, "b": 3},
        "stop": 4,
        "gap": 0.4,
        "wait_pulses": False,
        "read": {"status": (2, 1), "fault": (2, 2), "battery": (3, 1), "charging": (3, 2)},
        "status": {
            1: "ожидание",
            2: "уборка",
            3: "пауза",
            4: "ошибка",
            5: "зарядка",
            6: "едет на базу",
            7: "заряжен",
        },
        "dock_action": (2, 8, []),
        "cancel_return_action": (11, 2, []),
        "find_action": (4, 1, []),
    },
}

HELLO_PACKET = bytes.fromhex(
    "21310020ffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
)


class MiioProtocolError(RuntimeError):
    """Raised when local miIO UDP communication or device reply fails."""


class MiioLanClient:
    """Thread-safe pure-Python miIO LAN UDP 54321 client (AES-128-CBC + MD5)."""

    def __init__(self, ip: str, token: str, timeout: float = 2.0) -> None:
        self.ip = ip.strip()
        self.token_hex = token.strip().lower()
        if len(self.token_hex) != 32:
            raise ValueError("Токен Xiaomi должен состоять из 32 hex-символов")
        self.token = bytes.fromhex(self.token_hex)
        self.key = hashlib.md5(self.token).digest()
        self.iv = hashlib.md5(self.key + self.token).digest()
        self.timeout = timeout

        self._lock = threading.Lock()
        self._did: int | None = None
        self._stamp_offset: int | None = None
        self._msg_id = 0

    def _encrypt(self, payload: bytes) -> bytes:
        cipher = AES.new(self.key, AES.MODE_CBC, self.iv)
        return cipher.encrypt(pad(payload, 16))

    def _decrypt(self, ciphertext: bytes) -> bytes:
        cipher = AES.new(self.key, AES.MODE_CBC, self.iv)
        raw = cipher.decrypt(ciphertext)
        try:
            return unpad(raw, 16).rstrip(b"\x00")
        except ValueError:
            return raw.rstrip(b"\x00")

    def pack_packet(self, payload_obj: dict[str, Any], did: int, stamp: int) -> bytes:
        payload_bytes = json.dumps(payload_obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        encrypted = self._encrypt(payload_bytes)
        total_len = 32 + len(encrypted)
        head16 = (
            b"\x21\x31"
            + total_len.to_bytes(2, "big")
            + b"\x00\x00\x00\x00"
            + did.to_bytes(4, "big")
            + stamp.to_bytes(4, "big")
        )
        checksum = hashlib.md5(head16 + self.token + encrypted).digest()
        return head16 + checksum + encrypted

    def unpack_packet(self, packet: bytes) -> dict[str, Any]:
        if len(packet) < 32 or packet[:2] != b"\x21\x31":
            raise MiioProtocolError(f"Некорректный заголовок miIO (len={len(packet)})")
        pkt_len = int.from_bytes(packet[2:4], "big")
        if pkt_len > len(packet) or pkt_len < 32:
            raise MiioProtocolError(f"Некорректная длина пакета miIO: {pkt_len}")
        encrypted = packet[32:pkt_len]
        if not encrypted:
            return {}
        raw = self._decrypt(encrypted)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def hello(self, sock: socket.socket | None = None) -> tuple[int, int]:
        own_sock = sock is None
        if own_sock:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(self.timeout)
        try:
            sock.sendto(HELLO_PACKET, (self.ip, 54321))
            data, _ = sock.recvfrom(4096)
            if len(data) < 32 or data[:2] != b"\x21\x31":
                raise MiioProtocolError("Неверный ответ handshake miIO")
            did = int.from_bytes(data[8:12], "big")
            stamp = int.from_bytes(data[12:16], "big")
            self._did = did
            self._stamp_offset = stamp - int(time.monotonic())
            return did, stamp
        finally:
            if own_sock:
                sock.close()

    def _next_id(self) -> int:
        self._msg_id = (self._msg_id % 999999) + 1
        return self._msg_id

    def send(self, method: str, params: Any = None, timeout: float | None = None) -> Any:
        if params is None:
            params = []
        req_timeout = timeout or self.timeout
        with self._lock:
            last_err: Exception | None = None
            for attempt in range(2):
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.settimeout(req_timeout)
                try:
                    if self._did is None or self._stamp_offset is None or attempt > 0:
                        self.hello(sock)
                    stamp = max(1, int(time.monotonic()) + int(self._stamp_offset or 0))
                    msg_id = self._next_id()
                    packet = self.pack_packet(
                        {"id": msg_id, "method": method, "params": params},
                        self._did or 0,
                        stamp,
                    )
                    sock.sendto(packet, (self.ip, 54321))
                    data, _ = sock.recvfrom(65535)
                    resp = self.unpack_packet(data)
                    if "error" in resp:
                        raise MiioProtocolError(f"miIO error: {resp['error']}")
                    return resp.get("result")
                except (TimeoutError, socket.timeout, OSError, MiioProtocolError, json.JSONDecodeError) as exc:
                    last_err = exc
                    self._did = None
                    self._stamp_offset = None
                finally:
                    sock.close()
            raise MiioProtocolError(f"Ошибка связи с {self.ip}:54321: {last_err}") from last_err

    def info(self) -> dict[str, Any]:
        res = self.send("miIO.info", [])
        return res if isinstance(res, dict) else {"result": res}

    def get_properties(self, pairs: list[tuple[int, int]], timeout: float | None = None) -> dict[tuple[int, int], Any]:
        req = [{"did": f"{s}-{p}", "siid": s, "piid": p} for s, p in pairs]
        rows = self.send("get_properties", req, timeout=timeout) or []
        out: dict[tuple[int, int], Any] = {}
        for r in rows:
            key = (int(r["siid"]), int(r["piid"]))
            out[key] = r.get("value") if r.get("code") == 0 else f"code {r.get('code')}"
        return out

    def set_properties(self, items: list[tuple[int, int, Any]], timeout: float | None = None) -> list[dict[str, Any]]:
        req = [{"did": f"{s}-{p}", "siid": s, "piid": p, "value": v} for s, p, v in items]
        rows = self.send("set_properties", req, timeout=timeout)
        return rows if isinstance(rows, list) else []

    def action(self, siid: int, aiid: int, in_params: list[Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        payload = {
            "did": f"call-{siid}-{aiid}",
            "siid": siid,
            "aiid": aiid,
            "in": in_params or [],
        }
        res = self.send("action", payload, timeout=timeout)
        return res if isinstance(res, dict) else {"result": res}


class VacuumDriver:
    """High-level vacuum controller for AI-Robot with emergency stop support."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._cfg_override = config
        self.cancel_event = threading.Event()
        self.move_lock = threading.Lock()
        self._lan_client: MiioLanClient | None = None
        self._lan_sig: tuple[str, str] | None = None

    def _vac_cfg(self) -> dict[str, Any]:
        cfg = self._cfg_override if self._cfg_override is not None else env_setup.load_config()
        return cfg.get("vacuum", {})

    def _model_spec(self, vcfg: dict[str, Any]) -> dict[str, Any]:
        model = vcfg.get("model") or "ijai.vacuum.v3"
        if model not in MODELS:
            raise RuntimeError(f"Модель '{model}' не описана в MODELS")
        return MODELS[model]

    def _get_lan(self, vcfg: dict[str, Any], timeout: float = 2.0) -> MiioLanClient:
        ip = (vcfg.get("ip") or "").strip()
        token = (vcfg.get("token") or "").strip()
        if not ip or not token:
            raise RuntimeError(
                "IP или токен пылесоса не задан в config.json. Выполните: python body/discover_xiaomi.py --qr --open"
            )
        sig = (ip, token)
        if self._lan_client is None or self._lan_sig != sig:
            self._lan_client = MiioLanClient(ip, token, timeout=timeout)
            self._lan_sig = sig
        return self._lan_client

    def _get_cloud(self, vcfg: dict[str, Any]):
        from body.discover_xiaomi import XiaomiCloudClient

        session_path = env_setup.CACHE_DIR / "xiaomi_cloud_session.json"
        if not session_path.exists():
            raise RuntimeError("Облачная сессия Xiaomi не найдена (.cache/xiaomi_cloud_session.json)")
        return XiaomiCloudClient.from_file(session_path, server=vcfg.get("server") or "ru")

    def _use_cloud_first(self, vcfg: dict[str, Any]) -> bool:
        return (vcfg.get("transport") or "lan").lower() == "cloud"

    def _get_props(self, pairs: list[tuple[int, int]], timeout: float = 4.0) -> dict[tuple[int, int], Any]:
        vcfg = self._vac_cfg()
        if not self._use_cloud_first(vcfg):
            try:
                return self._get_lan(vcfg, timeout=timeout).get_properties(pairs, timeout=timeout)
            except Exception:
                if not (env_setup.CACHE_DIR / "xiaomi_cloud_session.json").exists():
                    raise
        did = str(vcfg.get("did") or "")
        return self._get_cloud(vcfg).miotspec_prop_get(did, pairs, timeout=timeout)

    def _set_prop(self, siid: int, piid: int, value: Any, timeout: float = 2.5) -> int | str | None:
        vcfg = self._vac_cfg()
        if not self._use_cloud_first(vcfg):
            try:
                rows = self._get_lan(vcfg, timeout=timeout).set_properties([(siid, piid, value)], timeout=timeout)
                if rows:
                    return rows[0].get("code")
                return 0
            except Exception as exc:
                if not (env_setup.CACHE_DIR / "xiaomi_cloud_session.json").exists():
                    return type(exc).__name__
        did = str(vcfg.get("did") or "")
        try:
            rows = self._get_cloud(vcfg).miotspec_prop_set(did, [(siid, piid, value)], timeout=timeout)
            if rows:
                return rows[0].get("code")
            return 0
        except Exception as exc:
            return type(exc).__name__

    def _call_action(self, siid: int, aiid: int, in_params: list[Any] | None = None, timeout: float = 5.0) -> dict[str, Any]:
        vcfg = self._vac_cfg()
        if not self._use_cloud_first(vcfg):
            try:
                return self._get_lan(vcfg, timeout=timeout).action(siid, aiid, in_params or [], timeout=timeout)
            except Exception:
                if not (env_setup.CACHE_DIR / "xiaomi_cloud_session.json").exists():
                    raise
        did = str(vcfg.get("did") or "")
        return self._get_cloud(vcfg).miotspec_action(did, siid, aiid, in_params or [], timeout=timeout)

    def status(self) -> dict[str, Any]:
        vcfg = self._vac_cfg()
        m = self._model_spec(vcfg)
        pairs = list(m["read"].values())
        values = self._get_props(pairs, timeout=4.0)
        got = {key: values.get(pair) for key, pair in m["read"].items()}
        st_code = got.get("status")
        fault_code = got.get("fault")
        out: dict[str, Any] = {
            "robot": m["name"],
            "model": vcfg.get("model"),
            "ip": vcfg.get("ip"),
            "status": st_code,
            "status_text": m["status"].get(st_code, "?"),
            "fault": fault_code,
            "fault_text": m.get("faults", {}).get(fault_code, "ошибка" if fault_code else "нет ошибок"),
            "battery": got.get("battery"),
        }
        if "task" in got:
            out["manual_mode"] = got["task"] == 5
        return out

    def parse_moves(self, tokens: list[str]) -> list[tuple[str, float]]:
        vcfg = self._vac_cfg()
        max_seconds = float(vcfg.get("max_seconds") or 25.0)
        moves: list[tuple[str, float]] = []
        flat_tokens: list[str] = []
        for item in tokens:
            flat_tokens.extend(str(item).replace(",", " ").split())
        for raw in flat_tokens:
            token = raw.strip().lower()
            if not token or token[0] not in "fblrw":
                raise ValueError(f"Неверный ход '{raw}'. Пример: f1.5 l0.3")
            try:
                secs = float(token[1:])
            except ValueError as exc:
                raise ValueError(f"Неверная длительность хода '{raw}'") from exc
            if secs <= 0:
                raise ValueError(f"Длительность хода должна быть > 0: '{raw}'")
            moves.append((token[0], secs))
        if not moves:
            raise ValueError("Список ходов пуст")
        total = sum(s for _, s in moves)
        if total > max_seconds:
            raise ValueError(f"Суммарное время хода {total:.1f} с > MAX_SECONDS ({max_seconds:g} с)")
        return moves

    def _send_stop_code(self, m: dict[str, Any]) -> Any:
        siid, piid = m["direction"]
        code = self._set_prop(siid, piid, m["stop"], timeout=2.5)
        if code not in (0, 1):
            code = self._set_prop(siid, piid, m["stop"], timeout=4.0)
        return code

    def emergency_stop(self) -> dict[str, Any]:
        """Immediately abort active moves and send STOP command."""
        self.cancel_event.set()
        vcfg = self._vac_cfg()
        m = self._model_spec(vcfg)
        code = self._send_stop_code(m)
        return {"robot": m["name"], "stop": code, "emergency": True}

    def move(self, tokens: list[str], gap: float | None = None) -> dict[str, Any]:
        vcfg = self._vac_cfg()
        m = self._model_spec(vcfg)
        moves = self.parse_moves(tokens)
        step_gap = float(gap if gap is not None else vcfg.get("gap") or m["gap"])
        if not 0.1 <= step_gap <= 1.0:
            raise ValueError("gap должен быть в диапазоне 0.1..1.0 с")

        siid, piid = m["direction"]
        codes: list[Any] = []
        started = time.monotonic()
        aborted = False

        with self.move_lock:
            self.cancel_event.clear()
            try:
                for key, seconds in moves:
                    if self.cancel_event.is_set():
                        aborted = True
                        break
                    end = time.monotonic() + seconds
                    if key == "w":
                        self._set_prop(siid, piid, m["stop"], timeout=2.0)
                        remaining = max(0.0, end - time.monotonic())
                        if self.cancel_event.wait(remaining):
                            aborted = True
                            break
                        continue
                    while time.monotonic() < end:
                        if self.cancel_event.is_set():
                            aborted = True
                            break
                        codes.append(self._set_prop(siid, piid, m["keys"][key], timeout=2.0))
                        wait_for = min(step_gap, max(0.02, end - time.monotonic()))
                        if self.cancel_event.wait(wait_for):
                            aborted = True
                            break
                    if aborted:
                        break
            finally:
                stop_code = self._send_stop_code(m)

        failed: dict[str, int] = {}
        for c in codes:
            if c not in (0, 1):
                failed[str(c)] = failed.get(str(c), 0) + 1
        out: dict[str, Any] = {
            "robot": m["name"],
            "moves": " ".join(f"{k}{s:g}" for k, s in moves),
            "pulses": len(codes),
            "accepted": len(codes) - sum(failed.values()),
            "stop": stop_code,
            "aborted": aborted,
            "seconds": round(time.monotonic() - started, 2),
        }
        if failed:
            out["failed"] = failed
        return out

    def exit_manual(self) -> dict[str, Any]:
        vcfg = self._vac_cfg()
        m = self._model_spec(vcfg)
        if "exit" not in m:
            return {"robot": m["name"], "exit": "not_supported"}
        siid, piid = m["direction"]
        return {"robot": m["name"], "exit": self._set_prop(siid, piid, m["exit"], timeout=4.0)}

    def _leave_manual_if_needed(self, m: dict[str, Any]) -> Any:
        if "exit" not in m:
            return None
        siid, piid = m["direction"]
        code = self._set_prop(siid, piid, m["exit"], timeout=4.0)
        time.sleep(0.6)
        return code

    def dock(self) -> dict[str, Any]:
        vcfg = self._vac_cfg()
        m = self._model_spec(vcfg)
        exit_code = self._leave_manual_if_needed(m)
        s, a, params = m["dock_action"]
        try:
            res = self._call_action(s, a, params, timeout=6.0)
        except Exception:
            if "dock_alt" not in m:
                raise
            s2, a2, p2 = m["dock_alt"]
            res = self._call_action(s2, a2, p2, timeout=6.0)
        return {"robot": m["name"], "exit": exit_code, "dock": res}

    def start_clean(self, mode: str = "auto") -> dict[str, Any]:
        """Start standard vacuum cleaning: auto (2.1), sweep (2.3), sweep_mop (2.5), mop (2.6)."""
        vcfg = self._vac_cfg()
        m = self._model_spec(vcfg)
        exit_code = self._leave_manual_if_needed(m)
        action_map = {"auto": (2, 1), "sweep": (2, 3), "sweep_mop": (2, 5), "mop": (2, 6)}
        s, a = action_map.get(mode, (2, 1))
        res = self._call_action(s, a, [], timeout=6.0)
        return {"robot": m["name"], "mode": mode, "exit": exit_code, "clean": res}

    def stop_clean(self) -> dict[str, Any]:
        """Stop / pause active vacuum cleaning (action 2.2)."""
        vcfg = self._vac_cfg()
        m = self._model_spec(vcfg)
        s, a, params = m.get("stop_action", (2, 2, []))
        res = self._call_action(s, a, params, timeout=5.0)
        return {"robot": m["name"], "stop_clean": res}

    def edge_clean(self) -> dict[str, Any]:
        """Clean along room walls/edges (ijai.vacuum.v3 action 7.3 with cleanType=1 Edge)."""
        vcfg = self._vac_cfg()
        m = self._model_spec(vcfg)
        exit_code = self._leave_manual_if_needed(m)
        try:
            res = self._call_action(
                7,
                3,
                [{"piid": 24, "value": ""}, {"piid": 25, "value": 1}, {"piid": 26, "value": 1}],
                timeout=6.0,
            )
            return {"robot": m["name"], "exit": exit_code, "edge_clean": res}
        except Exception:
            res = self._call_action(2, 1, [], timeout=6.0)
            return {"robot": m["name"], "exit": exit_code, "fallback_clean": res}

    def room_clean(self, room_ids: list[int]) -> dict[str, Any]:
        """Start cleaning specific mapped room IDs (action 2.7 / 7.3)."""
        if not room_ids:
            raise ValueError("Не указаны ID комнат для уборки")
        vcfg = self._vac_cfg()
        m = self._model_spec(vcfg)
        exit_code = self._leave_manual_if_needed(m)
        try:
            res = self._call_action(2, 7, [{"piid": 10, "value": json.dumps(room_ids)}], timeout=6.0)
        except Exception:
            res = self._call_action(
                7,
                3,
                [
                    {"piid": 24, "value": ",".join(str(x) for x in room_ids)},
                    {"piid": 25, "value": 0},
                    {"piid": 26, "value": 1},
                ],
                timeout=6.0,
            )
        return {"robot": m["name"], "exit": exit_code, "room_ids": room_ids, "room_clean": res}

    def find(self) -> dict[str, Any]:
        vcfg = self._vac_cfg()
        m = self._model_spec(vcfg)
        if "find_prop" in m:
            s, p, v = m["find_prop"]
            return {"robot": m["name"], "find": self._set_prop(s, p, v, timeout=4.0)}
        s, a, params = m["find_action"]
        return {"robot": m["name"], "find": self._call_action(s, a, params, timeout=5.0)}

    def cancel_return(self) -> dict[str, Any]:
        vcfg = self._vac_cfg()
        m = self._model_spec(vcfg)
        if "stop_action" in m:
            s, a, params = m["stop_action"]
            res = self._call_action(s, a, params, timeout=5.0)
            return {"robot": m["name"], "cancel_return": res}
        if "cancel_return_action" in m:
            s, a, params = m["cancel_return_action"]
            res = self._call_action(s, a, params, timeout=5.0)
            return {"robot": m["name"], "cancel_return": res}
        raise RuntimeError("Отмена возврата на базу не поддерживается этой моделью")

    def raw_get(self, siid: int, piid: int) -> dict[str, Any]:
        vals = self._get_props([(siid, piid)], timeout=4.0)
        return {f"{siid}.{piid}": vals.get((siid, piid))}

    def raw_set(self, siid: int, piid: int, value: Any) -> dict[str, Any]:
        return {"set": f"{siid}.{piid}", "code": self._set_prop(siid, piid, value, timeout=4.0)}

    def raw_action(self, siid: int, aiid: int, in_params: list[Any] | None = None) -> dict[str, Any]:
        return {"action": f"{siid}.{aiid}", "result": self._call_action(siid, aiid, in_params or [], timeout=5.0)}


def run_self_test() -> dict[str, Any]:
    """Offline self-test for move parsing and AES-128-CBC/MD5 miIO packet roundtrip."""
    drv = VacuumDriver({"vacuum": {"model": "ijai.vacuum.v3", "max_seconds": 25.0, "gap": 0.35}})
    parsed = drv.parse_moves(["f0.5", "l0.3", "w0.2"])
    assert parsed == [("f", 0.5), ("l", 0.3), ("w", 0.2)], parsed

    try:
        drv.parse_moves(["f30"])
        raise AssertionError("MAX_SECONDS check failed")
    except ValueError:
        pass

    token = "0123456789abcdef0123456789abcdef"
    client = MiioLanClient("127.0.0.1", token)
    sample = {"id": 42, "method": "get_properties", "params": [{"did": "2-1", "siid": 2, "piid": 1}]}
    pkt = client.pack_packet(sample, did=825372343, stamp=123456)
    unpacked = client.unpack_packet(pkt)
    assert unpacked == sample, f"Roundtrip mismatch: {unpacked}"

    return {
        "ok": True,
        "parsed_moves": parsed,
        "packet_bytes": len(pkt),
        "roundtrip_id": unpacked["id"],
    }


def main(argv: list[str]) -> int:
    if not argv:
        print(
            "Использование: python body/vacuum_driver.py "
            "<status|stop|exit|dock|find|cancel-return|get S P|set S P JSON|action S A [JSON]|self-test|f0.5 l0.3>"
        )
        return 1

    args = list(argv)
    gap: float | None = None
    if args[0].startswith("gap="):
        gap = float(args.pop(0)[4:])
    if not args:
        print("Не указана команда")
        return 1

    cmd = args[0]
    if cmd == "self-test":
        print(json.dumps(run_self_test(), ensure_ascii=False))
        return 0

    drv = VacuumDriver()
    if cmd == "status":
        out = drv.status()
    elif cmd == "stop":
        out = drv.emergency_stop()
    elif cmd == "exit":
        out = drv.exit_manual()
    elif cmd == "dock":
        out = drv.dock()
    elif cmd == "find":
        out = drv.find()
    elif cmd == "cancel-return":
        out = drv.cancel_return()
    elif cmd == "get" and len(args) >= 3:
        out = drv.raw_get(int(args[1]), int(args[2]))
    elif cmd == "set" and len(args) >= 4:
        out = drv.raw_set(int(args[1]), int(args[2]), json.loads(args[3]))
    elif cmd == "action" and len(args) >= 3:
        in_p = json.loads(args[3]) if len(args) >= 4 else []
        out = drv.raw_action(int(args[1]), int(args[2]), in_p)
    else:
        out = drv.move(args, gap=gap)

    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
