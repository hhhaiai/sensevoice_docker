import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import wave
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import uvicorn
from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from funasr_onnx import SenseVoiceSmall
from starlette.websockets import WebSocketState

MODEL_PATH = os.getenv("MODEL_PATH", "sensevoice-small")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8008"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
INTRA_THREADS = int(os.getenv("INTRA_OP_THREADS", "1"))
MAX_CONCURRENT_INFERENCE = int(os.getenv("MAX_CONCURRENT_INFERENCE", "2"))
INFERENCE_TIMEOUT_SEC = float(os.getenv("INFERENCE_TIMEOUT_SEC", "45"))
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "50"))
WS_PARTIAL_INTERVAL_SEC = float(os.getenv("WS_PARTIAL_INTERVAL_SEC", "1.2"))
WS_MAX_BUFFER_SEC = float(os.getenv("WS_MAX_BUFFER_SEC", "30"))

SAMPLE_RATE = 16000
MIN_PCM_BYTES = 320
TAG_PATTERN = re.compile(r"<\|.*?\|>")

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)
logger = logging.getLogger("sensevoice-server")


def str_to_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def clean_text(text: str) -> str:
    return TAG_PATTERN.sub("", text).strip()


def pcm16_bytes_to_float32(raw_pcm: bytes) -> np.ndarray:
    if len(raw_pcm) % 2 != 0:
        raw_pcm = raw_pcm[:-1]
    audio_int16 = np.frombuffer(raw_pcm, dtype=np.int16)
    return audio_int16.astype(np.float32) / 32768.0


def read_wav_as_float32(path: str) -> np.ndarray:
    with wave.open(path, "rb") as wav_file:
        channels = wav_file.getnchannels()
        sample_rate = wav_file.getframerate()
        sample_width = wav_file.getsampwidth()
        frames = wav_file.readframes(wav_file.getnframes())
    if sample_width != 2:
        raise RuntimeError(f"WAV sample_width={sample_width} unsupported, expected 16-bit PCM")
    if sample_rate != SAMPLE_RATE:
        raise RuntimeError(f"WAV sample_rate={sample_rate} unsupported, expected {SAMPLE_RATE}Hz")
    audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    return np.ascontiguousarray(audio, dtype=np.float32)


def decode_audio_via_ffmpeg(file_bytes: bytes, filename: str) -> np.ndarray:
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        raise RuntimeError("ffmpeg not found. Please install ffmpeg first.")
    suffix = Path(filename).suffix if filename else ".bin"
    with tempfile.TemporaryDirectory(prefix="sensevoice_") as temp_dir:
        input_path = os.path.join(temp_dir, f"input{suffix}")
        output_path = os.path.join(temp_dir, "output.wav")
        with open(input_path, "wb") as f:
            f.write(file_bytes)
        cmd = [
            ffmpeg_path,
            "-nostdin",
            "-y",
            "-i",
            input_path,
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-f",
            "wav",
            output_path,
        ]
        completed = subprocess.run(cmd, capture_output=True, text=True)
        if completed.returncode != 0:
            stderr_tail = completed.stderr[-500:] if completed.stderr else "unknown ffmpeg error"
            raise RuntimeError(f"ffmpeg convert failed: {stderr_tail}")
        return read_wav_as_float32(output_path)


@dataclass
class StreamSession:
    partial_interval_samples: int
    max_buffer_samples: int
    raw_pcm: bytearray
    next_partial_threshold: int

    @classmethod
    def create(cls) -> "StreamSession":
        interval_samples = max(1, int(SAMPLE_RATE * WS_PARTIAL_INTERVAL_SEC))
        max_buffer_samples = max(interval_samples, int(SAMPLE_RATE * WS_MAX_BUFFER_SEC))
        return cls(
            partial_interval_samples=interval_samples,
            max_buffer_samples=max_buffer_samples,
            raw_pcm=bytearray(),
            next_partial_threshold=interval_samples,
        )

    @property
    def total_samples(self) -> int:
        return len(self.raw_pcm) // 2

    def reset(self) -> None:
        self.raw_pcm.clear()
        self.next_partial_threshold = self.partial_interval_samples

    def append(self, chunk: bytes) -> None:
        self.raw_pcm.extend(chunk)
        max_bytes = self.max_buffer_samples * 2
        if len(self.raw_pcm) > max_bytes:
            self.raw_pcm = self.raw_pcm[-max_bytes:]
            self.next_partial_threshold = self.total_samples + self.partial_interval_samples

    def as_float32(self) -> np.ndarray:
        return pcm16_bytes_to_float32(bytes(self.raw_pcm))


