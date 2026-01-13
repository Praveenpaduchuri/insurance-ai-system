import os
import pandas as pd
from sqlalchemy import text
from dotenv import load_dotenv

load_dotenv()

load_dotenv()

# Global for caching model
_LOCAL_MODEL = None

def get_local_llm():
    """Lazy load the GPT4All model for local assistant."""
    global _LOCAL_MODEL
    if _LOCAL_MODEL is not None:
        return _LOCAL_MODEL
    try:
        from gpt4all import GPT4All
        _LOCAL_MODEL = GPT4All("Meta-Llama-3-8B-Instruct.Q4_0.gguf", n_ctx=4096)
        return _LOCAL_MODEL
    except Exception as e:
        print(f"Error loading Local LLM: {e}")
        return None

def call_llm(prompt, system_instruction="You are a helpful assistant."):
    """
    Unified LLM caller with fallbacks: Local -> OpenAI -> Gemini.
    """
    # 0. Try Local LLM (Priority if enabled)
    USE_LOCAL_LLM = os.getenv("USE_LOCAL_LLM", "true").lower() == "true"
    if USE_LOCAL_LLM:
        try:
            model = get_local_llm()
            if model:
                full_prompt = f"### System:\n{system_instruction}\n\n### User:\n{prompt}\n\n### Assistant:\n"
                with model.chat_session():
                    response = model.generate(full_prompt, max_tokens=1024, temp=0.1)
                print("Local Assistant Success.")
                return response, None
        except Exception as e:
            print(f"Local Assistant Error: {e}")


    # 2. Try OpenAI
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    if OPENAI_API_KEY:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=OPENAI_API_KEY)
            completion = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_instruction},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.1
            )
            return completion.choices[0].message.content, None
        except Exception as e:
            print(f"OpenAI Assistant Error: {e}")


    return None, "No LLM providers available (check keys)."

# Database Schema Context for the LLM
SCHEMA_CONTEXT = """
You are an expert SQL assistant for a Hospital Claims Database (PostgreSQL).
The table name is `claims`.

Columns:
- id (Integer)
- patient_name (String)
- insurance_company (String)
- claim_status (String): Values can be 'Approved', 'Rejected', 'Pending', 'Queried', etc.
- approved_amount (Float): Amount approved by insurance.
- total_bill_amount (Float): Total hospital bill.
- balance_amount (Float): Outstanding amount.
- processed_at (DateTime)

Rules:
1. Return ONLY the SQL query. No markdown, no explanation.
2. Use standard PostgreSQL syntax.
3. If the user asks for "total outstanding", sum(balance_amount).
4. If the user asks for "count", count(*).
5. IMPORTANT: For all text comparisons (patient_name, insurance_company, etc.), ALWAYS use `ILIKE` for case-insensitive matching. Example: `patient_name ILIKE '%suguna%'`.
"""

def generate_sql(question):
    """
    Generates a SQL query from natural language with fallbacks.
    """
    prompt = f"User Question: {question}\n\nGenerate a PostgreSQL query. Important: Use ILIKE '%term%' for names to handle case/spelling differences. SQL Query:"
    sql, error = call_llm(prompt, system_instruction=SCHEMA_CONTEXT)
    
    if sql:
        sql = sql.replace("```sql", "").replace("```", "").strip()
    return sql, error

def generate_answer(question, data):
    """
    Generates a natural language answer based on the data retrieved with fallbacks.
    """
    prompt = f"""
    User Question: {question}
    Database Result: {data}
    
    Provide a improved, natural language answer. e.g. "The total is ₹50,000."
    Keep it concise.
    """
    answer, error = call_llm(prompt)
    return answer if answer else f"Could not generate natural answer: {error}"

def ask_ai(question, db_session):
    """
    Main orchestration function.
    """
    # 1. Generate SQL
    sql_query, error = generate_sql(question)
    if not sql_query:
        return f"System Error: {error}"
    
    # 2. Execute SQL
    try:
        result = db_session.execute(text(sql_query))
        # Fetch data
        rows = result.fetchall()
        if not rows:
            return f"No data found for your query. (Tried: `{sql_query}`)"
        
        # Convert to list/string for LLM
        data_str = str(rows)
        
        # 3. Generate Natural Language Answer
        answer = generate_answer(question, data_str)
        return answer

    except Exception as e:
        return f"Query Error: {e}. (SQL: {sql_query})"
