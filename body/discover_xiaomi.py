"""Xiaomi LAN probe + Xiaomi Cloud QR login token extractor (zero Docker/HA)."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import random
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import requests
try:
    from Crypto.Cipher import ARC4
except ImportError:
    from Cryptodome.Cipher import ARC4

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import env_setup

env_setup.configure_env()

HELLO_PACKET = bytes.fromhex(
    "21310020ffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
)
SERVERS = ["ru", "de", "cn", "sg", "us", "i2"]


def mask_token(token: str | None) -> str:
    if not token:
        return ""
    if len(token) <= 8:
        return "***"
    return f"{token[:4]}...{token[-4:]}"


def scan_lan_miio(candidate_ips: list[str] | None = None, timeout: float = 1.5) -> dict[int, str]:
    """Discover local miIO devices on UDP 54321 and return {did: ip}."""
    found: dict[int, str] = {}
    targets = list(candidate_ips or [])

    for ip in targets:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        try:
            sock.sendto(HELLO_PACKET, (ip, 54321))
            data, addr = sock.recvfrom(1024)
            if len(data) >= 32 and data[:2] == b"\x21\x31":
                did = int.from_bytes(data[8:12], "big")
                found[did] = addr[0]
        except OSError:
            pass
        finally:
            sock.close()

    # Also broadcast on UDP 54321 to catch IP changes on the subnet.
    bsock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    bsock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    bsock.settimeout(timeout)
    try:
        bsock.sendto(HELLO_PACKET, ("255.255.255.255", 54321))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, addr = bsock.recvfrom(1024)
                if len(data) >= 32 and data[:2] == b"\x21\x31":
                    did = int.from_bytes(data[8:12], "big")
                    found.setdefault(did, addr[0])
            except OSError:
                break
    except OSError:
        pass
    finally:
        bsock.close()

    return found


class XiaomiCloudClient:
    """Xiaomi Cloud RC4-encrypted API client (compatible with PiotrMachowski extractor)."""

    def __init__(
        self,
        user_id: str | int | None = None,
        ssecurity: str | None = None,
        service_token: str | None = None,
        server: str = "ru",
    ) -> None:
        self._agent = self.generate_agent()
        self._device_id = self.generate_device_id()
        self._session = requests.Session()
        self.userId = str(user_id) if user_id is not None else None
        self._ssecurity = ssecurity
        self._serviceToken = service_token
        self.server = server

    @classmethod
    def from_file(cls, session_path: Path, server: str | None = None) -> "XiaomiCloudClient":
        data = json.loads(session_path.read_text(encoding="utf-8"))
        return cls(
            user_id=data["user_id"],
            ssecurity=data["ssecurity"],
            service_token=data["service_token"],
            server=server or data.get("server") or "ru",
        )

    def save_session(self, session_path: Path, server: str) -> None:
        session_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "user_id": self.userId,
            "ssecurity": self._ssecurity,
            "service_token": self._serviceToken,
            "server": server,
            "saved_at": int(time.time()),
        }
        session_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def get_api_url(country: str) -> str:
        return "https://" + ("" if country == "cn" else (country + ".")) + "api.io.mi.com/app"

    def signed_nonce(self, nonce: str) -> str:
        hash_object = hashlib.sha256(
            base64.b64decode(self._ssecurity or "") + base64.b64decode(nonce)
        )
        return base64.b64encode(hash_object.digest()).decode("utf-8")

    @staticmethod
    def generate_nonce(millis: int) -> str:
        nonce_bytes = os.urandom(8) + (int(millis / 60000)).to_bytes(4, byteorder="big")
        return base64.b64encode(nonce_bytes).decode()

    @staticmethod
    def generate_agent() -> str:
        agent_id = "".join(chr(random.randint(65, 69)) for _ in range(13))
        random_text = "".join(chr(random.randint(97, 122)) for _ in range(18))
        return f"{random_text}-{agent_id} APP/com.xiaomi.mihome APPV/10.5.201"

    @staticmethod
    def generate_device_id() -> str:
        return "".join(chr(random.randint(97, 122)) for _ in range(6))

    @staticmethod
    def generate_enc_signature(url: str, method: str, signed_nonce: str, params: dict[str, str]) -> str:
        signature_params = [str(method).upper(), url.split("com")[1].replace("/app/", "/")]
        for k, v in params.items():
            signature_params.append(f"{k}={v}")
        signature_params.append(signed_nonce)
        signature_string = "&".join(signature_params)
        return base64.b64encode(hashlib.sha1(signature_string.encode("utf-8")).digest()).decode()

    @staticmethod
    def encrypt_rc4(password: str, payload: str) -> str:
        r = ARC4.new(base64.b64decode(password))
        r.encrypt(bytes(1024))
        return base64.b64encode(r.encrypt(payload.encode())).decode()

    @staticmethod
    def decrypt_rc4(password: str, payload: str) -> bytes:
        r = ARC4.new(base64.b64decode(password))
        r.encrypt(bytes(1024))
        return r.encrypt(base64.b64decode(payload))

    @staticmethod
    def generate_enc_params(
        url: str, method: str, signed_nonce: str, nonce: str, params: dict[str, str], ssecurity: str
    ) -> dict[str, str]:
        enc = dict(params)
        enc["rc4_hash__"] = XiaomiCloudClient.generate_enc_signature(url, method, signed_nonce, enc)
        for k, v in list(enc.items()):
            enc[k] = XiaomiCloudClient.encrypt_rc4(signed_nonce, v)
        enc.update(
            {
                "signature": XiaomiCloudClient.generate_enc_signature(url, method, signed_nonce, enc),
                "ssecurity": ssecurity,
                "_nonce": nonce,
            }
        )
        return enc

    @staticmethod
    def to_json(response_text: str) -> dict[str, Any]:
        return json.loads(response_text.replace("&&&START&&&", ""))

    def execute_api_call_encrypted(self, url: str, params: dict[str, str], timeout: float = 15.0) -> dict[str, Any] | None:
        headers = {
            "Accept-Encoding": "identity",
            "User-Agent": self._agent,
            "Content-Type": "application/x-www-form-urlencoded",
            "x-xiaomi-protocal-flag-cli": "PROTOCAL-HTTP2",
            "MIOT-ENCRYPT-ALGORITHM": "ENCRYPT-RC4",
        }
        cookies = {
            "userId": str(self.userId),
            "yetAnotherServiceToken": str(self._serviceToken),
            "serviceToken": str(self._serviceToken),
            "locale": "en_GB",
            "timezone": "GMT+02:00",
            "is_daylight": "1",
            "dst_offset": "3600000",
            "channel": "MI_APP_STORE",
        }
        millis = round(time.time() * 1000)
        nonce = self.generate_nonce(millis)
        signed_nonce = self.signed_nonce(nonce)
        fields = self.generate_enc_params(
            url, "POST", signed_nonce, nonce, dict(params), self._ssecurity or ""
        )
        response = self._session.post(url, headers=headers, cookies=cookies, params=fields, timeout=timeout)
        if response.status_code == 200:
            decoded = self.decrypt_rc4(self.signed_nonce(fields["_nonce"]), response.text)
            return json.loads(decoded)
        return None

    def get_homes(self, country: str) -> dict[str, Any] | None:
        url = self.get_api_url(country) + "/v2/homeroom/gethome"
        params = {"data": '{"fg": true, "fetch_share": true, "fetch_share_dev": true, "limit": 300, "app_ver": 7}'}
        return self.execute_api_call_encrypted(url, params)

    def get_dev_cnt(self, country: str) -> dict[str, Any] | None:
        url = self.get_api_url(country) + "/v2/user/get_device_cnt"
        params = {"data": '{ "fetch_own": true, "fetch_share": true}'}
        return self.execute_api_call_encrypted(url, params)

    def get_devices(self, country: str, home_id: Any, owner_id: Any) -> dict[str, Any] | None:
        url = self.get_api_url(country) + "/v2/home/home_device_list"
        params = {
            "data": (
                f'{{"home_owner": {owner_id}, "home_id": {home_id}, '
                '"limit": 200, "get_split_device": true, "support_smart_home": true}'
            )
        }
        return self.execute_api_call_encrypted(url, params)

    def miotspec_prop_get(self, did: str, pairs: list[tuple[int, int]], timeout: float = 8.0) -> dict[tuple[int, int], Any]:
        url = self.get_api_url(self.server) + "/miotspec/prop/get"
        body = {"params": [{"did": did, "siid": s, "piid": p} for s, p in pairs]}
        res = self.execute_api_call_encrypted(url, {"data": json.dumps(body, separators=(",", ":"))}, timeout=timeout)
        rows = (res or {}).get("result") or []
        out: dict[tuple[int, int], Any] = {}
        for r in rows:
            out[(int(r["siid"]), int(r["piid"]))] = r.get("value") if r.get("code") == 0 else f"code {r.get('code')}"
        return out

    def miotspec_prop_set(self, did: str, items: list[tuple[int, int, Any]], timeout: float = 5.0) -> list[dict[str, Any]]:
        url = self.get_api_url(self.server) + "/miotspec/prop/set"
        body = {"params": [{"did": did, "siid": s, "piid": p, "value": v} for s, p, v in items]}
        res = self.execute_api_call_encrypted(url, {"data": json.dumps(body, separators=(",", ":"))}, timeout=timeout)
        rows = (res or {}).get("result")
        return rows if isinstance(rows, list) else []

    def miotspec_action(
        self, did: str, siid: int, aiid: int, in_params: list[Any] | None = None, timeout: float = 8.0
    ) -> dict[str, Any]:
        url = self.get_api_url(self.server) + "/miotspec/action"
        body = {"params": {"did": did, "siid": siid, "aiid": aiid, "in": in_params or []}}
        res = self.execute_api_call_encrypted(url, {"data": json.dumps(body, separators=(",", ":"))}, timeout=timeout)
        return (res or {}).get("result") or res or {}


class XiaomiCloudQRClient(XiaomiCloudClient):
    """QR-code login client hosting a local Russian QR page on http://127.0.0.1:31415."""

    def __init__(self, qr_port: int = 31415) -> None:
        super().__init__()
        self._qr_port = qr_port
        self._qr_image_url: str | None = None
        self._login_url: str | None = None
        self._long_polling_url: str | None = None
        self._timeout: float = 300.0
        self._location: str | None = None
        self._qr_bytes: bytes = b""
        self._status_state = {"done": False, "ok": False, "message": "Ожидаю сканирования QR-кода в Xiaomi Home..."}
        self._httpd: HTTPServer | None = None

    def login_step_1(self) -> bool:
        url = "https://account.xiaomi.com/longPolling/loginUrl"
        params = {
            "_qrsize": "480",
            "qs": "%3Fsid%3Dxiaomiio%26_json%3Dtrue",
            "callback": "https://sts.api.io.mi.com/sts",
            "_hasLogo": "false",
            "sid": "xiaomiio",
            "serviceParam": "",
            "_locale": "en_GB",
            "_dc": str(int(time.time() * 1000)),
        }
        resp = self._session.get(url, params=params, timeout=15)
        if resp.status_code != 200:
            return False
        data = self.to_json(resp.text)
        if "qr" not in data:
            return False
        self._qr_image_url = data["qr"]
        self._login_url = data["loginUrl"]
        self._long_polling_url = data["lp"]
        self._timeout = float(data.get("timeout") or 300)
        return True

    def _start_qr_server(self) -> None:
        client_ref = self

        class QRHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_GET(self) -> None:
                if self.path == "/qr.png":
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(client_ref._qr_bytes)
                    return
                if self.path == "/status":
                    body = json.dumps(client_ref._status_state, ensure_ascii=False).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(body)
                    return

                html = f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Вход в Xiaomi Home (QR)</title>
