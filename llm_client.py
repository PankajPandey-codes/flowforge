"""
llm_client.py — Shared Anthropic/Claude-CLI plumbing for auto_mapper.py and
early_suggester.py.

Both modules previously carried separate, drifting copies of this exact
logic: a MODEL string declared twice, two identical CLI subprocess callers,
two independent output-token-limit caches, and a streaming call that only
had retry/backoff on transient errors in one of the two copies (the other,
and a third ad-hoc non-streaming call in early_suggester.py's
suggest_get_from_post, had none at all).
"""

import os
import shutil
import subprocess
import time

MODEL = "claude-sonnet-4-6"
DEFAULT_OUTPUT_TOKEN_LIMIT = 128000
LLM_MAX_ATTEMPTS = 3
LLM_RETRY_BACKOFF_SECONDS = 2
CLI_TIMEOUT = 600  # seconds per CLI call

_OUTPUT_TOKEN_LIMIT_CACHE = None


def claude_cli_path():
    return shutil.which("claude")


def call_via_claude_cli(system: str, user_prompt: str, timeout: int = CLI_TIMEOUT) -> str:
    cli = claude_cli_path()
    if not cli:
        raise RuntimeError("claude CLI not found on PATH")
    combined = f"{system}\n\n{user_prompt}"
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    result = subprocess.run(
        [cli, "-p", "-"],
        input=combined,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude CLI exited {result.returncode}: {result.stderr[:300]}")
    return result.stdout


def is_transient_llm_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    if status_code in {408, 409, 425, 429, 500, 502, 503, 504}:
        return True

    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            error_type = str(error.get("type") or "").lower()
            if any(token in error_type for token in ("overload", "rate", "timeout")):
                return True

    text = str(exc).lower()
    return any(token in text for token in ("overload", "rate_limit", "timeout", "temporarily"))


def output_token_limit(client) -> int:
    global _OUTPUT_TOKEN_LIMIT_CACHE
    if _OUTPUT_TOKEN_LIMIT_CACHE:
        return _OUTPUT_TOKEN_LIMIT_CACHE
    if client is not None:
        try:
            model_info = client.models.retrieve(MODEL)
            limit = int(getattr(model_info, "max_tokens", 0) or 0)
            if limit > 0:
                _OUTPUT_TOKEN_LIMIT_CACHE = limit
                return limit
        except Exception:
            pass
    _OUTPUT_TOKEN_LIMIT_CACHE = DEFAULT_OUTPUT_TOKEN_LIMIT
    return DEFAULT_OUTPUT_TOKEN_LIMIT


def stream_message_text(
    client,
    *,
    system: str,
    user_prompt: str,
    temperature=0,
    max_tokens=None,
    max_attempts: int = LLM_MAX_ATTEMPTS,
    retry_backoff_seconds: int = LLM_RETRY_BACKOFF_SECONDS,
) -> str:
    """
    Stream a single-turn message via the Anthropic SDK, retrying on
    transient errors (rate limits, overloads, timeouts). Falls back to the
    Claude CLI when client is None (no API key configured).
    """
    if client is None:
        return call_via_claude_cli(system, user_prompt)

    attempts = max(1, max_attempts)
    for attempt in range(1, attempts + 1):
        chunks = []
        try:
            with client.messages.stream(
                model=MODEL,
                max_tokens=max_tokens or output_token_limit(client),
                system=system,
                temperature=temperature,
                messages=[{"role": "user", "content": user_prompt}],
            ) as stream:
                for text in stream.text_stream:
                    chunks.append(text)
            return "".join(chunks)
        except Exception as exc:
            if attempt >= attempts or not is_transient_llm_error(exc):
                raise
            time.sleep(retry_backoff_seconds * attempt)

    return ""
