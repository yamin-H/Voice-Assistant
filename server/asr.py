"""ASR module: Groq Whisper transcription logic."""

import asyncio
import io
import logging
import wave
from dataclasses import dataclass

from groq import Groq

from config import (
    ASR_MODEL,
    ASR_SAMPLE_RATE,
    ASR_CHANNELS,
    ASR_BYTES_PER_SAMPLE,
    ASR_SILENCE_PEAK,
)

logger = logging.getLogger("voice-agent.asr")

groq_client = Groq(api_key="")  # Will be set in agent.py after env validation


def set_groq_client(api_key: str) -> None:
    """Initialize the Groq client with the API key."""
    global groq_client
    groq_client = Groq(api_key=api_key)


@dataclass(slots=True)
class AsrChunk:
    seq: int
    chunk_ms: int
    turn_id: int
    pcm: bytes


def _pcm_to_wav(pcm: bytes) -> bytes:
    """Wrap raw 16 kHz mono PCM16 audio in a WAV container for Whisper."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(ASR_CHANNELS)
        wav.setsampwidth(ASR_BYTES_PER_SAMPLE)
        wav.setframerate(ASR_SAMPLE_RATE)
        wav.writeframes(pcm)
    return buffer.getvalue()


def _transcribe(pcm: bytes) -> str:
    """Blocking Groq call; run this in a worker thread."""
    result = groq_client.audio.transcriptions.create(
        file=("chunk.wav", _pcm_to_wav(pcm)),
        model=ASR_MODEL,
        response_format="json",
        temperature=0.0,
    )
    return result.text or ""


def _peak_amplitude(pcm: bytes) -> int:
    samples = memoryview(pcm).cast("h")
    return max(abs(min(samples)), abs(max(samples)))


def should_skip_chunk(pcm: bytes) -> bool:
    """Check if chunk is near-silent and should be skipped."""
    return _peak_amplitude(pcm) < ASR_SILENCE_PEAK


async def transcription_worker(
    queue: asyncio.Queue[AsrChunk | None],
    on_transcript_callback,
) -> None:
    """Transcribe chunks one at a time so partials stay in order."""
    speech_origin_ms: int | None = None

    while True:
        item = await queue.get()
        if item is None:
            return

        try:
            text = await asyncio.to_thread(_transcribe, item.pcm)
        except Exception:
            logger.exception("groq transcription failed")
            on_transcript_callback(item.turn_id, item.seq, "")
            continue

        text = " ".join(text.split())
        if not text:
            on_transcript_callback(item.turn_id, item.seq, "")
            continue

        if speech_origin_ms is None:
            speech_origin_ms = item.chunk_ms

        ms = item.chunk_ms - speech_origin_ms
        print(f'[asr] partial @{ms}ms: "{text}"', flush=True)
        on_transcript_callback(item.turn_id, item.seq, text)