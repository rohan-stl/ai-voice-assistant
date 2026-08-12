from voice_service import speech_to_text

text = speech_to_text("test_audio.mp3")

print("Transcription:")
print(text)