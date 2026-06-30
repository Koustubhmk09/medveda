import os
import time
import json
import mimetypes
from datetime import datetime
from dotenv import load_dotenv
from google.genai import types
from src.config_audio.languages import SUPPORTED_GEMINI_LANGUAGES
from src.services.gemini_key_manager import generate_content_with_rotation

BASE_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        ".."
    )
)

load_dotenv(
    os.path.join(BASE_DIR, ".env")
)

GEMINI_MODEL_NAME = os.getenv("GEMINI_AUDIO_MODEL", "gemini-2.5-flash")

os.makedirs("logs", exist_ok=True)
os.makedirs("uploads", exist_ok=True)

def get_language_list():
    return [
        {
            "code": code,
            "name": name
        }
        for code, name in SUPPORTED_GEMINI_LANGUAGES.items()
    ]

def normalize_language(language: str | None) -> str | None:
    if not language or language.strip().lower() == "auto":
        return None

    lang = language.strip().lower()
    if lang in SUPPORTED_GEMINI_LANGUAGES:
        return lang

    for code, name in SUPPORTED_GEMINI_LANGUAGES.items():
        if name.lower() == lang:
            return code
        
    return None  

def clean_text(text: str) -> str:
    if not text:
        return ""
    return text.strip()

def contains_indic_script(text: str) -> bool:
    return any(
        "\u0900" <= char <= "\u0d7f"
        for char in text
    )

def output_needs_translation_fix(
    transcript: str,
    output_text: str,
    detected_language_code: str | None,
    target_language_code: str | None,
) -> bool:
    if not target_language_code or not detected_language_code:
        return False
    if target_language_code == detected_language_code:
        return False

    if clean_text(transcript) == clean_text(output_text):
        return True

    if target_language_code == "en" and contains_indic_script(output_text):
        return True

    return False

def call_gemini(prompt: str, preserve_format: bool = False) -> str:
    response = generate_content_with_rotation(
        model=GEMINI_MODEL_NAME,
        contents=prompt,
    )
    if preserve_format:
        text = (response.text or "").strip()
    else:
        text = clean_text(response.text or "")
    if not text:
        raise ValueError("Gemini returned an empty response. Please try again with clearer audio or a shorter clip.")
    return text

def get_audio_mime_type(file_path: str) -> str:
    extension = os.path.splitext(file_path)[1].lower()
    if extension == ".webm":
        return "audio/webm"

    mime_type, _ = mimetypes.guess_type(file_path)
    if mime_type:
        return mime_type

    return {
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".ogg": "audio/ogg",
        ".flac": "audio/flac",
        ".webm": "audio/webm",
    }.get(extension, "audio/mpeg")

def strip_json_markdown(raw_text: str) -> str:
    """Extracts the innermost or cleanest valid JSON block available."""
    if not raw_text:
        return ""
    
    # Try to find content between ```json and ```
    import re
    json_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', raw_text, re.DOTALL | re.IGNORECASE)
    if json_match:
        return json_match.group(1).strip()
    
    # Surgical fallback: find the first { and the last }
    first_brace = raw_text.find('{')
    last_brace = raw_text.rfind('}')
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        return raw_text[first_brace:last_brace+1].strip()
        
    return raw_text.strip()

def normalize_detected_language_code(language_code: str | None) -> str | None:
    language_code = clean_text(str(language_code or "")).lower() or None
    if language_code not in SUPPORTED_GEMINI_LANGUAGES:
        return None
    return language_code

def build_audio_part(file_path: str) -> types.Part:
    with open(file_path, "rb") as audio_file:
        return types.Part.from_bytes(
            data=audio_file.read(),
            mime_type=get_audio_mime_type(file_path),
        )

def generate_gemini_audio_content(file_path: str, prompt: str):
    return generate_content_with_rotation(
        model=GEMINI_MODEL_NAME,
        contents=[prompt, build_audio_part(file_path)],
    )

def parse_gemini_audio_task_response(raw_text: str) -> tuple[str, str, str | None]:
    text = strip_json_markdown(raw_text)

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text, text, None

    transcript = clean_text(str(data.get("transcript", "")))
    output = clean_text(str(data.get("output_text", ""))) or transcript
    return transcript, output, normalize_detected_language_code(data.get("language_code"))

