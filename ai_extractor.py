import os
import json
from datetime import datetime
import re
from dotenv import load_dotenv

def clean_none_string(val):
    """Clean common 'empty' indicators from AI results."""
    if val is None:
        return None
    if isinstance(val, str):
        s = val.strip().lower()
        if s in ["none", "n/a", "null", "unknown", ""] or not s:
            return None
    return val

def clean_patient_name(name):
    """
    Strips noise like 'Details Patient Name', 'Policy No', 'Insured Empl' 
    that the AI might accidentally include.
    """
    if not name or not isinstance(name, str):
        return name
        
    name = name.strip()
    
    # List of common technical noise to remove
    noise_patterns = [
        (r"(?i)^details\s+patient\s+name\s+", ""),
        (r"(?i)^details\s+", ""),
        (r"(?i)^patient\s+name\s*[:\-]?\s*", ""),
        (r"(?i)^name\s+of\s+the\s+patient\s*[:\-]?\s*", ""),
        (r"(?i)\s+insured\s+empl$", ""),
        (r"(?i)\s+policy\s+no.*$", ""),
        (r"(?i)\s+main\s+member.*$", ""),
        (r"(?i)\s+insured$", ""),
        (r"(?i)\s+primary\s+member$", ""),
        (r"(?i)\s+beneficiary.*$", ""),
        (r"(?i)\s+patient$", ""),
        (r"(?i)hospital\s+payment\s*/\s*bulk\s+claim.*", ""),
        (r"(?i)summary\s+of\s+claims.*", ""),
    ]
    
    import re
    cleaned = name
    for pattern, replacement in noise_patterns:
        cleaned = re.sub(pattern, replacement, cleaned)
        
    # Final trim
    return cleaned.strip()

def clean_claim_number(claim_no):
    """
    Rejects claim numbers that are clearly garbage (table headers like 'erence', 'Gross').
    """
    if not claim_no or not isinstance(claim_no, str):
        return claim_no
        
    val = claim_no.strip().lower()
    
    # Junk values to reject immediately
    junk_values = ["erence", "gross", "claim no", "ref no", "remarks", "bill", "invoice", "none", "unknown", "null", "details", "sl no", "status"]
    
    if val in junk_values:
        print(f"[EXTRACTOR] Rejecting garbage Claim No: {claim_no}")
        return None
        
    # If it's too short (less than 4 chars) and not purely numeric, likely garbage
    if len(val) < 4 and not val.isdigit():
        print(f"[EXTRACTOR] Rejecting short/garbage Claim No: {claim_no}")
        return None
        
    return claim_no.strip()

load_dotenv()

# Global for caching model
_LOCAL_MODEL = None

def get_local_llm():
    """Lazy load the GPT4All model for local extraction."""
    global _LOCAL_MODEL
    if _LOCAL_MODEL is not None:
        return _LOCAL_MODEL
    
    try:
        from gpt4all import GPT4All
        # Use Llama 3 or similar small fast model with expanded context window
        _LOCAL_MODEL = GPT4All("Meta-Llama-3-8B-Instruct.Q4_0.gguf", n_ctx=4096)
        return _LOCAL_MODEL
    except Exception as e:
        print(f"Error loading GPT4All: {e}")
        return None

