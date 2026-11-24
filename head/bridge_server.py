"""Local FastAPI bridge server: Android phone WebSocket head + GPU/CPU STT + calm TTS + Vacuum driver."""

from __future__ import annotations

import asyncio
import base64
import collections
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
import json
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
import uvicorn

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import env_setup

env_setup.configure_env()

from body.vacuum_driver import VacuumDriver
from voice.agy_brain import AgyBrain
from voice.stt_engine import STTEngine, StreamingVADProcessor
from voice.tts_engine import TTSEngine
from voice.voice_brain import has_wake_name, is_echo, is_vacuum_system_voice, is_wake_command, parse_voice_intent

HEAD_HTML_PATH = ROOT_DIR / "head" / "static" / "head.html"
PANEL_HTML_PATH = ROOT_DIR / "head" / "static" / "panel.html"
INBOX_CURSOR_PATH = env_setup.CACHE_DIR / "inbox_cursor.txt"
PANEL_HISTORY_PATH = env_setup.RUNTIME_DIR / "panel_history.jsonl"


def _save_voice_prefs(**kwargs: Any) -> None:
    """Persist selected voice/AI UI preferences to config.json cleanly."""
    try:
        cfg = env_setup.load_config()
        vcfg = cfg.setdefault("voice", {})
        for k, v in kwargs.items():
            if v is not None:
                vcfg[k] = v
        env_setup.save_config(cfg)
    except Exception:
        pass


def _load_panel_history(limit: int = 100) -> list[dict[str, Any]]:
    if not PANEL_HISTORY_PATH.exists():
        return []
    items: list[dict[str, Any]] = []
    for line in PANEL_HISTORY_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    if len(items) > limit:
        items = items[-limit:]
        try:
            PANEL_HISTORY_PATH.write_text(
                "\n".join(json.dumps(x, ensure_ascii=False) for x in items) + "\n",
                encoding="utf-8",
            )
        except Exception:
            pass
    return items


def _append_panel_history(ev: dict[str, Any]) -> None:
    try:
        PANEL_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        with PANEL_HISTORY_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except Exception:
        pass


def read_inbox_lines() -> list[dict[str, Any]]:
    if not env_setup.INBOX_PATH.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line in env_setup.INBOX_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return entries


def get_inbox_cursor() -> int:
    if not INBOX_CURSOR_PATH.exists():
        return 0
    try:
        return max(0, int(INBOX_CURSOR_PATH.read_text(encoding="utf-8").strip()))
    except ValueError:
        return 0


def set_inbox_cursor(pos: int) -> None:
    INBOX_CURSOR_PATH.parent.mkdir(parents=True, exist_ok=True)
    INBOX_CURSOR_PATH.write_text(str(max(0, pos)), encoding="utf-8")