def get_language_name(code: str) -> str:
    return SUPPORTED_GEMINI_LANGUAGES.get(code, code)

def build_gemini_audio_task_prompt(
    task: str,
    language: str | None = None,
    target_language_name: str | None = None,
) -> str:
    language_hint = ""
    if language:
        language_hint = f"The expected spoken language in the audio is {get_language_name(language)}. "

    # 1. Rules for the "transcript" field (RAW SIDE)
    transcript_rules = (
        "MANDATORY RULES FOR THE 'transcript' FIELD: "
        "1. Always contain the exact spoken words in the ORIGINAL language of the audio. "
        "2. Diarize every turn: If one speaker, label as 'Person - '. If multiple, use 'Person 1 -', 'Person 2 -', etc. "
        "3. Every speaker change must start a NEW line. Never merge turns into one paragraph."
    )

    # 2. Rules for the "output_text" field (TASK SIDE)
    if task == "Summary":
        task_instruction = (
            f"Provide an accurate summary of the conversation in {target_language_name or 'the original language'}. "
            "Organize by logical sections. Do not use Person labels unless necessary for clarity."
        )
    elif task == "Keywords":
        task_instruction = (
            f"Extract only the most important keywords and concepts in {target_language_name or 'the original language'}. "
            "Return them as a clean comma-separated list."
        )
    else: # Full Transcript
        task_instruction = (
            f"Provide a full, verbatim transcription of the entire audio in {target_language_name or 'the original language'}. "
            "You MUST apply the same diarization rules (Person labels + new lines) as the transcript field."
        )

    return (
        "Analyze the audio and process it into the required JSON format. "
        f"{language_hint}\n\n"
        f"{transcript_rules}\n"
        f"RULES FOR THE 'output_text' FIELD: {task_instruction}\n\n"
        "TECHNICAL REQUIREMENTS: "
        "1. Return valid JSON only. "
        "2. CRITICAL: Escape all newlines as \\n. DO NOT use literal newlines inside JSON strings. "
        "3. Do not add any conversational filler or preambles outside the JSON.\n\n"
        'Return JSON in this exact shape: {"transcript":"...","language_code":"...","output_text":"..."}'
    )

def process_audio_with_gemini_direct(
    file_path: str,
    task: str,
    language: str | None = None,
    target_language_name: str | None = None,
) -> tuple[str, str, str | None]:
    prompt = build_gemini_audio_task_prompt(task, language, target_language_name)
    response = generate_gemini_audio_content(file_path, prompt)
    transcript, output_text, language_code = parse_gemini_audio_task_response(response.text or "")
    if not transcript:
        raise ValueError("Gemini audio transcription returned an empty response.")
    if not output_text:
        raise ValueError("Gemini audio processing returned an empty response.")
    return transcript, output_text, language_code

def build_diarization_chunk_prompt() -> str:
    """Prompt for one-speaker diarization chunks (reduces cross-speaker bleed in STT)."""
    return (
        "This audio clip is a short excerpt from a multi-speaker conversation and "
        "contains ONLY ONE speaker. Transcribe exactly what that single speaker says. "
        "Do not include speech from any other person, crosstalk, or background voices. "
        "Do not invent or complete sentences. Preserve the original spoken language. "
        "Return valid JSON only in this exact shape: "
        '{"transcript":"...","language_code":"...","output_text":"..."} '
        "Set output_text equal to transcript. "
        "Use ISO 639-1 language_code such as en, de, hi, mr, fr, es."
    )

def process_audio_diarization_chunk(file_path: str) -> dict:
    """
    Transcribe a single-speaker diarization chunk (used from diarization subprocess).
  Returns transcript_text, output_text, detected_language_code, and timing fields.
    """
    start_transcribe = time.perf_counter()
    prompt = build_diarization_chunk_prompt()
    response = generate_gemini_audio_content(file_path, prompt)
    transcript, output_text, language_code = parse_gemini_audio_task_response(
        response.text or ""
    )
    if not transcript:
        raise ValueError("Gemini chunk transcription returned an empty response.")
    elapsed = time.perf_counter() - start_transcribe
    return {
        "transcript_text": transcript,
        "output_text": output_text or transcript,
        "detected_language_code": language_code,
        "transcription_time": elapsed,
        "gemini_time": elapsed,
    }