def normalize_date_str(date_str):
    """Normalize various date formats to YYYY-MM-DD."""
    if not date_str:
        return None
    
    date_str = date_str.strip()
    
    # Formats to try (DD-MM-YYYY, DD/MM/YYYY, etc.)
    # Support for timestamps (03-12-2025 00:00:00)
    formats = [
        "%Y-%m-%d",          # 2025-12-09
        "%d/%m/%Y",          # 09/12/2025
        "%d-%m-%Y",          # 09-12-2025
        "%d %b %Y",          # 09 Dec 2025
        "%d %B %Y",          # 09 December 2025
        "%b %d, %Y",         # Dec 09, 2025
        "%d-%m-%Y %H:%M:%S", # 03-12-2025 00:00:00
        "%d/%m/%Y %H:%M:%S", # 03/12/2025 00:00:00
        "%Y-%m-%d %H:%M:%S", # 2025-12-03 00:00:00
    ]
    
    # Try stripping time if first pass fails
    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
            
    # Try removing trailing time strings like " 00:00:00" if present
    if " " in date_str:
        p_date = date_str.split(" ")[0]
        for fmt in ["%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d"]:
            try:
                return datetime.strptime(p_date, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue

    return date_str # Return original if all fail

def override_status_for_payment_advice(data, text):
    """
    Post-processing step: Check if document is a Payment Advice.
    If yes, force status to 'Settled' regardless of AI's determination.
    """
    if not data or not text:
        return data
    
    lower_text = text.lower()
    
    # Expanded Payment Advice indicators
    # Check for multiple patterns to catch Payment Advice documents
    payment_keywords = [
        "payment advice",
        "net payable",
        "utr number",
        "utr no",
        "neft",
        "rtgs",
        "amount disbursed",
        "payment made",
        "transaction reference",
        "remittance advice",
        "payment processed",
        "amount paid",
        "eft",
        "electronic fund transfer",
        "payment reference",
        "paid to hospital",
        "successfully settled"
    ]
    
    # Also check for combinations (Payment + Advice appearing anywhere)
    has_payment = "payment" in lower_text
    has_advice = "advice" in lower_text
    has_utr = "utr" in lower_text
    has_neft = "neft" in lower_text or "rtgs" in lower_text
    
    print(f"[DEBUG] Checking Payment Advice status...")
    print(f"[DEBUG] Has 'payment': {has_payment}, Has 'advice': {has_advice}")
    print(f"[DEBUG] Has 'utr': {has_utr}, Has 'neft/rtgs': {has_neft}")
    print(f"[DEBUG] Current status from AI: {data.get('claim_status', 'None')}")
    
    # Match if any keyword found OR if we have payment+advice combination
    if any(keyword in lower_text for keyword in payment_keywords) or (has_payment and has_advice):
        print("[OVERRIDE] ✓ Payment Advice detected. Forcing status to 'Settled'.")
        data["claim_status"] = "Settled"
    else:
        print("[DEBUG] No Payment Advice indicators found.")
    
    return data

def extract_claim_data(text_content, file_path=None):
    """
    Uses an LLM to parse unstructured text into structured JSON for insurance claims.
    If file_path is provided and it's an image/PDF, uses vision models for better extraction.
    """
    if not text_content or not text_content.strip():
        return None

    prompt = f"""
    You are an expert AI assistant for a hospital insurance desk specializing in extracting structured data from insurance claim documents.
    
    CRITICAL INSTRUCTIONS:
    1. Extract ONLY the exact information from the document - DO NOT make up or infer data
    2. Patient names are ALWAYS text names (e.g., "John Doe", "Baby of Sarah"), NEVER numbers or codes
    3. Claim numbers are alphanumeric codes (e.g., "CLM123456"), NOT patient names
    4. If you cannot find a field with confidence, use null instead of guessing
    
    Extract the following fields from the document and return ONLY a valid JSON object (no markdown, no explanations):
    
    PATIENT INFORMATION:
    PATIENT INFORMATION:
    - patient_name: Full patient name (ONLY the name, e.g., "Rajesh Kumar". REMOVE any prefixes like "Details Patient Name" or suffixes like "Insured", "Policy No", "Main Member". MUST be alphabetic text with spaces, NOT numbers.)
    - uhid_mrn: Hospital ID or Medical Record Number (alphanumeric code)
    
    INSURANCE DETAILS:
    - insurance_company: Insurance company name (e.g., ICICI Lombard, HDFC ERGO, Star Health, New India Assurance)
    - tpa_name: Third Party Administrator name. Look for headers or logos containing "TPA", "TPA Limited", "Health Services", etc. (e.g., Good Health Insurance TPA, Medi Assist, Paramount). Often appears at the top of the document.

    - claim_number: Unique claim identifier. Look for "Claim No", "Claim Intimation No", "Invoice Number", or "UTR Number" (alphanumeric). If multiple exist, prioritize the one that looks most like a claim registration ID.
    - claim_status: One of [Approved,Settled, Rejected, Pending, Queried, PreAuthorized]. IMPORTANT: If document is a Payment Advice or mentions UTR/Payment Made, it MUST be 'Settled'. Settled trumps Approved.
    - claim_date: Date of admission or claim submission. PRIORITY ORDER: 1. Admission Date, 2. Date of Admission (DOA), 3. Hospitalization Date, 4. Claim Submission Date. (format: YYYY-MM-DD)
    - settlement_date: Date when payment was processed/settled. PRIORITY ORDER: 1. Settlement Date, 2. Payment Date, 3. EFT Date, 4. Payment Processed Date. (format: YYYY-MM-DD)
    - submitted_date: DEPRECATED - Leave as null (kept for backward compatibility)
    - claim_type: Determine if 'Cashless' or 'Reimbursement'. Logic:
      - 'Cashless': If document mentions "TPA", "Network Hospital", "Pre-Auth", "Authorization", "Cashless" or if insurer paid the hospital directly.
      - 'Reimbursement': If document mentions "Reimbursement", "Refund to Patient", "Patient Paid", "Bill Submission" or if payment is to patient.
      - If explicitly stated as 'Emergency' or 'Planned', use those.
      - Default to "General" ONLY if absolutely no clue is found.
    
    FINANCIAL AMOUNTS (all numbers, use 0 if not found):
    - approved_amount: Amount approved/eligible before co-pays. In tables, look for "Payable Amount", "Eligible Amount", or "Admissible Amount".
    - settled_amount: Amount actually paid to hospital/patient after co-pays. Look for "Net Payable", "Final Amount", "Paid Amount".
    - rejected_amount: Amount rejected by insurance
    - outstanding_amount: Amount pending from insurance
    - total_bill_amount: The gross amount of the hospital bill. IMPORTANT: In Payment Advices, this is often labeled as "Invoice Amount", "Gross Amount", or "Claimed Amount". If "Total Bill" is not explicitly found, use "Invoice Amount" as the total bill. (number only)
    - claim_amount: The amount claimed from insurance. Often the same as total bill or invoice amount. (number only)
    - approved_amount: The amount approved by the insurance/TPA.
    - settled_amount: The final amount paid/disbursed. In Payment Advices, this is often "Net Amount" or "Amount Paid".
    - patient_payable_amount: Amount patient needs to pay
    - insurance_coverage_percent: Coverage percentage (0-100)
    - balance_amount: Outstanding balance hospital needs to receive
    
    ADDITIONAL INFO:
    - remarks: Any notes, queries, or rejection reasons
    - tat_followup_date: Follow-up or TAT date (format: YYYY-MM-DD)
    
    VALIDATION RULES:
    - patient_name MUST contain letters and spaces, CANNOT be purely numeric
    - All amounts must be positive numbers or 0
    - Dates must be in YYYY-MM-DD format
    
    Document Text:
    {text_content[:30000]}
    """

    # Try AI models in order of accuracy: OpenAI > Gemini > Local LLM
    # OpenAI GPT-4 is the most accurate for this task
    
    # 1. Try OpenAI FIRST (Most Accurate)
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    if OPENAI_API_KEY:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=OPENAI_API_KEY)
            
            completion = client.chat.completions.create(
                model="gpt-4o", # Most powerful model
                messages=[
                    {"role": "system", "content": "You are an expert insurance claims data extraction assistant. Extract data with 100% accuracy. Patient names are ALWAYS text (like 'John Doe' or 'Baby of Sarah'), NEVER numbers or codes."},
                    {"role": "user", "content": prompt}
                ],
                response_format={"type": "json_object"},
                temperature=0.1  # Low temperature for consistency
            )
            raw_json = completion.choices[0].message.content
            data = json.loads(raw_json)
            
            # Validate patient name
            if validate_extraction(data):
                data = override_status_for_payment_advice(data, text_content)
                print("[OK] OpenAI GPT-4 Extraction Success")
                return apply_financial_formulas(data, text_content)
            else:
                print("[WARN] OpenAI extraction failed validation, trying next model...")
        except Exception as e:
             print(f"OpenAI Error: {e}")



    # 4. Try Local LLM (Last Resort)
    USE_LOCAL_LLM = os.getenv("USE_LOCAL_LLM", "false").lower() == "true"
    
    if USE_LOCAL_LLM:
        try:
            model = get_local_llm()
            
            if model:
                # Simplified prompt for local model
                local_prompt = f"""
### System:
You are a helpful insurance claims processor. Extract fields from the text into valid JSON.
Patient names are ALWAYS text (e.g. "John Smith"), NEVER numbers.

### User:
Text:
{text_content[:8000]}

### Assistant:
{{
"""
                with model.chat_session():
                    response = model.generate(local_prompt, max_tokens=1024, temp=0.1)
                
                full_json_str = "{" + response
                clean_json = full_json_str.replace("```json", "").replace("```", "").strip()
                
                end = clean_json.rfind("}")
                if end != -1:
                    clean_json = clean_json[:end+1]
                else:
                    clean_json += "}"
                
                try:
                    data = json.loads(clean_json)
                    if validate_extraction(data):
                        print("[OK] Local LLM Extraction Success")
                        return apply_financial_formulas(data, text_content)
                except json.JSONDecodeError:
                    print(f"Local LLM JSON Error")
            else:
                print("Local LLM Model failed to load.")

        except Exception as e:
            print(f"Local LLM Error: {e}")

    # All AI models failed or unavailable

    print("[WARN] All AI models failed or produced invalid data. Falling back to Regex.")
    data = extract_claim_data_regex(text_content)
    return apply_financial_formulas(data, text_content)

