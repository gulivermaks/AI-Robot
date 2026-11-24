"""Streaming Silero VAD + faster-whisper (CUDA / CPU) + instant STOP reflex."""

from __future__ import annotations

import argparse
import asyncio
import collections
from datetime import datetime, timezone
import io
import json
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import env_setup

env_setup.configure_env()

import av
import numpy as np
import torch
from faster_whisper import WhisperModel
from silero_vad import load_silero_vad

SAMPLE_RATE = 16000
VAD_FRAME_SAMPLES = 512  # 32 ms at 16 kHz

HALLUCINATION_PHRASES = {
    "субтитры",
    "субтитры сделал",
    "субтитры делал",
    "продолжение следует",
    "подписывайтесь на канал",
    "спасибо за просмотр",
    "редактор субтитров",
    "корректор",
    "переводчик",
    "музыка",
    "аплодисменты",
    "смех",
    "тишина",
}


def decode_audio_to_pcm16k(audio_bytes: bytes) -> np.ndarray:
    """Decode MP3/WAV/OGG bytes into 16 kHz mono float32 numpy array via PyAV (no ffmpeg CLI needed)."""
    resampler = av.AudioResampler(format="flt", layout="mono", rate=SAMPLE_RATE)
    chunks: list[np.ndarray] = []
    with av.open(io.BytesIO(audio_bytes)) as container:
        stream = next((s for s in container.streams if s.type == "audio"), None)
        if stream is None:
            raise RuntimeError("В аудиофайле не найден аудиопоток")
        for frame in container.decode(stream):
            for out_frame in resampler.resample(frame):
                arr = out_frame.to_ndarray()
                chunks.append(arr.reshape(-1))
        for out_frame in resampler.resample(None):
            arr = out_frame.to_ndarray()
            chunks.append(arr.reshape(-1))
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32, copy=False)


def normalize_words(text: str) -> list[str]:
    return re.findall(r"[а-яёa-z0-9]+", text.lower().replace("ё", "е"))


def is_hallucination(text: str) -> bool:
    cleaned = " ".join(normalize_words(text))
    if not cleaned:
        return True
    if cleaned in HALLUCINATION_PHRASES:
        return True
    for bad in ("субтитр", "dimatorzok", "продолжение следует", "подписывайтесь", "спасибо за просмотр"):
        if bad in cleaned and len(cleaned) < 70:
            return True
    return False