class HeadSession:
    """Shared coordinator for head phone WebSocket, voice pipeline, and vacuum body."""

    def __init__(self) -> None:
        self.cfg = env_setup.load_config()
        self.vacuum = VacuumDriver()
        self.tts = TTSEngine()

        self.stt: STTEngine | None = None
        self.vad_proc: StreamingVADProcessor | None = None
        self.stt_ready = False
        self.stt_error: str | None = None

        self.ws: WebSocket | None = None
        self.ws_lock = asyncio.Lock()
        self.cam_w = 0
        self.cam_h = 0
        self.battery: int | None = None
        self.last_frame_md5: str | None = None
        self.last_frame_ts: float = 0.0

        self.pending_frames: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.pending_tts: dict[str, asyncio.Future[dict[str, Any]]] = {}

        self.is_speaking = False
        self.is_moving = False
        self.last_speaking_end = 0.0

        self.loop: asyncio.AbstractEventLoop | None = None
        self.pcm_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=200)
        self.voice_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=32)
        self.transcript_event = asyncio.Event()
        self._bg_tasks: list[asyncio.Task[Any]] = []
        self.chunks_received = 0
        self.last_chunk_ts = 0.0

        self.auto_voice: bool = bool(self.cfg.get("voice", {}).get("auto_voice", True))
        self.voice_muted: bool = False
        self.speaker_muted: bool = False
        self.speak_thoughts: bool = bool(self.cfg.get("voice", {}).get("speak_thoughts", True))
        self.wake_word_only: bool = bool(self.cfg.get("voice", {}).get("wake_word_only", False))
        self.autonomous_mode: bool = False
        self.autonomous_interval_s: float = float(self.cfg.get("voice", {}).get("autonomous_interval_s", 5.0))
        self.autonomous_step_count: int = 0
        self._autonomous_task: asyncio.Task[Any] | None = None
        self._low_batt_warned: bool = False
        self._pending_user_msgs: list[dict[str, Any]] = []
        self.agy = AgyBrain(
            model=str(self.cfg.get("voice", {}).get("agy_model") or "gemini-3.8-flash-medium"),
            enabled=bool(self.cfg.get("voice", {}).get("agy_enabled", True)),
        )
        self._agy_spoke_in_turn: bool = False
        self._agy_task: asyncio.Task[Any] | None = None
        self._listen_waiters: int = 0
        self._voice_lock = asyncio.Lock()
        self.last_spoken_text: str = ""
        self.last_spoken_ts: float = 0.0
        self.ignore_vacuum_voice_until: float = 0.0
        self.last_voice_action: dict[str, Any] | None = None
        self._manual_idle_task: asyncio.Task[Any] | None = None
        self.panel_sockets: set[WebSocket] = set()
        self.panel_events: collections.deque[dict[str, Any]] = collections.deque(
            _load_panel_history(100), maxlen=300
        )
        self.last_frame_path: Path | None = None
        if env_setup.FRAMES_DIR.exists():
            existing_frames = sorted(env_setup.FRAMES_DIR.glob("frame-*.jpg"))
            if existing_frames:
                self.last_frame_path = existing_frames[-1]

    def get_health_dict(self) -> dict[str, Any]:
        vcfg = env_setup.load_config().get("vacuum", {})
        return {
            "ok": True,
            "head_connected": self.ws is not None,
            "cam_w": self.cam_w,
            "cam_h": self.cam_h,
            "battery": self.battery,
            "stt_ready": self.stt_ready,
            "stt_device": self.stt.device if self.stt else None,
            "stt_compute_type": self.stt.compute_type if self.stt else None,
            "stt_error": self.stt_error,
            "vacuum_configured": bool(vcfg.get("ip") and vcfg.get("token")),
            "vacuum_model": vcfg.get("model"),
            "tts_engine": env_setup.load_config().get("voice", {}).get("tts_engine", "edge"),
            "auto_voice": self.auto_voice,
            "voice_muted": self.voice_muted,
            "speaker_muted": self.speaker_muted,
            "speak_thoughts": self.speak_thoughts,
            "wake_word_only": self.wake_word_only,
            "autonomous_mode": self.autonomous_mode,
            "autonomous_interval_s": self.autonomous_interval_s,
            "autonomous_step": self.autonomous_step_count,
            "pending_user_count": len(self._pending_user_msgs),
            "agy_enabled": self.agy.enabled,
            "agy_busy": self.agy.busy,
            "agy_model": self.agy.model,
            "agy_conversation_id": self.agy.conversation_id,
            "antigravity_listening": self._listen_waiters > 0,
            "last_voice_action": self.last_voice_action,
            "inbox_count": len(read_inbox_lines()),
            "chunks_received": self.chunks_received,
            "is_speaking": self.is_speaking,
            "is_moving": self.is_moving,
        }

    async def _broadcast_panel(self, payload: dict[str, Any]) -> None:
        if not self.panel_sockets:
            return
        msg_text = json.dumps(payload, ensure_ascii=False)
        sockets = list(self.panel_sockets)

        async def _send_one(pws: WebSocket) -> WebSocket | None:
            try:
                await asyncio.wait_for(pws.send_text(msg_text), timeout=1.5)
                return None
            except Exception:
                return pws

        results = await asyncio.gather(*(_send_one(pws) for pws in sockets), return_exceptions=True)
        for pws, res in zip(sockets, results):
            if res is not None:
                self.panel_sockets.discard(pws)
                try:
                    await asyncio.wait_for(pws.close(), timeout=0.5)
                except Exception:
                    pass

    async def emit_panel_event(
        self,
        kind: str,
        text: str,
        *,
        meta: str = "",
        url: str | None = None,
        in_feed: bool = True,
    ) -> dict[str, Any]:
        ev: dict[str, Any] = {
            "id": uuid.uuid4().hex[:10],
            "ts": round(time.time(), 3),
            "time_str": datetime.now().strftime("%H:%M:%S"),
            "kind": kind,
            "text": text,
            "meta": meta,
            "in_feed": in_feed,
        }
        if url is not None:
            ev["url"] = url
        self.panel_events.append(ev)
        if in_feed:
            _append_panel_history(ev)
        await self._broadcast_panel({"type": "event", "event": ev})
        return ev

    async def check_battery_guard(self, battery: int | None) -> None:
        if battery is None:
            return
        if battery >= 30:
            self._low_batt_warned = False
            return
        if battery <= 20 and not self._low_batt_warned:
            self._low_batt_warned = True
            if self.autonomous_mode:
                await self.stop_autonomous(cancel_current=False)
            await self.emit_panel_event(
                "action", f"🪫 Низкий заряд АКБ ({battery}%) — «Свободная жизнь» остановлена"
            )
            if not self.speaker_muted and self.ws is not None:
                asyncio.create_task(
                    self._speak_or_panel(
                        f"Заряд батареи {battery} процентов, пора вернуться на зарядную базу.",
                        wait=False,
                    )
                )

    def _stop_vacuum_bg(self, *, exit_manual: bool = True) -> None:
        async def _worker() -> None:
            try:
                await asyncio.to_thread(self.vacuum.emergency_stop)
                if exit_manual:
                    await asyncio.to_thread(self.vacuum.exit_manual)
            except Exception:
                pass

        asyncio.create_task(_worker())

    async def broadcast_panel_health(self) -> None:
        await self._broadcast_panel({"type": "health", "health": self.get_health_dict()})

    async def _speak_or_panel(self, text: str, wait: bool = True) -> None:
        if self.speaker_muted:
            self.last_spoken_text = text
            await self.emit_panel_event("robot_say", text, meta="тихий режим")
        elif self.ws is not None:
            await self.speak(text, wait=wait)
        else:
            await self.emit_panel_event("robot_say", text, meta="без динамика")

    async def sync_rest_ui(self) -> None:
        if self.agy.busy:
            await self.send_ui("thinking", f"Думаю ({self.agy.model})...")
        elif self.speaker_muted and self.voice_muted:
            await self.send_ui("idle", "Тихий режим (без голоса). Пишите в веб-пульт.")
        elif self.voice_muted:
            await self.send_ui("idle", "Микрофон заглушён. Скажите: Жужа, слушай меня.")
        else:
            await self.send_ui("listening")

    def _on_emergency_stop_from_vad(self, phrase: str) -> None:
        # Execute stop synchronously in the audio worker thread for minimum latency.
        self.autonomous_mode = False
        try:
            self.vacuum.emergency_stop()
        except Exception:
            pass
        self.is_moving = False
        self.is_speaking = False
        if self.loop is not None:
            self.loop.call_soon_threadsafe(
                lambda: asyncio.create_task(self._notify_emergency_stop(f"СТОП по команде: «{phrase}»"))
            )

    async def start_autonomous(self) -> None:
        self.agy.enabled = True
        self.autonomous_mode = True
        if self._autonomous_task is None or self._autonomous_task.done():
            self._autonomous_task = asyncio.create_task(self._autonomous_loop())
        await self.broadcast_panel_health()

    async def stop_autonomous(self, *, cancel_current: bool = False) -> None:
        self.autonomous_mode = False
        if self._autonomous_task is not None and not self._autonomous_task.done():
            self._autonomous_task.cancel()
        if cancel_current:
            self._pending_user_msgs.clear()
            await self.agy.cancel()
            if self._agy_task is not None and not self._agy_task.done():
                self._agy_task.cancel()
        await self.broadcast_panel_health()

    async def _autonomous_loop(self) -> None:
        try:
            while self.autonomous_mode:
                if self.agy.busy or self.is_speaking or not self.voice_queue.empty() or self._pending_user_msgs:
                    await asyncio.sleep(0.5)
                    continue
                if not self.agy.enabled:
                    self.autonomous_mode = False
                    await self.emit_panel_event("action", "Свободная жизнь остановлена (ИИ выключен)")
                    await self.broadcast_panel_health()
                    break

                self.autonomous_step_count += 1
                step_no = self.autonomous_step_count
                await self.emit_panel_event("action", f"🧭 Свободная жизнь · импульс #{step_no}")
                prompt_text = (
                    f"Автономный импульс #{step_no}. Осмотри свежий кадр, продолжи свой собственный план "
                    "или придумай новое исследование комнаты шаг за шагом."
                )
                self._agy_task = asyncio.create_task(self._run_agy_turn(prompt_text, source="autonomous"))
                try:
                    await self._agy_task
                except asyncio.CancelledError:
                    break

                if not self.autonomous_mode:
                    break

                # Pause between autonomous impulses so user can speak, read web panel, or tap STOP
                ticks = max(2, int(self.autonomous_interval_s * 2))
                for _ in range(ticks):
                    if not self.autonomous_mode or not self.voice_queue.empty() or self.agy.busy or self._pending_user_msgs:
                        break
                    await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            pass
        finally:
            self._autonomous_task = None
            await self.broadcast_panel_health()

    def _enqueue_voice_entry(self, entry: dict[str, Any]) -> None:
        if not self.auto_voice:
            self.transcript_event.set()
            return
        if self.voice_queue.full():
            try:
                self.voice_queue.get_nowait()
                self.voice_queue.task_done()
            except asyncio.QueueEmpty:
                pass
        self.voice_queue.put_nowait(entry)

    def _on_transcript_from_vad(self, entry: dict[str, Any]) -> None:
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self._enqueue_voice_entry, entry)

    def _schedule_manual_exit(self, delay_s: float = 25.0) -> None:
        if self._manual_idle_task is not None and not self._manual_idle_task.done():
            self._manual_idle_task.cancel()

        async def _exit_later() -> None:
            await asyncio.sleep(delay_s)
            if not self.is_moving:
                try:
                    await asyncio.to_thread(self.vacuum.exit_manual)
                except Exception:
                    pass

        self._manual_idle_task = asyncio.create_task(_exit_later())

    async def _voice_brain_worker(self) -> None:
        while True:
            entry = await self.voice_queue.get()
            try:
                from_panel = entry.get("source") == "panel"
                if not self.auto_voice:
                    continue
                if self.ws is None and not from_panel:
                    continue
                text = str(entry.get("text") or "").strip()
                if not text:
                    continue
                if not from_panel:
                    if is_echo(text, self.last_spoken_text, time.monotonic() - self.last_spoken_ts):
                        continue
                    if is_vacuum_system_voice(text):
                        self.last_voice_action = {"ignored_vacuum_voice": text, "ts": time.time()}
                        continue

                if self.voice_muted and not from_panel:
                    if not self.speaker_muted and is_wake_command(text):
                        set_inbox_cursor(len(read_inbox_lines()))
                        await self.emit_panel_event("user_voice", text)
                        async with self._voice_lock:
                            self.voice_muted = False
                            self.last_voice_action = {"heard": text, "action": "wake", "ts": time.time()}
                            await self._speak_or_panel("Я проснулась и снова слушаю вас!", wait=True)
                            await self.broadcast_panel_health()
                        if self._listen_waiters > 0:
                            self.transcript_event.set()
                    continue

                rooms_cfg = env_setup.load_config().get("vacuum", {}).get("rooms") or {}
                intent = parse_voice_intent(text, rooms_cfg=rooms_cfg)
                if intent is None:
                    continue

                # Ignore ambient vacuum body chatter shortly after dock/clean commands (allow stop & sleep!)
                if not from_panel and time.monotonic() < self.ignore_vacuum_voice_until and intent.action not in ("stop", "sleep", "cancel_return"):
                    self.last_voice_action = {"ignored_during_vacuum_speech": text, "ts": time.time()}
                    continue

                # Wake-word filter: when enabled, ignore room speech unless addressing Жужа (always allow stop!)
                if self.wake_word_only and not from_panel and intent.action != "stop" and not has_wake_name(text):
                    self.last_voice_action = {"ignored_no_wake_name": text, "ts": time.time()}
                    continue

                if not from_panel:
                    await self.emit_panel_event("user_voice", text)

                # While deep AI is busy: allow stop/sleep/auto_off immediately, queue all other human input
                # so POST /step receives it on the next step OR _run_agy_turn answers right after finishing!
                if self.agy.busy and intent.action not in ("stop", "sleep", "auto_off"):
                    self._pending_user_msgs.append(
                        {
                            "ts": round(time.time(), 3),
                            "text": text,
                            "source": "panel" if from_panel else "voice",
                            "stop_triggered": bool(entry.get("stop_triggered")),
                        }
                    )
                    self.last_voice_action = {
                        "heard": text,
                        "action": "queued_for_agy",
                        "pending_count": len(self._pending_user_msgs),
                        "ts": time.time(),
                    }
                    if from_panel:
                        await self.emit_panel_event("action", "Передам Жуже на ближайшем шаге...")
                    await self.broadcast_panel_health()
                    continue

                if self.agy.busy and intent.action == "auto_off":
                    await self.stop_autonomous(cancel_current=False)
                    await self.emit_panel_event("action", "🧭 Режим «Свободная жизнь» выключен голосом (доделаю шаг)")
                    continue

                # Deep AI vision & open dialogue: delegate to external /listen waiter OR built-in agy CLI
                if intent.action in ("vision", "chat"):
                    self.last_voice_action = {
                        "heard": text,
                        "action": intent.action,
                        "to_antigravity": self._listen_waiters > 0,
                        "agy_enabled": self.agy.enabled,
                        "ts": time.time(),
                    }
                    if self._listen_waiters > 0:
                        if intent.action == "vision" and self.ws is not None and not self.speaker_muted:
                            await self.speak("Смотрю, секунду.", wait=False)
                        self.transcript_event.set()
                    elif self.agy.enabled:
                        set_inbox_cursor(len(read_inbox_lines()))
                        if self.agy.busy:
                            await self.emit_panel_event("action", "Додумываю предыдущую мысль...")
                        else:
                            if intent.action == "vision" and self.ws is not None and not self.speaker_muted:
                                await self.speak("Смотрю, секунду.", wait=False)
                            src = str(entry.get("source") or "voice")
                            self._agy_task = asyncio.create_task(self._run_agy_turn(text, source=src))
                    else:
                        set_inbox_cursor(len(read_inbox_lines()))
                        async with self._voice_lock:
                            await self._speak_or_panel(
                                "Глубокий ИИ сейчас выключен. Включите его на экране Samsung или скажите: включи живой ИИ.",
                                wait=True,
                            )
                    continue

                if intent.action == "stop":
                    await self.stop_autonomous(cancel_current=True)
                    set_inbox_cursor(len(read_inbox_lines()))
                    await self._execute_voice_intent(text, intent)
                    if self._listen_waiters > 0:
                        self.transcript_event.set()
                    continue

                if intent.action == "sleep":
                    set_inbox_cursor(len(read_inbox_lines()))
                    if self.agy.busy:
                        # Mute mic immediately WITHOUT interrupting her ongoing AI thought/action!
                        self.voice_muted = True
                        self.last_voice_action = {"heard": text, "action": "sleep_while_busy", "ts": time.time()}
                        await self.emit_panel_event("action", "Микрофон заглушён (додумываю текущий ответ...)")
                        await self.broadcast_panel_health()
                    else:
                        await self._execute_voice_intent(text, intent)
                        if self._listen_waiters > 0:
                            self.transcript_event.set()
                    continue

                set_inbox_cursor(len(read_inbox_lines()))
                async with self._voice_lock:
                    await self._execute_voice_intent(text, intent)
            except Exception as exc:
                self.last_voice_action = {"error": str(exc), "ts": time.time()}
            finally:
                self.voice_queue.task_done()

    async def _run_agy_turn(self, user_text: str, source: str = "voice") -> None:
        frame_file: str | None = None
        if self.ws is not None:
            try:
                f_res = await self.capture_frame(width=640)
                frame_file = f_res.get("file")
            except Exception:
                pass

        await self.send_ui("thinking", f"Думаю ({self.agy.model})...")
        self.agy.busy = True
        self._agy_spoke_in_turn = False
        await self.broadcast_panel_health()

        try:
            res = await self.agy.run_turn(
                user_text,
                frame_path=frame_file,
                battery=self.battery,
                status_text="на связи",
                source=source,
                autonomous_mode=self.autonomous_mode,
                autonomous_step=self.autonomous_step_count,
            )
            if res.get("cancelled"):
                return
            if not res.get("ok") and res.get("error"):
                await self.emit_panel_event("action", f"Ошибка ИИ: {res['error']}")
                await self._speak_or_panel("Не удалось получить ответ модели, попробуйте ещё раз.", wait=True)
                return

            thought = (res.get("thought") or "").strip()
            reply = (res.get("reply") or "").strip()
            meta = f"{res.get('model')} · {res.get('duration_s')}с · {res.get('thinking_tokens', 0)} thinking tok"
            if thought:
                await self.emit_panel_event("thought", thought, meta=meta)

            if reply and not res.get("silent") and reply != self.last_spoken_text.strip():
                await self._speak_or_panel(reply, wait=True)
            elif not self._agy_spoke_in_turn and not res.get("silent"):
                self.agy.busy = False
                await self.sync_rest_ui()
        except asyncio.CancelledError:
            await self.agy.cancel()
        except Exception as exc:
            await self.emit_panel_event("action", f"Сбой ИИ: {exc}")
        finally:
            self.agy.busy = False
            next_turn_text: str | None = None
            next_turn_src = "voice"
            if self._pending_user_msgs and self.agy.enabled and not self.agy._cancel_requested:
                pending = list(self._pending_user_msgs)
                self._pending_user_msgs.clear()
                next_turn_text = "\n".join(m["text"] for m in pending if m.get("text"))
                next_turn_src = str(pending[-1].get("source") or "voice")
                set_inbox_cursor(len(read_inbox_lines()))
            await self.sync_rest_ui()
            await self.broadcast_panel_health()
            if next_turn_text:
                self._agy_task = asyncio.create_task(self._run_agy_turn(next_turn_text, source=next_turn_src))

    async def _execute_voice_intent(self, raw_text: str, intent: Any) -> None:
        act = intent.action
        self.last_voice_action = {"heard": raw_text, "action": act, "params": intent.params, "ts": time.time()}

        if act == "sleep":
            if self._manual_idle_task is not None and not self._manual_idle_task.done():
                self._manual_idle_task.cancel()
            try:
                await asyncio.to_thread(self.vacuum.emergency_stop)
                await asyncio.to_thread(self.vacuum.exit_manual)
            except Exception:
                pass
            self.is_moving = False
            self.voice_muted = True
            await self._speak_or_panel(intent.reply, wait=True)
            await self.send_ui("idle", "Режим сна (не слушаю). Скажите: Жужа, слушай меня.")
            await self.emit_panel_event("action", "Перешла в режим сна (микрофон заглушён)")
            await self.broadcast_panel_health()
            return

        if act == "stop":
            try:
                await asyncio.to_thread(self.vacuum.emergency_stop)
            except Exception:
                pass
            self.is_moving = False
            await self._speak_or_panel(intent.reply or "Остановилась!", wait=True)
            await self.emit_panel_event("action", "Экстренная остановка")
            return

        if act == "agy_on":
            self.agy.enabled = True
            await self._speak_or_panel(intent.reply, wait=True)
            await self.emit_panel_event("action", f"Глубокий ИИ включён ({self.agy.model})")
            await self.broadcast_panel_health()
            return

        if act == "agy_off":
            self.agy.enabled = False
            await self.agy.cancel()
            await self._speak_or_panel(intent.reply, wait=True)
            await self.emit_panel_event("action", "Глубокий ИИ выключен")
            await self.broadcast_panel_health()
            return

        if act == "agy_reset":
            self.agy.reset_conversation()
            await self._speak_or_panel(intent.reply, wait=True)
            await self.emit_panel_event("action", "Новый диалог (память очищена)")
            await self.broadcast_panel_health()
            return

        if act == "auto_on":
            await self._speak_or_panel(intent.reply, wait=True)
            await self.emit_panel_event("action", "🧭 Режим «Свободная жизнь» включён голосом")
            await self.start_autonomous()
            return

        if act == "auto_off":
            await self.stop_autonomous(cancel_current=False)
            await self._speak_or_panel(intent.reply, wait=True)
            await self.emit_panel_event("action", "🧭 Режим «Свободная жизнь» выключен голосом")
            return

        if act == "say":
            await self._speak_or_panel(intent.reply, wait=True)
            return

        if act == "status":
            st = await asyncio.to_thread(self.vacuum.status)
            batt = st.get("battery")
            st_text = st.get("status_text") or "ожидание"
            fault_text = st.get("fault_text") or "ошибок нет"
            batt_str = f"{batt} процентов" if batt is not None else "неизвестен"
            if st.get("battery") is not None:
                await self.send_ui("listening", battery=int(st["battery"]))
                await self.broadcast_panel_health()
            await self._speak_or_panel(f"Заряд {batt_str}, состояние: {st_text}, {fault_text}.", wait=True)
            return

        if act == "frame":
            if self.ws is not None:
                await self.capture_frame()
                await self._speak_or_panel(intent.reply, wait=True)
            else:
                await self._speak_or_panel("Камера телефона-головы сейчас не подключена.", wait=True)
            return

        if act == "find":
            await asyncio.to_thread(self.vacuum.find)
            await self._speak_or_panel(intent.reply, wait=True)
            return

        if act == "dock":
            self.ignore_vacuum_voice_until = time.monotonic() + 15.0
            await asyncio.gather(
                self._speak_or_panel(intent.reply, wait=True),
                asyncio.to_thread(self.vacuum.dock),
            )
            await self.emit_panel_event("action", "Отправилась на зарядную базу")
            return

        if act == "cancel_return":
            self.ignore_vacuum_voice_until = time.monotonic() + 5.0
            await asyncio.gather(
                self._speak_or_panel(intent.reply, wait=True),
                asyncio.to_thread(self.vacuum.cancel_return),
            )
            await self.emit_panel_event("action", "Отмена возврата на базу")
            return

        if act == "start_clean":
            mode = str(intent.params.get("mode") or "auto")
            self.ignore_vacuum_voice_until = time.monotonic() + 8.0
            await asyncio.gather(
                self._speak_or_panel(intent.reply, wait=True),
                asyncio.to_thread(self.vacuum.start_clean, mode),
            )
            await self.emit_panel_event("action", f"Запущена штатная уборка ({mode})")
            return

        if act == "stop_clean":
            self.ignore_vacuum_voice_until = time.monotonic() + 5.0
            await asyncio.gather(
                self._speak_or_panel(intent.reply, wait=True),
                asyncio.to_thread(self.vacuum.stop_clean),
            )
            await self.emit_panel_event("action", "Уборка остановлена")
            return

        if act == "edge_clean":
            self.ignore_vacuum_voice_until = time.monotonic() + 8.0
            await asyncio.gather(
                self._speak_or_panel(intent.reply, wait=True),
                asyncio.to_thread(self.vacuum.edge_clean),
            )
            await self.emit_panel_event("action", "Уборка по периметру")
            return

        if act == "room_clean":
            rid = int(intent.params["room_id"])
            self.ignore_vacuum_voice_until = time.monotonic() + 8.0
            await asyncio.gather(
                self._speak_or_panel(intent.reply, wait=True),
                asyncio.to_thread(self.vacuum.room_clean, [rid]),
            )
            await self.emit_panel_event("action", f"Уборка комнаты ID={rid}")
            return

        if act == "room_clean_unknown":
            await self._speak_or_panel(intent.reply, wait=True)
            return

        if act == "exit":
            await asyncio.gather(
                self._speak_or_panel(intent.reply, wait=True),
                asyncio.to_thread(self.vacuum.exit_manual),
            )
            return

        if act == "move":
            moves = list(intent.params.get("moves") or [])
            if not moves:
                return
            if self._manual_idle_task is not None and not self._manual_idle_task.done():
                self._manual_idle_task.cancel()

            async def _run_move() -> None:
                self.is_moving = True
                try:
                    await asyncio.to_thread(self.vacuum.move, moves, None)
                finally:
                    self.is_moving = False

            await asyncio.gather(
                _run_move(),
                self._speak_or_panel(intent.reply, wait=True),
            )
            await self.emit_panel_event("action", f"Ход: {' '.join(moves)}")
            await self.send_ui("listening")
            self._schedule_manual_exit(25.0)

    async def _notify_emergency_stop(self, caption: str) -> None:
        await self.agy.cancel()
        if self._agy_task is not None and not self._agy_task.done():
            self._agy_task.cancel()
        await self.send_ws({"type": "stop_audio"})
        await self.send_ui("stopped", caption)
        await self.broadcast_panel_health()
        self.transcript_event.set()

    def _load_stt_sync(self) -> None:
        try:
            stt = STTEngine()
            vad = StreamingVADProcessor(
                stt,
                on_emergency_stop=self._on_emergency_stop_from_vad,
                on_transcript=self._on_transcript_from_vad,
                is_robot_speaking=lambda: self.is_speaking,
                is_robot_moving=lambda: self.is_moving,
                last_speaking_end=lambda: self.last_speaking_end,
            )
            self.stt = stt
            self.vad_proc = vad
            self.stt_ready = True
            self.stt_error = None
        except Exception as exc:
            self.stt_error = str(exc)
            self.stt_ready = False

    async def _pcm_worker(self) -> None:
        while True:
            chunk = await self.pcm_queue.get()
            try:
                if self.vad_proc is not None:
                    await asyncio.to_thread(self.vad_proc.process_pcm16, chunk)
            except Exception:
                pass
            finally:
                self.pcm_queue.task_done()

    async def startup(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._bg_tasks.append(asyncio.create_task(asyncio.to_thread(self._load_stt_sync)))
        self._bg_tasks.append(asyncio.create_task(asyncio.to_thread(self.tts.warmup_silero_sync)))
        self._bg_tasks.append(asyncio.create_task(asyncio.to_thread(self.agy.refresh_models_sync)))
        self._bg_tasks.append(asyncio.create_task(self._pcm_worker()))
        self._bg_tasks.append(asyncio.create_task(self._voice_brain_worker()))

    async def shutdown(self) -> None:
        await self.agy.cancel()
        for task in self._bg_tasks:
            task.cancel()
        if self._bg_tasks:
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)

    async def send_ws(self, payload: dict[str, Any]) -> bool:
        async with self.ws_lock:
            if self.ws is None:
                return False
            try:
                await asyncio.wait_for(
                    self.ws.send_text(json.dumps(payload, ensure_ascii=False)),
                    timeout=3.0,
                )
                return True
            except Exception:
                self.ws = None
                return False

    async def send_ui(self, state: str, caption: str | None = None, battery: int | None = None) -> None:
        if battery is not None:
            self.battery = battery
            await self.check_battery_guard(self.battery)
        msg: dict[str, Any] = {"type": "ui", "state": state}
        if caption is not None:
            msg["caption"] = caption
        if self.battery is not None:
            msg["battery"] = self.battery
        await self.send_ws(msg)

    async def capture_frame(
        self,
        width: int | None = None,
        quality: float | None = None,
        *,
        in_feed: bool = True,
    ) -> dict[str, Any]:
        if self.ws is None:
            raise HTTPException(status_code=503, detail="Телефон-голова не подключён по WebSocket")
        hcfg = env_setup.load_config().get("head", {})
        w = int(width or hcfg.get("default_frame_width") or 640)
        q = float(quality or hcfg.get("jpeg_quality") or 0.78)
        keep = int(hcfg.get("keep_frames") or 20)

        req_id = uuid.uuid4().hex[:12]
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self.pending_frames[req_id] = fut
        started = time.monotonic()

        try:
            ok = await self.send_ws({"type": "grab_frame", "id": req_id, "width": w, "quality": q})
            if not ok:
                raise HTTPException(status_code=503, detail="Соединение с телефоном-головой потеряно")
            data = await asyncio.wait_for(fut, timeout=5.0)
        except asyncio.TimeoutError as exc:
            raise HTTPException(status_code=504, detail="Таймаут получения кадра от телефона-головы") from exc
        finally:
            self.pending_frames.pop(req_id, None)

        if data.get("error"):
            raise HTTPException(status_code=503, detail=data["error"])

        jpeg_bytes = base64.b64decode(data["jpeg_b64"])
        md5 = hashlib.md5(jpeg_bytes).hexdigest()
        now_mono = time.monotonic()
        stale = md5 == self.last_frame_md5 and (now_mono - self.last_frame_ts) > 0.8
        self.last_frame_md5 = md5
        self.last_frame_ts = now_mono

        now = datetime.now()
        name = f"frame-{now.strftime('%H%M%S')}-{now.microsecond // 1000:03d}.jpg"
        env_setup.FRAMES_DIR.mkdir(parents=True, exist_ok=True)
        out_file = env_setup.FRAMES_DIR / name
        out_file.write_bytes(jpeg_bytes)
        self.last_frame_path = out_file

        existing = sorted(env_setup.FRAMES_DIR.glob("frame-*.jpg"))
        for old in existing[:-keep]:
            try:
                old.unlink()
            except OSError:
                pass

        latency_ms = int((time.monotonic() - started) * 1000)
        frame_url = f"/frames/{out_file.name}"
        await self.emit_panel_event(
            "frame",
            out_file.name,
            meta=f"{data.get('w')}x{data.get('h')} · {latency_ms} мс",
            url=frame_url,
            in_feed=in_feed,
        )

        return {
            "ok": True,
            "file": str(out_file),
            "url": frame_url,
            "w": data.get("w"),
            "h": data.get("h"),
            "cam_w": data.get("cam_w"),
            "cam_h": data.get("cam_h"),
            "md5": md5,
            "stale": stale,
            "latency_ms": latency_ms,
        }

    async def speak(
        self,
        text: str,
        wait: bool = True,
        engine: str | None = None,
        emit_event: bool = True,
    ) -> dict[str, Any]:
        if self.speaker_muted:
            self.last_spoken_text = text
            if emit_event:
                await self.emit_panel_event("robot_say", text, meta="тихий режим")
            return {"ok": True, "muted": True, "silent": True}
        if self.ws is None:
            raise HTTPException(status_code=503, detail="Телефон-голова не подключён для воспроизведения речи")
        self.last_spoken_text = text
        self.last_spoken_ts = time.monotonic()
        if emit_event:
            await self.emit_panel_event("robot_say", text)
        audio_bytes, mime, used_engine = await self.tts.synthesize(text, engine=engine)
        req_id = uuid.uuid4().hex[:12]
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self.pending_tts[req_id] = fut

        self.is_speaking = True
        self.last_spoken_ts = time.monotonic()
        await self.send_ui("speaking", text)
        payload = {
            "type": "play_audio",
            "id": req_id,
            "mime": mime,
            "audio_b64": base64.b64encode(audio_bytes).decode("ascii"),
        }
        try:
            ok = await self.send_ws(payload)
            if not ok:
                raise HTTPException(status_code=503, detail="Соединение с телефоном-головой потеряно")
            if wait:
                max_wait = max(12.0, len(text) * 0.14 + 5.0)
                try:
                    ack = await asyncio.wait_for(fut, timeout=max_wait)
                    interrupted = bool(ack.get("interrupted"))
                except asyncio.TimeoutError:
                    interrupted = False
                return {"ok": True, "engine": used_engine, "bytes": len(audio_bytes), "interrupted": interrupted}
            else:
                async def _clear_bg():
                    try:
                        await asyncio.wait_for(fut, timeout=max(12.0, len(text) * 0.14 + 5.0))
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        pass
                    finally:
                        self.pending_tts.pop(req_id, None)
                        self.is_speaking = False
                        self.last_speaking_end = time.monotonic()
                        await self.sync_rest_ui()
                asyncio.create_task(_clear_bg())
                return {"ok": True, "engine": used_engine, "bytes": len(audio_bytes), "async": True}
        finally:
            if wait:
                self.pending_tts.pop(req_id, None)
                self.is_speaking = False
                self.last_speaking_end = time.monotonic()
                await self.sync_rest_ui()


