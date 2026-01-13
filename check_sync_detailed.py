import os
import sys

# Ensure project root is in path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.db import SessionLocal, ProcessingLog, Claim
from sqlalchemy import func
import os
from dotenv import load_dotenv

load_dotenv()

def check_sync_detailed():
    session = SessionLocal()
    try:
        total_claims = session.query(Claim).count()
        print(f"Total Claims in DB: {total_claims}")
        
        print("\n--- Summary (Last 15 Mins) ---")
        recent_time = datetime.utcnow() - timedelta(minutes=15)
        summary = session.query(ProcessingLog.status, func.count(ProcessingLog.id)).filter(ProcessingLog.timestamp >= recent_time).group_by(ProcessingLog.status).all()
        for status, count in summary:
            print(f"{status}: {count}")
            
        print("\n--- Latest 20 Logs (Any time) ---")
        logs = session.query(ProcessingLog).order_by(ProcessingLog.timestamp.desc()).limit(20).all()
        for log in logs:
            err = f" | Error: {log.error_message}" if log.error_message else ""
            print(f"[{log.timestamp}] {log.status} | {log.email_subject[:60]}{err}")
            
    except Exception as e:
        print(f"Error: {e}")
    finally:
        session.close()

if __name__ == "__main__":
    check_sync_detailed()
