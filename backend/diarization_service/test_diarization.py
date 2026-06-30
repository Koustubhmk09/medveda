from speaker_diarization import (
    build_normalized_audio_path,
    build_person_map_by_talk_time,
    diarize_segments,
    prepare_transcription_segments,
    split_audio_by_speaker,
    transcribe_speaker_chunks,
)

audio_path = "../audio.mp3"

norm, cleanup = build_normalized_audio_path(audio_path)
try:
    raw = diarize_segments(norm)
    segments = prepare_transcription_segments(raw)
    person_map = build_person_map_by_talk_time(segments)
    chunks = split_audio_by_speaker(norm, segments)
finally:
    cleanup()

final_output = transcribe_speaker_chunks(chunks, person_map=person_map, batch_size=3)

print("\n")
print("=" * 60)
print("FINAL TRANSCRIPT")
print("=" * 60)
print("\n")
print(final_output)