session = HeadSession()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await session.startup()
    try:
        yield
    finally:
        await session.shutdown()


app = FastAPI(title="AI-Robot Bridge", lifespan=lifespan)


class FrameReq(BaseModel):
    width: int | None = None
    quality: float | None = None


class SayReq(BaseModel):
    text: str
    wait: bool = True
    engine: str | None = None
    thought: str | None = None


class ThinkReq(BaseModel):
    text: str
    model: str | None = None


class PanelSendReq(BaseModel):
    text: str


class PanelCmdReq(BaseModel):
    cmd: str
    value: float | None = None
    silent: bool = False


class UiReq(BaseModel):
    state: str = "idle"
    caption: str | None = None
    battery: int | None = None


class ListenReq(BaseModel):
    timeout: float = 15.0
    with_frame: bool = True
    frame_width: int | None = None


class MoveReq(BaseModel):
    moves: list[str]
    gap: float | None = None


class StepReq(BaseModel):
    moves: list[str] = []
    say: str | None = None
    thought: str | None = None
    gap: float | None = None
    frame_width: int | None = None
    no_frame: bool = False


class AutoVoiceReq(BaseModel):
    enabled: bool = True


class AgyConfigReq(BaseModel):
    enabled: bool | None = None
    model: str | None = None
    reset: bool = False