class ASRService:
    def __init__(self) -> None:
        self.model: Optional[SenseVoiceSmall] = None
        self.ready = False
        self.startup_error: Optional[str] = None
        self.started_at = time.time()
        self.model_name = "unknown"
        self.model_size_mb = 0.0
        self.quantize = False
        self.semaphore = asyncio.Semaphore(MAX_CONCURRENT_INFERENCE)

    def _detect_model(self) -> None:
        model_dir = Path(MODEL_PATH)
        if not model_dir.exists():
            raise RuntimeError(f"Model path not found: {model_dir.resolve()}")

        model_candidates = [model_dir / "model.onnx", model_dir / "model_quant.onnx", model_dir / "model_full.onnx"]
        for candidate in model_candidates:
            if candidate.exists():
                self.model_name = candidate.name
                self.model_size_mb = candidate.stat().st_size / (1024 * 1024)
                self.quantize = candidate.name == "model_quant.onnx"
                return

        split_candidates = list(model_dir.glob("*.onnx.part000"))
        if split_candidates:
            raise RuntimeError("ONNX model fragments detected. Run ./auto_merge.sh first.")
        raise RuntimeError(f"No model.onnx/model_quant.onnx/model_full.onnx under {model_dir.resolve()}")

    def _load_sync(self) -> None:
        self._detect_model()
        logger.info("Loading model from %s (core=%s, %.2f MB)", os.path.abspath(MODEL_PATH), self.model_name, self.model_size_mb)
        self.model = SenseVoiceSmall(model_dir=MODEL_PATH, quantize=self.quantize, intra_op_num_threads=INTRA_THREADS)
        logger.info("Warming up model with 1-second dummy audio")
        dummy = np.zeros(SAMPLE_RATE, dtype=np.float32)
        self.model(dummy, language="auto", use_itn=False)
        self.ready = True
        logger.info("Model ready, service listening on port %s", PORT)

    async def startup(self) -> None:
        try:
            await asyncio.to_thread(self._load_sync)
            self.startup_error = None
        except Exception as exc:
            self.ready = False
            self.startup_error = str(exc)
            logger.exception("Model startup failed: %s", exc)

    async def shutdown(self) -> None:
        self.ready = False
        self.model = None

    def _infer_sync(self, audio: np.ndarray, language: str, use_itn: bool) -> str:
        if self.model is None:
            raise RuntimeError("Model not loaded")
        result = self.model(audio, language=language, use_itn=use_itn)
        if isinstance(result, list):
            text = result[0] if result else ""
        else:
            text = str(result)
        return clean_text(text)

    async def transcribe(self, audio: np.ndarray, language: str = "auto", use_itn: bool = False) -> dict:
        if not self.ready:
            detail = self.startup_error or "Model is not ready"
            raise HTTPException(status_code=503, detail=detail)
        audio = np.ascontiguousarray(audio, dtype=np.float32).reshape(-1)
        if audio.size == 0:
            return {"text": "", "latency_ms": 0, "audio_duration": 0.0, "rtf": 0.0}
        audio_duration = audio.size / SAMPLE_RATE
        start = time.perf_counter()
        async with self.semaphore:
            try:
                text = await asyncio.wait_for(
                    asyncio.to_thread(self._infer_sync, audio, language, use_itn),
                    timeout=INFERENCE_TIMEOUT_SEC,
                )
            except asyncio.TimeoutError as exc:
                raise HTTPException(status_code=504, detail="Inference timeout") from exc
        latency = time.perf_counter() - start
        return {
            "text": text,
            "latency_ms": int(latency * 1000),
            "audio_duration": round(audio_duration, 4),
            "rtf": round(latency / audio_duration, 4) if audio_duration > 0 else 0.0,
        }

    def health(self) -> dict:
        return {
            "ready": self.ready,
            "model_path": os.path.abspath(MODEL_PATH),
            "model_name": self.model_name,
            "model_size_mb": round(self.model_size_mb, 2),
            "uptime_sec": int(time.time() - self.started_at),
            "max_concurrent_inference": MAX_CONCURRENT_INFERENCE,
            "startup_error": self.startup_error,
        }


asr_service = ASRService()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await asr_service.startup()
    yield
    await asr_service.shutdown()


