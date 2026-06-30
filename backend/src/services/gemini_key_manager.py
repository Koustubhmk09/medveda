import os
import threading
import time
from typing import Any

from dotenv import load_dotenv
from google import genai
from google.genai import types


BASE_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
    )
)

load_dotenv(os.path.join(BASE_DIR, ".env"))


MAX_REQUEST_TIMEOUT_SECONDS = 30
DEFAULT_TOTAL_TIMEOUT_SECONDS = 60
MAX_TOTAL_TIMEOUT_SECONDS = 90
MAX_MODELS = 3
MAX_RETRIES_PER_MODEL = 3


def _debug_log(message: str) -> None:
    print(f"[Gemini]\n{message}", flush=True)


def _retry_log(message: str) -> None:
    print(f"[Gemini Retry]\n{message}", flush=True)


def _load_api_keys() -> list[str]:
    raw_keys = os.getenv("GEMINI_API_KEYS", "")
    keys = [
        key.strip()
        for key in raw_keys.split(",")
        if key.strip()
    ]

    if keys:
        return keys

    legacy_key = (
        os.getenv("gemini_api_key", "").strip()
        or os.getenv("GEMINI_API_KEY", "").strip()
    )
    return [legacy_key] if legacy_key else []


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _error_values(error: Exception) -> tuple[str, ...]:
    return (
        str(error).lower(),
        str(getattr(error, "status", "")).lower(),
        str(getattr(error, "code", "")).lower(),
        error.__class__.__name__.lower(),
    )


def _matches_error(error: Exception, markers: tuple[str, ...]) -> bool:
    return any(
        marker in value
        for value in _error_values(error)
        for marker in markers
    )


def _is_model_overload_error(error: Exception) -> bool:
    return _matches_error(
        error,
        (
            "503",
            "unavailable",
            "high demand",
            "temporarily unavailable",
        ),
    )


def _is_retryable_error(error: Exception) -> bool:
    return _matches_error(
        error,
        (
            "503",
            "unavailable",
            "429",
            "resource_exhausted",
            "quota",
            "rate limit",
            "ratelimit",
            "500",
            "internal",
            "timeout",
            "timed out",
            "network",
            "connection",
            "server disconnected",
            "remoteprotocolerror",
            "temporarily",
        ),
    )


def _fallback_models(requested_model: str | None) -> list[str]:
    configured = os.getenv(
        "GEMINI_MODEL_FALLBACKS",
        "gemini-2.0-flash,gemini-2.5-flash",
    )
    models = [
        model.strip()
        for model in configured.split(",")
        if model.strip()
    ]
    if requested_model:
        models = [
            requested_model,
            *[model for model in models if model != requested_model],
        ]
    models = models or ([requested_model] if requested_model else ["gemini-2.0S-flash"])
    return models[:MAX_MODELS]


def _backoff_seconds(retry_number: int) -> int:
    return min(4, 2 ** max(0, retry_number - 1))


def _status_label(error: Exception) -> str:
    text = str(error).lower()
    status = str(getattr(error, "status", "") or "").upper()
    code = str(getattr(error, "code", "") or "")
    if "503" in text or status == "UNAVAILABLE":
        return "503 received"
    if "429" in text or status == "RESOURCE_EXHAUSTED":
        return "429 received"
    if "500" in text or status == "INTERNAL":
        return "500 received"
    if status:
        return f"{status} received"
    if code:
        return f"{code} received"
    return error.__class__.__name__


