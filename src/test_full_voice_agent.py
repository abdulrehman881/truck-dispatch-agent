
import os
from pathlib import Path

import sounddevice as sd
from scipy.io.wavfile import write
from dotenv import load_dotenv
from groq import Groq
from elevenlabs.client import ElevenLabs


# Load API keys from the project root
PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")

groq_key = os.getenv("GROQ_API_KEY")
elevenlabs_key = os.getenv("ELEVENLABS_API_KEY")

if not groq_key or not elevenlabs_key:
    raise ValueError("Check GROQ_API_KEY and ELEVENLABS_API_KEY in .env")

groq = Groq(api_key=groq_key)
elevenlabs = ElevenLabs(api_key=elevenlabs_key)

OUTPUT_DIR = PROJECT_ROOT / "output"
OUTPUT_DIR.mkdir(exist_ok=True)


def record_audio(seconds=6, sample_rate=16000):
    """Record a short voice message from your microphone."""
    print(f"\nSpeak now! Recording for {seconds} seconds...")

    recording = sd.rec(
        int(seconds * sample_rate),
        samplerate=sample_rate,
        channels=1,
        dtype="int16",
    )
    sd.wait()

    audio_path = OUTPUT_DIR / "recorded_message.wav"
    write(str(audio_path), sample_rate, recording)

    print(f"Recording saved: {audio_path}")
    return audio_path


def transcribe_audio(audio_path):
    """Convert the recorded audio into text using Groq."""
    print("\nTranscribing your voice...")

    with open(audio_path, "rb") as audio_file:
        transcription = groq.audio.transcriptions.create(
            file=(audio_path.name, audio_file.read()),
            model="whisper-large-v3-turbo",
            response_format="text",
        )

    if isinstance(transcription, str):
        text = transcription.strip()
    else:
        text = transcription.text.strip()

    if not text:
        raise ValueError("No speech was recognized. Please try again.")

    print(f"You said: {text}")
    return text


def generate_response(user_text):
    """Generate Adam's reply using Groq."""
    response = groq.chat.completions.create(
        model="openai/gpt-oss-20b",
        messages=[
            {
                "role": "system",
                "content": (
                    "You are Adam, a friendly truck dispatch assistant. "
                    "Speak naturally and keep replies concise. "
                    "Ask one question at a time. "
                    "Never invent carrier details, truck availability, "
                    "or load information. This is a local test, not a real call."
                ),
            },
            {"role": "user", "content": user_text},
        ],
        temperature=0.3,
    )

    reply = response.choices[0].message.content

    if not reply:
        raise ValueError("Groq returned an empty response.")

    reply = reply.strip()
    print(f"\nAdam: {reply}")
    return reply


def speak_response(text):
    """Convert Adam's reply into an MP3 using ElevenLabs."""
    print("\nGenerating speech...")

    audio = elevenlabs.text_to_speech.convert(
        text=text,
        voice_id="JBFqnCBsd6RMkjVDRZzb",
        model_id="eleven_flash_v2_5",
        output_format="mp3_44100_128",
    )

    audio_path = OUTPUT_DIR / "adam_response.mp3"

    with open(audio_path, "wb") as audio_file:
        for chunk in audio:
            audio_file.write(chunk)

    print(f"Adam's audio saved: {audio_path}")
    return audio_path


if __name__ == "__main__":
    wav_path = record_audio(seconds=6)
    recognized_text = transcribe_audio(wav_path)
    response_text = generate_response(recognized_text)
    speak_response(response_text)

    print("\nVoice pipeline test completed!")
