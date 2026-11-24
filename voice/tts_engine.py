"""Russian calm female TTS engine: Edge-TTS primary (SvetlanaNeural) + offline Silero v5 fallback."""

from __future__ import annotations

import argparse
import asyncio
import gc
import io
import os
import re
import sys
import threading
import time
import wave
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import env_setup

env_setup.configure_env()

import edge_tts
import numpy as np
import requests


_LATIN_TO_CYR = {
    "a": "а", "b": "б", "c": "ц", "d": "д", "e": "е", "f": "ф", "g": "г",
    "h": "х", "i": "и", "j": "дж", "k": "к", "l": "л", "m": "м", "n": "н",
    "o": "о", "p": "п", "q": "к", "r": "р", "s": "с", "t": "т", "u": "у",
    "v": "в", "w": "в", "x": "кс", "y": "и", "z": "з",
}

_UNITS = ("ноль", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять")
_TEENS = (
    "десять", "одиннадцать", "двенадцать", "тринадцать", "четырнадцать",
    "пятнадцать", "шестнадцать", "семнадцать", "восемнадцать", "девятнадцать",
)
_TENS = ("", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят", "семьдесят", "восемьдесят", "девяносто")
_HUNDREDS = (
    "", "сто", "двести", "триста", "четыреста",
    "пятьсот", "шестьсот", "семьсот", "восемьсот", "девятьсот",
)


def _int_to_ru(n: int) -> str:
    if n < 0:
        return "минус " + _int_to_ru(-n)
    if n < 10:
        return _UNITS[n]
    if n < 20:
        return _TEENS[n - 10]
    if n < 100:
        t, u = divmod(n, 10)
        return _TENS[t] + (f" {_UNITS[u]}" if u else "")
    if n < 1000:
        h, rem = divmod(n, 100)
        return _HUNDREDS[h] + (f" {_int_to_ru(rem)}" if rem else "")
    if n < 1_000_000:
        th, rem = divmod(n, 1000)
        rem_str = f" {_int_to_ru(rem)}" if rem else ""
        if th % 10 == 1 and th % 100 != 11:
            unit = "тысяча"
        elif 2 <= th % 10 <= 4 and not (12 <= th % 100 <= 14):
            unit = "тысячи"
        else:
            unit = "тысяч"
        th_words = _int_to_ru(th).replace("один", "одна").replace("два", "две") if th % 10 in (1, 2) and th % 100 not in (11, 12) else _int_to_ru(th)
        return f"{th_words} {unit}{rem_str}"
    return " ".join(_UNITS[int(ch)] for ch in str(n))


def sanitize_for_silero(text: str) -> str:
    """Convert numbers, %, and ASCII words into safe Cyrillic text for Silero TTS."""
    s = text.strip()
    s = s.replace("%", " процентов ").replace("+", " плюс ").replace("°", " градусов ")

    def repl_num(match: re.Match[str]) -> str:
        raw = match.group(0)
        if "." in raw or "," in raw:
            parts = re.split(r"[.,]", raw)
            return " точка ".join(_int_to_ru(int(p)) for p in parts if p)
        return _int_to_ru(int(raw))

    s = re.sub(r"\d+(?:[.,]\d+)?", repl_num, s)
    s = re.sub(
        r"[A-Za-z]+",
        lambda m: "".join(_LATIN_TO_CYR.get(ch.lower(), "") for ch in m.group(0)),
        s,
    )
    s = re.sub(r"[^А-Яа-яЁё0-9\s.,!?;:\-—]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or "Готово."


class TTSEngine:
    """Thread-safe async/sync Russian TTS engine with Edge-TTS and Silero v5 fallback."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._cfg_override = config
        self._silero_model: Any = None
        self._silero_speaker: str | None = None
        self._silero_sample_rate: int = 48000
        self._silero_lock = threading.Lock()
        self._edge_cooldown_until: float = 0.0
        self._audio_cache: dict[tuple[str, str], tuple[bytes, str, str]] = {}

    def _voice_cfg(self) -> dict[str, Any]:
        cfg = self._cfg_override if self._cfg_override is not None else env_setup.load_config()
        return cfg.get("voice", {})

    def warmup_silero_sync(self) -> None:
        try:
            self._load_silero_sync()
            # Trigger one dummy pass so PyTorch JIT compiles kernels before live conversation.
            self.synthesize_silero_sync("Готово.")
        except Exception:
            pass

    async def synthesize_edge(self, text: str) -> tuple[bytes, str]:
        vcfg = self._voice_cfg()
        voice = vcfg.get("edge_voice") or "ru-RU-SvetlanaNeural"
        rate = vcfg.get("edge_rate") or "-6%"
        pitch = vcfg.get("edge_pitch") or "+0Hz"

        comm = edge_tts.Communicate(text=text, voice=voice, rate=rate, pitch=pitch)
        chunks: list[bytes] = []

        async def _collect() -> None:
            async for item in comm.stream():
                if item.get("type") == "audio":
                    chunks.append(item["data"])

        await asyncio.wait_for(_collect(), timeout=2.8)
        audio = b"".join(chunks)
        if not audio:
            raise RuntimeError("Edge-TTS вернул пустое аудио")
        return audio, "audio/mpeg"

    def _load_silero_sync(self) -> None:
        if self._silero_model is not None:
            return
        env_setup.configure_env()
        import torch

        vcfg = self._voice_cfg()
        model_id = vcfg.get("silero_model_id") or "v5_ru"
        pref_speaker = vcfg.get("silero_speaker") or "kseniya"
        self._silero_sample_rate = int(vcfg.get("silero_sample_rate") or 48000)

        silero_dir = env_setup.CACHE_DIR / "torch" / "silero"
        silero_dir.mkdir(parents=True, exist_ok=True)

        sources = [
            ("v5_ru.pt", "https://models.silero.ai/models/tts/ru/v5_ru.pt"),
            ("v4_ru.pt", "https://huggingface.co/Derur/silero-models/resolve/main/tts/ru/ru_v4/v4_ru.pt"),
        ]
        if model_id == "v4_ru":
            sources = [sources[1], sources[0]]

        def _load_pt(pt_path: Path):
            imp = torch.package.PackageImporter(io.BytesIO(pt_path.read_bytes()))
            return imp.load_pickle("tts_models", "model")

        model = None
        last_err: Exception | None = None

        # 1. Check already cached local checkpoints first (offline-safe).
        for filename, _ in sources:
            local_pt = silero_dir / filename
            if local_pt.is_file() and local_pt.stat().st_size > 1_000_000:
                try:
                    model = _load_pt(local_pt)
                    break
                except Exception as exc:
                    last_err = exc

        # 2. Otherwise download sequentially into .cache/torch/silero.
        if model is None:
            for filename, url in sources:
                local_pt = silero_dir / filename
                part_pt = silero_dir / f"{filename}.part"
                try:
                    with requests.get(url, stream=True, timeout=(5, 30)) as resp:
                        resp.raise_for_status()
                        with part_pt.open("wb") as fh:
                            for chunk in resp.iter_content(chunk_size=1024 * 256):
                                if chunk:
                                    fh.write(chunk)
                    os.replace(part_pt, local_pt)
                    model = _load_pt(local_pt)
                    break
                except Exception as exc:
                    last_err = exc
                    try:
                        if part_pt.exists():
                            part_pt.unlink()
                    except OSError:
                        pass

        if model is None:
            raise RuntimeError(f"Не удалось загрузить Silero TTS: {last_err}") from last_err

        model.to(torch.device("cpu"))
        try:
            if torch.get_num_threads() > 2:
                torch.set_num_threads(2)
        except Exception:
            pass
        speakers = list(getattr(model, "speakers", []) or [])
        chosen = None
        for cand in (pref_speaker, "kseniya", "xenia", "baya"):
            if cand in speakers:
                chosen = cand
                break
        if chosen is None:
            chosen = speakers[0] if speakers else "xenia"

        self._silero_model = model
        self._silero_speaker = chosen

    @staticmethod
    def _split_chunks(clean: str, max_chars: int = 120) -> list[str]:
        parts = [p.strip() for p in re.split(r"(?<=[.!?;:])\s+", clean) if p.strip()]
        chunks: list[str] = []
        for part in parts:
            if len(part) <= max_chars:
                chunks.append(part)
                continue
            words = part.split()
            cur = ""
            for w in words:
                cand = f"{cur} {w}".strip() if cur else w
                if len(cand) <= max_chars:
                    cur = cand
                else:
                    if cur:
                        chunks.append(cur)
                    cur = w
            if cur:
                chunks.append(cur)
        return chunks or [clean[:max_chars]]

    def synthesize_silero_sync(self, text: str) -> tuple[bytes, str]:
        import torch

        with self._silero_lock:
            self._load_silero_sync()
            clean = sanitize_for_silero(text)
            chunks = self._split_chunks(clean, max_chars=120)
            target_sr = self._silero_sample_rate
            pcm_parts: list[np.ndarray] = []

            def _run_chunks(sr: int) -> list[np.ndarray]:
                out_parts: list[np.ndarray] = []
                gap = np.zeros(int(sr * 0.08), dtype=np.int16)
                with torch.inference_mode():
                    for idx, ch in enumerate(chunks):
                        audio_tensor = self._silero_model.apply_tts(
                            text=ch,
                            speaker=self._silero_speaker,
                            sample_rate=sr,
                            put_accent=True,
                            put_yo=True,
                        )
                        pcm_f32 = audio_tensor.detach().cpu().numpy()
                        del audio_tensor
                        pcm_i16 = np.clip(pcm_f32 * 32767.0, -32768.0, 32767.0).astype(np.int16)
                        out_parts.append(pcm_i16)
                        if idx + 1 < len(chunks):
                            out_parts.append(gap)
                return out_parts

            try:
                pcm_parts = _run_chunks(target_sr)
            except Exception as exc:
                gc.collect()
                if target_sr != 24000:
                    # 24 kHz skips Silero PQMF conv1d upsampler, dramatically lowering CPU RAM footprint
                    target_sr = 24000
                    pcm_parts = _run_chunks(target_sr)
                else:
                    raise exc

            pcm_all = np.concatenate(pcm_parts) if len(pcm_parts) > 1 else pcm_parts[0]
            buf = io.BytesIO()
            with wave.open(buf, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(target_sr)
                wf.writeframes(pcm_all.tobytes())
            return buf.getvalue(), "audio/wav"

    async def synthesize_silero(self, text: str) -> tuple[bytes, str]:
        return await asyncio.to_thread(self.synthesize_silero_sync, text)

    async def synthesize(self, text: str, engine: str | None = None) -> tuple[bytes, str, str]:
        """Synthesize text -> (audio_bytes, mime_type, used_engine)."""
        vcfg = self._voice_cfg()
        choice = (engine or vcfg.get("tts_engine") or "edge").lower()
        key = (choice, text.strip())
        if len(text) <= 80 and key in self._audio_cache:
            return self._audio_cache[key]

        if choice == "silero" or (engine is None and time.monotonic() < self._edge_cooldown_until):
            try:
                data, mime = await self.synthesize_silero(text)
                res = (data, mime, f"silero:{self._silero_speaker}")
            except Exception:
                data, mime = await self.synthesize_edge(text)
                res = (data, mime, f"edge:{vcfg.get('edge_voice') or 'ru-RU-SvetlanaNeural'}")
        else:
            try:
                data, mime = await self.synthesize_edge(text)
                res = (data, mime, f"edge:{vcfg.get('edge_voice') or 'ru-RU-SvetlanaNeural'}")
            except Exception:
                self._edge_cooldown_until = time.monotonic() + 60.0
                data, mime = await self.synthesize_silero(text)
                res = (data, mime, f"silero:{self._silero_speaker}")

        if len(text) <= 80:
            if len(self._audio_cache) >= 64:
                self._audio_cache.clear()
            self._audio_cache[key] = res
        return res


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Тест синтеза речи (SvetlanaNeural + Silero v5)")
    parser.add_argument("text", help="Текст для озвучивания")
    parser.add_argument("--engine", choices=["edge", "silero"], default=None)
    parser.add_argument("--out", type=Path, default=None, help="Путь для сохранения аудиофайла")
    args = parser.parse_args(argv)

    tts = TTSEngine()
    audio, mime, used = asyncio.run(tts.synthesize(args.text, engine=args.engine))

    out_path = args.out
    if out_path is None:
        ext = ".mp3" if mime == "audio/mpeg" else ".wav"
        out_path = env_setup.CACHE_DIR / "tmp" / f"tts_test{ext}"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(audio)

    print(
        f'{{"ok": true, "engine": "{used}", "mime": "{mime}", "bytes": {len(audio)}, "out": "{out_path}"}}'
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