class GeminiKeyManager:
    """
    Central Gemini client manager with quota-aware key rotation.

    It keeps the last working key as the active key and only advances when a
    quota/rate-limit style error is seen.
    """

    def __init__(self) -> None:
        self._keys = _load_api_keys()
        self._clients: dict[int, genai.Client] = {}
        self._current_index = 0
        self._lock = threading.Lock()

    def _get_client(self, index: int) -> genai.Client:
        if index not in self._clients:
            timeout_seconds = _int_env(
                "GEMINI_REQUEST_TIMEOUT_SECONDS",
                MAX_REQUEST_TIMEOUT_SECONDS,
                1,
                MAX_REQUEST_TIMEOUT_SECONDS,
            )
            self._clients[index] = genai.Client(
                api_key=self._keys[index],
                http_options=types.HttpOptions(
                    timeout=timeout_seconds * 1000,
                    retry_options=types.HttpRetryOptions(attempts=1),
                ),
            )
        return self._clients[index]

    def _candidate_indices(self) -> list[int]:
        with self._lock:
            start = self._current_index
        return [
            (start + offset) % len(self._keys)
            for offset in range(len(self._keys))
        ]

    def _mark_working_key(self, index: int) -> None:
        with self._lock:
            self._current_index = index

    def _advance_after_quota(self, exhausted_index: int) -> None:
        with self._lock:
            if self._current_index == exhausted_index:
                self._current_index = (exhausted_index + 1) % len(self._keys)

    def generate_content(self, **kwargs: Any) -> Any:
        if not self._keys:
            raise ValueError(
                "No Gemini API keys configured. Set GEMINI_API_KEYS or gemini_api_key in .env."
            )

        _debug_log("Starting request")

        requested_model = kwargs.get("model")
        models = _fallback_models(str(requested_model) if requested_model else None)
        last_retryable_error: Exception | None = None
        total_attempts = 0
        total_deadline_seconds = _int_env(
            "GEMINI_TOTAL_TIMEOUT_SECONDS",
            DEFAULT_TOTAL_TIMEOUT_SECONDS,
            10,
            MAX_TOTAL_TIMEOUT_SECONDS,
        )
        deadline = time.monotonic() + total_deadline_seconds
        max_attempts_per_model = 1 + _int_env(
            "GEMINI_MAX_RETRIES_PER_MODEL",
            MAX_RETRIES_PER_MODEL,
            0,
            MAX_RETRIES_PER_MODEL,
        )
        max_retries_per_model = max_attempts_per_model - 1

        for model_index, model in enumerate(models):
            request_kwargs = {**kwargs, "model": model}
            candidates = self._candidate_indices()
            if not candidates:
                break

            for model_attempt in range(1, max_attempts_per_model + 1):
                if time.monotonic() >= deadline:
                    _debug_log("Failed after max retries\nTotal timeout reached")
                    raise TimeoutError(
                        f"Gemini request exceeded {total_deadline_seconds}s total timeout."
                    ) from last_retryable_error

                candidate_position = min(max(0, model_attempt - 2), len(candidates) - 1)
                index = candidates[candidate_position]
                total_attempts += 1
                _retry_log(
                    f"Attempt {model_attempt}/{max_attempts_per_model}\n"
                    f"Total attempt {total_attempts}"
                )
                _debug_log(f"Trying key {index + 1}\nModel: {model}")

                try:
                    response = self._get_client(index).models.generate_content(
                        **request_kwargs
                    )
                    self._mark_working_key(index)
                    if total_attempts > 1:
                        _debug_log("Recovered successfully")
                    return response
                except Exception as error:
                    if not _is_retryable_error(error):
                        _debug_log(f"Non-retryable failure\n{_status_label(error)}")
                        raise

                    last_retryable_error = error
                    retry_number = min(model_attempt, max_retries_per_model)
                    _retry_log(
                        f"{_status_label(error)}\n"
                        f"Retry {retry_number}/{max_retries_per_model}"
                    )

                    if model_attempt < max_attempts_per_model:
                        if model_attempt >= 2 and candidate_position + 1 < len(candidates):
                            self._advance_after_quota(index)
                            _debug_log("Switching key")
                        sleep_for = min(
                            _backoff_seconds(model_attempt),
                            max(0, deadline - time.monotonic()),
                        )
                        if sleep_for:
                            time.sleep(sleep_for)
                        continue

                    has_next_model = model_index + 1 < len(models)
                    if has_next_model:
                        next_model = models[model_index + 1]
                        prefix = (
                            "Switching model"
                            if _is_model_overload_error(error)
                            else "Trying fallback model"
                        )
                        _debug_log(f"{prefix}:\n{next_model}")
                        sleep_for = min(1, max(0, deadline - time.monotonic()))
                        if sleep_for:
                            time.sleep(sleep_for)
                    else:
                        _debug_log("Failed after max retries")

        raise RuntimeError(
            "All Gemini API keys and fallback models are exhausted or temporarily unavailable."
        ) from last_retryable_error


gemini_key_manager = GeminiKeyManager()


def generate_content_with_rotation(**kwargs: Any) -> Any:
    return gemini_key_manager.generate_content(**kwargs)
