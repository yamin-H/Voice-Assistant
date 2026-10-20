"""Step 2: LiveKit agent worker with Groq Whisper transcription.

The worker joins the "voice-demo" room, subscribes to participants' microphone
tracks, and sends fixed 3-second audio chunks to Groq Whisper. Each transcript
is printed as:

    [asr] partial @{ms}ms: "{text}"

There is intentionally no VAD or turn detection in this step, so chunks are
fixed-length and the timestamp is measured from the first chunk that produces
a transcript (the first detected speech).

Run from the repository root:
    server\\.venv\\Scripts\\python.exe -m livekit.agents start server/agent.py --dev --log-format colored
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import wave
from pathlib import Path

from dotenv import load_dotenv
from groq import Groq
from livekit import rtc
from livekit.agents import AgentServer, JobContext

ROOT_DIR = Path(__file__).resolve().parent.parent
ROOM_NAME = "voice-demo"

ASR_MODEL = "whisper-large-v3-turbo"
ASR_SAMPLE_RATE = 16000
ASR_CHANNELS = 1
ASR_CHUNK_SECONDS = 3.0
ASR_BYTES_PER_SAMPLE = 2
ASR_CHUNK_BYTES = int(ASR_SAMPLE_RATE * ASR_CHUNK_SECONDS) * ASR_BYTES_PER_SAMPLE
# Below this peak amplitude a chunk is treated as silence. This only avoids
# sending an empty buffer to Whisper; it is not VAD or turn detection.
ASR_SILENCE_PEAK = 32

# Load credentials from the repository root before the SDK reads them.
load_dotenv(ROOT_DIR / ".env")
load_dotenv(Path(__file__).resolve().parent / ".env", override=True)

for _name in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "GROQ_API_KEY"):
    if not os.environ.get(_name):
        raise SystemExit(f"{_name} is missing. Add it to {ROOT_DIR / '.env'}.")

# Logging is configured by LiveKit's worker; just use a named logger.
logger = logging.getLogger("voice-agent")

groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])

server = AgentServer()


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


async def _transcription_worker(queue: asyncio.Queue[tuple[int, bytes] | None]) -> None:
    """Transcribe chunks one at a time so partials stay in order."""
    speech_origin_ms: int | None = None

    while True:
        item = await queue.get()
        if item is None:
            return

        chunk_ms, pcm = item
        try:
            text = await asyncio.to_thread(_transcribe, pcm)
        except Exception:
            logger.exception("groq transcription failed")
            continue

        text = " ".join(text.split())
        if not text:
            continue

        # Without VAD, use the first chunk that actually contains speech as t=0.
        if speech_origin_ms is None:
            speech_origin_ms = chunk_ms

        ms = chunk_ms - speech_origin_ms
        print(f'[asr] partial @{ms}ms: "{text}"', flush=True)


async def _transcribe_track(track: rtc.RemoteTrack, participant: rtc.RemoteParticipant) -> None:
    """Collect audio frames from one microphone track into 3-second chunks."""
    logger.info("transcribing microphone from %s", participant.identity)

    queue: asyncio.Queue[tuple[int, bytes] | None] = asyncio.Queue()
    worker = asyncio.create_task(_transcription_worker(queue))
    stream = rtc.AudioStream(
        track,
        sample_rate=ASR_SAMPLE_RATE,
        num_channels=ASR_CHANNELS,
        frame_size_ms=20,
    )

    buffer = bytearray()
    chunk_index = 0

    try:
        async for event in stream:
            buffer.extend(event.frame.data)
            while len(buffer) >= ASR_CHUNK_BYTES:
                pcm = bytes(buffer[:ASR_CHUNK_BYTES])
                del buffer[:ASR_CHUNK_BYTES]
                chunk_ms = int(chunk_index * ASR_CHUNK_SECONDS * 1000)
                # Skip near-silent chunks so Whisper does not hallucinate
                # phrases like "Thank you." on an empty buffer.
                if _peak_amplitude(pcm) >= ASR_SILENCE_PEAK:
                    queue.put_nowait((chunk_ms, pcm))
                chunk_index += 1
    except Exception as exc:
        logger.info("audio stream from %s ended: %s", participant.identity, exc)
    finally:
        # Flush the tail if it contains at least half a second of audio.
        if len(buffer) >= ASR_SAMPLE_RATE * ASR_BYTES_PER_SAMPLE // 2 and _peak_amplitude(bytes(buffer)) >= ASR_SILENCE_PEAK:
            queue.put_nowait((int(chunk_index * ASR_CHUNK_SECONDS * 1000), bytes(buffer)))
        queue.put_nowait(None)
        await worker
        await stream.aclose()


@server.rtc_session()
async def entrypoint(ctx: JobContext) -> None:
    """Called by LiveKit when this worker is dispatched to a room."""
    processed_tracks: set[str] = set()
    asr_tasks: set[asyncio.Task[None]] = set()

    def handle_track(
        track: rtc.Track,
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
    ) -> None:
        if track.kind != rtc.TrackKind.KIND_AUDIO:
            return

        track_id = publication.sid or track.sid or f"{participant.identity}-{id(track)}"
        if track_id in processed_tracks:
            return
        processed_tracks.add(track_id)

        task = asyncio.create_task(_transcribe_track(track, participant))
        asr_tasks.add(task)
        task.add_done_callback(asr_tasks.discard)

    # Register before connecting so tracks from participants already in the
    # room are not missed while the agent subscribes.
    ctx.room.on("track_subscribed", handle_track)
    await ctx.connect()

    if ctx.room.name != ROOM_NAME:
        logger.warning("agent dispatched to %r instead of %r, leaving", ctx.room.name, ROOM_NAME)
        return

    # Safety net for tracks that were already subscribed during connect().
    for participant in ctx.room.remote_participants.values():
        for publication in participant.track_publications.values():
            if publication.track is not None and publication.track.kind == rtc.TrackKind.KIND_AUDIO:
                handle_track(publication.track, publication, participant)

    logger.info("agent connected")
    logger.info("room=%s participants=%s", ctx.room.name, [p.identity for p in ctx.room.remote_participants.values()])

    # Nothing else to do in step 2. Stay in the room until it closes (or the worker stops).
    disconnected = asyncio.Event()
    ctx.room.on("disconnected", lambda *_: disconnected.set())
    try:
        await disconnected.wait()
    finally:
        for task in list(asr_tasks):
            task.cancel()
        if asr_tasks:
            await asyncio.gather(*asr_tasks, return_exceptions=True)
        logger.info("agent disconnected")


if __name__ == "__main__":
    # Convenience for `python server/agent.py`; the CLI below is the intended entry point.
    server.run(devmode=True)
