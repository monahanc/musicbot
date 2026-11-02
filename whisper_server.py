import asyncio
import logging
import os
import re
import struct
import time
from dataclasses import dataclass, field

import numpy as np
from aiohttp import WSMsgType, web
from faster_whisper import WhisperModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MODEL_SIZE = os.getenv("WHISPER_MODEL", "small")
DEVICE = os.getenv("WHISPER_DEVICE", "cuda")
COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "float16")
PORT = int(os.getenv("WHISPER_PORT", "8000"))

SAMPLE_RATE = 16000
WINDOW_SECONDS = 2.5
WINDOW_SAMPLES = int(SAMPLE_RATE * WINDOW_SECONDS)
STEP_SECONDS = 0.25
MIN_NEW_SAMPLES = int(SAMPLE_RATE * 0.20)
MIN_WINDOW_SAMPLES = int(SAMPLE_RATE * 0.45)
IDLE_RESET_SECONDS = 1.2
STATE_TIMEOUT_SECONDS = 15.0
MIN_RMS = 0.015
MAX_NO_SPEECH_PROB = 0.55
MIN_AVG_LOGPROB = -1.05
INITIAL_PROMPT = "Ruby-chan, Ayumu-chan, Shiki-chan, Minna, Nani ga suki?"

VAD_PARAMETERS = {
    "threshold": 0.45,
    "min_speech_duration_ms": 150,
    "min_silence_duration_ms": 100,
    "speech_pad_ms": 200,
}

CHAN_SUFFIX = r"(?:chan|chen|chun|shan|chon|san)"

RUBY_CHAN_PATTERN = re.compile(
    rf"\brub[yi][\s\-]*{CHAN_SUFFIX}\b|\brub[yi]chan\b",
    re.IGNORECASE,
)
AYUMU_CHAN_PATTERN = re.compile(
    rf"\b(?:h?ayum[uo]|aiyu\s*mu|aimu)[\s\-]*{CHAN_SUFFIX}\b"
    r"|\b(?:h?ayum[uo]chan|ayumu)\b",
    re.IGNORECASE,
)
SHIKI_CHAN_PATTERN = re.compile(
    rf"\b(?:shik[iy]|shek[iy]|sik[iy])[\s\-]*{CHAN_SUFFIX}\b|\bshik[iy]chan\b",
    re.IGNORECASE,
)
MINNA_PATTERN = re.compile(
    rf"\b(?:min+a|me+na|nina)(?:[\s\-]*{CHAN_SUFFIX})?\b",
    re.IGNORECASE,
)
NANI_GA_SUKI_PATTERN = re.compile(
    r"\bnan[iy]\s*ga\b|\bnan[iy]\b.*?\bs[uao]?k[iy]\b|\bnan[iy]gasuk[iy]\b",
    re.IGNORECASE,
)


def match_aiscream_trigger(
    text: str, current_state: str | None
) -> tuple[str | None, str] | None:
    """Evaluates text against the AiScReam state machine.

    Returns (next_state, clip_name) if triggered, else None.
    """
    if not text:
        return None
    cleaned = re.sub(r"[^\w\s\-]", " ", text.lower())

    matches: list[tuple[str | None, str]] = []
    if NANI_GA_SUKI_PATTERN.search(cleaned):
        if current_state is not None:
            clip = f"suki_{current_state}"
            next_state = None if current_state == "minna" else current_state
            matches.append((next_state, clip))
        else:
            matches.append((None, ""))

    if SHIKI_CHAN_PATTERN.search(cleaned):
        matches.append(("shiki", "hai_shiki"))

    if AYUMU_CHAN_PATTERN.search(cleaned):
        matches.append(("ayumu", "hai_ayumu"))

    if RUBY_CHAN_PATTERN.search(cleaned):
        matches.append(("ruby", "hai_ruby"))

    if MINNA_PATTERN.search(cleaned):
        if current_state == "shiki":
            matches.append(("minna", "hai_minna"))
        else:
            matches.append((None, ""))

    if len(matches) != 1 or not matches[0][1]:
        return None
    return matches[0]


@dataclass
class UserAudioStream:
    buffer: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=np.float32))
    new_samples: int = 0
    last_packet_time: float = 0.0
    cooldown_until: float = 0.0

    def append_pcm16(self, pcm_bytes: bytes, now: float) -> None:
        samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        if len(samples) == 0:
            return
        if (
            self.last_packet_time > 0
            and (now - self.last_packet_time) > IDLE_RESET_SECONDS
        ):
            self.buffer = samples[-WINDOW_SAMPLES:]
            self.new_samples = len(self.buffer)
        else:
            combined = np.concatenate((self.buffer, samples))
            if len(combined) > WINDOW_SAMPLES:
                combined = combined[-WINDOW_SAMPLES:]
            self.buffer = combined
            self.new_samples += len(samples)
        self.last_packet_time = now

    def clear(self) -> None:
        self.buffer = np.empty(0, dtype=np.float32)
        self.new_samples = 0


def load_model() -> tuple[WhisperModel, str]:
    try:
        logger.info(
            "Loading Whisper model %s on %s (%s)...",
            MODEL_SIZE,
            DEVICE,
            COMPUTE_TYPE,
        )
        model = WhisperModel(MODEL_SIZE, device=DEVICE, compute_type=COMPUTE_TYPE)
        actual_device = DEVICE
    except (RuntimeError, ValueError, OSError) as e:
        logger.warning(
            "Failed to load model on %s (%s), falling back to cpu: %s",
            DEVICE,
            COMPUTE_TYPE,
            e,
        )
        model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")
        actual_device = "cpu"

    logger.info("Warming up Whisper model...")
    dummy_audio = np.zeros(8000, dtype=np.float32)
    segments, _ = model.transcribe(
        dummy_audio,
        language="en",
        beam_size=1,
        vad_filter=False,
        condition_on_previous_text=False,
    )
    list(segments)
    logger.info("Whisper model warmup complete.")
    return model, actual_device