def translate_text(text: str, target_language_name: str) -> str:
    """
    Translates the given text into the target language using Gemini.
    target_language_name should be a human-readable name, e.g. 'Marathi', 'Hindi', 'French'.
    """
    prompt = (
        f"Translate the following text into {target_language_name}. "
        "Return ONLY the translated text without any explanation. "
        "Preserve the exact conversation structure, speaker labels, order, "
        "blank lines, and line breaks. "
        "If a line starts with a speaker label like 'Person 1 -' or 'Person - ', keep that "
        "label exactly as-is and translate only the spoken text after the dash. "
        "Do not merge speaker turns into one paragraph. "
        "Do not renumber, rename, remove, or translate speaker labels.\n\n"
        f"{text}"
    )
    return call_gemini(prompt, preserve_format=True)

def process_text_with_gemini(
    transcript_text: str,
    task: str,
    target_language_name: str | None = None,
) -> str:
    """
    Process already transcribed
    speaker transcript using
    the SAME Gemini logic as
    original system.
    """

    # Full transcript
    if task == "Full Transcript":
        result = transcript_text

    elif task == "Summary":

        prompt = (
            "Summarize the following "
            "conversation clearly:\n\n"
            f"{transcript_text}"
        )

        result = call_gemini(
            prompt
        )

    elif task == "Keywords":

        prompt = (
            "Extract important "
            "keywords from this "
            "conversation.\n\n"
            f"{transcript_text}"
        )

        result = call_gemini(
            prompt
        )

    else:
        result = transcript_text

    # USE EXISTING TRANSLATION FLOW
    if (
        target_language_name
        and target_language_name
        != "auto"
    ):

        result = translate_text(
            result,
            target_language_name
        )

    return result

def get_language_name(code: str) -> str:
    return SUPPORTED_GEMINI_LANGUAGES.get(code, code)

def save_log(entry: str):
    with open("logs/log.txt", "a", encoding="utf-8") as f:
        f.write(entry + "\n")

def build_process_response(
    file_path: str,
    task: str,
    transcript_text: str,
    output_text: str,
    detected_lang_code: str,
    target_language_name: str | None,
    translated: bool,
    transcription_time: float,
    gemini_time: float,
) -> dict:
    detected_lang_name = get_language_name(detected_lang_code)
    token_count = len(transcript_text.split())

    log_entry = f"""
Time: {datetime.now()}
File: {file_path}
Task: {task}
Engine: Gemini direct
Detected Language: {detected_lang_name} ({detected_lang_code})
Language Confidence: N/A
Gemini Audio Fallback: True
Target Language: {target_language_name or 'N/A'}
Translated: {translated}
Tokens: {token_count}
Transcription Time: {transcription_time:.2f}sec
Gemini Time: {gemini_time:.2f}sec
Output: {output_text[:150]}...
----------------------------------------"""
    save_log(log_entry)

    return {
        "transcript_text": transcript_text,
        "output_text": output_text,
        "detected_language": detected_lang_name,
        "detected_language_code": detected_lang_code,
        "language_confidence": 1.0,
        "used_gemini_transcription": True,
        "transcription_time": transcription_time,
        "gemini_time": gemini_time,
        "token_count": token_count,
        "translated": translated,
        "target_language_name": target_language_name,
    }

