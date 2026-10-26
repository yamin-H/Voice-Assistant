"""Entry point: LiveKit agent worker with ASR, VAD, and LLM."""

from __future__ import annotations

import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import AgentServer, JobContext

from config import ROOM_NAME, ROOT_DIR
from asr import transcription_worker, set_groq_client, AsrChunk
from vad import TurnDetector, run_vad_loop
from llm import LLMClient
from tts import TTSClient

load_dotenv(ROOT_DIR / ".env")
load_dotenv(Path(__file__).resolve().parent / ".env", override=True)

for _name in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET", "GROQ_API_KEY"):
    if not os.environ.get(_name):
        raise SystemExit(f"{_name} is missing. Add it to {ROOT_DIR / '.env'}.")

logger = logging.getLogger("voice-agent")

set_groq_client(os.environ["GROQ_API_KEY"])
llm_client = LLMClient(os.environ["GROQ_API_KEY"])

server = AgentServer()


@server.rtc_session()
async def entrypoint(ctx: JobContext) -> None:
    """Called by LiveKit when this worker is dispatched to a room."""
    processed_tracks: set[str] = set()
    asr_tasks: set[asyncio.Task[None]] = set()

    def handle_turn_commit(transcript: str, turn_start_ms: int) -> None:
        async def run_turn() -> None:
            async def on_complete_async(full: str, ms: int) -> None:
                print(f'[llm] complete @{ms}ms: "{full}"', flush=True)
                try:
                    await tts_client.speak(full, turn_start_ms)
                except Exception as e:
                    print(f"[tts] error: {e}", flush=True)
                    import traceback
                    traceback.print_exc()
            await llm_client.stream_response(
                transcript,
                on_token=lambda token, ms: print(
                    f'[llm] token @{ms}ms: "{token}"', flush=True),
                on_complete=lambda full, ms: asyncio.create_task(
                    on_complete_async(full, ms)
                ),
            )
        asyncio.create_task(run_turn())

    def handle_speech_start(turn_id: int, turn_start_ms: float) -> None:
        if tts_client._speaking:
            tts_client.cancel()

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

        logger.info("transcribing microphone from %s", participant.identity)

        detector = TurnDetector(
            on_turn_commit_callback=handle_turn_commit,
            on_speech_start_callback=handle_speech_start,
        )
        queue: asyncio.Queue[AsrChunk | None] = asyncio.Queue()

        asr_task = asyncio.create_task(transcription_worker(queue, detector.on_transcript))
        asr_tasks.add(asr_task)
        asr_task.add_done_callback(asr_tasks.discard)

        vad_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="silero-vad")

        vad_task = asyncio.create_task(run_vad_loop(track, detector, queue, vad_executor))
        asr_tasks.add(vad_task)
        vad_task.add_done_callback(asr_tasks.discard)

    ctx.room.on("track_subscribed", handle_track)
    await ctx.connect()

    tts_client = TTSClient(room=ctx.room)

    if ctx.room.name != ROOM_NAME:
        logger.warning("agent dispatched to %r instead of %r, leaving", ctx.room.name, ROOM_NAME)
        return

    for participant in ctx.room.remote_participants.values():
        for publication in participant.track_publications.values():
            if publication.track is not None and publication.track.kind == rtc.TrackKind.KIND_AUDIO:
                handle_track(publication.track, publication, participant)

    logger.info("agent connected")
    logger.info("room=%s participants=%s", ctx.room.name, [p.identity for p in ctx.room.remote_participants.values()])

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
    server.run(devmode=True)