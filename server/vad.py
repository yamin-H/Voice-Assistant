"""VAD module: Silero VAD and turn detection logic."""

import asyncio
import time
import warnings
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from itertools import count

import torch
from silero_vad import load_silero_vad

from config import (
    VAD_WINDOW_SAMPLES,
    VAD_START_THRESHOLD,
    VAD_END_THRESHOLD,
    SILENCE_TIMER_SECONDS,
    TURN_TRANSCRIPT_TIMEOUT_SECONDS,
    TURN_COMMIT_THRESHOLD,
    COMPLETION_SCORE_COMPLETE,
    COMPLETION_SCORE_INCOMPLETE,
    ASR_SAMPLE_RATE,
    ASR_CHUNK_BYTES,
    ASR_MIN_FLUSH_BYTES,
    ASR_BYTES_PER_SAMPLE,
)
from asr import AsrChunk, should_skip_chunk


class VadState(Enum):
    SPEAKING = "SPEAKING"
    SILENT = "SILENT"


def _load_vad_model():
    """Load and warm up a fresh, stateful VAD model for one track."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        model = load_silero_vad()

    model.reset_states()
    with torch.no_grad():
        model(torch.zeros(VAD_WINDOW_SAMPLES, dtype=torch.float32), ASR_SAMPLE_RATE)
    model.reset_states()
    return model


def _completion_score(text: str) -> float:
    """Simple sentence-completion heuristic."""
    text = text.strip()
    if not text:
        return COMPLETION_SCORE_INCOMPLETE

    stripped = text.rstrip("\"'”’")
    if stripped and stripped[-1] in ".!?,;:":
        return COMPLETION_SCORE_COMPLETE

    if len(text.split()) > 4:
        return COMPLETION_SCORE_COMPLETE

    return COMPLETION_SCORE_INCOMPLETE


class TurnDetector:
    """Tracks VAD state and commits a turn after sustained silence."""

    def __init__(self, on_turn_commit_callback=None, on_speech_start_callback=None) -> None:
        self.state = VadState.SILENT
        self.turn_id = 0
        self.latest_partial = ""
        self.latest_seq = -1
        self._speech_started_at: float | None = None
        self._silence_task: asyncio.Task[None] | None = None
        self._transcript_event = asyncio.Event()
        self._on_turn_commit = on_turn_commit_callback
        self._on_speech_start = on_speech_start_callback

    def on_speech(self) -> None:
        if self.state is VadState.SPEAKING:
            return

        self.state = VadState.SPEAKING
        self._cancel_silence_timer()

        if self._speech_started_at is None:
            self.turn_id += 1
            self._speech_started_at = time.monotonic()
            self.latest_partial = ""
            self.latest_seq = -1
        
        if self._on_speech_start:
            self._on_speech_start(self.turn_id, self._speech_started_at * 1000)

    def on_silence(self, flush_seq: int | None) -> None:
        if self.state is VadState.SILENT:
            return

        self.state = VadState.SILENT
        if self._speech_started_at is None:
            return

        self._cancel_silence_timer()
        self._silence_task = asyncio.create_task(
            self._silence_timer(flush_seq, self.turn_id)
        )

    def on_transcript(self, turn_id: int, seq: int, text: str) -> None:
        """Called by the ASR worker, including for empty/failed chunks."""
        if turn_id != self.turn_id:
            return

        self.latest_seq = max(self.latest_seq, seq)
        if text:
            self.latest_partial = text
        self._transcript_event.set()

    def close(self) -> None:
        self._cancel_silence_timer()

    def _cancel_silence_timer(self) -> None:
        if self._silence_task is not None and not self._silence_task.done():
            self._silence_task.cancel()
        self._silence_task = None

    async def _silence_timer(self, flush_seq: int | None, turn_id: int) -> None:
        await asyncio.sleep(SILENCE_TIMER_SECONDS)
        if not self._is_silent_turn(turn_id):
            return

        if flush_seq is not None and self.latest_seq < flush_seq:
            await self._wait_for_flush(flush_seq, turn_id)

        if not self._is_silent_turn(turn_id):
            return

        score = _completion_score(self.latest_partial)
        if score <= TURN_COMMIT_THRESHOLD:
            return

        assert self._speech_started_at is not None
        ms = int((time.monotonic() - self._speech_started_at) * 1000)
        print(f"[turn] completion score {score:.1f} at @{ms}ms; commit", flush=True)

        if self._on_turn_commit:
            self._on_turn_commit(self.latest_partial, ms)

        self._speech_started_at = None
        self.latest_partial = ""
        self._silence_task = None

    async def _wait_for_flush(self, flush_seq: int, turn_id: int) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + TURN_TRANSCRIPT_TIMEOUT_SECONDS

        while self._is_silent_turn(turn_id) and self.latest_seq < flush_seq:
            self._transcript_event.clear()
            if not self._is_silent_turn(turn_id) or self.latest_seq >= flush_seq:
                return

            remaining = deadline - loop.time()
            if remaining <= 0:
                return
            try:
                await asyncio.wait_for(self._transcript_event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return

    def _is_silent_turn(self, turn_id: int) -> bool:
        return (
            self.state is VadState.SILENT
            and turn_id == self.turn_id
            and self._speech_started_at is not None
        )


async def run_vad_loop(
    track,
    detector: TurnDetector,
    asr_queue: asyncio.Queue,
    vad_executor: ThreadPoolExecutor,
) -> None:
    """Run VAD inference loop on audio frames from a track."""
    from livekit import rtc

    stream = rtc.AudioStream(
        track,
        sample_rate=ASR_SAMPLE_RATE,
        num_channels=1,
        frame_size_ms=20,
    )

    buffer = bytearray()
    buffer_start_sample = 0
    sequence = count()
    vad_state = VadState.SILENT
    vad_window = torch.empty(0, dtype=torch.float32)

    vad_model = await asyncio.get_running_loop().run_in_executor(vad_executor, _load_vad_model)

    def send_pcm(pcm: bytes) -> int | None:
        nonlocal buffer_start_sample

        chunk_ms = int(buffer_start_sample * 1000 / ASR_SAMPLE_RATE)
        buffer_start_sample += len(pcm) // ASR_BYTES_PER_SAMPLE

        if should_skip_chunk(pcm):
            return None

        seq = next(sequence)
        asr_queue.put_nowait(
            AsrChunk(seq=seq, chunk_ms=chunk_ms, turn_id=detector.turn_id, pcm=pcm)
        )
        return seq

    def flush_buffer() -> int | None:
        if len(buffer) < ASR_MIN_FLUSH_BYTES:
            return None
        pcm = bytes(buffer)
        buffer.clear()
        return send_pcm(pcm)

    try:
        async for event in stream:
            frame_data = bytes(event.frame.data)
            buffer.extend(frame_data)

            while len(buffer) >= ASR_CHUNK_BYTES:
                pcm = bytes(buffer[:ASR_CHUNK_BYTES])
                del buffer[:ASR_CHUNK_BYTES]
                send_pcm(pcm)

            samples = torch.frombuffer(bytearray(frame_data), dtype=torch.int16).float()
            samples = samples / 32768.0
            vad_window = torch.cat((vad_window, samples))
            if vad_window.numel() > VAD_WINDOW_SAMPLES:
                vad_window = vad_window[-VAD_WINDOW_SAMPLES:]
            if vad_window.numel() < VAD_WINDOW_SAMPLES:
                continue

            probability_tensor = await asyncio.get_running_loop().run_in_executor(
                vad_executor, vad_model, vad_window, ASR_SAMPLE_RATE
            )
            probability = float(probability_tensor.item())

            if vad_state is VadState.SILENT:
                if probability >= VAD_START_THRESHOLD:
                    vad_state = VadState.SPEAKING
                    detector.on_speech()
            elif probability < VAD_END_THRESHOLD:
                vad_state = VadState.SILENT
                flush_seq = flush_buffer()
                detector.on_silence(flush_seq)
    except Exception as exc:
        import logging
        logger = logging.getLogger("voice-agent.vad")
        logger.info("audio stream ended: %s", exc)
    finally:
        detector.close()
        flush_buffer()
        asr_queue.put_nowait(None)
        await stream.aclose()
        vad_executor.shutdown(wait=True)