import imaplib
import email
from email.header import decode_header
import os
import sys

# Ensure src dir is in path for imports
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.append(current_dir)

import time
from datetime import datetime, timedelta
from dotenv import load_dotenv
from attachment_parser import save_attachment, extract_content_from_file
from ai_extractor import extract_claim_data
from db import SessionLocal, Claim, ClaimHistory, ProcessingLog, init_db

load_dotenv()

# CRITICAL: Ensure database schema is up-to-date before processing
# This adds missing columns like claim_amount
init_db()

EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASS = os.getenv("EMAIL_PASS")
EMAIL_SERVER = os.getenv("EMAIL_SERVER")

def clean_text(text):
    return "".join(c if c.isprintable() else " " for c in text)

def safe_decode(value, encoding):
    """Safely decode email subject/headers."""
    if isinstance(value, bytes):
        try:
            if encoding and encoding.lower() != "unknown-8bit":
                return value.decode(encoding, errors="ignore")
            else:
                return value.decode("utf-8", errors="ignore")
        except:
            return value.decode("latin1", errors="ignore")
    return str(value)

def process_single_email(uid, msg_data, db):
    """Process a single email: parse, extract, upsert to DB."""
    try:
        msg = email.message_from_bytes(msg_data[0][1])
        
        # --- SUBJECT ---
        subject_raw, enc = decode_header(msg["Subject"])[0]
        subject = safe_decode(subject_raw, enc)
        print(f"Processing: {subject}", flush=True)

        # --- DATE ---
        date_tuple = email.utils.parsedate_tz(msg.get('Date'))
        if date_tuple:
            email_date = datetime.fromtimestamp(email.utils.mktime_tz(date_tuple))
        else:
            email_date = datetime.utcnow() # Fallback

        # Check if this SPECIFIC email UID is already the current record
        # existing_uid = db.query(Claim).filter(Claim.email_uid == str(uid)).first()
        # if existing_uid:
        #     print("Skipping duplicate email UID? No, FORCE UPDATE for TPA.")
        #     # return

        # --- BODY & ATTACHMENTS ---
        body_text = ""
        attachment_text = ""
        
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition"))

                if content_type == "text/plain" and "attachment" not in content_disposition:
                    body_text += safe_decode(part.get_payload(decode=True), part.get_content_charset())
                
                elif "attachment" in content_disposition or part.get_filename():
                    filename = part.get_filename() or "unknown"
                    if "whatsapp" in filename.lower() or "image-" in filename.lower() or "img-" in filename.lower():
                         print(f"Skipping WhatsApp/Generic Image: {filename}")
                         continue

                    print(f"Saving Attachment: {filename}")
                    try:
                        file_path = save_attachment(part, str(uid))
                        if file_path:
                            # 1. Check if it is a ZIP file
                            if file_path.lower().endswith(".zip"):
                                import zipfile
                                print(f"Detected ZIP file: {file_path}. Extracting...")
                                try:
                                    extracted_text_list = []
                                    with zipfile.ZipFile(file_path, 'r') as zip_ref:
                                        # Extract to a subdirectory to avoid clutter
                                        extract_dir = os.path.join(os.path.dirname(file_path), f"zip_extract_{uid}")
                                        os.makedirs(extract_dir, exist_ok=True)
                                        zip_ref.extractall(extract_dir)
                                        
                                        # Iterate through extracted files
                                        first_file_copied = False
                                        for root, dirs, files in os.walk(extract_dir):
                                            for subfile in files:
                                                sub_path = os.path.join(root, subfile)
                                                # Skip generic/garbage files
                                                if subfile.startswith(".") or "__MACOSX" in sub_path or sub_path.endswith(".zip"): 
                                                    continue
                                                
                                                print(f"Processing Zip Content: {subfile}")
                                                sub_content = extract_content_from_file(sub_path)
                                                if sub_content:
                                                    extracted_text_list.append(f"[Zip Content: {subfile}]\n{sub_content}")
                                                    
                                                # VIEWER COPY: Copy extracted files to main attachments dir so viewer can find them
                                                # Format: {uid}_{subfile}
                                                viewer_filename = f"{uid}_{subfile}"
                                                viewer_path = os.path.join(os.path.dirname(file_path), viewer_filename)
                                                import shutil
                                                try:
                                                    shutil.copy2(sub_path, viewer_path)
                                                    print(f"Copied for viewer: {viewer_path}")
                                                except Exception as copy_err:
                                                    print(f"Viewer copy error: {copy_err}")
                                    
                                    if extracted_text_list:
                                        attachment_text += "\n" + "\n".join(extracted_text_list)
                                        
                                except Exception as ze:
                                    print(f"Zip Extraction Error: {ze}")
                                    # Fallback: Just try to read the zip as text (unlikely to work but keeps flow)
                                    pass

                            else:
                                # Normal File (PDF, Image, etc.)
                                content = extract_content_from_file(file_path)
                                attachment_text += f"\n[Attachment Content: {os.path.basename(file_path)}]\n{content}"
                    except Exception as e:
                        print(f"Error processing attachment {filename}: {e}")
        else:
            charset = msg.get_content_charset() or "utf-8"
            body_text = safe_decode(msg.get_payload(decode=True), charset)

        combined_text = f"Subject: {subject}\n\nBody:\n{body_text}\n\nAttachments:\n{attachment_text}"
        
        # --- AI EXTRACTION (Pure AI) ---
        extracted_data = extract_claim_data(combined_text)
        
        # VALIDATION: Check for Patient Name
        # VALIDATION: Check for Patient Name
        if extracted_data:
            p_name = extracted_data.get("patient_name")
            claim_no = extracted_data.get("claim_number")

            # RELAXED VALIDATION: 
            # If Patient Name is missing, BUT we have a valid Claim Number (e.g. Invoice/Payment Advice), allow it.
            if (not p_name or str(p_name).lower() in ["none", "null", "unknown", ""]) and not claim_no:
                print(f"Skipping Email {uid}: No valid Patient Name AND No Claim Number.")
                log = ProcessingLog(email_subject=subject, status="Skipped", error_message="No Patient Name (Validation Failed)")
                db.add(log)
                db.commit()
                return
            
            # If Patient Name is missing but we have a Claim Number, use a placeholder
            if not p_name or str(p_name).lower() in ["none", "null", "unknown", ""]:
                 print(f"Warning: Missing Patient Name for Claim {claim_no}. Using placeholder.")
                 p_name = " Hospital Payment / Bulk Claim"
                 extracted_data["patient_name"] = p_name
            
            # UPSERT LOGIC: MATCH BY UID FIRST (Most Robust)
            existing_claim = db.query(Claim).filter(Claim.email_uid == str(uid)).first()
            
            # If no UID match, try Claim Number
            if not existing_claim and claim_no:
                existing_claim = db.query(Claim).filter(Claim.claim_number == claim_no).first()
            
            # FALLBACK MATCHING: If still no match, try Patient Name + UHID
            if not existing_claim:
                uhid = extracted_data.get("uhid_mrn")
                if p_name and uhid:
                    existing_claim = db.query(Claim).filter(
                        Claim.patient_name == p_name,
                        Claim.uhid_mrn == uhid
                    ).first()
                    if existing_claim:
                         print(f"Match found by Patient/UHID fallback for {p_name}")

            if existing_claim:
                # Check dates to see if this is an update
                if existing_claim.email_date and email_date < existing_claim.email_date:
                    print(f"Skipping older update for Claim {claim_no or p_name}")
                    log = ProcessingLog(email_subject=subject, status="Skipped", error_message="Newer data exists")
                    db.add(log)
                    db.commit()
                    return
                else:
                    print(f"Updating existing Claim {claim_no or p_name}")
                    # Update fields
                    existing_claim.email_uid = str(uid)
                    existing_claim.email_date = email_date
                    
                    # Overwrite Patient Name if current is generic/garbage OR new is provided
                    curr_name = str(existing_claim.patient_name or "").lower()
                    if p_name and (p_name != existing_claim.patient_name):
                        # Always overwrite if current is a placeholder or has technical noise
                        if "hospital payment" in curr_name or "details" in curr_name or "insured" in curr_name:
                             print(f"Cleaning up Patient Name: '{existing_claim.patient_name}' -> '{p_name}'")
                             existing_claim.patient_name = p_name
                        else:
                             # Standard update
                             existing_claim.patient_name = p_name

                    existing_claim.uhid_mrn = extracted_data.get("uhid_mrn") or existing_claim.uhid_mrn
                    existing_claim.insurance_company = extracted_data.get("insurance_company") or existing_claim.insurance_company
                    
                    # CRITICAL: Preserve claim_number if new extraction doesn't find it or returns 'None'
                    # BUT: Force overwrite if existing claim_number is known garbage ('erence', 'Gross')
                    new_claim_no = extracted_data.get("claim_number")
                    curr_claim_no = str(existing_claim.claim_number or "").lower()
                    
                    known_garbage_nos = ["erence", "gross", "none", "null", "unknown", "ref", "clm"] # clm alone is too short
                    
                    if new_claim_no and str(new_claim_no).lower() not in ["none", "n/a", "null", ""]:
                        existing_claim.claim_number = new_claim_no
                    elif curr_claim_no in known_garbage_nos or len(curr_claim_no) < 4:
                        # Clear it if it was garbage and we don't have a better one now
                        print(f"Clearing garbage Claim No: {existing_claim.claim_number}")
                        existing_claim.claim_number = None
                    # else: keep existing claim_number intact
                    
                    # Fix TPA update fallback: Try New -> Try Existing -> Fallback to Insurance
                    new_tpa = extracted_data.get("tpa_name")
                    if new_tpa:
                        existing_claim.tpa_name = new_tpa
                    elif not existing_claim.tpa_name:
                        # If DB is empty, and New is empty, try Insurance
                        existing_claim.tpa_name = existing_claim.insurance_company

                    existing_claim.claim_status = extracted_data.get("claim_status") or existing_claim.claim_status
                    existing_claim.claim_type = extracted_data.get("claim_type") or existing_claim.claim_type
                    # Update date fields - use new fields if available, fallback to submitted_date for compatibility
                    existing_claim.claim_date = extracted_data.get("claim_date") or existing_claim.claim_date
                    existing_claim.settlement_date = extracted_data.get("settlement_date") or existing_claim.settlement_date
                    existing_claim.submitted_date = extracted_data.get("submitted_date") or existing_claim.submitted_date
                    
                    # Update amounts only if provided/nonzero to avoid clearing data
                    for field in ["approved_amount", "settled_amount", "rejected_amount", 
                                 "outstanding_amount", "total_bill_amount", "patient_payable_amount", 
                                 "insurance_coverage_percent", "balance_amount"]:
                        val = extracted_data.get(field)
                        if val is not None and val != 0:
                            setattr(existing_claim, field, val)
                    
                    existing_claim.remarks = extracted_data.get("remarks") or existing_claim.remarks
                    existing_claim.tat_followup_date = extracted_data.get("tat_followup_date") or existing_claim.tat_followup_date
                    existing_claim.processed_at = datetime.utcnow()
                    
                # Ensure tables exist (Hack since migration couldn't run)
                # Ideally check once, but for robustness:
                # DEDUPLICATION: Check if this update has already been logged in history
                existing_history = db.query(ClaimHistory).filter(
                    ClaimHistory.claim_number == existing_claim.claim_number,
                    ClaimHistory.email_uid == str(uid)
                ).first()

                if existing_history:
                    print(f"[HISTORY] Update already logged for Claim {existing_claim.claim_number} (Email {uid}). Skipping duplicate history log.")
                else:
                    # Create History Record
                    history = ClaimHistory(
                        claim_number=existing_claim.claim_number,
                        email_uid=str(uid),
                        email_date=email_date,
                        total_settled_so_far=existing_claim.settled_amount,
                        amount_received=extracted_data.get("settled_amount", 0), # Simplified assumption
                        status=existing_claim.claim_status,
                        remarks=f"Update: {extracted_data.get('remarks', '')}"
                    )
                    db.add(history)
                
                db.add(existing_claim)
            else:
                if not claim_no:
                    # If we still don't have a claim number, we MUST have Patient/UHID
                    if not p_name or not extracted_data.get("uhid_mrn"):
                        print(f"Skipping Email {uid}: Insufficient identifiers for new record (No Claim No AND No Patient/UHID).")
                        return
                
                # If we HAVE a claim_number, and it is 'Settled', we allow it even without Patient/UHID
                # (Common for anonymous Bank Transfers/Payment Advices)
                if claim_no and extracted_data.get("claim_status") == "Settled":
                    print(f"Allowing Anonymous Payment for Claim {claim_no}")
                elif not p_name and not extracted_data.get("uhid_mrn"):
                    print(f"Skipping Email {uid}: Missing Patient Name and UHID for non-settled claim.")
                    return

                print(f"Creating new Claim {claim_no or 'TEMP'}")
                claim = Claim(
                    email_uid=str(uid),
                    email_date=email_date,
                    patient_name=extracted_data.get("patient_name"),
                    uhid_mrn=extracted_data.get("uhid_mrn"),
                    insurance_company=extracted_data.get("insurance_company"),
                    tpa_name=extracted_data.get("tpa_name") or extracted_data.get("insurance_company"), # Fallback to Insurance
                    claim_number=extracted_data.get("claim_number"),
                    claim_status=extracted_data.get("claim_status"),
                    claim_type=extracted_data.get("claim_type") or "General",
                    # Save both new date fields and old for compatibility
                    claim_date=extracted_data.get("claim_date"),
                    settlement_date=extracted_data.get("settlement_date"),
                    submitted_date=extracted_data.get("submitted_date"),
                    approved_amount=extracted_data.get("approved_amount", 0),
                    settled_amount=extracted_data.get("settled_amount", 0),
                    rejected_amount=extracted_data.get("rejected_amount", 0),
                    outstanding_amount=extracted_data.get("outstanding_amount", 0),
                    total_bill_amount=extracted_data.get("total_bill_amount", 0),
                    patient_payable_amount=extracted_data.get("patient_payable_amount", 0),
                    insurance_coverage_percent=extracted_data.get("insurance_coverage_percent", 0),
                    balance_amount=extracted_data.get("balance_amount", 0),
                    remarks=extracted_data.get("remarks"),
                    tat_followup_date=extracted_data.get("tat_followup_date")
                )
                db.add(claim)
                db.flush() # Get ID/Defaults
                
                # History for New Claim
                from db import ClaimHistory
                ClaimHistory.__table__.create(db.get_bind(), checkfirst=True)
                
                history = ClaimHistory(
                    claim_number=claim.claim_number,
                    email_uid=str(uid),
                    email_date=email_date,
                    total_settled_so_far=claim.settled_amount,
                    amount_received=claim.settled_amount,
                    status=claim.claim_status,
                    remarks="Initial Create"
                )
                db.add(history)

            
            # Log Success
            log = ProcessingLog(email_subject=subject, status="Success")
            db.add(log)
            db.commit()
            print("Successfully saved/updated DB.")
            
        else:
            # Log Failure
            log = ProcessingLog(email_subject=subject, status="Failed", error_message="AI Extraction returned empty")
            db.add(log)
            db.commit()

    except Exception as e:
        print(f"Error processing email {uid}: {e}")
        try:
            log = ProcessingLog(email_subject=subject if 'subject' in locals() else "Unknown", status="Error", error_message=str(e))
            db.add(log)
            db.commit()
        except:
            pass

