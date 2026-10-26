# Voice Assistant — Steps 1–3

Minimal real-time voice setup: a LiveKit agent worker joins the `voice-demo` room, a browser page publishes microphone audio, the agent transcribes that audio with Groq Whisper, and Silero VAD detects when a spoken turn is complete.

There is no LLM, TTS, or audio playback yet.

## What's here

| Path | Purpose |
| --- | --- |
| `server/agent.py` | LiveKit agent worker. Joins `voice-demo`, transcribes 3-second chunks with Groq Whisper, runs Silero VAD on every audio frame, and commits complete turns. |
| `server/token_server.py` | Local web server. Serves the client and mints room tokens (keeps the API secret out of the browser). |
| `client/index.html` | Test page. Joins `voice-demo`, publishes the mic, shows connection/participant/mic status. |
| `server/.venv/` | Python virtual environment with `livekit-agents`, `python-dotenv`, `groq`, `silero-vad`, `torch`, `aiohttp`. |
| `server/requirements.txt` | Exact installed versions. |

## 1. Add real credentials

Edit the `.env` in the repo root. It currently contains placeholders:

```dotenv
LIVEKIT_URL=wss://your-project.livekit.cloud
LIVEKIT_API_KEY=
LIVEKIT_API_SECRET=
GROQ_API_KEY=
```

Fill in the LiveKit values from your project (Cloud dashboard → Settings → Keys) and your Groq key (console.groq.com → API Keys). The agent refuses to start while any of these is empty.

## 2. Environment setup (already done)

The virtual environment lives in `server/.venv` and already has everything installed. To recreate it:

```powershell
python -m venv server\.venv
.\server\.venv\Scripts\python.exe -m pip install -r server\requirements.txt
```

`silero-vad` pulls in CPU PyTorch, so the first install is a large download.

No activation is required if you call the venv's `python.exe` directly. If you prefer activation:

```powershell
.\server\.venv\Scripts\Activate.ps1
```

## 3. Run it

Use two terminals from the repo root.

**Terminal 1 — token server + client page**

```powershell
.\server\.venv\Scripts\python.exe server\token_server.py
```

You should see `voice-demo client: http://127.0.0.1:8000`.

**Terminal 2 — agent worker**

```powershell
.\server\.venv\Scripts\python.exe -m livekit.agents start server/agent.py --dev --log-format colored
```

Wait for `registered worker` in the log. The first microphone track also loads Silero VAD, which can take a second.

**Browser**

Open <http://127.0.0.1:8000>, click **Join room**, and allow microphone access.

## 4. What success looks like

- Token server log: `GET / 200`, then `GET /token 200`.
- Agent terminal: `received job request` for room `voice-demo`, then `agent connected`, `room=voice-demo participants=[...]`, and `transcribing microphone from ...`.
- Browser: green **Connected**, 2 participants (you and an `agent-...` entry with an `agent` badge), **Mic live**, and the level meter moves when you speak.
- Agent terminal while you speak, roughly every 3 seconds:

  ```
  [asr] partial @0ms: "hello this is a test"
  [asr] partial @3000ms: "of the voice assistant"
  [asr] partial @6000ms: "transcription"
  ```

- Agent terminal after you finish a sentence and stop for at least half a second:

  ```
  [turn] completion score 0.8 at @2483ms; commit
  ```

  The score is `0.8` when the latest partial ends with punctuation or has more than four words, and `0.4` otherwise. Only a score above `0.6` commits. The commit line appears after the 500 ms silence window plus the time Groq needs to return the final short chunk (usually 1–2 seconds).

- The page does not play any audio back. That is expected.

The agent joins automatically when the room is created. It leaves when the room closes, and the worker stays registered for the next room.

## How transcription and turn detection work

- Each remote microphone track is decoded to 16 kHz mono PCM16.
- Audio is collected into fixed 3-second chunks for partial transcripts.
- Silero VAD runs on every incoming 20 ms frame, using a rolling 512-sample window (the window size Silero requires at 16 kHz). It tracks two states: `SPEAKING` and `SILENT`.
- When VAD switches from `SPEAKING` to `SILENT`, a 500 ms silence timer starts.
- The remaining audio is also flushed to Groq at that moment, so the latest partial includes the end of the sentence instead of waiting for the next 3-second boundary.
- After 500 ms of silence, the latest partial is scored:
  - ends with punctuation, or more than four words → `0.8`
  - otherwise → `0.4`
- If the score is above `0.6`, the agent prints the commit line. If you start speaking again before the timer expires, the timer is cancelled and the turn continues.
- Empty/near-silent chunks are skipped so Whisper does not hallucinate on them.
- `[asr]` timestamps are milliseconds from the first chunk that produced a transcript. `[turn]` timestamps are milliseconds from the VAD-detected start of speech for that turn.
- Chunks are transcribed sequentially, so lines print in order. Expect the first line a few seconds after you start speaking.
- Fixed 3-second chunks can still cut through words. The free-form flush at speech end reduces that at the end of a turn.

## Troubleshooting

- **`LIVEKIT_API_KEY is missing`** — check the LiveKit values in the root `.env`.
- **`GROQ_API_KEY is missing`** — add your Groq key to the root `.env`.
- **HTTP 401 / 403 when joining** — API key/secret do not belong to the project in `LIVEKIT_URL`.
- **Groq 401** — the `GROQ_API_KEY` is invalid or revoked.
- **Groq 429** — rate limited. Wait a minute; each 3-second chunk and each speech-end flush is one transcription request.
- **No `[asr]` lines** — speak, then pause; confirm the mic meter moves, and check the agent terminal for `transcribing microphone from ...`.
- **No `[turn]` line** — speak a full sentence (punctuation or more than four words), then stay quiet for at least 500 ms. If you resume sooner, the timer is cancelled. Check that an `[asr]` line appeared and wait another second for the final chunk. A short, incomplete fragment scores `0.4` and intentionally does not commit.
- **`Could not connect` in the browser** — confirm the URL starts with `wss://` and the project is active. Check the token server terminal too.
- **Agent never appears** — the worker terminal must be running and show `registered worker`. Make sure you join exactly `voice-demo`.
- **Microphone blocked** — allow mic permission for the site. Use `http://localhost` or `http://127.0.0.1`; opening the file directly (`file://`) will not work.
- **Port 8000 busy** — run `server\token_server.py --port 8001` and open that port.

### No LiveKit project yet?

You can test against a local LiveKit server instead:

```powershell
livekit-server --dev
```

Then set the environment variables in both terminals before starting Python:

```powershell
$env:LIVEKIT_URL = "ws://127.0.0.1:7880"
$env:LIVEKIT_API_KEY = "devkey"
$env:LIVEKIT_API_SECRET = "secret"
```

Process environment variables take precedence over `.env`, so you do not have to edit the file for this. `GROQ_API_KEY` is still required.
