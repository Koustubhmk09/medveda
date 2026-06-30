import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from pydub import AudioSegment
from pyannote.audio import Pipeline

from diarization_io import debug_print

# ---------------------------------------------------------------------------
# Paths & env
# ---------------------------------------------------------------------------
BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
load_dotenv(os.path.join(BACKEND_ROOT, ".env"))

# Legacy default used only when FFmpeg is not on PATH and no env is set.
_DEFAULT_FFMPEG_FALLBACK = (
    r"E:\ffmpeg-8.1-essentials_build\ffmpeg-8.1-essentials_build\bin"
)

HUGGINGFACE_TOKEN = os.getenv("HUGGINGFACE_TOKEN")
if not HUGGINGFACE_TOKEN:
    raise ValueError("HUGGINGFACE_TOKEN not found in .env")

SKIPPED_CHUNK_MESSAGE = (
    "[Skipped small audio segment due to temporary AI service issue]"
)


def _configure_ffmpeg() -> None:
    """Prefer PATH, then FFMPEG_DIR / FFMPEG_PATH, then legacy Windows fallback."""
    ffmpeg_bin = shutil.which("ffmpeg")
    ffprobe_bin = shutil.which("ffprobe")
    if ffmpeg_bin and ffprobe_bin:
        AudioSegment.converter = ffmpeg_bin
        AudioSegment.ffprobe = ffprobe_bin
        return

    ffmpeg_dir = os.getenv("FFMPEG_DIR", "").strip()
    if ffmpeg_dir:
        bin_dir = ffmpeg_dir
    else:
        explicit = os.getenv("FFMPEG_PATH", "").strip()
        bin_dir = os.path.dirname(explicit) if explicit else _DEFAULT_FFMPEG_FALLBACK

    os.environ["PATH"] += os.pathsep + bin_dir
    ext = ".exe" if os.name == "nt" else ""
    AudioSegment.converter = os.path.join(bin_dir, f"ffmpeg{ext}")
    AudioSegment.ffprobe = os.path.join(bin_dir, f"ffprobe{ext}")


_configure_ffmpeg()


def _apply_pipeline_accuracy_tweaks(pl: Pipeline) -> None:
    """
    Tune clustering for dynamic speaker search. min_cluster_size still affects
    short-turn retention.
    """
    if os.getenv("DIARIZATION_CLUSTERING_THRESHOLD_SCALE", "").strip():
        scale = float(os.getenv("DIARIZATION_CLUSTERING_THRESHOLD_SCALE"))
        base_t = float(pl.clustering.threshold)
        pl.clustering.threshold = float(max(0.32, min(1.65, base_t * scale)))

    mcs = int(os.getenv("DIARIZATION_MIN_CLUSTER_SIZE", "4"))
    pl.clustering.min_cluster_size = max(1, min(18, mcs))


debug_print("Loading diarization model...")
pipeline = Pipeline.from_pretrained(
    "pyannote/speaker-diarization-3.1",
    use_auth_token=HUGGINGFACE_TOKEN,
)
_apply_pipeline_accuracy_tweaks(pipeline)
debug_print("Diarization model loaded successfully!")


def _audio_duration_seconds(path: str) -> float:
    seg = AudioSegment.from_file(path)
    return float(len(seg) / 1000.0)


def build_normalized_audio_path(audio_path: str) -> tuple[str, Callable[[], None]]:
    """
    Downmix to mono and resample to 16 kHz (pyannote / ECAPA expect this regime).
    Returns (path_to_use, cleanup_callback). cleanup is no-op if the original path
    is returned unchanged.
    """
    audio = AudioSegment.from_file(audio_path)
    if audio.channels == 1 and audio.frame_rate == 16000:
        return audio_path, lambda: None

    processed = audio.set_channels(1).set_frame_rate(16000)
    fd, tmp = tempfile.mkstemp(suffix=".wav", prefix="diar_norm_")
    os.close(fd)
    processed.export(tmp, format="wav")

    def _cleanup(p: str = tmp) -> None:
        try:
            if os.path.isfile(p):
                os.unlink(p)
        except OSError:
            pass

    return tmp, _cleanup