def validate_extraction(data):
    """
    Validates extracted data to ensure it makes sense.
    Returns True if valid, False otherwise.
    """
    if not data:
        return False
    
    # Check patient name - MUST be text, not numbers
    patient_name = data.get("patient_name")
    if patient_name:
        patient_name_str = str(patient_name).strip()
        
        # Reject if it's purely numeric or contains too many numbers/dots
        if patient_name_str.replace(".", "").replace("-", "").replace(" ", "").isdigit():
            print(f"[WARN] Invalid patient name (numeric): {patient_name_str}")
            return False
        
        # Reject if it's too short (less than 2 characters)
        if len(patient_name_str) < 2:
            print(f"[WARN] Invalid patient name (too short): {patient_name_str}")
            return False
            
        # Check if it has at least some letters
        if not any(c.isalpha() for c in patient_name_str):
            print(f"[WARN] Invalid patient name (no letters): {patient_name_str}")
            return False
    
    return True

def extract_claim_data_regex(text):
    """Fallback regex extraction when AI fails."""
    import re
    
    data = {}
    
    # Helper for regex
    def find_val(pattern, text, type_conv=str):
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            # Clean up the value
            val = match.group(1).strip()
            if type_conv == float:
                 try:
                     # Remove commas and non-numeric chars except decimal
                     clean_val = re.sub(r'[^\d.]', '', val)
                     return float(clean_val) if clean_val else 0.0
                 except: 
                     return 0.0
            return val.replace(",", "")
        return None

    # Common Patterns
    # Stop at newline for names to avoid grabbing next lines
    # REGEX PATTERNS - BACKUP FOR WHEN AI FAILS (OpenAI Quota Limit)
    # These pass through cleaning filters later to remain clean
    data["patient_name"] = find_val(r"(?:Patient|Insured|Member)\s*(?:Name)?\s*[:\-\s]+\s*([A-Za-z\s\.]{3,50})(?:\s|[\r\n])", text)
    data["uhid_mrn"] = find_val(r"(?:UHID|MRN|Reg|Hosp)\s*(?:No|Number|ID)?\s*[:\-\s]+\s*([A-Za-z0-9\-/]+)", text)
    data["claim_number"] = find_val(r"(?:Claim (?:Intimation )?(?:Number|No\.?|Id)|Claim #|CCN#?|Claim Ref)\s*[:\-]?\s*([A-Za-z0-9\-\_\/]+)", text)
    
    # Insurance and TPA extraction can stay as they are generally safer
    data["insurance_company"] = find_val(r"(?:Insurance(?: Company| Co\.?| Corp\.?)?|Insurer|Payer)\s*[:\-]\s*([^\n\r]+)", text)
    data["tpa_name"] = find_val(r"(?:TPA|Third Party Administrator)\s*[:\-]\s*([^\n\r]+)", text)
    
    # 1. Known TPA List (Specific & High Confidence)
    # Catches cases where the word "TPA" might be missing or separated (e.g. "Medi Assist Pvt Ltd")
    KNOWN_TPA_MAP = {
        r"GOOD\s+HEALTH\s+INSURANCE\s+TPA": "Good Health Insurance TPA Limited",
        r"HEALTH\s*INDIA\s+INSURANCE\s+TPA": "Health India Insurance TPA Services Pvt. Ltd.",
        r"HEALTHINDIA\s+INSURANCE\s+TPA": "Health India Insurance TPA Services Pvt. Ltd.",
        r"MEDI\s*ASSIST": "Medi Assist Insurance TPA Pvt. Ltd.",
        r"PARAMOUNT\s+HEALTH": "Paramount Health Services & Insurance TPA Pvt. Ltd.",
        r"VIDAL\s+HEALTH": "Vidal Health Insurance TPA Pvt. Ltd.",
        r"MD\s+INDIA": "MDIndia Health Insurance TPA Pvt. Ltd.",
        r"HERITAGE\s+HEALTH": "Heritage Health Insurance TPA Pvt. Ltd.",
        r"FAMILY\s+HEALTH\s+PLAN": "Family Health Plan Insurance TPA Ltd.",
        r"RAKSHA\s+HEALTH": "Raksha Health Insurance TPA Pvt. Ltd.",
        r"ERICSON\s+INSURANCE\s+TPA": "Ericson Insurance TPA Pvt. Ltd.",
        r"UNITED\s+HEALTH\s+CARE": "United Health Care Parekh Insurance TPA Pvt. Ltd.",
        r"VIPUL\s+MEDCORP": "Vipul MedCorp Insurance TPA Pvt. Ltd."
    }

    if not data.get("tpa_name"):
        for pattern, standardized_name in KNOWN_TPA_MAP.items():
            if re.search(pattern, text, re.IGNORECASE):
                print(f"[EXTRACTOR] Found Known TPA: {standardized_name}")
                data["tpa_name"] = standardized_name
                break

    # 2. Dynamic TPA Pattern Matcher (Universal)
        # Pattern 1: Standard "Name ... TPA ... Suffix"
        # Matches: "Good Health Insurance TPA", "MEDI ASSIST TPA", "Paramount TPA"
        # Group 1: The Name (1-5 words before TPA)
        # Group 2: "TPA"
        # Group 3: Suffix (Optional)
        # Regex explanation:
        # (?:[A-Za-z0-9\.\&]+\s+){1,6}  --> Matches 1 to 6 words (letters, numbers, dots, amps) ending with space
        # (?:TPA|Third Party Administrator) --> The Anchor
        dynamic_match = re.search(r"((?:[A-Za-z0-9\.\&]+\s+){1,6})(TPA|Third Party Administrator)(\s+(?:Limited|Services|Pvt|Ltd|Private|\.|&)+)?", text, re.IGNORECASE)
        if dynamic_match:
             # Reconstruct full name
             full_name = f"{dynamic_match.group(1)}{dynamic_match.group(2)}{dynamic_match.group(3) or ''}"
             full_name = full_name.strip()
             # Sanity check: Not too long, not a sentence
             if len(full_name) > 5 and len(full_name) < 60:
                  # Filter out common false positives if the capture is just "Insurance TPA"
                  if full_name.lower().strip() not in ["insurance tpa", "health insurance tpa"]: 
                      print(f"[EXTRACTOR] Found Dynamic TPA: {full_name}")
                      data["tpa_name"] = full_name

    # 1.5 Special Check: Email Subject Line
    # User Suggestion: "search in headings of the mails"
    if not data.get("tpa_name"):
        subject_match = re.search(r"Subject:.*?([A-Za-z \.]{4,}\sTPA\b)", text, re.IGNORECASE)
        if subject_match:
             print(f"[EXTRACTOR] Found TPA in Subject Line: {subject_match.group(1)}")
             data["tpa_name"] = subject_match.group(1).strip()

    # 2. Improved TPA Regex (Fallback)
    if not data.get("tpa_name"):
        # Capture letters, spaces, dots BEFORE "TPA" but not newlines. Limit length.
        # Robust Suffix: Matches "Services Pvt. Ltd.", "Limited", "TPA Pvt Ltd", etc.
        tpa_match = re.search(r"([A-Za-z \.]{4,50}\s?TPA\s+(?:Services|Pvt|Private|Limited|Ltd|\.|[ ])+)", text, re.IGNORECASE)
        if tpa_match:
             # Clean up match
             candidate = tpa_match.group(1).strip()
             if len(candidate) < 50: # Sanity check length
                 data["tpa_name"] = candidate
    
    # 3. Catch-All TPA Regex (Broadest)
    # Looks for any capitalized string ending in TPA or TPA Limited/Services
    if not data.get("tpa_name"):
         # Matches "Some Name TPA" or "Some Name TPA Pvt Ltd"
         # \b[A-Z]... starts with capital letter, contains letters/spaces, Has TPA, ends with commonly used words or end of line
         catch_all = re.search(r"([A-Z][A-Za-z\s\.]+\bTPA\b(?:[A-Za-z\s\.]*)?)", text)
         if catch_all:
             candidate = catch_all.group(1).strip()
             # Validate it looks like a name (not a sentence)
             if len(candidate) > 3 and len(candidate) < 60 and "\n" not in candidate:
                 data["tpa_name"] = candidate

    # Status - Enhanced with multiple keyword variations
    lower_text = text.lower()
    
    # Check for Settled (payment completed)
    # Includes "Payment Advice" and Bank References (UTR/NEFT)
    if any(word in lower_text for word in ["payment settled", "settlement processed", "payment made", "amount disbursed", "paid out", "eft processed", "settled", "payment advice", "net payable", "utr number", "utr no", "neft"]):
        data["claim_status"] = "Settled"

    # Check for Approved
    elif any(word in lower_text for word in ["claim approved", "approval granted", "approved for", "sanctioned", "authorized"]):
        data["claim_status"] = "Approved"
    
    # Check for Rejected
    elif any(word in lower_text for word in ["claim rejected", "claim denied", "rejected due to", "declined", "repudiated", "disallowed", "not approved"]):
        data["claim_status"] = "Rejected"
    
    # Check for Queried (needs more info)
    elif any(word in lower_text for word in ["query raised", "pending documents", "clarification needed", "additional information", "deficiency", "query", "document required"]):
        data["claim_status"] = "Queried"
    
    # Single word fallbacks (less specific)
    elif "approved" in lower_text: 
        data["claim_status"] = "Approved"
    elif "rejected" in lower_text: 
        data["claim_status"] = "Rejected"
    
    # Default to Pending
    else: 
        data["claim_status"] = "Pending"
    
    # Dates - Extract both claim_date and settlement_date separately
    # Support YYYY-MM-DD, DD/MM/YYYY, and DD MMM YYYY (e.g., 09 Dec 2025)
    
    # 1. Extract SETTLEMENT DATE (Payment Processed Date)
    settlement_regex = r"(?:Settlement|Payment|EFT|Discharge)\s*Date\s*[:\-\s]+\s*(\d{4}[-/]\d{2}[-/]\d{2}|\d{2}[-/]\d{2}[-/]\d{4}|\d{2}\s+[A-Za-z]{3}\s+\d{4})(?:\b|\s+\d{2}:\d{2})"
    settlement_date = find_val(settlement_regex, text)
    if settlement_date:
        data["settlement_date"] = normalize_date_str(settlement_date)
        # Backward compatibility
        data["submitted_date"] = normalize_date_str(settlement_date)
    
    # 2. Extract CLAIM DATE (Admission/Claim Submission Date)
    claim_date_regex = r"(?:Admission|DOA|Hospitalization|Claim\s+Submission)\s*Date\s*[:\-\s]+\s*(\d{4}[-/]\d{2}[-/]\d{2}|\d{2}[-/]\d{2}[-/]\d{4}|\d{2}\s+[A-Za-z]{3}\s+\d{4})(?:\b|\s+\d{2}:\d{2})"
    claim_date = find_val(claim_date_regex, text)
    if claim_date:
        data["claim_date"] = normalize_date_str(claim_date)

    
    # Amounts - improved patterns
    # Matches "Total Bill Amount", "Total Bill", "Bill Amount", etc.
    # Added "settled for" as Approved Amount for Bajaj/HealthIndia documents
    data["approved_amount"] = find_val(r"(?:Approved|Payable|Eligible|Admissible|Settled for)\s*(?:Amount|Amt|Sum)?(?:\s*\(INR\))?\s*[:\-]?\s*(?:INR|Rs\.?)?\s*([0-9,]+(?:\.\d+)?)", text, float) or 0
    data["settled_amount"] = find_val(r"(?:Settled(?: for)?|Paid|Net Payable|Disbursed)\s*(?:Amount|Amt|Sum)?(?:\s*\(INR\))?\s*[:\-]?\s*(?:INR|Rs\.?)?\s*([0-9,]+(?:\.\d+)?)", text, float) or 0
    data["rejected_amount"] = find_val(r"(?:Rejected|Non-Payable|Deducted|Disallowed)\s*(?:Amount|Amt|Sum)?(?:\s*\(INR\))?\s*[:\-]?\s*(?:INR|Rs\.?)?\s*([0-9,]+(?:\.\d+)?)", text, float) or 0
    
    # Total Bill Amount regex - improved
    # Matches "Total Bill", "Invoice Amount", "Claimed Amount", "Gross Amount", etc.
    total_bill = find_val(r"(?:Total|Bill|Invoice|Claimed|Gross|Net)\s*(?:Bill|Amount|Total|Sum|amount of|payable)?(?:\s*\(INR\))?\s*[:\-]?\s*(?:INR|Rs\.?|\$)?\s*([0-9,]+(?:\.\d+)?)", text, float) or 0
    data["total_bill_amount"] = total_bill
    data["claim_amount"] = total_bill # Default claim amount to total bill in fallback
    
    return data

