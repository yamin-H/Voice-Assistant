"""LLM module: Groq LLM streaming and conversation history."""

import time
from typing import Callable
from groq import AsyncGroq
from config import LLM_MODEL, SYSTEM_PROMPT


class LLMClient:
    """Handles Groq LLM streaming with conversation history."""

    def __init__(self, api_key: str) -> None:
        self.client = AsyncGroq(api_key=api_key)
        self.conversation_history: list[dict[str, str]] = [
            {"role": "system", "content": SYSTEM_PROMPT}
        ]

    async def stream_response(
        self,
        user_text: str,
        on_token: Callable[[str, int], None],
        on_complete: Callable[[str, int], None],
    ) -> None:
        """Stream LLM response for the given user text."""
        self.conversation_history.append({"role": "user", "content": user_text})

        turn_start = time.monotonic()
        full_response = ""

        try:
            stream = await self.client.chat.completions.create(
                model=LLM_MODEL,
                messages=self.conversation_history,
                stream=True,
                temperature=0.7,
                max_tokens=150,
            )

            async for chunk in stream:
                token = chunk.choices[0].delta.content or ""
                if token:
                    full_response += token
                    ms = int((time.monotonic() - turn_start) * 1000)
                    on_token(token, ms)

            self.conversation_history.append(
                {"role": "assistant", "content": full_response}
            )

            ms = int((time.monotonic() - turn_start) * 1000)
            on_complete(full_response, ms)

        except Exception as e:
            ms = int((time.monotonic() - turn_start) * 1000)
            print(f"[llm] error @{ms}ms: {e}", flush=True)