def process_audio(
    file_path: str,
    task: str,
    language: str | None = None,
    target_language: str | None = None,
) -> dict:
    """
    Transcribes the audio at file_path, processes it based on task,
    and optionally translates the output into target_language.

    Args:
        file_path:       Path to the audio file.
        task:            One of "Full Transcript", "Summary", "Keywords".
        language:        Gemini language code to hint (or None for auto-detect).
        target_language: Gemini language code for the output language (or None / "auto").

    Returns:
        dict with keys: transcript_text, output_text, detected_language,
                        transcription_time, gemini_time, token_count,
                        translated, target_language_name.
    """
    start_transcribe = time.perf_counter()

    norm_target = normalize_language(target_language)
    target_language_name = get_language_name(norm_target) if norm_target else None

    cleaned, output_text, detected_lang_code = process_audio_with_gemini_direct(
        file_path=file_path,
        task=task,
        language=language,
        target_language_name=target_language_name,
    )
    transcription_time = time.perf_counter() - start_transcribe
    gemini_time = transcription_time
    detected_lang_code = detected_lang_code or language or "unknown"
    translated = bool(norm_target and norm_target != detected_lang_code)

    if output_needs_translation_fix(cleaned, output_text, detected_lang_code, norm_target):
        start_translate = time.perf_counter()
        output_text = translate_text(output_text, target_language_name)
        gemini_time += time.perf_counter() - start_translate

    return build_process_response(
        file_path=file_path,
        task=task,
        transcript_text=cleaned,
        output_text=output_text,
        detected_lang_code=detected_lang_code,
        target_language_name=target_language_name,
        translated=translated,
        transcription_time=transcription_time,
        gemini_time=gemini_time,
    )

