
import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv

# Load database settings from this project's .env file
project_root = Path(__file__).resolve().parent
load_dotenv(project_root / ".env")

try:
    with psycopg.connect(
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT"),
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        connect_timeout=5,
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT current_database(), current_user;"
            )
            database, username = cur.fetchone()

            print("SUCCESS: Python connected to PostgreSQL!")
            print(f"Database: {database}")
            print(f"Username: {username}")

except Exception as e:
    print("DATABASE CONNECTION FAILED")
    print(f"Error type: {type(e).__name__}")
    print(f"Error: {e}")
