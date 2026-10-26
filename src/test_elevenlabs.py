
import os
from dotenv import load_dotenv
from elevenlabs.client import ElevenLabs
from elevenlabs.play import play

load_dotenv()

api_key = os.getenv("ELEVENLABS_API_KEY")

if not api_key:
    raise ValueError("ELEVENLABS_API_KEY is missing from .env")

client = ElevenLabs(api_key=api_key)

audio = client.text_to_speech.convert(
    text="Hello! This is the Truck Dispatch AI voice assistant.",
    voice_id="JBFqnCBsd6RMkjVDRZzb",
    model_id="eleven_flash_v2_5",
    output_format="mp3_44100_128",
)

with open("elevenlabs_test.mp3", "wb") as f:
    for chunk in audio:
        f.write(chunk)

print("Audio saved to elevenlabs_test.mp3")

print("ElevenLabs voice test completed.")
