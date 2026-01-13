import os
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Text, Date, text, inspect
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Database configuration - Use PostgreSQL
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise ValueError("DATABASE_URL not found in environment variables. Please set it in .env.")

# Create the engine
# Note: check_same_thread is only used for SQLite
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class Claim(Base):
    __tablename__ = "claims"

    id = Column(Integer, primary_key=True, index=True)
    email_uid = Column(String(255), unique=True, index=True)
    email_date = Column(DateTime)
    patient_name = Column(String(255))
    uhid_mrn = Column(String(100))
    insurance_company = Column(String(255))
    tpa_name = Column(String(255))  # Third Party Administrator


    claim_number = Column(String(100), unique=True)
    claim_status = Column(String(50))  # Approved, Rejected, etc.
    
    # Date fields
    claim_date = Column(String(50))  # Admission/Claim submission date
    settlement_date = Column(String(50))  # Payment processed/settlement date
    submitted_date = Column(String(50))  # DEPRECATED: Keeping for backward compatibility during migration
    claim_type = Column(String(50))  # Cashless, Reimbursement, etc.
    
    # Amounts
    approved_amount = Column(Float, default=0.0)
    settled_amount = Column(Float, default=0.0)
    rejected_amount = Column(Float, default=0.0)
    outstanding_amount = Column(Float, default=0.0)
    total_bill_amount = Column(Float, default=0.0)
    claim_amount = Column(Float, default=0.0)  # Amount submitted to insurance (may differ from total bill)
    patient_payable_amount = Column(Float, default=0.0)
    insurance_coverage_percent = Column(Float, default=0.0)
    balance_amount = Column(Float, default=0.0)

    remarks = Column(Text)
    tat_followup_date = Column(String(50))
    
    processed_at = Column(DateTime, default=datetime.utcnow)

class ClaimHistory(Base):
    __tablename__ = "claim_history"

    id = Column(Integer, primary_key=True, index=True)
    claim_number = Column(String(100), index=True) # Link to Claim logic
    
    email_uid = Column(String(255))
    email_date = Column(DateTime)
    
    amount_received = Column(Float, default=0.0)
    total_settled_so_far = Column(Float, default=0.0)
    
    status = Column(String(50))
    remarks = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)


class ProcessingLog(Base):
    __tablename__ = "processing_logs"

    id = Column(Integer, primary_key=True, index=True)
    email_subject = Column(String(500))
    status = Column(String(50)) # Success, Failed
    error_message = Column(Text)
    timestamp = Column(DateTime, default=datetime.utcnow)

def check_and_migrate():
    """Auto-migration to add missing columns."""
    try:
        inspector = inspect(engine)
        if inspector.has_table("claims"):
            columns = [col['name'] for col in inspector.get_columns('claims')]
            
            # Migration 1: Add tpa_name column if missing
            if 'tpa_name' not in columns:
                print("[MIGRATION] Adding missing column 'tpa_name' to claims table...")
                with engine.connect() as conn:
                    conn.execute(text("ALTER TABLE claims ADD COLUMN tpa_name VARCHAR(255)"))
                    conn.commit()
                print("[MIGRATION] Success.")
            
            # Migration 2: Add claim_amount column if missing
            if 'claim_amount' not in columns:
                print("[MIGRATION] Adding missing column 'claim_amount' to claims table...")
                with engine.connect() as conn:
                    conn.execute(text("ALTER TABLE claims ADD COLUMN claim_amount FLOAT DEFAULT 0.0"))
                    conn.commit()
                print("[MIGRATION] Success.")

            # Migration 3: Fix existing Settled claims balance -> 0
            print("[MIGRATION] Ensuring all 'Settled' claims have 0 balance...")
            with engine.connect() as conn:
                # We also update patient_payable_amount to reflect total_bill - settled_amount for these claims
                conn.execute(text("UPDATE claims SET balance_amount = 0, outstanding_amount = 0 WHERE claim_status = 'Settled'"))
                conn.execute(text("UPDATE claims SET patient_payable_amount = total_bill_amount - settled_amount WHERE claim_status = 'Settled' AND total_bill_amount > settled_amount"))
                
                # Cleanup: Delete placeholder records that are TRULY junk
                # Preserve if they have a Claim Number OR any financial amount
                print("[MIGRATION] Cleaning up empty/anonymous junk records...")
                conn.execute(text("""
                    DELETE FROM claims 
                    WHERE patient_name LIKE '%Hospital Payment / Bulk Claim%' 
                    AND (claim_number IS NULL OR claim_number = '')
                    AND (settled_amount = 0 OR settled_amount IS NULL)
                    AND (total_bill_amount = 0 OR total_bill_amount IS NULL)
                """))
                
                conn.commit()
            print("[MIGRATION] Success.")
        
        # Backfill: If TPA is NULL, copy Insurance Company
        with engine.connect() as conn:
             # Standard SQL for backfill
             conn.execute(text("UPDATE claims SET tpa_name = insurance_company WHERE tpa_name IS NULL OR tpa_name = ''"))
             # Backfill claim_amount with total_bill_amount if not set
             conn.execute(text("UPDATE claims SET claim_amount = total_bill_amount WHERE claim_amount IS NULL OR claim_amount = 0"))
             conn.commit()

    except Exception as e:
        print(f"[MIGRATION WARNING] {e}")

def init_db():
    try:
        Base.metadata.create_all(bind=engine)
        check_and_migrate()
    except Exception as e:
        print("\n" + "="*50)
        print("DATABASE CONNECTION ERROR")
        print("="*50)
        print(f"Failed to connect to PostgreSQL: {e}")
        print("\nPlease ensure:")
        print("1. Your PostgreSQL server is RUNNING (check Services or pgAdmin).")
        print("2. The database 'insurance_db' exists.")
        print("3. Your credentials in .env are correct.")
        print("="*50 + "\n")
        raise e

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

if __name__ == "__main__":
    print("Initializing Database...")
    init_db()
    print("Database Initialized.")

