"""Configuration constants for the voice assistant."""

from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
ROOM_NAME = "voice-demo"

# ASR (Groq Whisper)
ASR_MODEL = "whisper-large-v3-turbo"
ASR_SAMPLE_RATE = 16000
ASR_CHANNELS = 1
ASR_CHUNK_SECONDS = 0.5                                                    # was 3.0
ASR_BYTES_PER_SAMPLE = 2
ASR_CHUNK_BYTES = int(ASR_SAMPLE_RATE * ASR_CHUNK_SECONDS) * ASR_BYTES_PER_SAMPLE
ASR_MIN_FLUSH_SECONDS = 0.1                                                # was 0.3
ASR_MIN_FLUSH_BYTES = int(ASR_SAMPLE_RATE * ASR_MIN_FLUSH_SECONDS) * ASR_BYTES_PER_SAMPLE
ASR_SILENCE_PEAK = 32

# VAD (Silero)
VAD_WINDOW_SAMPLES = 512
VAD_START_THRESHOLD = 0.5
VAD_END_THRESHOLD = 0.35

# Turn detection
SILENCE_TIMER_SECONDS = 0.3                                              
TURN_TRANSCRIPT_TIMEOUT_SECONDS = 5.0
TURN_COMMIT_THRESHOLD = 0.6

# LLM (Groq)
LLM_MODEL = "openai/gpt-oss-120b"
SYSTEM_PROMPT = (
    "You are a real-time voice assistant. The user is speaking to you via "
    "microphone and their speech is transcribed automatically. Never say "
    "'type' or 'text'. Keep every response under 2 sentences, natural and "
    "conversational for spoken audio only."
)

# Completion scoring
COMPLETION_SCORE_COMPLETE = 0.8
COMPLETION_SCORE_INCOMPLETE = 0.4