@app.get("/")
async def index():
    return FileResponse(HEAD_HTML_PATH, media_type="text/html; charset=utf-8")


@app.get("/panel")
async def panel_page():
    return FileResponse(
        PANEL_HTML_PATH,
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@app.get("/panel/sync")
async def get_panel_sync():
    latest = session.last_frame_path
    if latest is None or not latest.is_file():
        frames = sorted(env_setup.FRAMES_DIR.glob("frame-*.jpg")) if env_setup.FRAMES_DIR.exists() else []
        latest = frames[-1] if frames else None
        session.last_frame_path = latest
    latest_name = latest.name if latest else None
    return JSONResponse(
        {
            "ok": True,
            "health": session.get_health_dict(),
            "agy_models": session.agy.models_cache,
            "latest_frame": "/frames/latest.jpg" if latest_name else None,
            "latest_frame_name": latest_name,
            "events": list(session.panel_events),
        },
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@app.get("/frames/latest.jpg")
async def latest_frame_file():
    latest = session.last_frame_path
    if latest is None or not latest.is_file():
        frames = sorted(env_setup.FRAMES_DIR.glob("frame-*.jpg")) if env_setup.FRAMES_DIR.exists() else []
        latest = frames[-1] if frames else None
        session.last_frame_path = latest
    if latest is None:
        raise HTTPException(status_code=404, detail="Кадров пока нет")
    return FileResponse(
        latest,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@app.get("/frames/{filename}")
async def frame_by_name(filename: str):
    safe = Path(filename).name
    if safe != filename or not (safe.startswith("frame-") and safe.endswith(".jpg")):
        raise HTTPException(status_code=400, detail="Некорректное имя кадра")
    fpath = env_setup.FRAMES_DIR / safe
    if not fpath.is_file():
        raise HTTPException(status_code=404, detail="Кадр удалён или не найден")
    return FileResponse(fpath, media_type="image/jpeg")


@app.get("/health")
async def health():
    return session.get_health_dict()


@app.post("/think")
async def post_think(req: ThinkReq):
    txt = req.text.strip()
    if not txt:
        return {"ok": True}
    ev = await session.emit_panel_event("thought", txt, meta=(req.model or "").strip())
    spoken = False
    if session.speak_thoughts and not session.speaker_muted and session.ws is not None:
        try:
            await session.speak(txt, wait=True, emit_event=False)
            spoken = True
        except Exception:
            pass
    return {"ok": True, "event": ev, "spoken": spoken}


@app.post("/panel/send")
async def post_panel_send(req: PanelSendReq):
    text = req.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Пустое сообщение")
    entry = {
        "ts": round(time.time(), 3),
        "iso": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "text": text,
        "source": "panel",
        "duration_s": 0.0,
        "stt_ms": 0,
        "stop_triggered": False,
    }
    env_setup.INBOX_PATH.parent.mkdir(parents=True, exist_ok=True)
    with env_setup.INBOX_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    await session.emit_panel_event("user_text", text)
    if session.auto_voice:
        session._enqueue_voice_entry(entry)
    else:
        session.transcript_event.set()
    return {"ok": True, "entry": entry}


@app.post("/panel/command")
async def post_panel_command(req: PanelCmdReq):
    cmd = req.cmd.strip().lower()
    if cmd == "stop":
        session.autonomous_mode = False
        if session._autonomous_task is not None and not session._autonomous_task.done():
            session._autonomous_task.cancel()
        session._pending_user_msgs.clear()
        if session._agy_task is not None and not session._agy_task.done():
            session._agy_task.cancel()
        asyncio.create_task(session.agy.cancel())
        session.is_moving = False
        session.is_speaking = False
        session._stop_vacuum_bg(exit_manual=False)
        asyncio.create_task(session.send_ws({"type": "stop_audio"}))
        asyncio.create_task(session.send_ui("stopped", "Стоп с пульта!"))
        asyncio.create_task(session.emit_panel_event("action", "Стоп с веб-пульта"))
        asyncio.create_task(session.broadcast_panel_health())
        return {"ok": True, "health": session.get_health_dict()}
    if cmd == "sleep":
        session.voice_muted = True
        if session.agy.busy:
            asyncio.create_task(session.emit_panel_event("action", "Микрофон выключен с пульта (додумываю текущий ответ...)"))
            asyncio.create_task(session.broadcast_panel_health())
            return {
                "ok": True,
                "voice_muted": True,
                "finishing_thought": True,
                "health": session.get_health_dict(),
            }
        if session.is_moving:
            session._stop_vacuum_bg(exit_manual=True)
        session.is_moving = False
        asyncio.create_task(session.sync_rest_ui())
        asyncio.create_task(session.emit_panel_event("action", "Режим сна (микрофон выключен с пульта)"))
        asyncio.create_task(session.broadcast_panel_health())
        return {"ok": True, "voice_muted": True, "health": session.get_health_dict()}
    if cmd == "wake":
        session.voice_muted = False
        asyncio.create_task(session.sync_rest_ui())
        asyncio.create_task(session.emit_panel_event("action", "Слух включён с пульта"))
        asyncio.create_task(session.broadcast_panel_health())
        return {"ok": True, "voice_muted": False, "health": session.get_health_dict()}
    if cmd == "voice_off":
        session.voice_muted = True
        session.speaker_muted = True
        session.is_speaking = False
        for req_id, fut in list(session.pending_tts.items()):
            if not fut.done():
                fut.set_result({"interrupted": True, "muted": True})
        asyncio.create_task(session.send_ws({"type": "stop_audio"}))
        if session.agy.busy:
            asyncio.create_task(session.emit_panel_event("action", "Голосовой режим выключен с пульта (додумываю в тихий чат...)"))
        else:
            if session.is_moving:
                session._stop_vacuum_bg(exit_manual=True)
            session.is_moving = False
            asyncio.create_task(session.emit_panel_event("action", "Голосовой режим выключен с пульта (только текст)"))
        asyncio.create_task(session.sync_rest_ui())
        asyncio.create_task(session.broadcast_panel_health())
        return {
            "ok": True,
            "voice_muted": True,
            "speaker_muted": True,
            "finishing_thought": session.agy.busy,
            "health": session.get_health_dict(),
        }
    if cmd == "voice_on":
        session.speaker_muted = False
        session.voice_muted = False
        asyncio.create_task(session.sync_rest_ui())
        asyncio.create_task(session.emit_panel_event("action", "Голосовой режим полностью включён с пульта"))
        asyncio.create_task(session.broadcast_panel_health())
        return {
            "ok": True,
            "voice_muted": False,
            "speaker_muted": False,
            "health": session.get_health_dict(),
        }
    if cmd == "auto_on":
        await session.start_autonomous()
        await session.emit_panel_event("action", "🧭 Режим «Свободная жизнь» включён — Жужа действует по своему плану")
        return {
            "ok": True,
            "autonomous_mode": True,
            "agy_enabled": session.agy.enabled,
            "health": session.get_health_dict(),
        }
    if cmd == "auto_off":
        was_busy = session.agy.busy
        await session.stop_autonomous(cancel_current=False)
        msg = (
            "🧭 Режим «Свободная жизнь» выключен (доделает текущий шаг и остановится)"
            if was_busy
            else "🧭 Режим «Свободная жизнь» выключен"
        )
        await session.emit_panel_event("action", msg)
        return {
            "ok": True,
            "autonomous_mode": False,
            "finishing_thought": was_busy,
            "health": session.get_health_dict(),
        }
    if cmd == "thoughts_on":
        session.speak_thoughts = True
        _save_voice_prefs(speak_thoughts=True)
        await session.emit_panel_event("action", "💭 Озвучка живых мыслей включена")
        await session.broadcast_panel_health()
        return {"ok": True, "speak_thoughts": True, "health": session.get_health_dict()}
    if cmd == "thoughts_off":
        session.speak_thoughts = False
        _save_voice_prefs(speak_thoughts=False)
        await session.emit_panel_event("action", "💭 Мысли только текстом (без озвучки шагов)")
        await session.broadcast_panel_health()
        return {"ok": True, "speak_thoughts": False, "health": session.get_health_dict()}
    if cmd == "wake_word_on":
        session.wake_word_only = True
        _save_voice_prefs(wake_word_only=True)
        await session.emit_panel_event("action", "🎯 Режим «Только по имени Жужа» включён")
        await session.broadcast_panel_health()
        return {"ok": True, "wake_word_only": True, "health": session.get_health_dict()}
    if cmd == "wake_word_off":
        session.wake_word_only = False
        _save_voice_prefs(wake_word_only=False)
        await session.emit_panel_event("action", "🎯 Свободный слух (без обязательного слова «Жужа»)")
        await session.broadcast_panel_health()
        return {"ok": True, "wake_word_only": False, "health": session.get_health_dict()}
    if cmd == "set_interval":
        val = max(2.0, min(120.0, float(req.value or 5.0)))
        session.autonomous_interval_s = val
        _save_voice_prefs(autonomous_interval_s=val)
        await session.emit_panel_event("action", f"⏱ Пауза «Свободной жизни»: {int(val)} с")
        await session.broadcast_panel_health()
        return {"ok": True, "autonomous_interval_s": val, "health": session.get_health_dict()}
    if cmd == "frame":
        return await session.capture_frame(in_feed=not req.silent)
    if cmd == "status":
        st = await asyncio.to_thread(session.vacuum.status)
        if st.get("battery") is not None:
            session.battery = int(st["battery"])
            await session.check_battery_guard(session.battery)
            await session.broadcast_panel_health()
        await session.emit_panel_event("action", f"Статус: {st.get('status_text')}, АКБ {st.get('battery')}%")
        return {"ok": True, **st, "health": session.get_health_dict()}
    if cmd == "dock":
        session.ignore_vacuum_voice_until = time.monotonic() + 15.0
        res = await asyncio.to_thread(session.vacuum.dock)
        await session.emit_panel_event("action", "На зарядную базу (с пульта)")
        return {"ok": True, **res}
    if cmd == "clean":
        session.ignore_vacuum_voice_until = time.monotonic() + 8.0
        res = await asyncio.to_thread(session.vacuum.start_clean, "auto")
        await session.emit_panel_event("action", "Уборка запущена (с пульта)")
        return {"ok": True, **res}
    if cmd == "stop_clean":
        session.ignore_vacuum_voice_until = time.monotonic() + 5.0
        res = await asyncio.to_thread(session.vacuum.stop_clean)
        await session.emit_panel_event("action", "Уборка остановлена (с пульта)")
        return {"ok": True, **res}
    raise HTTPException(status_code=400, detail=f"Неизвестная команда пульта: {cmd}")


@app.get("/agy/models")
async def get_agy_models():
    return {
        "ok": True,
        "models": session.agy.models_cache,
        "current": session.agy.model,
        "enabled": session.agy.enabled,
    }


@app.post("/agy/config")
async def post_agy_config(req: AgyConfigReq):
    changed: list[str] = []
    if req.enabled is not None:
        session.agy.enabled = req.enabled
        if not req.enabled:
            await session.stop_autonomous(cancel_current=True)
        changed.append("ИИ: ВКЛ" if req.enabled else "ИИ: ВЫКЛ")
    if req.model and req.model.strip():
        session.agy.model = req.model.strip()
        _save_voice_prefs(agy_model=session.agy.model)
        changed.append(f"Модель: {session.agy.model}")
    if req.reset:
        session.agy.reset_conversation()
        changed.append("Новый диалог")
    if changed:
        await session.emit_panel_event("action", " · ".join(changed))
    await session.broadcast_panel_health()
    return {
        "ok": True,
        "enabled": session.agy.enabled,
        "model": session.agy.model,
        "conversation_id": session.agy.conversation_id,
        "health": session.get_health_dict(),
    }


@app.websocket("/ws/panel")
async def ws_panel(ws: WebSocket):
    await ws.accept()
    session.panel_sockets.add(ws)
    try:
        frames = sorted(env_setup.FRAMES_DIR.glob("frame-*.jpg")) if env_setup.FRAMES_DIR.exists() else []
        await ws.send_text(
            json.dumps(
                {
                    "type": "init",
                    "health": session.get_health_dict(),
                    "agy_models": session.agy.models_cache,
                    "latest_frame": "/frames/latest.jpg" if frames else None,
                    "events": list(session.panel_events),
                },
                ensure_ascii=False,
            )
        )
        while True:
            raw_msg = await ws.receive_text()
            if raw_msg == "ping" or '"ping"' in raw_msg:
                await ws.send_text('{"type":"pong"}')
    except WebSocketDisconnect:
        pass
    finally:
        session.panel_sockets.discard(ws)


@app.post("/auto_voice")
async def post_auto_voice(req: AutoVoiceReq):
    session.auto_voice = req.enabled
    await session.broadcast_panel_health()
    return {"ok": True, "auto_voice": session.auto_voice}


@app.websocket("/ws/head")
async def ws_head(ws: WebSocket):
    await ws.accept()
    old_ws = session.ws
    session.ws = ws
    if old_ws is not None and old_ws is not ws:
        try:
            await old_ws.close(code=1000)
        except Exception:
            pass
    try:
        while True:
            msg = await ws.receive()
            if msg.get("bytes") is not None:
                chunk = msg["bytes"]
                session.chunks_received += 1
                session.last_chunk_ts = time.monotonic()
                if session.pcm_queue.full():
                    try:
                        session.pcm_queue.get_nowait()
                        session.pcm_queue.task_done()
                    except asyncio.QueueEmpty:
                        pass
                session.pcm_queue.put_nowait(chunk)
            elif msg.get("text") is not None:
                try:
                    data = json.loads(msg["text"])
                except json.JSONDecodeError:
                    continue
                mtype = data.get("type")
                if mtype == "ready":
                    session.cam_w = int(data.get("cam_w") or 0)
                    session.cam_h = int(data.get("cam_h") or 0)
                elif mtype == "frame":
                    fut = session.pending_frames.get(data.get("id", ""))
                    if fut and not fut.done():
                        fut.set_result(data)
                elif mtype == "tts_done":
                    session.is_speaking = False
                    now_m = time.monotonic()
                    session.last_speaking_end = now_m
                    session.last_spoken_ts = now_m
                    fut = session.pending_tts.get(data.get("id", ""))
                    if fut and not fut.done():
                        fut.set_result(data)
                    await session.sync_rest_ui()
                elif mtype == "emergency_stop":
                    await session.stop_autonomous(cancel_current=True)
                    try:
                        await asyncio.to_thread(session.vacuum.emergency_stop)
                    except Exception:
                        pass
                    session.is_moving = False
                    session.is_speaking = False
                    await session.send_ws({"type": "stop_audio"})
                    await session.send_ui("stopped", "Нажата кнопка СТОП на экране!")
    except WebSocketDisconnect:
        pass
    finally:
        if session.ws is ws:
            session.ws = None


@app.post("/frame")
async def post_frame(req: FrameReq | None = None):
    req = req or FrameReq()
    return await session.capture_frame(width=req.width, quality=req.quality)


@app.post("/say")
async def post_say(req: SayReq):
    if session.agy.busy:
        session._agy_spoke_in_turn = True
    if req.thought and req.thought.strip():
        await session.emit_panel_event("thought", req.thought.strip())
    return await session.speak(req.text, wait=req.wait, engine=req.engine)


@app.post("/stop_audio")
async def post_stop_audio():
    session.is_speaking = False
    session.last_speaking_end = time.monotonic()
    await session.send_ws({"type": "stop_audio"})
    return {"ok": True}


@app.post("/ui")
async def post_ui(req: UiReq):
    await session.send_ui(req.state, req.caption, req.battery)
    return {"ok": True}


@app.get("/inbox")
async def get_inbox(consume: bool = Query(default=False), limit: int = Query(default=20)):
    entries = read_inbox_lines()
    if consume:
        cursor = get_inbox_cursor()
        unread = entries[cursor:]
        set_inbox_cursor(len(entries))
        return {"ok": True, "entries": unread[-limit:], "total": len(entries)}
    return {"ok": True, "entries": entries[-limit:], "total": len(entries)}


@app.post("/listen")
async def post_listen(req: ListenReq | None = None):
    req = req or ListenReq()
    entries = read_inbox_lines()
    cursor = get_inbox_cursor()
    if cursor < len(entries):
        unread = entries[cursor:]
        set_inbox_cursor(len(entries))
        frame = await session.capture_frame(width=req.frame_width) if (req.with_frame and session.ws) else None
        return {"ok": True, "heard": unread, "text": " ".join(e["text"] for e in unread), "frame": frame}

    session.transcript_event.clear()
    session._listen_waiters += 1
    if not session.voice_muted:
        await session.send_ui("listening", "Слушаю вас...")
    try:
        await asyncio.wait_for(session.transcript_event.wait(), timeout=max(0.5, req.timeout))
    except asyncio.TimeoutError:
        pass
    finally:
        session._listen_waiters = max(0, session._listen_waiters - 1)

    entries_after = read_inbox_lines()
    unread = entries_after[cursor:]
    set_inbox_cursor(len(entries_after))
    frame = await session.capture_frame(width=req.frame_width) if (req.with_frame and session.ws) else None
    return {
        "ok": True,
        "heard": unread,
        "text": " ".join(e["text"] for e in unread),
        "voice_muted": session.voice_muted,
        "frame": frame,
    }


@app.post("/vacuum/status")
async def vac_status():
    res = await asyncio.to_thread(session.vacuum.status)
    if res.get("battery") is not None:
        await session.send_ui("idle", battery=int(res["battery"]))
    return {"ok": True, **res}


@app.post("/vacuum/move")
async def vac_move(req: MoveReq):
    session.is_moving = True
    await session.send_ui("moving", f"Еду: {' '.join(req.moves)}")
    try:
        res = await asyncio.to_thread(session.vacuum.move, req.moves, req.gap)
        return {"ok": True, **res}
    finally:
        session.is_moving = False
        await session.sync_rest_ui()


@app.post("/vacuum/stop")
async def vac_stop():
    await session.stop_autonomous(cancel_current=True)
    res = await asyncio.to_thread(session.vacuum.emergency_stop)
    session.is_moving = False
    session.is_speaking = False
    await session.send_ws({"type": "stop_audio"})
    await session.send_ui("stopped", "Стоп!")
    await session.broadcast_panel_health()
    return {"ok": True, **res}


@app.post("/vacuum/exit")
async def vac_exit():
    return {"ok": True, **await asyncio.to_thread(session.vacuum.exit_manual)}


@app.post("/vacuum/dock")
async def vac_dock():
    await session.send_ui("moving", "Возвращаюсь на базу...")
    return {"ok": True, **await asyncio.to_thread(session.vacuum.dock)}


@app.post("/vacuum/find")
async def vac_find():
    return {"ok": True, **await asyncio.to_thread(session.vacuum.find)}


@app.post("/vacuum/cancel-return")
async def vac_cancel_return():
    return {"ok": True, **await asyncio.to_thread(session.vacuum.cancel_return)}


@app.post("/step")
async def post_step(req: StepReq):
    if session.agy.busy and req.say:
        session._agy_spoke_in_turn = True
    thought_txt = (req.thought or "").strip()
    if thought_txt:
        await session.emit_panel_event("thought", thought_txt)
    settle_s = float(env_setup.load_config().get("vacuum", {}).get("settle_seconds") or 0.5)

    async def _do_move():
        if not req.moves:
            return None
        session.is_moving = True
        await session.emit_panel_event("action", f"🚗 Ход: {' '.join(req.moves)}")
        await session.send_ui("moving", req.say or thought_txt or f"Еду: {' '.join(req.moves)}")
        try:
            return await asyncio.to_thread(session.vacuum.move, req.moves, req.gap)
        finally:
            session.is_moving = False

    async def _do_say():
        try:
            if req.say:
                return await session.speak(req.say, wait=True, emit_event=True)
            if thought_txt and session.speak_thoughts and not session.speaker_muted and session.ws is not None:
                # Speak thought asynchronously while frame capture proceeds immediately after move!
                return await session.speak(thought_txt, wait=False, emit_event=False)
            return None
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    move_res, say_res = await asyncio.gather(_do_move(), _do_say())
    if move_res is not None:
        await asyncio.sleep(settle_s)

    frame_res = None
    if not req.no_frame and session.ws is not None:
        frame_res = await session.capture_frame(width=req.frame_width)

    await session.sync_rest_ui()
    # Deliver all clean unmuted human speech and web panel messages queued since the previous step
    heard = list(session._pending_user_msgs)
    session._pending_user_msgs.clear()
    set_inbox_cursor(len(read_inbox_lines()))
    if heard:
        heard_preview = "; ".join(m["text"] for m in heard if m.get("text"))[:140]
        await session.emit_panel_event("action", f"👂 Передано Жуже на шаге: «{heard_preview}»")
        await session.broadcast_panel_health()

    return {
        "ok": True,
        "move": move_res,
        "say": say_res,
        "frame": frame_res,
        "heard": heard,
        "stop_triggered": any(e.get("stop_triggered") for e in heard),
        "voice_muted": session.voice_muted,
        "speaker_muted": session.speaker_muted,
        "speak_thoughts": session.speak_thoughts,
        "autonomous_mode": session.autonomous_mode,
    }


def main() -> None:
    hcfg = env_setup.load_config().get("head", {})
    host = hcfg.get("bridge_host") or "127.0.0.1"
    port = int(hcfg.get("bridge_port") or 8765)
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
