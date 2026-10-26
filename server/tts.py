"""TTS module: Edge TTS (Microsoft) for voice output."""

import asyncio
import io
import time
from typing import Callable

import edge_tts
from livekit import rtc
from pydub import AudioSegment


class TTSClient:
    """Handles Edge TTS synthesis and LiveKit audio publishing."""

    def __init__(self, room: rtc.Room, voice: str = "en-US-AriaNeural") -> None:
        self.room = room
        self.voice = voice
        self._audio_source: rtc.AudioSource | None = None
        self._track: rtc.LocalAudioTrack | None = None
        self._speaking: bool = False
        self._cancelled: bool = False

    async def _ensure_track(self) -> rtc.LocalAudioTrack:
        """Create or reuse the audio track for publishing."""
        if self._track is not None:
            return self._track

        self._audio_source = rtc.AudioSource(sample_rate=24000, num_channels=1)
        self._track = rtc.LocalAudioTrack.create_audio_track("tts-output", self._audio_source)
        options = rtc.TrackPublishOptions()
        options.source = rtc.TrackSource.SOURCE_MICROPHONE
        await self.room.local_participant.publish_track(self._track, options)
        return self._track

    def cancel(self) -> None:
        """Cancel ongoing TTS playback."""
        self._cancelled = True

    async def speak(
        self,
        text: str,
        turn_start_ms: float,
        on_start: Callable[[int], None] | None = None,
    ) -> None:
        """Convert text to speech and stream to LiveKit room."""
        if not text.strip():
            return

        await self._ensure_track()

        # Reset cancellation flag at start of each speak() call
        self._cancelled = False
        self._speaking = True

        # Record speak() entry time for fallback elapsed calculation
        speak_entry = time.monotonic()

        # Collect all MP3 chunks from edge-tts
        communicate = edge_tts.Communicate(text, self.voice)
        mp3_buffer = b""
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                mp3_buffer += chunk["data"]

        if not mp3_buffer:
            print("[tts] error: edge-tts returned empty audio buffer", flush=True)
            self._speaking = False
            return

        # Decode MP3 to raw PCM in a thread so the event loop is not blocked
        loop = asyncio.get_event_loop()

        def decode_mp3() -> bytes:
            audio = AudioSegment.from_mp3(io.BytesIO(mp3_buffer))
            audio = audio.set_frame_rate(24000).set_channels(1).set_sample_width(2)
            return audio.raw_data

        pcm_data: bytes = await loop.run_in_executor(None, decode_mp3)

        # Compute elapsed ms from turn commit.
        # turn_start_ms is time.monotonic() * 1000 (ms); normalise if caller
        # passed raw seconds by mistake (value would be < 1e6 at typical uptime).
        now_ms = time.monotonic() * 1000
        if turn_start_ms > 1_000_000:
            # Caller passed milliseconds correctly
            elapsed_ms = int(now_ms - turn_start_ms)
        else:
            # Caller passed seconds — convert
            elapsed_ms = int(now_ms - turn_start_ms * 1000)

        # Clamp to a sane range so the log is readable
        if elapsed_ms < 0 or elapsed_ms > 60_000:
            elapsed_ms = int((time.monotonic() - speak_entry) * 1000)

        preview = text[:30] + ("..." if len(text) > 30 else "")
        print(f'[tts] audio-out @{elapsed_ms}ms: "{preview}"', flush=True)

        if on_start:
            on_start(elapsed_ms)

        # Push PCM frames in 100ms chunks (2400 samples @ 24kHz)
        chunk_size = 2400 * 2  # samples * bytes-per-sample
        for i in range(0, len(pcm_data), chunk_size):
            if self._cancelled:
                now_ms = time.monotonic() * 1000
                cancel_elapsed = int(now_ms - turn_start_ms) if turn_start_ms > 1_000_000 else int((time.monotonic() - speak_entry) * 1000)
                print(f"[barge-in] tts cancelled at @{cancel_elapsed}ms", flush=True)
                break
            frame_bytes = pcm_data[i : i + chunk_size]
            samples_per_channel = len(frame_bytes) // 2
            if self._audio_source:
                await self._audio_source.capture_frame(
                    rtc.AudioFrame(
                        data=frame_bytes,
                        sample_rate=24000,
                        num_channels=1,
                        samples_per_channel=samples_per_channel,
                    )
                )

        # Let the last frame finish playing before returning
        await asyncio.sleep(0.1)
        self._speaking = False

    async def close(self) -> None:
        """Clean up audio track."""
        if self._track:
            await self.room.local_participant.unpublish_track(self._track.sid)
            self._track = None
            self._audio_source = None