def process_multi_speaker_audio(
    file_path: str,
    task: str,
    target_language_name: str | None = None,
):
    """
    Run diarization in a separate venv (subprocess), then Gemini for text tasks.
    Returns both the API response payload and internal timing details.
    """
    import json
    import os
    import subprocess
    import sys
    import time

    project_root = os.path.abspath(
        os.path.join(
            os.path.dirname(__file__),
            "..",
            "..",
        )
    )

    diarization_folder = os.path.join(
        project_root,
        "diarization_service",
    )

    if os.name == "nt":
        python_exe = os.path.join(
            diarization_folder,
            "venv",
            "Scripts",
            "python.exe",
        )
    else:
        python_exe = os.path.join(
            diarization_folder,
            "venv",
            "bin",
            "python",
        )

    script_path = os.path.join(
        diarization_folder,
        "run_diarization.py",
    )

    start_sub = time.perf_counter()
    absolute_file_path = os.path.abspath(
         file_path
    )
    diarization_timeout_raw = os.getenv("DIARIZATION_SUBPROCESS_TIMEOUT_SECONDS", "").strip()
    diarization_timeout = None
    if diarization_timeout_raw:
        try:
            diarization_timeout = max(300, int(diarization_timeout_raw))
        except ValueError:
            diarization_timeout = None
    try:
        result = subprocess.run(
            [python_exe, script_path, absolute_file_path],
            stdout=subprocess.PIPE,
            stderr=None,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=diarization_folder,
            env=os.environ.copy(),
            timeout=diarization_timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(
            f"Diarization subprocess timed out after {diarization_timeout}s."
        ) from exc
    subprocess_time = time.perf_counter() - start_sub

    if result.returncode != 0:
        raise Exception(
            f"Diarization subprocess failed (code {result.returncode}). "
            "Check the server terminal for diarization logs."
        )

    if diarization_folder not in sys.path:
        sys.path.insert(0, diarization_folder)
    try:
        import importlib
        diar_io = importlib.import_module("diarization_io")
        _, speaker_result = diar_io.parse_subprocess_stdout(result.stdout or "")
    finally:
        if sys.path and sys.path[0] == diarization_folder:
            sys.path.pop(0)

    speaker_transcript = speaker_result.get(
        "speaker_transcript",
        "",
    )
    speaker_analysis = speaker_result.get("speaker_analysis") or {"mode": "multi"}
    speaker_mode = speaker_analysis.get("mode", "fallback")
    _n = speaker_result.get("num_speakers", 0)
    try:
        num_sp = int(_n) if _n is not None else 0
    except (TypeError, ValueError):
        num_sp = 0

    timing_info = speaker_result.get("_timing", {})
    timing_info.setdefault("diarization", subprocess_time)
    timing_info.setdefault("chunk_creation", 0.0)
    timing_info.setdefault("gemini", 0.0)
    timing_info.setdefault("translation", 0.0)

    if speaker_mode in ("single", "fallback") or not speaker_transcript.strip():
        print("[Pipeline]")
        print("Using normal transcription pipeline")
        direct_result = process_audio(
            file_path=file_path,
            task=task,
            language=None,
            target_language=target_language_name,
        )
        direct_result["transcription_time"] = (
            direct_result.get("transcription_time", 0.0) + subprocess_time
        )
        direct_result["num_speakers"] = 1 if speaker_mode == "single" else 0
        direct_result["speaker_analysis"] = speaker_analysis
        direct_result["diarization_mode"] = speaker_mode
        return direct_result, timing_info

    tgt_raw = (target_language_name or "").strip()
    if not tgt_raw or tgt_raw.lower() in ("none", "auto"):
        target_for_gemini = None
    else:
        target_for_gemini = tgt_raw

    start_g = time.perf_counter()
    output_text = process_text_with_gemini(
        transcript_text=speaker_transcript,
        task=task,
        target_language_name=target_for_gemini,
    )
    gemini_time = time.perf_counter() - start_g

    timing_info["gemini"] = max(timing_info.get("gemini", 0.0), gemini_time)

    lang_code = speaker_result.get("detected_language_code")
    if lang_code:
        detected_lang_code = normalize_detected_language_code(lang_code) or lang_code
        detected_label = get_language_name(detected_lang_code)
    elif num_sp <= 0:
        detected_lang_code = "unknown"
        detected_label = "Unknown"
    else:
        detected_lang_code = "unknown"
        detected_label = "Unknown"

    norm_target = normalize_language(target_language_name)
    translated = bool(
        target_for_gemini
        and detected_lang_code != "unknown"
        and norm_target
        and norm_target != detected_lang_code
    )

    response_payload = {
        "transcript_text": speaker_transcript,
        "output_text": output_text,
        "detected_language": detected_label,
        "detected_language_code": detected_lang_code,
        "language_confidence": 1.0 if lang_code else None,
        "transcription_time": subprocess_time,
        "gemini_time": gemini_time,
        "token_count": len(speaker_transcript.split()),
        "translated": translated,
        "target_language_name": target_for_gemini,
        "num_speakers": num_sp,
        "speaker_analysis": speaker_analysis,
        "diarization_mode": speaker_mode,
    }

    return response_payload, timing_info

def can_answer_from_transcript(
    question: str,
    transcript: str,
) -> bool:
    """
    Ask Gemini whether the question
    can be answered using transcript.
    """

    prompt = f"""
You are a transcript analyzer.

Question:
{question}

Transcript:
{transcript}

Can this question be answered
from the transcript?

Return ONLY:
YES
or
NO
"""

    response = call_gemini(prompt)

    return (
        "YES" in response.upper()
    )


def answer_from_transcript(
    question: str,
    transcript: str,
) -> str:
    """
    Answer using transcript only.
    """

    prompt = f"""
You are an assistant.

Answer ONLY using the transcript.

If transcript does not contain
the answer, say:
NOT_FOUND

Question:
{question}

Transcript:
{transcript}

Return ONLY the answer.
"""

    return call_gemini(prompt)


def web_answer(
    question: str,
) -> str:
    """
    Answer using web knowledge.
    """

    prompt = f"""
Answer this question clearly
and in simple language:

{question}
"""

    return call_gemini(prompt)


def answer_user_question(
    question: str,
    transcript: str,
):
    """
    Smart QA logic.

    1. Check transcript
    2. If found -> transcript answer
    3. Else -> web answer
    """

    can_answer = (
        can_answer_from_transcript(
            question=question,
            transcript=transcript
        )
    )

    if can_answer:

        answer = (
            answer_from_transcript(
                question=question,
                transcript=transcript
            )
        )

        if (
            "NOT_FOUND"
            not in answer.upper()
        ):
            return {
                "answer": answer,
                "source": "audio"
            }

    web_result = web_answer(
        question
    )

    return {
        "answer":
        (
            "This topic was not "
            "discussed in uploaded "
            "audio.\n\n"
            + web_result
        ),
        "source": "web"
    }