def _resolve_diarization_kwargs(duration_sec: float) -> dict:
    """
    Map audio duration and env to pipeline() keyword arguments.
    Default for dialogue-length audio: dynamic speaker search (not fixed count with
    max_speakers=8, which over-clusters into 3–4+ phantom speakers).
    """
    max_sp = int(os.getenv("DIARIZATION_MAX_SPEAKERS", "8"))
    max_sp = max(1, min(20, max_sp))
    num_env = os.getenv("DIARIZATION_NUM_SPEAKERS", "").strip()
    if num_env.isdigit():
        n = int(num_env)
        return {"num_speakers": max(1, min(max_sp, n))}

    min_override = os.getenv("DIARIZATION_MIN_SPEAKERS", "").strip()
    if min_override.lstrip("-").isdigit():
        ms = max(1, int(min_override))
        return {"min_speakers": ms, "max_speakers": max_sp}

    return {"min_speakers": 1, "max_speakers": max_sp}

def log_diarization_segments(
    segments: list[dict],
    stage: str
) -> None:
    """
    Debug log for inspecting
    turn boundaries.
    """

    debug_print(f"\n===== {stage.upper()} =====")
    debug_print(f"Total segments: {len(segments)}\n")

    for index, seg in enumerate(
        segments
    ):

        start_f = float(
            seg["start"]
        )

        end_f = float(
            seg["end"]
        )

        duration = (
            end_f
            - start_f
        )

        debug_print(
            f"[{index:03d}] {seg['speaker']} | "
            f"{start_f:.2f}s -> {end_f:.2f}s | duration={duration:.2f}s"
        )

    debug_print("===== END SEGMENTS =====\n")

