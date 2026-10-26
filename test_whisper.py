
from pathlib import Path
from dotenv import load_dotenv
from groq import Groq
import os

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")

client = Groq(
    api_key=os.getenv("GROQ_API_KEY"),
    timeout=60.0,
    max_retries=0,
)

audio_path = input(
    "C:/Users/Rocky computer/Downloads/adam_response.mp3"
).strip().strip('"')

try:
    with open(audio_path, "rb") as audio_file:
        result = client.audio.transcriptions.create(
            file=(Path(audio_path).name, audio_file.read()),
            model="whisper-large-v3-turbo",
            response_format="text",
        )

    print("\nTranscription successful:")
    print(result)

except Exception as exc:
    print("\nWhisper test failed:")
    print(type(exc).__name__)
    print(repr(exc))