def apply_financial_formulas(data, text_content):
    """
    Apply final formatting, normalization, and business rules to the 
    extracted data to ensure mathematical consistency.
    """
    if not data:
        return data

    # 1. Normalize Numbers (handle commas, strings, etc.)
    financial_fields = [
        "approved_amount", "settled_amount", "rejected_amount", 
        "total_bill_amount", "claim_amount", "patient_payable_amount", 
        "balance_amount", "outstanding_amount"
    ]
    for field in financial_fields:
        val = data.get(field, 0)
        if isinstance(val, str):
            try:
                # Remove commas and non-numeric chars except decimal
                val = re.sub(r'[^\d.]', '', val)
                data[field] = float(val) if val else 0.0
            except:
                data[field] = 0.0
        elif val is None:
            data[field] = 0.0
        else:
            data[field] = float(val)

    # 1b. Sanitize text fields (No "None" or "N/A" strings)
    text_fields = ["claim_number", "patient_name", "uhid_mrn", "insurance_company", "tpa_name", "remarks"]
    for field in text_fields:
        val = clean_none_string(data.get(field))
        if field == "patient_name" and val:
            val = clean_patient_name(val)
        if field == "claim_number" and val:
            val = clean_claim_number(val)
        data[field] = val

    # 2. Normalize Dates
    date_fields = ["claim_date", "settlement_date", "submitted_date", "tat_followup_date"]
    for field in date_fields:
        if data.get(field):
            data[field] = normalize_date_str(data[field])

    # 3. Handle 'Total Bill' & 'Claim Amount' Fallbacks
    # If Total Bill is 0, try to use Claim Amount or Settled Amount (logic: bill must be at least what was paid)
    if not data.get("total_bill_amount") or data.get("total_bill_amount") == 0:
        if data.get("claim_amount") and data.get("claim_amount") > 0:
            data["total_bill_amount"] = data["claim_amount"]
        elif data.get("settled_amount") and data.get("settled_amount") > 0:
            data["total_bill_amount"] = data["settled_amount"]
            
    # Conversely, if Claim Amount is missing, use Total Bill
    if not data.get("claim_amount") or data.get("claim_amount") == 0:
        data["claim_amount"] = data.get("total_bill_amount", 0)

    # 4. Mandatory Business formulas (User Requests)
    total = data.get("total_bill_amount", 0)
    approved = data.get("approved_amount", 0)
    settled = data.get("settled_amount", 0)
    status = data.get("claim_status", "Pending")

    # Rule 1: Rejected = Total Bill - Approved
    # We enforce this strictly for the dashboard
    calculated_rejected = max(0, total - approved)
    if data.get("rejected_amount", 0) == 0 or abs(data.get("rejected_amount", 0) - calculated_rejected) > 1:
        data["rejected_amount"] = calculated_rejected

    # Rule 2: Patient Payable = Total Bill - Approved
    data["patient_payable_amount"] = max(0, total - approved)

    # Rule 3: Insurance Balance
    if status == "Pending":
        data["balance_amount"] = total
        data["approved_amount"] = 0
        data["settled_amount"] = 0
        data["rejected_amount"] = 0
        data["patient_payable_amount"] = 0
    elif status == "Settled":
        # Rule: If Settled, insurance balance is 0 because they've paid.
        data["balance_amount"] = 0.0
        # For settled claims, what the patient owes is actually what wasn't paid by insurance
        # (assuming approved_amount was the expectation, but settled_amount is the reality)
        data["patient_payable_amount"] = max(0, total - settled)
    elif status == "Approved":
        # If Approved but not yet Settled, balance is what we expect to receive
        data["balance_amount"] = max(0, approved - settled)
    else:
        # Default balance calculation for other statuses (Queried, Rejected, etc.)
        data["balance_amount"] = max(0, approved - settled)
    
    # Re-map outstanding for backward compatibility (Safer access)
    data["outstanding_amount"] = data.get("balance_amount", 0.0)

    return data

