from src.db import init_db, SessionLocal, Claim
import os
from dotenv import load_dotenv

load_dotenv()

def test_connection():
    url = os.getenv("DATABASE_URL")
    print(f"Testing connection to: {url}")
    try:
        init_db()
        print("Successfully connected and initialized PostgreSQL schema!")
        
        session = SessionLocal()
        count = session.query(Claim).count()
        print(f"Current claim count: {count}")
        session.close()
    except Exception as e:
        print(f"Connection failed: {e}")
        print("\nMake sure:")
        print("1. Your PostgreSQL server is running.")
        print("2. The database exists (default name: 'insurance_db').")
        print("3. Your credentials in .env are correct.")

if __name__ == "__main__":
    test_connection()
