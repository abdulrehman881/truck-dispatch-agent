
import os
from pathlib import Path

from dotenv import load_dotenv
from groq import Groq
from elevenlabs.client import ElevenLabs


PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")

groq_api_key = os.getenv("GROQ_API_KEY")
elevenlabs_api_key = os.getenv("ELEVENLABS_API_KEY")

if not groq_api_key:
    raise ValueError("GROQ_API_KEY is missing from the project .env file")

if not elevenlabs_api_key:
    raise ValueError("ELEVENLABS_API_KEY is missing from the project .env file")


groq_client = Groq(api_key=groq_api_key)
elevenlabs_client = ElevenLabs(api_key=elevenlabs_api_key)


SYSTEM_PROMPT = """
You are Adam, a friendly AI assistant for a truck dispatching company.

You speak clearly and naturally.
Keep your responses concise and conversational.
Ask one question at a time.
Never invent carrier information, truck availability, or load details.
For this test, do not claim that a real call or booking has occurred.
"""


def generate_response(user_message: str) -> str:
    """Generate a reply using Groq."""

    response = groq_client.chat.completions.create(
        model="openai/gpt-oss-20b",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        temperature=0.3,
    )

    reply = response.choices[0].message.content

    if not reply:
        raise ValueError("Groq returned an empty response")

    return reply.strip()


def generate_speech(text: str) -> Path:
    """Convert the AI reply into an MP3 using ElevenLabs."""

    audio = elevenlabs_client.text_to_speech.convert(
        text=text,
        voice_id="JBFqnCBsd6RMkjVDRZzb",
        model_id="eleven_flash_v2_5",
        output_format="mp3_44100_128",
    )

    output_dir = PROJECT_ROOT / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    audio_path = output_dir / "voice_pipeline_test.mp3"

    with open(audio_path, "wb") as audio_file:
        for chunk in audio:
            audio_file.write(chunk)

    return audio_path


if __name__ == "__main__":
    test_message = (
        "A trucking company owner answers the phone. "
        "Greet them and ask whether they operate their own trucks."
    )

    print("\nGenerating a response with Groq...")
    reply = generate_response(test_message)
    print(f"\nAdam: {reply}")

    print("\nConverting the response into speech...")
    audio_path = generate_speech(reply)

    print(f"\nSuccess! Audio saved to: {audio_path}")
