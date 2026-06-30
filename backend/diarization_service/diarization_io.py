"""Subprocess stdout protocol: human-readable debug logs, then a single JSON payload."""

import json
import sys

JSON_MARKER = "@@DIARIZATION_RESULT_JSON@@"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def debug_print(message: str = "") -> None:
    """Write debug lines to stdout (visible in FastAPI terminal before JSON)."""
    sys.stdout.write(message + "\n")
    sys.stdout.flush()


def emit_result(payload: dict) -> None:
    """Print marker + one JSON object on the last line (for reliable parsing)."""
    sys.stdout.write(JSON_MARKER + "\n")
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def parse_subprocess_stdout(stdout: str) -> tuple[list[str], dict]:
    """
    Split captured stdout into debug lines and the result payload.
    Forwards debug lines to the parent process terminal.
    """
    text = stdout or ""
    if JSON_MARKER not in text:
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if not lines:
            raise ValueError("Diarization subprocess returned empty stdout.")
        for line in lines[:-1]:
            print(line, flush=True)
        try:
            return lines[:-1], json.loads(lines[-1])
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Diarization subprocess did not return valid JSON: {lines[-1][:200]}"
            ) from exc

    debug_block, _, json_block = text.partition(JSON_MARKER)
    debug_lines = [ln for ln in debug_block.splitlines() if ln.strip()]
    for line in debug_lines:
        print(line, flush=True)

    payload_text = json_block.strip()
    if not payload_text:
        raise ValueError("Diarization subprocess returned no JSON after marker.")
    try:
        return debug_lines, json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid JSON after {JSON_MARKER}: {payload_text[:200]}"
        ) from exc
