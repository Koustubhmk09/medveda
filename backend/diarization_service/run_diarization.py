"""
CLI entry for the diarization venv.

Stdout protocol:
  1. Human-readable debug lines (segments, person map, transcription progress)
  2. Line: @@DIARIZATION_RESULT_JSON@@
  3. Single-line JSON payload (parsed by FastAPI parent)

Environment (optional): see Backend/.env for diarization tuning overrides.
"""

import shutil
import sys
import tempfile
import os
import time
from urllib.parse import urlparse

from diarization_io import debug_print, emit_result


def _drop_dead_local_proxy_env() -> None:
    """
    Some terminals/IDEs set proxy variables to 127.0.0.1:9 to intentionally
    disable outbound network access. Hugging Face then fails with ProxyError
    before it can load the diarization model. Drop only that sentinel proxy.
    """
    proxy_names = (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    )
    for name in proxy_names:
        value = os.environ.get(name, "").strip()
        if not value:
            continue
        parsed = urlparse(value)
        if parsed.hostname in {"127.0.0.1", "localhost"} and parsed.port == 9:
            os.environ.pop(name, None)


def main() -> None:
    if len(sys.argv) < 2:
        sys.stderr.write("Usage: python run_diarization.py <audio_file_path>\n")
        sys.exit(1)

    _drop_dead_local_proxy_env()

    audio_path = sys.argv[1]
    debug_print(f"=== DIARIZATION START: {audio_path} ===\n")

    from speaker_diarization import (
        analyze_speaker_distribution,
        build_normalized_audio_path,
        build_person_map_by_talk_time,
        diarize_segments,
        normalize_person_labels,
        prepare_transcription_segments,
        split_audio_by_speaker,
        transcribe_speaker_chunks,
    )

    start_total = time.perf_counter()
    start_norm = time.perf_counter()
    norm_path, cleanup_norm = build_normalized_audio_path(audio_path)
    normalization_time = time.perf_counter() - start_norm
    chunk_dir = tempfile.mkdtemp(prefix="diar_chunks_")

    try:
        start_diarization = time.perf_counter()
        raw_segments = diarize_segments(norm_path)
        diarization_time = time.perf_counter() - start_diarization
        segments = prepare_transcription_segments(raw_segments)
        speaker_analysis = analyze_speaker_distribution(segments)
        analysis_mode = speaker_analysis.get("mode", "fallback")
        num_speakers = int(speaker_analysis.get("speaker_count") or 0)
        detected_language_code = None

        if analysis_mode in ("single", "fallback"):
            payload = {
                "speaker_transcript": "",
                "output": "",
                "num_speakers": 1 if analysis_mode == "single" else 0,
                "detected_language_code": None,
                "speaker_analysis": speaker_analysis,
                "_timing": {
                    "normalization": normalization_time,
                    "diarization": diarization_time,
                    "chunk_creation": 0.0,
                    "gemini": 0.0,
                    "translation": 0.0,
                },
            }
        else:
            speaker_ids = set(speaker_analysis.get("speaker_ids") or [])
            if speaker_ids:
                segments = [s for s in segments if s["speaker"] in speaker_ids]
                debug_print(
                    "Filtered chunks to meaningful speakers: "
                    + ", ".join(sorted(speaker_ids))
                    + "\n"
                )
            person_map = build_person_map_by_talk_time(segments)
            chunk_start = time.perf_counter()
            chunks = split_audio_by_speaker(
                norm_path,
                segments,
                output_dir=chunk_dir,
            )
            chunk_creation_time = time.perf_counter() - chunk_start
            debug_print(f"Created {len(chunks)} audio chunks for transcription\n")
            gemini_start = time.perf_counter()
            result = transcribe_speaker_chunks(
                chunks,
                person_map=person_map,
                batch_size=3,
            )
            gemini_time = time.perf_counter() - gemini_start
            detected_language_code = result.get("detected_language_code")
            speaker_transcript = normalize_person_labels(
                result.get("transcript", "")
            )
            speaker_output = normalize_person_labels(
                result.get("output", "")
            )
            payload = {
                "speaker_transcript": speaker_transcript,
                "output": speaker_output,
                "num_speakers": num_speakers,
                "detected_language_code": detected_language_code,
                "speaker_analysis": speaker_analysis,
                "_timing": {
                    "normalization": normalization_time,
                    "diarization": diarization_time,
                    "chunk_creation": chunk_creation_time,
                    "gemini": gemini_time,
                    "translation": 0.0,
                },
            }

        debug_print("\n=== DIARIZATION COMPLETE ===\n")
    finally:
        cleanup_norm()
        shutil.rmtree(chunk_dir, ignore_errors=True)

    emit_result(payload)


if __name__ == "__main__":
    main()