<style>
  body {{ font-family: system-ui, -apple-system, sans-serif; background:#0f172a; color:#f8fafc;
          display:flex; align-items:center; justify-content:center; min-height:100vh; margin:0; }}
  .card {{ background:#1e293b; border-radius:16px; padding:28px; max-width:440px; text-align:center;
           box-shadow:0 20px 50px rgba(0,0,0,.45); }}
  img {{ background:#fff; padding:12px; border-radius:12px; width:280px; height:280px; }}
  .msg {{ margin-top:14px; font-size:15px; color:#93c5fd; }}
  .ok {{ color:#4ade80; font-weight:600; }}
  .err {{ color:#f87171; font-weight:600; }}
  ol {{ text-align:left; font-size:14px; color:#cbd5e1; line-height:1.5; }}
</style>
</head>
<body>
  <div class="card">
    <h2>Вход в аккаунт Xiaomi</h2>
    <img src="/qr.png" alt="Xiaomi QR">
    <ol>
      <li>Откройте приложение <b>Xiaomi Home</b> на телефоне.</li>
      <li>Перейдите в <b>Профиль</b> → иконка сканера QR в правом верхнем углу.</li>
      <li>Отсканируйте код и нажмите <b>Подтвердить вход</b>.</li>
    </ol>
    <div id="status" class="msg">Ожидаю сканирования...</div>
  </div>
<script>
setInterval(async () => {{
  try {{
    const r = await fetch('/status');
    const s = await r.json();
    const el = document.getElementById('status');
    el.textContent = s.message;
    if (s.done) el.className = 'msg ' + (s.ok ? 'ok' : 'err');
  }} catch {{}}
}}, 1200);
</script>
</body>
</html>"""
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html.encode("utf-8"))

        self._httpd = HTTPServer(("127.0.0.1", self._qr_port), QRHandler)
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def login_step_2(self, open_browser: bool = False) -> Path:
        if not self._qr_image_url:
            raise RuntimeError("QR image URL не получен")
        resp = self._session.get(self._qr_image_url, timeout=15)
        if resp.status_code != 200 or not resp.content:
            raise RuntimeError(f"Не удалось скачать QR-картинку: HTTP {resp.status_code}")

        self._qr_bytes = resp.content
        qr_path = env_setup.CACHE_DIR / "xiaomi_qr.png"
        qr_path.write_bytes(self._qr_bytes)

        self._start_qr_server()
        qr_url = f"http://127.0.0.1:{self._qr_port}"
        if open_browser:
            webbrowser.open(qr_url)

        print(f"QR_READY_FILE: {qr_path}", flush=True)
        print(f"QR_READY_URL: {qr_url}", flush=True)
        print(f"QR_LOGIN_URL: {self._login_url}", flush=True)
        return qr_path

    def login_step_3(self, max_wait: float = 280.0) -> bool:
        if not self._long_polling_url:
            return False
        deadline = time.monotonic() + min(max_wait, self._timeout)
        while time.monotonic() < deadline:
            try:
                resp = self._session.get(self._long_polling_url, timeout=10)
            except requests.exceptions.Timeout:
                continue
            except requests.exceptions.RequestException as exc:
                self._status_state = {"done": True, "ok": False, "message": f"Ошибка сети: {exc}"}
                return False

            if resp.status_code == 200:
                data = self.to_json(resp.text)
                if "ssecurity" in data and "location" in data:
                    self.userId = str(data["userId"])
                    self._ssecurity = data["ssecurity"]
                    self._location = data["location"]
                    self._status_state = {
                        "done": False,
                        "ok": False,
                        "message": "QR подтверждён! Получаю токены устройств...",
                    }
                    return True
        self._status_state = {"done": True, "ok": False, "message": "Время ожидания QR истекло."}
        return False

    def login_step_4(self) -> bool:
        if not self._location:
            return False
        resp = self._session.get(
            self._location,
            headers={"content-type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        if resp.status_code != 200:
            return False
        token = resp.cookies.get("serviceToken") or self._session.cookies.get("serviceToken")
        if not token:
            return False
        self._serviceToken = token
        return True

    def stop_qr_server(self) -> None:
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:
                pass
            self._httpd = None


def discover_devices(
    client: XiaomiCloudQRClient,
    servers: list[str],
    check_all_servers: bool = False,
) -> tuple[list[dict[str, Any]], str | None]:
    all_devices: list[dict[str, Any]] = []
    found_server: str | None = None

    for server in servers:
        homes_to_check: list[tuple[Any, Any]] = []
        homes = client.get_homes(server)
        if homes and homes.get("result") and homes["result"].get("homelist"):
            for h in homes["result"]["homelist"]:
                homes_to_check.append((h["id"], client.userId))

        dev_cnt = client.get_dev_cnt(server)
        if dev_cnt and dev_cnt.get("result") and dev_cnt["result"].get("share"):
            for h in dev_cnt["result"]["share"].get("share_family") or []:
                homes_to_check.append((h["home_id"], h["home_owner"]))

        for home_id, owner_id in homes_to_check:
            devs = client.get_devices(server, home_id, owner_id)
            info_list = ((devs or {}).get("result") or {}).get("device_info") or []
            for d in info_list:
                d_copy = dict(d)
                d_copy["_server"] = server
                all_devices.append(d_copy)
                if found_server is None and (".vacuum." in d.get("model", "") or d.get("model") == "ijai.vacuum.v3"):
                    found_server = server

        if found_server and not check_all_servers:
            break

    return all_devices, found_server


def pick_vacuum(devices: list[dict[str, Any]], lan_map: dict[int, str], target_model: str) -> dict[str, Any] | None:
    vacuums = [
        d for d in devices
        if d.get("model") == target_model or ".vacuum." in d.get("model", "")
    ]
    if not vacuums:
        # Fallback: match any device whose numeric did matches local LAN miIO devices
        vacuums = [d for d in devices if str(d.get("did", "")).isdigit() and int(d["did"]) in lan_map]
    if not vacuums:
        return None
    for v in vacuums:
        if v.get("model") == target_model:
            return v
    return vacuums[0]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Поиск пылесоса Xiaomi в LAN и получение токена через QR")
    parser.add_argument("--scan-only", action="store_true", help="Только локальное сканирование UDP 54321")
    parser.add_argument("--qr", action="store_true", help="Выполнить вход по QR-коду Xiaomi")
    parser.add_argument("--open", action="store_true", help="Открыть локальную страницу с QR в браузере")
    parser.add_argument("--timeout", type=float, default=280.0, help="Таймаут ожидания сканирования QR, с")
    parser.add_argument("--all-servers", action="store_true", help="Проверить все серверы Xiaomi")
    parser.add_argument("--server", type=str, default=None, help="Приоритетный сервер (ru, de, cn, sg, us, i2)")
    args = parser.parse_args(argv)

    cfg = env_setup.load_config()
    vcfg = cfg.setdefault("vacuum", {})
    candidate_ips = vcfg.get("candidate_ips") or []
    if vcfg.get("ip") and vcfg["ip"] not in candidate_ips:
        candidate_ips = [vcfg["ip"], *candidate_ips]

    lan_map = scan_lan_miio(candidate_ips)
    print("LAN_MIIO:", json.dumps({str(k): v for k, v in lan_map.items()}, ensure_ascii=False), flush=True)

    if args.scan_only or not args.qr:
        return 0

    servers = [args.server] + [s for s in SERVERS if s != args.server] if args.server else list(SERVERS)

    client = XiaomiCloudQRClient()
    try:
        if not client.login_step_1():
            raise RuntimeError("Xiaomi login_step_1 не вернул QR-ссылку")
        client.login_step_2(open_browser=args.open)
        if not client.login_step_3(max_wait=args.timeout):
            raise RuntimeError("Вход по QR не завершён вовремя")
        if not client.login_step_4():
            raise RuntimeError("Не удалось получить serviceToken Xiaomi")

        devices, found_server = discover_devices(client, servers, check_all_servers=args.all_servers)
        devices_path = env_setup.CACHE_DIR / "xiaomi_devices.json"
        # Save sanitized list plus full tokens locally inside .cache/
        devices_path.write_text(json.dumps(devices, indent=2, ensure_ascii=False), encoding="utf-8")

        vac = pick_vacuum(devices, lan_map, vcfg.get("model") or "ijai.vacuum.v3")
        if vac is None:
            client._status_state = {"done": True, "ok": False, "message": "Вход выполнен, но пылесос в аккаунте не найден."}
            summary = [{"name": d.get("name"), "model": d.get("model"), "did": d.get("did")} for d in devices]
            raise RuntimeError(f"Пылесос не найден. Устройства в аккаунте: {json.dumps(summary, ensure_ascii=False)}")

        did_str = str(vac.get("did", ""))
        did_int = int(did_str) if did_str.isdigit() else None
        lan_ip = lan_map.get(did_int) if did_int is not None else None
        final_ip = lan_ip or vac.get("localip") or vcfg.get("ip") or ""
        final_server = found_server or vac.get("_server") or servers[0]
        token = str(vac.get("token") or "")

        session_path = env_setup.CACHE_DIR / "xiaomi_cloud_session.json"
        client.save_session(session_path, final_server)

        vcfg.update(
            {
                "name": vac.get("name") or vcfg.get("name") or "Mi Robot Vacuum-Mop 2 Pro",
                "model": vac.get("model") or vcfg.get("model") or "ijai.vacuum.v3",
                "did": did_str,
                "ip": final_ip,
                "token": token,
                "server": final_server,
                "mac": vac.get("mac", ""),
            }
        )
        env_setup.save_config(cfg)

        # Immediate smoke test via VacuumDriver.status()
        from body.vacuum_driver import VacuumDriver

        drv = VacuumDriver()
        status_res = drv.status()
        client._status_state = {
            "done": True,
            "ok": True,
            "message": f"Успех! {vcfg['model']} @ {final_ip}, заряд {status_res.get('battery')}%. Страницу можно закрыть.",
        }
        time.sleep(1.5)
        print(
            "DISCOVERY_OK:",
            json.dumps(
                {
                    "name": vcfg["name"],
                    "model": vcfg["model"],
                    "did": did_str,
                    "ip": final_ip,
                    "server": final_server,
                    "token_masked": mask_token(token),
                    "status": status_res,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 0
    finally:
        client.stop_qr_server()


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr, flush=True)
        raise SystemExit(1)