def _speaker_talk_durations(segments: list[dict]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for seg in segments:
        sp = seg["speaker"]
        totals[sp] = totals.get(sp, 0.0) + float(seg["end"]) - float(seg["start"])
    return totals

def build_person_map_by_talk_time(segments: list[dict]) -> dict[str, str]:
    """
    Map pyannote cluster IDs to Person 1 / Person 2 by total speech duration.

    First-seen cluster order is wrong when a short noise blip gets a new label
    before the main speaker (common with min_speakers heuristics).
    """
    totals = _speaker_talk_durations(segments)
    ranked = sorted(totals.keys(), key=lambda sp: totals[sp], reverse=True)
    person_map = {sp: f"Person {i + 1}" for i, sp in enumerate(ranked)}
    debug_print("===== PERSON MAPPING (by total talk time) =====")
    for sp in ranked:
        debug_print(f"  {sp}: {totals[sp]:.2f}s → {person_map[sp]}")
    debug_print("===== END PERSON MAPPING =====\n")
    return person_map

def normalize_person_labels(text: str) -> str:
    """
    Re-number rendered Person labels from scratch in encounter order.

    This is a final output guard: if any previous step leaks non-contiguous
    labels such as Person 1 / Person 3, the transcript still leaves this
    service as Person 1 / Person 2.
    """
    label_map: dict[str, str] = {}

    def repl(match: re.Match) -> str:
        old_label = match.group(1)
        if old_label not in label_map:
            label_map[old_label] = f"Person {len(label_map) + 1}"
        return match.group(0).replace(old_label, label_map[old_label], 1)

    return re.sub(r"(?m)^(Person\s+\d+)(\s*-)", repl, text)

def analyze_speaker_distribution(segments: list[dict]) -> dict:
    """
    Decide whether diarization is useful enough to drive the final transcript.

    Pyannote can split a single lecturer into tiny phantom clusters. This layer
    treats speaker count as recurring conversational evidence instead of a
    talk-time percentage.
    """
    if not segments:
        debug_print("Speaker analysis: no diarization segments -> fallback")
        return {"mode": "fallback"}

    totals = _speaker_talk_durations(segments)
    total_talk = sum(totals.values())
    if total_talk <= 0:
        debug_print("Speaker analysis: zero talk time -> fallback")
        return {"mode": "fallback"}

    min_valid_turn_duration = float(
        os.getenv("DIARIZATION_MIN_VALID_TURN_SEC", "0.25")
    )
    min_recurring_turns = int(
        os.getenv("DIARIZATION_MIN_RECURRING_TURNS", "2")
    )
    strong_recurring_turns = int(
        os.getenv("DIARIZATION_STRONG_RECURRING_TURNS", "3")
    )
    min_recurring_total = float(
        os.getenv("DIARIZATION_MIN_RECURRING_TOTAL_SEC", "0.70")
    )
    single_substantial_turn = float(
        os.getenv("DIARIZATION_SINGLE_SUBSTANTIAL_TURN_SEC", "2.0")
    )
    max_tiny_turn_ratio = float(
        os.getenv("DIARIZATION_MAX_TINY_TURN_RATIO", "0.60")
    )

    turn_counts = {speaker: 0 for speaker in totals}
    valid_turn_counts = {speaker: 0 for speaker in totals}
    valid_durations = {speaker: 0.0 for speaker in totals}
    max_turn_durations = {speaker: 0.0 for speaker in totals}
    tiny_turns = 0
    for seg in segments:
        speaker = seg["speaker"]
        duration = float(seg["end"]) - float(seg["start"])
        turn_counts[speaker] = turn_counts.get(speaker, 0) + 1
        max_turn_durations[speaker] = max(
            max_turn_durations.get(speaker, 0.0),
            duration,
        )
        if duration >= min_valid_turn_duration:
            valid_turn_counts[speaker] = valid_turn_counts.get(speaker, 0) + 1
            valid_durations[speaker] = valid_durations.get(speaker, 0.0) + duration
        else:
            tiny_turns += 1

    ranked = sorted(totals.items(), key=lambda item: item[1], reverse=True)
    tiny_turn_ratio = tiny_turns / max(1, len(segments))

    speaker_sequence = [
        seg["speaker"]
        for seg in sorted(segments, key=lambda item: float(item["start"]))
        if float(seg["end"]) - float(seg["start"]) >= min_valid_turn_duration
    ]
    compressed_sequence = []
    for speaker in speaker_sequence:
        if not compressed_sequence or compressed_sequence[-1] != speaker:
            compressed_sequence.append(speaker)

    alternation_counts = {speaker: 0 for speaker in totals}
    for left, right in zip(compressed_sequence, compressed_sequence[1:]):
        if left == right:
            continue
        alternation_counts[left] = alternation_counts.get(left, 0) + 1
        alternation_counts[right] = alternation_counts.get(right, 0) + 1

    real_speakers = []
    for speaker, duration in ranked:
        valid_turns = valid_turn_counts.get(speaker, 0)
        valid_total = valid_durations.get(speaker, 0.0)
        alternations = alternation_counts.get(speaker, 0)
        has_recurring_evidence = (
            valid_turns >= strong_recurring_turns
            and valid_total >= min_recurring_total
        )
        has_conversational_evidence = (
            valid_turns >= min_recurring_turns
            and valid_total >= min_recurring_total
            and alternations >= 2
        )
        has_single_substantial_turn = (
            max_turn_durations.get(speaker, 0.0) >= single_substantial_turn
        )
        if (
            has_recurring_evidence
            or has_conversational_evidence
            or has_single_substantial_turn
        ):
            real_speakers.append(speaker)

    debug_print("===== SPEAKER DISTRIBUTION ANALYSIS =====")
    debug_print(f"Total talk time: {total_talk:.2f}s")
    for speaker, duration in ranked:
        debug_print(
            f"  {speaker}: {duration:.2f}s, "
            f"{duration / total_talk * 100:.1f}%, "
            f"turns={turn_counts.get(speaker, 0)}, "
            f"valid_turns={valid_turn_counts.get(speaker, 0)}, "
            f"alternations={alternation_counts.get(speaker, 0)}, "
            f"max_turn={max_turn_durations.get(speaker, 0.0):.2f}s"
        )
    debug_print(
        f"Tiny turn ratio: {tiny_turn_ratio:.2f}; "
        f"real speakers: {len(real_speakers)}"
    )
    debug_print("===== END SPEAKER DISTRIBUTION ANALYSIS =====\n")

    if len(real_speakers) >= 2:
        return {
            "mode": "multi",
            "speaker_count": len(real_speakers),
            "speaker_ids": real_speakers,
        }

    if len(real_speakers) == 1:
        return {"mode": "single"}

    if tiny_turn_ratio >= max_tiny_turn_ratio and len(totals) > 1:
        return {"mode": "fallback"}

    return {"mode": "single"}

def resolve_exclusive_segments(segments: list[dict]) -> list[dict]:
    """
    Ensure a single speaker label per instant: split overlaps at the midpoint.
    """
    if not segments:
        return []

    min_len = float(os.getenv("DIARIZATION_MIN_SEGMENT_SEC", "0.05"))
    margin = float(os.getenv("DIARIZATION_BOUNDARY_MARGIN_SEC", "0.05"))
    segs = [s.copy() for s in sorted(segments, key=lambda x: float(x["start"]))]

    resolved: list[dict] = []
    for seg in segs:
        start = float(seg["start"])
        end = float(seg["end"])
        if end - start < min_len:
            continue
        if not resolved:
            resolved.append({"speaker": seg["speaker"], "start": start, "end": end})
            continue

        prev = resolved[-1]
        prev_end = float(prev["end"])

        if seg["speaker"] == prev["speaker"]:
            if start < prev_end:
                prev["end"] = round(max(float(prev["end"]), end), 3)
            else:
                resolved.append(
                    {
                        "speaker": seg["speaker"],
                        "start": round(start, 3),
                        "end": round(end, 3),
                    }
                )
            continue

        if start < prev_end:
            mid = (prev_end + start) / 2.0
            prev["end"] = round(max(float(prev["start"]) + min_len, mid - margin / 2), 3)
            start = round(min(end - min_len, mid + margin / 2), 3)
            if start >= end:
                continue

        resolved.append({"speaker": seg["speaker"], "start": start, "end": end})

    out: list[dict] = []
    for seg in resolved:
        if float(seg["end"]) - float(seg["start"]) >= min_len:
            out.append(
                {
                    "speaker": seg["speaker"],
                    "start": round(float(seg["start"]), 3),
                    "end": round(float(seg["end"]), 3),
                }
            )
    return out

def absorb_short_spurious_turns(segments: list[dict]) -> list[dict]:
    """
    Optional: relabel tiny noise islands only (NOT short real replies).

    Default OFF — the previous default (0.45s) wrongly merged guest lines like
    "But can boxing make it?" into the host when sandwiched between host turns.
    """
    if os.getenv("DIARIZATION_ABSORB_ISLANDS", "0").strip().lower() not in (
        "1",
        "true",
        "yes",
    ):
        return segments

    if len(segments) < 3:
        return segments

    max_island = float(os.getenv("DIARIZATION_MAX_ISLAND_SEC", "0.18"))
    segs = [s.copy() for s in segments]
    changed = True
    while changed:
        changed = False
        for i in range(1, len(segs) - 1):
            cur = segs[i]
            dur = float(cur["end"]) - float(cur["start"])
            if dur >= max_island:
                continue
            prev_sp = segs[i - 1]["speaker"]
            nxt_sp = segs[i + 1]["speaker"]
            if prev_sp == nxt_sp and prev_sp != cur["speaker"]:
                cur["speaker"] = prev_sp
                changed = True
    debug_print(
        f"(absorb_islands enabled: relabeled segments shorter than {max_island}s)"
    )
    return segs

def collapse_to_k_speakers(segments: list[dict], k: int = 2) -> list[dict]:
    """If pyannote still emits >k labels, map minority clusters onto dominant ones."""
    if not segments:
        return segments

    totals = _speaker_talk_durations(segments)
    if len(totals) <= k:
        return segments

    keep = set(sorted(totals.keys(), key=lambda sp: totals[sp], reverse=True)[:k])
    segs = [s.copy() for s in sorted(segments, key=lambda x: float(x["start"]))]
    for i, seg in enumerate(segs):
        if seg["speaker"] in keep:
            continue
        prev_sp = segs[i - 1]["speaker"] if i > 0 else None
        nxt_sp = segs[i + 1]["speaker"] if i + 1 < len(segs) else None
        if prev_sp in keep:
            seg["speaker"] = prev_sp
        elif nxt_sp in keep:
            seg["speaker"] = nxt_sp
        else:
            seg["speaker"] = next(iter(keep))
    debug_print(
        f"Collapsed {len(totals)} speaker clusters → {k} ({', '.join(sorted(keep))})"
    )
    return segs

def prepare_transcription_segments(segments: list[dict]) -> list[dict]:
    """
    Post-diarization: exclusive timeline, optional island cleanup, then minimal
    same-speaker merge (touching fragments only).
    """
    segments = resolve_exclusive_segments(segments)
    log_diarization_segments(segments, "exclusive")
    segments = absorb_short_spurious_turns(segments)
    segments = merge_contiguous_same_speaker(segments)
    log_diarization_segments(segments, "ready-for-chunks")
    return segments

def merge_contiguous_same_speaker(
    segments: list,
    max_gap: float | None = None,
) -> list[dict]:
    """
    Merge only consecutive fragments of the SAME speaker when the gap is tiny
    (pyannote split one utterance into multiple segments). Never merges across
    another speaker's turn.
    """
    if max_gap is None:
        max_gap = float(os.getenv("DIARIZATION_MERGE_MAX_GAP", "0.05"))
    if not segments:
        return []

    segs = sorted(segments, key=lambda x: float(x["start"]))
    merged: list[dict] = []
    current = segs[0].copy()
    for nxt in segs[1:]:
        if (
            current["speaker"] == nxt["speaker"]
            and float(nxt["start"]) - float(current["end"]) <= max_gap
        ):
            current["end"] = nxt["end"]
        else:
            merged.append(current)
            current = nxt.copy()
    merged.append(current)
    return merged

def diarize_segments(normalized_audio_path: str) -> list[dict]:
    """
    Run speaker diarization on audio (mono 16 kHz WAV recommended).
    """
    duration = _audio_duration_seconds(normalized_audio_path)
    kwargs = _resolve_diarization_kwargs(duration)
    debug_print(f"Pyannote pipeline kwargs: {kwargs} (audio duration={duration:.2f}s)")
    diarization = pipeline(normalized_audio_path, **kwargs)

    min_len = float(os.getenv("DIARIZATION_MIN_SEGMENT_SEC", "0.05"))
    segments: list[dict] = []
    for turn, _, speaker in diarization.itertracks(yield_label=True):
        start_f = float(turn.start)
        end_f = float(turn.end)
        if end_f - start_f < min_len:
            continue
        segments.append(
            {
                "speaker": speaker,
                "start": round(start_f, 3),
                "end": round(end_f, 3),
            }
        )
    log_diarization_segments(segments, "raw")
    return segments

def diarize_audio(audio_path: str) -> list[dict]:
    """Convenience: normalize if needed, diarize, delete temp file."""
    norm, cleanup = build_normalized_audio_path(audio_path)
    try:
        return diarize_segments(norm)
    finally:
        cleanup()

def merge_speaker_segments(segments: list, max_gap: float | None = None) -> list[dict]:
    """Backward-compatible alias for merge_contiguous_same_speaker."""
    return merge_contiguous_same_speaker(segments, max_gap=max_gap)

def _boundary_padding_ms(index: int, segment: dict, segments: list) -> tuple[int, int]:
    """
    Asymmetric padding: no extension into a neighbouring *different* speaker.
    Uses at most a fraction of the inter-speaker gap for safety margin.
    """
    dur_ms = max(1.0, (float(segment["end"]) - float(segment["start"])) * 1000.0)
    max_pad = int(os.getenv("DIARIZATION_MAX_PADDING_MS", "40"))
    ratio = float(os.getenv("DIARIZATION_PADDING_RATIO", "0.06"))
    gap_fraction = float(os.getenv("DIARIZATION_PADDING_GAP_FRACTION", "0.05"))
    base = min(max_pad, max(8, int(dur_ms * ratio)))

    pad_before = base
    pad_after = base

    if index > 0:
        prev = segments[index - 1]
        if prev["speaker"] != segment["speaker"]:
            gap_ms = (float(segment["start"]) - float(prev["end"])) * 1000.0
            pad_before = 0 if gap_ms <= 0 else min(pad_before, int(gap_ms * gap_fraction))

    if index + 1 < len(segments):
        nxt = segments[index + 1]
        if nxt["speaker"] != segment["speaker"]:
            gap_ms = (float(nxt["start"]) - float(segment["end"])) * 1000.0
            pad_after = 0 if gap_ms <= 0 else min(pad_after, int(gap_ms * gap_fraction))

    return pad_before, pad_after

def split_audio_by_speaker(
    audio_path: str,
    segments: list,
    output_dir: str = "speaker_chunks",
):
    os.makedirs(output_dir, exist_ok=True)
    audio = AudioSegment.from_file(audio_path)
    speaker_chunks = []

    for index, segment in enumerate(segments):
        pad_before, pad_after = _boundary_padding_ms(index, segment, segments)
        start_ms = max(0, int(float(segment["start"]) * 1000) - pad_before)
        end_ms = min(len(audio), int(float(segment["end"]) * 1000) + pad_after)
        if end_ms <= start_ms:
            continue
        speaker_audio = audio[start_ms:end_ms]
        chunk_filename = f"{segment['speaker']}_{index}.wav"
        chunk_path = os.path.join(output_dir, chunk_filename)
        speaker_audio.export(chunk_path, format="wav")
        speaker_chunks.append(
            {
                "speaker": segment["speaker"],
                "start": segment["start"],
                "end": segment["end"],
                "chunk_path": chunk_path,
            }
        )
    return speaker_chunks

def _main_python_exe() -> str:
    if os.name == "nt":
        return os.path.join(BACKEND_ROOT, "venv", "Scripts", "python.exe")
    return os.path.join(BACKEND_ROOT, "venv", "bin", "python")

def _is_retryable_transcribe_failure(message: str) -> bool:
    text = message.lower()
    return any(
        marker in text
        for marker in (
            "remoteprotocolerror",
            "server disconnected",
            "timeout",
            "temporarily unavailable",
            "503",
            "502",
            "500",
        )
    )

def _run_gemini_transcribe(file_path: str) -> dict:
    """Invoke main-venv single-speaker chunk transcription for a diarization clip."""
    script = (
        "import sys, os, json\n"
        f"sys.path.insert(0, {json.dumps(BACKEND_ROOT)})\n"
        "from services.audio_transcript import process_audio_diarization_chunk\n"
        f"result = process_audio_diarization_chunk({json.dumps(file_path)})\n"
        "print(json.dumps(result))\n"
    )
    max_attempts = int(os.getenv("GEMINI_CHUNK_TRANSCRIBE_ATTEMPTS", "1"))
    max_attempts = max(1, min(5, max_attempts))
    subprocess_timeout = int(os.getenv("GEMINI_CHUNK_SUBPROCESS_TIMEOUT_SECONDS", "75"))
    subprocess_timeout = max(10, min(90, subprocess_timeout))
    last_error = ""

    for attempt in range(1, max_attempts + 1):
        try:
            response = subprocess.run(
                [_main_python_exe(), "-c", script],
                capture_output=True,
                text=True,
                timeout=subprocess_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            last_error = (
                "Gemini transcribe subprocess timed out after "
                f"{subprocess_timeout}s"
            )
            debug_print(last_error)
            if attempt < max_attempts:
                time.sleep(min(4, attempt))
                continue
            raise RuntimeError(last_error) from exc

        if response.returncode == 0 and response.stdout.strip():
            lines = [line for line in response.stdout.splitlines() if line.strip()]
            for line in lines[:-1]:
                debug_print(line)
            try:
                return json.loads(lines[-1])
            except json.JSONDecodeError as exc:
                raise ValueError(
                    "Gemini transcribe subprocess did not return valid JSON on its "
                    f"last stdout line: {lines[-1][:200]}"
                ) from exc

        last_error = response.stderr or response.stdout
        if (
            attempt < max_attempts
            and _is_retryable_transcribe_failure(last_error)
        ):
            debug_print(
                "Gemini transcribe subprocess failed transiently; "
                f"retrying ({attempt + 1}/{max_attempts})"
            )
            time.sleep(min(4, attempt))
            continue

        break

    raise RuntimeError(
        f"Gemini transcribe subprocess failed (code {response.returncode}):\n"
        f"{last_error}"
    )

def transcribe_labeled_full_audio(file_path: str, label: str = "Person 1") -> dict:
    """Single speaker path: one Gemini audio call, keep Person-label format."""
    result = _run_gemini_transcribe(file_path)
    transcript = (result.get("transcript_text") or "").strip()
    lang_code = result.get("detected_language_code")
    labeled = f"{label} - {transcript}" if transcript else ""
    return {
        "transcript": labeled,
        "output": labeled,
        "detected_language_code": lang_code,
    }

def _pick_detected_language(codes: list) -> str | None:
    """Majority vote on ISO codes from chunk transcriptions."""
    valid = [c for c in codes if c]
    if not valid:
        return None
    return max(set(valid), key=valid.count)

def transcribe_speaker_chunks(
    chunks: list,
    person_map: dict[str, str],
    batch_size: int = 3,
):
    """
    Transcribe each diarized chunk via the main venv (one subprocess per chunk).
    Chunks are processed in chronological order. Person labels come from
    talk-time ranking, not first-seen cluster ID.
    """
    final_transcript = []
    final_output = []
    language_codes: list[str | None] = []
    total_gemini_time = 0.0

    ordered = sorted(chunks, key=lambda c: float(c["start"]))
    concurrency = max(
        1,
        min(
            int(os.getenv("DIARIZATION_CHUNK_CONCURRENCY", "3")),
            4,
        ),
    )

    def _transcribe_chunk_item(index: int, chunk: dict) -> dict:
        speaker_id = chunk["speaker"]
        person_name = person_map.get(speaker_id, speaker_id)
        debug_print(
            f"Transcribing {person_name} ({speaker_id}) "
            f"[{chunk['start']:.3f}s – {chunk['end']:.3f}s]"
        )
        result = _run_gemini_transcribe(chunk["chunk_path"])
        transcript = (result.get("transcript_text") or "").strip()
        output = (result.get("output_text") or "").strip()
        return {
            "transcript": transcript,
            "output": output,
            "language_code": result.get("detected_language_code"),
            "gemini_time": float(result.get("gemini_time") or 0.0),
        }

    for i in range(0, len(ordered), batch_size):
        batch = ordered[i : i + batch_size]
        if len(batch) == 1:
            chunk = batch[0]
            try:
                result = _transcribe_chunk_item(0, chunk)
                language_codes.append(result["language_code"])
                if result["transcript"]:
                    final_transcript.append(
                        f"{person_map.get(chunk['speaker'], chunk['speaker'])} - {result['transcript']}"
                    )
                if result["output"]:
                    final_output.append(
                        f"{person_map.get(chunk['speaker'], chunk['speaker'])} - {result['output']}"
                    )
                total_gemini_time += result["gemini_time"]
            except Exception as e:
                debug_print(f"Failed chunk {chunk.get('chunk_path')}")
                debug_print(str(e))
                final_transcript.append(SKIPPED_CHUNK_MESSAGE)
                final_output.append(SKIPPED_CHUNK_MESSAGE)
            continue

        results = [None] * len(batch)
        futures = {}
        with ThreadPoolExecutor(max_workers=min(concurrency, len(batch))) as executor:
            for batch_index, chunk in enumerate(batch):
                futures[executor.submit(_transcribe_chunk_item, batch_index, chunk)] = batch_index

            for future in as_completed(futures):
                batch_index = futures[future]
                chunk = batch[batch_index]
                try:
                    result = future.result()
                    results[batch_index] = result
                except Exception as e:
                    debug_print(f"Failed chunk {chunk.get('chunk_path')}")
                    debug_print(str(e))
                    results[batch_index] = {
                        "transcript": None,
                        "output": None,
                        "language_code": None,
                        "gemini_time": 0.0,
                    }

        for batch_index, chunk in enumerate(batch):
            result = results[batch_index]
            if result and result["transcript"]:
                speaker_id = chunk["speaker"]
                person_name = person_map.get(speaker_id, speaker_id)
                final_transcript.append(f"{person_name} - {result['transcript']}")
                final_output.append(f"{person_name} - {result['output']}")
            else:
                final_transcript.append(SKIPPED_CHUNK_MESSAGE)
                final_output.append(SKIPPED_CHUNK_MESSAGE)
            language_codes.append(result["language_code"])
            total_gemini_time += float(result["gemini_time"] or 0.0)

    return {
        "transcript": "\n\n".join(final_transcript),
        "output": "\n\n".join(final_output),
        "detected_language_code": _pick_detected_language(language_codes),
        "gemini_time": total_gemini_time,
    }

def get_number_of_speakers(file_path: str) -> int:
    """Estimate speaker count using the same normalization + diarization path."""
    norm, cleanup = build_normalized_audio_path(file_path)
    try:
        segments = diarize_segments(norm)
    finally:
        cleanup()
    return len({s["speaker"] for s in segments})