def correct_extracted_dates(data, text):
    """
    Hybrid Logic: Check if the AI extracted 'Admission Date' by mistake.
    If so, force override with Settlement Date from Regex.
    """
    import re
    if not data or not text: 
        return data

    submitted_date = data.get("submitted_date")
    # Normalize for comparison
    normalized_submitted = normalize_date_str(submitted_date) if submitted_date else None
    
    # 1. Find BAD dates (Admission) in text
    # Look for "Date of Admission: 11 Sep 2025" or "DOA: ..."
    # We want to capture the specific string the AI might have seen.
    admission_patterns = [
        r"(?:Admission|Hosp|Birth|Policy)\s*Date\s*[:\-\s]+\s*(\d{4}[-/]\d{2}[-/]\d{2}|\d{2}[-/]\d{2}[-/]\d{4}|\d{2}\s+[A-Za-z]{3}\s+\d{4})",
        r"DOA\s*[:\-\s]+\s*(\d{4}[-/]\d{2}[-/]\d{2}|\d{2}[-/]\d{2}[-/]\d{4}|\d{2}\s+[A-Za-z]{3}\s+\d{4})"
    ]
    
    is_bad_date = False
    for pat in admission_patterns:
        match = re.search(pat, text, re.IGNORECASE)
        if match:
            bad_date_str = match.group(1).replace(",", "").strip()
            norm_bad = normalize_date_str(bad_date_str)
            if norm_bad and norm_bad == normalized_submitted:
                print(f"[FIX] Detected AI picked Admission Date ({norm_bad}). Rejecting...")
                is_bad_date = True
                break
    
    # 2. If bad date OR if we just want to be sure, try to find Settlement Date
    # Regex for Settlement Date is the gold standard here.
    settlement_regex = r"(?:Settlement|Payment|Discharge)\s*Date\s*[:\-\s]+\s*(\d{4}[-/]\d{2}[-/]\d{2}|\d{2}[-/]\d{2}[-/]\d{4}|\d{2}\s+[A-Za-z]{3}\s+\d{4})(?:\b|\s+\d{2}:\d{2})"
    settlement_match = re.search(settlement_regex, text, re.IGNORECASE)
    
    if settlement_match:
        better_date = settlement_match.group(1).replace(",", "").strip()
        data["submitted_date"] = better_date
        print(f"[FIX] Force-applied Settlement Date found in text: {better_date}")
    elif is_bad_date:
        # If we found it was bad, but didn't find a settlement date, CLEAR it rather than keeping the bad one.
        data["submitted_date"] = None
        print("[FIX] Cleared submitted_date (was Admission Date)")
        
    return data


