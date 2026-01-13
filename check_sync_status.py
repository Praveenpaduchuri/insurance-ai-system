from src.db import SessionLocal, ProcessingLog, Claim
import os
from dotenv import load_dotenv

load_dotenv()

def check_db_contents():
    session = SessionLocal()
    try:
        logs = session.query(ProcessingLog).order_by(ProcessingLog.timestamp.desc()).limit(20).all()
        print(f"--- Latest 20 Processing Logs ---")
        if not logs:
            print("No logs found.")
        for log in logs:
            print(f"[{log.timestamp}] Status: {log.status} | Subject: {log.email_subject[:50]}... | Error: {log.error_message}")
            
        claim_count = session.query(Claim).count()
        print(f"\nTotal Claims: {claim_count}")
        
    except Exception as e:
        print(f"Error checking DB: {e}")
    finally:
        session.close()

if __name__ == "__main__":
    check_db_contents()