def transcribe_array(
    model: WhisperModel, audio: np.ndarray, language: str | None = "en"
) -> str:
    if len(audio) == 0:
        return ""

    segments, _ = model.transcribe(
        audio,
        language=language,
        beam_size=1,
        vad_filter=True,
        vad_parameters=VAD_PARAMETERS,
        initial_prompt=INITIAL_PROMPT,
        condition_on_previous_text=False,
        temperature=0.0,
        max_new_tokens=24,
        repetition_penalty=1.1,
    )
    valid_texts = []
    for seg in segments:
        if seg.no_speech_prob > MAX_NO_SPEECH_PROB:
            logger.debug(
                "Rejected high no_speech_prob (%.2f)",
                seg.no_speech_prob,
            )
            continue
        if seg.avg_logprob < MIN_AVG_LOGPROB:
            logger.debug(
                "Rejected low avg_logprob (%.2f)",
                seg.avg_logprob,
            )
            continue
        if seg.text.strip():
            valid_texts.append(seg.text.strip())
    return " ".join(valid_texts).strip()


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response(
        {
            "status": "ok",
            "model": MODEL_SIZE,
            "device": request.app["device"],
        }
    )


async def handle_ws(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=15.0)
    await ws.prepare(request)

    model: WhisperModel = request.app["model"]
    lock: asyncio.Lock = request.app["lock"]
    streams: dict[int, UserAudioStream] = {}
    current_state: str | None = None
    last_state_time: float = 0.0
    closed = False

    async def inference_loop() -> None:
        nonlocal current_state, last_state_time
        while not closed and not ws.closed:
            await asyncio.sleep(STEP_SECONDS)
            now = time.monotonic()

            if (
                current_state is not None
                and (now - last_state_time) > STATE_TIMEOUT_SECONDS
            ):
                logger.info(
                    "AiScReam state %r timed out after %.1fs; resetting to idle.",
                    current_state,
                    STATE_TIMEOUT_SECONDS,
                )
                current_state = None

            for user_id, stream in list(streams.items()):
                if len(stream.buffer) == 0:
                    continue

                idle_time = now - stream.last_packet_time
                should_flush_idle = idle_time >= 0.35 and stream.new_samples > 0
                should_step = (
                    stream.new_samples >= MIN_NEW_SAMPLES
                    and len(stream.buffer) >= MIN_WINDOW_SAMPLES
                )

                if not (should_step or should_flush_idle):
                    if idle_time > IDLE_RESET_SECONDS:
                        stream.clear()
                    continue

                if now < stream.cooldown_until:
                    stream.new_samples = 0
                    continue

                audio_snapshot = stream.buffer.copy()
                stream.new_samples = 0

                rms = float(np.sqrt(np.mean(audio_snapshot**2)))
                if rms < MIN_RMS:
                    if should_flush_idle:
                        stream.clear()
                    continue

                t0 = time.perf_counter()
                async with lock:
                    text = await asyncio.to_thread(
                        transcribe_array, model, audio_snapshot, "en"
                    )
                dt_ms = (time.perf_counter() - t0) * 1000.0

                matched = match_aiscream_trigger(text, current_state)
                if matched is not None:
                    logger.info(
                        "Stream [user=%s, state=%s, %.2fs, %.0fms]: %r -> %s",
                        user_id,
                        current_state,
                        len(audio_snapshot) / SAMPLE_RATE,
                        dt_ms,
                        text,
                        matched,
                    )
                    next_state, clip_name = matched
                    current_state = next_state
                    last_state_time = time.monotonic()
                    stream.clear()
                    cooldown = 3.2 if clip_name.startswith("suki_") else 1.0
                    stream.cooldown_until = last_state_time + cooldown
                    if not ws.closed:
                        await ws.send_json(
                            {
                                "type": "trigger",
                                "user_id": user_id,
                                "clip": clip_name,
                                "state": current_state,
                                "text": text,
                                "latency_ms": round(dt_ms, 1),
                            }
                        )
                elif should_flush_idle:
                    stream.clear()

    task = asyncio.create_task(inference_loop())
    logger.info("WebSocket client connected from %s", request.remote)
    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                payload = msg.data
                if len(payload) <= 8:
                    continue
                (user_id,) = struct.unpack(">Q", payload[:8])
                pcm_bytes = payload[8:]
                stream = streams.get(user_id)
                if stream is None:
                    stream = streams[user_id] = UserAudioStream()
                stream.append_pcm16(pcm_bytes, time.monotonic())
            elif msg.type == WSMsgType.ERROR:
                logger.warning("WebSocket error: %s", ws.exception())
    finally:
        closed = True
        task.cancel()
        logger.info("WebSocket client disconnected from %s", request.remote)

    return ws


def create_app() -> web.Application:
    model, actual_device = load_model()
    app = web.Application()
    app["model"] = model
    app["device"] = actual_device
    app["lock"] = asyncio.Lock()
    app.router.add_get("/health", handle_health)
    app.router.add_get("/ws", handle_ws)
    return app


def main():
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