class STTEngine:
    """Thread-safe faster-whisper + Silero VAD engine targeting CUDA / CPU."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._cfg_override = config
        vcfg = self._voice_cfg()

        self.language = vcfg.get("stt_language") or "ru"
        self.stop_words = {
            w.lower().replace("ё", "е")
            for w in (vcfg.get("stop_words") or ["стоп", "стой", "замри", "остановись", "тормози"])
        }
        self.initial_prompt = str(
            vcfg.get("stt_initial_prompt")
            or "Жужа, повернись направо, повернись налево, развернись на 90 градусов, проедь прямо, полметра, один метр, на базу, док-станция, на зарядку, осмотрись, стоп, начни уборку, останови уборку."
        )

        self.vad_model = load_silero_vad(onnx=True)
        self._transcribe_lock = threading.Lock()

        model_id = vcfg.get("stt_model") or "turbo"
        pref_device = vcfg.get("stt_device") or "cuda"
        pref_compute = vcfg.get("stt_compute_type") or "int8_float16"
        download_root = env_setup.to_short_path(env_setup.CACHE_DIR / "whisper")

        attempts: list[tuple[str, str]] = []
        if pref_device == "cuda":
            attempts.append(("cuda", pref_compute))
            if pref_compute != "int8":
                attempts.append(("cuda", "int8"))
        attempts.append(("cpu", "int8"))

        last_err: Exception | None = None
        self.whisper: WhisperModel | None = None
        self.device = "cpu"
        self.compute_type = "int8"

        for dev, ctype in attempts:
            try:
                self.whisper = WhisperModel(
                    model_id,
                    device=dev,
                    compute_type=ctype,
                    download_root=download_root,
                )
                self.device = dev
                self.compute_type = ctype
                break
            except Exception as exc:
                last_err = exc

        if self.whisper is None:
            raise RuntimeError(f"Не удалось загрузить faster-whisper ({model_id}): {last_err}") from last_err

    def _voice_cfg(self) -> dict[str, Any]:
        cfg = self._cfg_override if self._cfg_override is not None else env_setup.load_config()
        return cfg.get("voice", {})

    def is_stop_command(self, text: str) -> bool:
        words = normalize_words(text)
        return any(w in self.stop_words for w in words)

    def vad_prob(self, frame_f32: np.ndarray) -> float:
        tensor = torch.from_numpy(np.ascontiguousarray(frame_f32, dtype=np.float32))
        res = self.vad_model(tensor, SAMPLE_RATE)
        return float(res.item() if hasattr(res, "item") else res)

    def reset_vad(self) -> None:
        if hasattr(self.vad_model, "reset_states"):
            self.vad_model.reset_states()

    def transcribe_pcm(self, audio_f32: np.ndarray) -> tuple[str, int]:
        if len(audio_f32) < int(0.12 * SAMPLE_RATE):
            return "", 0
        rms = float(np.sqrt(np.mean(np.square(audio_f32, dtype=np.float32))))
        if rms < 0.0008:
            return "", 0
        started = time.monotonic()
        with self._transcribe_lock:
            segments, _ = self.whisper.transcribe(
                audio_f32,
                language=self.language,
                initial_prompt=self.initial_prompt,
                beam_size=1,
                vad_filter=False,
                condition_on_previous_text=False,
                without_timestamps=True,
                no_speech_threshold=0.65,
            )
            parts = [seg.text.strip() for seg in segments if seg.text and seg.text.strip()]
        elapsed_ms = int((time.monotonic() - started) * 1000)
        text = " ".join(parts).strip()
        if is_hallucination(text):
            return "", elapsed_ms
        return text, elapsed_ms


class StreamingVADProcessor:
    """Incremental 16 kHz PCM16 processor with Silero VAD, early STOP reflex, and inbox.jsonl logging."""

    def __init__(
        self,
        stt: STTEngine,
        *,
        on_emergency_stop: Callable[[str], None] | None = None,
        on_transcript: Callable[[dict[str, Any]], None] | None = None,
        is_robot_speaking: Callable[[], bool] | None = None,
        is_robot_moving: Callable[[], bool] | None = None,
        last_speaking_end: Callable[[], float] | None = None,
        inbox_path: Path | None = None,
    ) -> None:
        self.stt = stt
        self.on_emergency_stop = on_emergency_stop
        self.on_transcript = on_transcript
        self.is_robot_speaking = is_robot_speaking or (lambda: False)
        self.is_robot_moving = is_robot_moving or (lambda: False)
        self.last_speaking_end = last_speaking_end or (lambda: 0.0)
        self.inbox_path = inbox_path or env_setup.INBOX_PATH

        vcfg = stt._voice_cfg()
        self.vad_threshold = float(vcfg.get("vad_threshold") or 0.55)
        self.speaking_vad_threshold = float(vcfg.get("speaking_vad_threshold") or 0.82)
        self.min_silence_frames = max(3, int(float(vcfg.get("vad_min_silence_ms") or 500) / 32.0))
        self.min_speech_frames = max(3, int(float(vcfg.get("vad_min_speech_ms") or 200) / 32.0))
        self.max_speech_frames = max(32, int(float(vcfg.get("vad_max_speech_s") or 15.0) / 0.032))

        self._pcm_tail = np.zeros(0, dtype=np.float32)
        self._preroll: collections.deque[np.ndarray] = collections.deque(maxlen=10)  # ~320 ms
        self._utterance: list[np.ndarray] = []
        self._in_speech = False
        self._speech_frames = 0
        self._silence_frames = 0
        self._early_check_frames = 0
        self._early_stop_fired = False
        self._inbox_lock = threading.Lock()

    def _current_threshold(self) -> float:
        if self.is_robot_speaking():
            return self.speaking_vad_threshold
        if time.monotonic() - self.last_speaking_end() < 0.35:
            return self.speaking_vad_threshold
        return self.vad_threshold

    def _append_inbox(self, text: str, duration_s: float, stt_ms: int, stop_triggered: bool) -> dict[str, Any]:
        entry = {
            "ts": round(time.time(), 3),
            "iso": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "text": text,
            "duration_s": round(duration_s, 2),
            "stt_ms": stt_ms,
            "stop_triggered": stop_triggered,
        }
        self.inbox_path.parent.mkdir(parents=True, exist_ok=True)
        with self._inbox_lock:
            with self.inbox_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def _reset_utterance(self) -> None:
        self._utterance.clear()
        self._in_speech = False
        self._speech_frames = 0
        self._silence_frames = 0
        self._early_check_frames = 0
        self._early_stop_fired = False
        self.stt.reset_vad()

    def _finalize_utterance(self) -> None:
        if not self._utterance or self._speech_frames < self.min_speech_frames:
            self._reset_utterance()
            return
        audio = np.concatenate(self._utterance)
        duration_s = len(audio) / SAMPLE_RATE
        already_stopped = self._early_stop_fired
        self._reset_utterance()

        text, stt_ms = self.stt.transcribe_pcm(audio)
        if not text:
            return
        stop_hit = self.stt.is_stop_command(text)
        if stop_hit and not already_stopped and self.on_emergency_stop is not None:
            self.on_emergency_stop(text)
        entry = self._append_inbox(text, duration_s, stt_ms, stop_hit)
        if self.on_transcript is not None:
            self.on_transcript(entry)

    def _maybe_early_stop(self) -> None:
        if self._early_stop_fired:
            return
        if not (self.is_robot_moving() or self.is_robot_speaking()):
            return
        # Run an early Whisper check after ~0.5s of speech, then every ~0.45s while continuing.
        if self._speech_frames < 16 or (self._speech_frames - self._early_check_frames) < 14:
            return
        self._early_check_frames = self._speech_frames
        audio = np.concatenate(self._utterance)
        text, stt_ms = self.stt.transcribe_pcm(audio)
        if text and self.stt.is_stop_command(text):
            self._early_stop_fired = True
            if self.on_emergency_stop is not None:
                self.on_emergency_stop(text)
            entry = self._append_inbox(text, len(audio) / SAMPLE_RATE, stt_ms, True)
            if self.on_transcript is not None:
                self.on_transcript(entry)
            self._reset_utterance()

    def process_pcm16(self, pcm_bytes: bytes) -> None:
        if not pcm_bytes:
            return
        pcm_i16 = np.frombuffer(pcm_bytes, dtype=np.int16)
        if pcm_i16.size == 0:
            return
        pcm_f32 = pcm_i16.astype(np.float32) / 32768.0
        buf = np.concatenate([self._pcm_tail, pcm_f32]) if self._pcm_tail.size else pcm_f32

        offset = 0
        total = buf.shape[0]
        while offset + VAD_FRAME_SAMPLES <= total:
            frame = buf[offset : offset + VAD_FRAME_SAMPLES]
            offset += VAD_FRAME_SAMPLES
            prob = self.stt.vad_prob(frame)
            thresh = self._current_threshold()

            if prob >= thresh:
                if not self._in_speech:
                    self._in_speech = True
                    self._utterance.extend(self._preroll)
                    self._speech_frames = 0
                    self._silence_frames = 0
                self._utterance.append(frame)
                self._speech_frames += 1
                self._silence_frames = 0
                self._maybe_early_stop()
                if self._in_speech and len(self._utterance) >= self.max_speech_frames:
                    self._finalize_utterance()
            else:
                if self._in_speech:
                    self._utterance.append(frame)
                    self._silence_frames += 1
                    if self._silence_frames >= self.min_silence_frames:
                        self._finalize_utterance()
                else:
                    self._preroll.append(frame)

        self._pcm_tail = buf[offset:].copy() if offset < total else np.zeros(0, dtype=np.float32)

    def flush(self) -> None:
        if self._in_speech:
            self._finalize_utterance()
        else:
            self._reset_utterance()


def run_self_test() -> dict[str, Any]:
    from voice.tts_engine import TTSEngine

    phrase = "Привет, робот! Проверка связи. Стоп!"
    tts = TTSEngine()
    audio_bytes, mime, used_tts = asyncio.run(tts.synthesize(phrase))
    pcm = decode_audio_to_pcm16k(audio_bytes)
    pcm_padded = np.concatenate([pcm, np.zeros(int(0.8 * SAMPLE_RATE), dtype=np.float32)])
    pcm_i16_bytes = np.clip(pcm_padded * 32767.0, -32768.0, 32767.0).astype(np.int16).tobytes()

    stt = STTEngine()
    # Warm up CUDA kernels so measured inference latency reflects real runtime.
    stt.transcribe_pcm(np.zeros(SAMPLE_RATE, dtype=np.float32))

    direct_text, direct_ms = stt.transcribe_pcm(pcm)

    stop_events: list[str] = []
    transcripts: list[dict[str, Any]] = []
    test_inbox = env_setup.CACHE_DIR / "tmp" / "selftest_inbox.jsonl"
    if test_inbox.exists():
        test_inbox.unlink()

    proc = StreamingVADProcessor(
        stt,
        on_emergency_stop=stop_events.append,
        on_transcript=transcripts.append,
        is_robot_moving=lambda: True,
        inbox_path=test_inbox,
    )
    chunk_bytes = 2048  # 64 ms @ 16 kHz Int16
    for i in range(0, len(pcm_i16_bytes), chunk_bytes):
        proc.process_pcm16(pcm_i16_bytes[i : i + chunk_bytes])
    proc.flush()

    ok = bool(transcripts) and bool(stop_events) and stt.device == "cuda"
    return {
        "ok": ok,
        "tts_engine": used_tts,
        "tts_mime": mime,
        "stt_device": stt.device,
        "stt_compute_type": stt.compute_type,
        "direct_text": direct_text,
        "direct_stt_ms": direct_ms,
        "stop_events": stop_events,
        "vad_transcripts": transcripts,
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Silero VAD + faster-whisper (CUDA / CPU)")
    parser.add_argument("--self-test", action="store_true", help="Проверить TTS -> PyAV -> VAD -> CUDA Whisper -> СТОП")
    args = parser.parse_args(argv)

    if args.self_test:
        res = run_self_test()
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1

    stt = STTEngine()
    print(json.dumps({"ok": True, "device": stt.device, "compute_type": stt.compute_type}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
