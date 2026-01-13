from src.db import SessionLocal, Claim
from src.assistant import ask_ai, generate_sql
from sqlalchemy import text

def debug_suguna():
    db = SessionLocal()
    try:
        # 1. Check Actual Data
        print("\n--- 1. DATABASE CONTENT ---")
        results = db.execute(text("SELECT patient_name FROM claims WHERE patient_name ILIKE '%Suguna%'")).fetchall()
        if results:
            for r in results:
                print(f"Found in DB: '{r[0]}'")
        else:
            print("CRITICAL: No patient found with 'Suguna' in DB!")

        # 2. Check SQL Generation
        query = "Give me details of K Suguna"
        print(f"\n--- 2. AI SQL GENERATION for '{query}' ---")
        sql, error = generate_sql(query)
        if error:
            print(f"Error generating SQL: {error}")
        else:
            print(f"Generated SQL: {sql}")

        # 3. Check Full execution
        print(f"\n--- 3. FULL EXECUTION ---")
        answer = ask_ai(query, db)
        print(f"Final Answer: {answer}")

    finally:
        db.close()

if __name__ == "__main__":
    debug_suguna()