def process_single_email_wrapper(args):
    """Wrapper for threaded execution that manages its own DB session."""
    uid, msg_data = args
    db = SessionLocal()
    try:
        process_single_email(uid, msg_data, db)
    except Exception as e:
        print(f"Thread Error for {uid}: {e}")
    finally:
        db.close()

def fetch_and_process_emails():
    """Main loop to fetch unread emails using Concurrent Sync."""
    if not all([EMAIL_SERVER, EMAIL_USER, EMAIL_PASS]):
        print("Missing Email Config.")
        return

    mail = imaplib.IMAP4_SSL(EMAIL_SERVER)
    mail.login(EMAIL_USER, EMAIL_PASS)
    mail.select("inbox")

    db = SessionLocal()
    
    # 1. Get Last Synced UID from DB
    last_uid = 0
    try:
        # Optimization: Use SQL max() instead of fetching all
        # But for safety with string UIDs, we stick to python logic or cast in SQL
        # Let's keep logic simple: get all strings and parse.
        # Ideally: db.query(func.max(cast(Claim.email_uid, Integer))).scalar() 
        # but that depends on non-dirty data.
        all_uids = db.query(Claim.email_uid).all()
        max_id = 0
        for r in all_uids:
            uid_str = r[0]
            if uid_str and uid_str.isdigit():
                 u = int(uid_str)
                 if u > max_id:
                     max_id = u
        last_uid = max_id
    except Exception as e:
        print(f"Error determining last UID: {e}")
    finally:
        db.close() # Close main thread session, workers make their own
        
    print(f"Last Synced UID: {last_uid}")
    
    # 2. Search Strategy
    email_ids = []
    
    
    # 2. Search Strategy: Sync last 14 days to catch missed/failed claims
    # This is more efficient than "ALL" but more robust than "Incremental"
    email_ids = []
    days_back = 14
    since_date = (datetime.now() - timedelta(days=days_back)).strftime("%d-%b-%Y")
    
    print(f"Syncing emails from last {days_back} days (Since {since_date})...", flush=True)
    try:
        # IMAP search criteria for date
        status, response = mail.uid('search', None, f'(SINCE "{since_date}")')
        if response[0]:
            email_ids = response[0].split()
    except Exception as e:
        print(f"IMAP Search Error: {e}")

    print(f"Found {len(email_ids)} emails to check.", flush=True)

    if not email_ids:
        mail.logout()
        return

    # 3. Fetch Data Phase (Sequential Fetch, Fast)
    print("Fetching raw email data...", flush=True)
    fetch_list = []
    
    # Limit batch size to prevent memory issues if too many
    # For now, let's process all found (incremental should be small)
    for eid in email_ids:
        uid_str = eid.decode()
        # Double check existence (quick check using a temp session or skip)
        # We can skip this check if we trust the search > last_uid logic
        # But let's keep it safe.
        
        # Actually, let's skip the DB check here to speed up fetch. 
        # process_single_email does a DB check anyway.
        
        status, msg_data = mail.uid('fetch', eid, "(RFC822)")
        if status == 'OK':
            fetch_list.append((uid_str, msg_data))
    
    mail.logout()
    
    # 4. Process Phase (Concurrent)
    if fetch_list:
        print(f"Starting parallel processing for {len(fetch_list)} emails...", flush=True)
        import concurrent.futures
        
        # Max workers = 4 to avoid hitting LLM rate limits too hard
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            executor.map(process_single_email_wrapper, fetch_list)
            
    print("Sync Cycle Complete.")

if __name__ == "__main__":
    fetch_and_process_emails()

