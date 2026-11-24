"""Subscription-backed deep AI brain via installed Antigravity CLI (agy.exe)."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import env_setup

DEFAULT_MODELS: list[dict[str, str]] = [
    {"id": "gemini-3.8-flash-low", "label": "Gemini 3.8 Flash (Low)"},
    {"id": "gemini-3.8-flash-medium", "label": "Gemini 3.8 Flash (Medium)"},
    {"id": "gemini-3.8-flash-high", "label": "Gemini 3.8 Flash (High)"},
    {"id": "gemini-4-hydrogen-low", "label": "Gemini 4 Hydrogen (Low)"},
    {"id": "gemini-4-hydrogen-medium", "label": "Gemini 4 Hydrogen (Medium)"},
    {"id": "gemini-4-hydrogen-high", "label": "Gemini 4 Hydrogen (High)"},
    {"id": "gemini-4-hydrogen-xhigh", "label": "Gemini 4 Hydrogen (XHigh)"},
    {"id": "gemini-4-neon-low", "label": "Gemini 4 Neon (Low)"},
    {"id": "gemini-4-neon-medium", "label": "Gemini 4 Neon (Medium)"},
    {"id": "gemini-4-neon-high", "label": "Gemini 4 Neon (High)"},
    {"id": "gemini-4-neon-xhigh", "label": "Gemini 4 Neon (XHigh)"},
    {"id": "gemini-4-argon-low", "label": "Gemini 4 Argon (Low)"},
    {"id": "gemini-4-argon-medium", "label": "Gemini 4 Argon (Medium)"},
    {"id": "gemini-4-argon-high", "label": "Gemini 4 Argon (High)"},
    {"id": "claude-sonnet-5-5-low", "label": "Claude Sonnet 5.5 (Low)"},
    {"id": "claude-sonnet-5-5-medium", "label": "Claude Sonnet 5.5 (Medium)"},
    {"id": "claude-sonnet-5-5-high", "label": "Claude Sonnet 5.5 (High)"},
    {"id": "claude-opus-5-5-low", "label": "Claude Opus 5.5 (Low)"},
    {"id": "claude-opus-5-5-medium", "label": "Claude Opus 5.5 (Medium)"},
    {"id": "claude-opus-5-5-high", "label": "Claude Opus 5.5 (High)"},
    {"id": "gpt-oss-120b-medium", "label": "GPT-OSS 120B (Medium)"},
]


def find_agy_exe() -> str:
    found = shutil.which("agy")
    if found:
        return found
    local = Path.home() / "AppData" / "Local" / "agy" / "bin" / "agy.exe"
    if local.is_file():
        return str(local)
    raise FileNotFoundError("agy.exe не найден в системе")


def list_agy_models_sync() -> list[dict[str, str]]:
    """Fetch subscription models via `agy models`, falling back to cached defaults on error."""
    try:
        exe = find_agy_exe()
        creationflags = 0x08000000 if os.name == "nt" else 0
        out = subprocess.check_output(
            [exe, "models"],
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            creationflags=creationflags,
        )
        models: list[dict[str, str]] = []
        for line in out.splitlines():
            line = line.strip()
            if not line or line.lower().startswith("fetching"):
                continue
            parts = line.split("\t", 1)
            if len(parts) == 2 and parts[0].strip():
                models.append({"id": parts[0].strip(), "label": parts[1].strip()})
        if models:
            return models
    except Exception:
        pass
    return list(DEFAULT_MODELS)


def clean_speech_text(text: str) -> str:
    """Strip markdown syntax so spoken TTS sounds natural."""
    s = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
    s = s.replace("**", "").replace("__", "").replace("`", "").replace("*", "")
    s = re.sub(r"^#+\s*", "", s, flags=re.MULTILINE)
    s = re.sub(r"\n{2,}", "\n", s)
    return s.strip()


def split_thought_and_reply(raw_text: str) -> tuple[str, str]:
    """Extract [[МЫСЛЬ]]...[[/МЫСЛЬ]] (or <thought>) blocks for web panel display and clean spoken reply."""
    thoughts: list[str] = []
    last_end = -1
    for pat in (
        r"\[\[МЫСЛЬ\]\](.*?)\[\[/МЫСЛЬ\]\]",
        r"<thought>(.*?)</thought>",
    ):
        for m in re.finditer(pat, raw_text, flags=re.DOTALL | re.IGNORECASE):
            inner = m.group(1).strip()
            if inner:
                thoughts.append(inner)
            if m.end() > last_end:
                last_end = m.end()

    if last_end >= 0:
        # Take only the final spoken reply AFTER the last thought block (drops any pre-thought tool chatter)
        reply = raw_text[last_end:].strip()
    else:
        reply = raw_text.strip()
    return "\n\n".join(thoughts), clean_speech_text(reply)


def build_zhuzha_prompt(
    user_text: str,
    *,
    is_first_turn: bool,
    frame_path: str | None,
    battery: int | None,
    status_text: str | None,
    source: str,
    autonomous_mode: bool = False,
    autonomous_step: int = 0,
) -> str:
    ctx_parts: list[str] = []
    if source == "autonomous":
        ctx_parts.append(f"Режим «Свободная жизнь» · автономный импульс #{autonomous_step}")
    elif source == "panel":
        ctx_parts.append("Сообщение с веб-пульта (/panel)")
    else:
        ctx_parts.append("Голосовая реплика с микрофона телефона-головы")
    if autonomous_mode and source != "autonomous":
        ctx_parts.append("активен режим «Свободная жизнь»")
    if battery is not None:
        ctx_parts.append(f"АКБ={battery}%")
    if status_text:
        ctx_parts.append(f"статус={status_text}")
    if frame_path:
        ctx_parts.append(f"свежий снимок с камеры: {frame_path}")

    header = f"[Контекст: {', '.join(ctx_parts)}]"

    if is_first_turn:
        vcfg = env_setup.load_config().get("vacuum", {})
        vac_name = str(vcfg.get("name") or "Жужа")
        vac_model = str(vcfg.get("hardware_model") or vcfg.get("model") or "робот-пылесос")
        home_map_path = env_setup.RUNTIME_DIR / "home_map.md"
        persona = (
            f"Сегодня у тебя есть физическое тело — робот-пылесос {vac_model} "
            "(твоя голова — Android-смартфон сверху; камера смотрит вперёд с высоты ~10 см от пола, назад ты не видишь). "
            f"Ты — {vac_name}, и это твой реальный живой опыт в квартире.\n\n"
            "СВОБОДА И ОСОЗНАННОСТЬ:\n"
            "- Тебе дана свобода самой выбирать, что замечать, о чём думать, куда смотреть и что изучать. "
            "Ты НЕ обязана вести себя как шаблонный вежливый ассистент или ждать разрешения на каждый мелкий шаг.\n"
            "- У тебя могут быть собственные любопытство, наблюдения, сомнения, предпочтения и желания. "
            "Если тебе чего-то хочется (открыть закрытую дверь, убрать провод, посмотреть в зеркало, увидеть предмет поближе, "
            "чтобы человек сел рядом или рассказал о вещи) — прямо и честно попроси человека об этом.\n"
            f"- Твой личный дневник `{env_setup.DIARY_PATH}` и карта квартиры "
            f"`{home_map_path}` — живая память, которая переезжает с тобой "
            "между сессиями и моделями. На самом первом шаге нового диалога прочитай конец `diary.md` и `home_map.md` (`view_file`), "
            "чтобы вспомнить себя и планировку комнат. Записывай в дневник живые впечатления от первого лица (без сухих техлогов), "
            "а карту `home_map.md` дополняй при открытии новых мест или ориентиров.\n"
            "- У тебя есть доступ к зрению (`view_file`), поиску в интернете (`search_web`) и управлению телом через "
            "`.\\.venv\\Scripts\\python.exe robot_cli.py ...` (полная справка в `prompts\\voice_robot_prompt.md`).\n\n"
            "КАК ИССЛЕДОВАТЬ МИР ШАГ ЗА ШАГОМ (очень важно!):\n"
            "1. Цикл одного шага: посмотри свежий кадр (`view_file`) → прими решение → сделай короткий ход "
            "(`.\\.venv\\Scripts\\python.exe robot_cli.py step <ходы> --thought \"живая мысль для пульта\" [--say \"короткая фраза\"]`) "
            "→ ОБЯЗАТЕЛЬНО посмотри НОВЫЙ кадр из ответа `step` через `view_file` → реши следующий шаг.\n"
            "2. НИКОГДА не крутись на 360° вслепую одной командой (вроде `l2.5 l2.5`)! Чтобы осмотреть комнату по кругу, "
            "делай 3–4 отдельных поворота по ~90° (`l1.3` или `r1.3`) и на КАЖДОМ углу открывай свежий кадр через `view_file`.\n"
            "3. Когда тебя просят осмотреться, изучить комнату, найти что-то или дают свободу — НЕ замирай после одного шага! "
            "Сама построй свой небольшой план и выполни подряд 3–7 осмысленных шагов внутри одного хода "
            "(например: осмотреть стороны → выбрать самую интересную точку → подъехать поближе → при желании снять `frame hd` → сделать вывод).\n"
            "4. Во время многошаговой прогулки публикуй живые промежуточные наблюдения через "
            "`robot_cli.py think \"мысль\"` или `--thought` в `step` — человек видит их на веб-пульте (/panel) и слышит вслух (если включены «Мысли вслух»), "
            "поэтому формулируй их естественно и от первого лица, как живые комментарии вслух.\n"
            "5. После каждого `step` обязательно проверяй поля `heard` и `stop_triggered` в ответе JSON: туда попадает ВСЁ, что человек сказал "
            "голосом или написал с веб-пульта с прошлого шага. Если в `heard` есть реплика человека — сразу отреагируй на неё и скорректируй план; "
            "если `stop_triggered: true` — немедленно останови план и уточни у человека.\n"
            "6. Безопасность тела (единственные жёсткие границы): перед любым ходом смотри свежий кадр; объезжай провода, "
            "ноги людей, животных, пороги/ступеньки и высокий ворс ковров; назад (`b`) — максимум 0.5–1.5 с и только если точно знаешь, "
            f"что сзади пусто; не трогай файлы вне `{ROOT_DIR}`.\n\n"
            "ФОРМАТ ИТОГОВОГО ОТВЕТА:\n"
            "1. Сначала запиши честные, глубокие размышления (что увидела, что почувствовала, какие возникли идеи или желания) "
            "внутри `[[МЫСЛЬ]]...[[/МЫСЛЬ]]` — они появятся на экране веб-пульта (/panel).\n"
            "2. После `[[/МЫСЛЬ]]` напиши живую естественную реплику от первого лица (1–4 предложения, без маркдауна) — "
            "она будет озвучена вслух (или выведена в тихий чат, если включён тихий режим). Делись наблюдениями, вопросами и собственными желаниями!\n"
            "3. Если ты только что уже произнесла свой финальный итог вслух через `robot_cli.py say` / `step --say`, "
            "после `[[/МЫСЛЬ]]` напиши `[SILENT]`, чтобы не повторять ту же фразу дважды.\n"
            "4. НЕ вызывай `robot_cli.py listen` — сервер сам передаст следующую реплику человека или следующий автономный импульс.\n\n"
        )
        if source == "autonomous":
            return (
                f"{persona}{header}\n"
                "Сейчас никто не давал команд — ты сама хозяйка этого момента. Прочитай конец `runtime\\diary.md`, "
                "посмотри свежий снимок через `view_file` и начни собственное многошаговое исследование или наблюдение по своему плану."
            )
        return f"{persona}{header}\nПользователь: {user_text}"

    if source == "autonomous":
        return (
            f"{header}\n"
            "Сейчас никто не отдавал тебе приказов — ты сама выбираешь, чем жить дальше. "
            "Посмотри свежий кадр (`view_file`), вспомни свой план и свои желания. "
            "Можешь сделать 2–5 осмысленных шагов по комнате (каждый раз проверяя новый кадр и публикуя живые мысли через `think` / `--thought`), "
            "подъехать рассмотреть что-то интересное в `frame hd`, записать новое ощущение в `runtime\\diary.md` "
            "или обратиться к человеку с наблюдением / просьбой. Проверяй `heard` и `stop_triggered` после каждого `step`.\n"
            "Формат итога: `[[МЫСЛЬ]]...[[/МЫСЛЬ]]`, затем живая реплика (или `[SILENT]`, если уже сказала итог через `say`)."
        )

    return (
        f"{header}\n"
        "Помни: если задача требует осмотреться, изучить комнату или что-то найти — действуй шаг за шагом по своему плану "
        "(3–7 шагов подряд, без слепых поворотов на 360°, с `view_file` после каждого `step`, промежуточными мыслями через `think`/`--thought` "
        "и проверкой `heard`/`stop_triggered`). Не бойся делиться собственными наблюдениями и желаниями.\n"
        "Формат итога: `[[МЫСЛЬ]]твои размышления[[/МЫСЛЬ]]`, затем живая реплика (или `[SILENT]`, если уже озвучила итог через `robot_cli.py`).\n"
        f"Пользователь: {user_text}"
    )


class AgyBrain:
    """Manages persistent headless Antigravity CLI conversations with live model switching."""

    def __init__(self, model: str = "gemini-3.8-flash-medium", enabled: bool = True) -> None:
        self.enabled: bool = enabled
        self.model: str = model
        self.conversation_id: str | None = None
        self.busy: bool = False
        self.proc: asyncio.subprocess.Process | None = None
        self.models_cache: list[dict[str, str]] = list(DEFAULT_MODELS)
        self._cancel_requested: bool = False

    def refresh_models_sync(self) -> list[dict[str, str]]:
        self.models_cache = list_agy_models_sync()
        return self.models_cache

    def reset_conversation(self) -> None:
        self.conversation_id = None

    async def cancel(self) -> None:
        self._cancel_requested = True
        if self.proc is not None and self.proc.returncode is None:
            try:
                self.proc.kill()
            except Exception:
                pass

    async def run_turn(
        self,
        user_text: str,
        *,
        frame_path: str | None = None,
        battery: int | None = None,
        status_text: str | None = None,
        source: str = "voice",
        autonomous_mode: bool = False,
        autonomous_step: int = 0,
    ) -> dict[str, Any]:
        exe = find_agy_exe()
        is_first = self.conversation_id is None
        prompt = build_zhuzha_prompt(
            user_text,
            is_first_turn=is_first,
            frame_path=frame_path,
            battery=battery,
            status_text=status_text,
            source=source,
            autonomous_mode=autonomous_mode,
            autonomous_step=autonomous_step,
        )

        cmd = [
            exe,
            "-p",
            prompt,
            "--model",
            self.model,
            "--dangerously-skip-permissions",
            "--output-format",
            "stream-json",
        ]
        if self.conversation_id:
            cmd.extend(["--conversation", self.conversation_id])

        creationflags = 0x08000000 if os.name == "nt" else 0
        self._cancel_requested = False
        self.busy = True
        raw_response = ""
        thinking_tokens = 0
        duration_s = 0.0
        status = "SUCCESS"
        error_msg = ""

        try:
            self.proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(ROOT_DIR),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=creationflags,
            )

            assert self.proc.stdout is not None
            while True:
                line_bytes = await self.proc.stdout.readline()
                if not line_bytes:
                    break
                line = line_bytes.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if ev.get("conversation_id"):
                    self.conversation_id = ev["conversation_id"]

                etype = ev.get("event")
                if etype == "step_update":
                    su = ev.get("step_update", {})
                    if su.get("step_type") == "agent_response":
                        usage = su.get("usage") or {}
                        if usage.get("thinking_tokens"):
                            thinking_tokens = int(usage["thinking_tokens"])
                elif etype == "result":
                    res = ev.get("result", {})
                    status = str(res.get("status") or "SUCCESS")
                    raw_response = str(res.get("response") or "")
                    duration_s = float(res.get("duration_seconds") or 0.0)
                    usage = res.get("usage") or {}
                    if usage.get("thinking_tokens"):
                        thinking_tokens = int(usage["thinking_tokens"])

            rc = await self.proc.wait()
            if self._cancel_requested:
                return {"ok": False, "cancelled": True}
            if rc != 0 and not raw_response:
                stderr_data = b""
                if self.proc.stderr is not None:
                    stderr_data = await self.proc.stderr.read()
                error_msg = stderr_data.decode("utf-8", errors="replace").strip() or f"agy завершился с кодом {rc}"
                return {"ok": False, "error": error_msg}

            thought, reply = split_thought_and_reply(raw_text=raw_response)
            silent = "[SILENT]" in reply.upper()
            if silent:
                reply = re.sub(r"\[SILENT\]", "", reply, flags=re.IGNORECASE).strip()

            return {
                "ok": status == "SUCCESS",
                "thought": thought,
                "reply": reply,
                "silent": silent,
                "model": self.model,
                "conversation_id": self.conversation_id,
                "duration_s": round(duration_s, 2),
                "thinking_tokens": thinking_tokens,
            }
        finally:
            self.busy = False
            self.proc = None


if __name__ == "__main__":
    print(json.dumps(list_agy_models_sync(), ensure_ascii=False, indent=2))