app = FastAPI(title="SenseVoice Service", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_log_middleware(request: Request, call_next):
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("Unhandled error while processing %s %s", request.method, request.url.path)
        raise
    elapsed_ms = (time.perf_counter() - start) * 1000
    logger.info("%s %s -> %s (%.1fms)", request.method, request.url.path, response.status_code, elapsed_ms)
    return response


@app.exception_handler(Exception)
async def unhandled_exception_handler(_: Request, exc: Exception):
    logger.exception("Unhandled application exception")
    return JSONResponse(status_code=500, content={"detail": str(exc)})


@app.get("/health")
async def health():
    return asr_service.health()


@app.get("/ready")
async def ready():
    status = asr_service.health()
    if not status["ready"]:
        raise HTTPException(status_code=503, detail="Service not ready")
    return status


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = Path(__file__).parent / "web" / "index.html"
    if not html_path.exists():
        raise HTTPException(status_code=500, detail=f"Missing page: {html_path}")
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@app.post("/api/transcribe/pcm")
async def transcribe_pcm(request: Request):
    body_bytes = await request.body()
    if not body_bytes or len(body_bytes) < MIN_PCM_BYTES:
        return {"text": "", "latency_ms": 0, "audio_duration": 0.0, "rtf": 0.0}
    language = request.query_params.get("language", "auto")
    use_itn = str_to_bool(request.query_params.get("use_itn", "false"))
    audio = pcm16_bytes_to_float32(body_bytes)
    return await asr_service.transcribe(audio, language=language, use_itn=use_itn)


@app.post("/transcribe_stream")
async def transcribe_stream_compat(request: Request):
    return await transcribe_pcm(request)


@app.post("/api/transcribe/file")
async def transcribe_file(
    file: UploadFile = File(...),
    language: str = "auto",
    use_itn: bool = False,
):
    payload = await file.read()
    if not payload:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(payload) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"File too large, max {MAX_UPLOAD_MB}MB")
    try:
        audio = await asyncio.to_thread(decode_audio_via_ffmpeg, payload, file.filename or "audio.bin")
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result = await asr_service.transcribe(audio, language=language, use_itn=use_itn)
    result["filename"] = file.filename
    return result


async def send_ws_result(ws: WebSocket, event: str, result: dict) -> None:
    payload = {"event": event}
    payload.update(result)
    await ws.send_json(payload)


@app.websocket("/ws/transcribe")
async def ws_transcribe(ws: WebSocket):
    await ws.accept()
    language = ws.query_params.get("language", "auto")
    use_itn = str_to_bool(ws.query_params.get("use_itn", "false"))
    session = StreamSession.create()
    try:
        await ws.send_json({"event": "ready", "sample_rate": SAMPLE_RATE})
        while True:
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                break
            data = message.get("bytes")
            if data is not None:
                if len(data) >= 2:
                    session.append(data)
                    if session.total_samples >= session.next_partial_threshold:
                        partial = await asr_service.transcribe(session.as_float32(), language=language, use_itn=use_itn)
                        await send_ws_result(ws, "partial", partial)
                        session.next_partial_threshold = session.total_samples + session.partial_interval_samples
                continue

            text = message.get("text")
            if text is None:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                await ws.send_json({"event": "error", "detail": "Invalid JSON command"})
                continue

            event = payload.get("event", "")
            if event == "flush":
                final = await asr_service.transcribe(session.as_float32(), language=language, use_itn=use_itn)
                await send_ws_result(ws, "final", final)
                session.reset()
            elif event == "end":
                final = await asr_service.transcribe(session.as_float32(), language=language, use_itn=use_itn)
                await send_ws_result(ws, "final", final)
                session.reset()
                await ws.send_json({"event": "closed"})
                await ws.close()
                return
            elif event == "reset":
                session.reset()
                await ws.send_json({"event": "reset"})
            elif event == "ping":
                await ws.send_json({"event": "pong"})
            else:
                await ws.send_json({"event": "error", "detail": f"Unsupported event: {event}"})
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected by client")
    except Exception as exc:
        logger.exception("WebSocket error")
        try:
            await ws.send_json({"event": "error", "detail": str(exc)})
        except Exception:
            pass
    finally:
        try:
            if ws.application_state in {WebSocketState.CONNECTED, WebSocketState.CONNECTING}:
                await ws.close()
        except RuntimeError:
            # Close frame may already be sent by server/client side.
            pass


if __name__ == "__main__":
    uvicorn.run("server:app", host=HOST, port=PORT, log_level=LOG_LEVEL.lower())
