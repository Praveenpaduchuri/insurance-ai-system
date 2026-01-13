import os
import sys
import time
from pydub import AudioSegment

# Add local bin to PATH for ffmpeg/ffprobe
bin_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bin"))
if bin_path not in os.environ["PATH"]:
    os.environ["PATH"] += os.pathsep + bin_path

import pandas as pd
import plotly.express as px
import streamlit as st

st.set_page_config(page_title="Insurance Claim Dashboard (Live)", layout="wide", page_icon="🏥")

# Add src to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from db import SessionLocal, Claim, ClaimHistory, init_db
from email_reader import fetch_and_process_emails
from assistant import ask_ai
from attachment_parser import extract_content_from_file
from ai_extractor import extract_claim_data

# Auto-migrate database on start
try:
    init_db()
except Exception as e:
    st.error(f"Database Initialization Error: {e}")

# --- CUSTOM CSS ---
st.markdown("""
<style>
    .main {
        background-color: #f8f9fa;
    }
    .stMetric {
        background-color: #ffffff;
        padding: 15px;
        border-radius: 10px;
        box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
    }
    .css-1d391kg {
        padding-top: 1rem;
    }
    h1, h2, h3 {
        color: #2c3e50;
    }
    /* Increase table font size */
    [data-testid="stTable"] td, [data-testid="stTable"] th {
        font-size: 14px !important;
    }
    .stDataFrame div[data-testid="stTable"] {
        font-size: 14px !important;
    }
    /* Target elements in st.dataframe */
    div[data-testid="stDataFrame"] div[role="gridcell"], 
    div[data-testid="stDataFrame"] div[role="columnheader"] {
        font-size: 13px !important;
    }
</style>
""", unsafe_allow_html=True)

# --- HEADER & GLOBAL SEARCH ---
col_head, col_search = st.columns([3, 1])
with col_head:
    st.markdown("<h1 style='color: #2c3e50;'>🏥 Insurance Claim Analytics</h1>", unsafe_allow_html=True)


with col_search:
    st.write("") # Formatting spacer
    search_term = st.text_input("🔎 Global Search", placeholder="Type to filter...", label_visibility="collapsed")

# --- SIDEBAR & FILTERS ---
st.sidebar.header("⚙️ Settings & Filters")

# Sync Button & Auto-Sync
if "last_sync" not in st.session_state:
    st.session_state.last_sync = time.time()

col_sync, col_auto = st.sidebar.columns(2)
if col_sync.button("🔄 Sync Now"):
    st.session_state.last_sync = 0 # Force immediate update logic below

# Auto-Sync Settings
# Persist selection using Query Params (survives Meta Refresh)
try:
    # Streamlit > 1.30
    qp = st.query_params
except:
    # Older Streamlit
    qp = st.experimental_get_query_params()

# Get default from URL or default to "Off"
default_sync = qp.get("auto_sync", "Off")
if isinstance(default_sync, list): default_sync = default_sync[0] # Handle old query param format

options = ["Off", "2 Min", "5 Min", "15 Min"]
try:
    default_index = options.index(default_sync)
except:
    default_index = 0

def update_sync_param():
    # Update URL when changed
    val = st.session_state.auto_sync_key
    try:
        st.query_params["auto_sync"] = val
    except:
        st.experimental_set_query_params(auto_sync=val)

auto_sync_interval = col_auto.selectbox(
    "Auto-Sync", 
    options,
    index=default_index,
    key="auto_sync_key",
    on_change=update_sync_param,
    help="Automatically fetch new emails periodically."
)

# Ensure param is set initially if missing
if "auto_sync" not in qp:
    try:
        st.query_params["auto_sync"] = auto_sync_interval
    except:
        st.experimental_set_query_params(auto_sync=auto_sync_interval)

# Convert selection to seconds
interval_map = {"Off": 0, "2 Min": 120, "5 Min": 300, "15 Min": 900}
seconds = interval_map[auto_sync_interval]

# Sync Logic (Triggered by Button OR Timer)
should_sync = False
if seconds > 0:
    time_since_sync = time.time() - st.session_state.last_sync
    if time_since_sync >= seconds:
        should_sync = True
    else:
        # Show countdown or status
        remaining = int(seconds - time_since_sync)
        st.sidebar.caption(f"Next sync in {remaining}s")

# Force sync from button override
if st.session_state.last_sync == 0:
    should_sync = True

if should_sync:
    with st.spinner("Auto-Syncing Emails..."):
        try:
            fetch_and_process_emails()
            st.session_state.last_sync = time.time()
            st.sidebar.success("Sync Complete!")
            # Force immediate rerun to refresh UI with new data
            st.rerun()
        except Exception as e:
            st.sidebar.error(f"Sync Failed: {e}")

# Meta Refresh for Auto-Sync (Only if enabled)
if seconds > 0:
    st.markdown(f'<meta http-equiv="refresh" content="{seconds}">', unsafe_allow_html=True)


# AI Assistant section starts here
with st.sidebar.expander("🤖 AI Assistant", expanded=True):
    st.caption("Ask about your data")
    
    # Initialize Chat History
    if "messages" not in st.session_state:
        st.session_state.messages = []
    
    # Initialize voice state
    if "last_audio_response" not in st.session_state:
        st.session_state.last_audio_response = None
    
    user_query = None
    detected_lang = "english" # Default
    
    # ---------------------------------------------------------
    # FFmpeg Auto-Setup (Embedded)
    # ---------------------------------------------------------
    import os
    import sys
    import shutil
    import pydub
    from pydub import AudioSegment
    
    BIN_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bin"))
    FFMPEG_EXE = os.path.join(BIN_DIR, "ffmpeg.exe")
    FFPROBE_EXE = os.path.join(BIN_DIR, "ffprobe.exe")
    
    # 1. Ensure bin exists and logic to download
    if not os.path.exists(FFMPEG_EXE) or not os.path.exists(FFPROBE_EXE):
        with st.spinner("⚙️ Setting up audio components (FFmpeg)... This happens once."):
            try:
                if not os.path.exists(BIN_DIR):
                    os.makedirs(BIN_DIR)
                
                # Check if we have the zip or need to download
                import urllib.request
                import zipfile
                import ssl
                ssl._create_default_https_context = ssl._create_unverified_context
                
                URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
                ZIP_FILE = os.path.join(BIN_DIR, "ffmpeg.zip")
                
                if not os.path.exists(ZIP_FILE):
                    urllib.request.urlretrieve(URL, ZIP_FILE)
                
                # Extract
                with zipfile.ZipFile(ZIP_FILE, 'r') as zip_ref:
                    zip_ref.extractall(BIN_DIR)
                
                # Move exe to bin root
                for root, dirs, files in os.walk(BIN_DIR):
                    if "ffmpeg.exe" in files:
                        shutil.move(os.path.join(root, "ffmpeg.exe"), FFMPEG_EXE)
                        shutil.move(os.path.join(root, "ffprobe.exe"), FFPROBE_EXE)
                        break
                
                # Cleanup
                if os.path.exists(ZIP_FILE):
                    os.remove(ZIP_FILE)
                    
            except Exception as e:
                st.error(f"Failed to setup FFmpeg: {e}")
    
    # 2. Register paths
    if os.path.exists(FFMPEG_EXE):
        AudioSegment.converter = FFMPEG_EXE
        pydub.utils.get_prober_name = lambda: FFPROBE_EXE
        # Also update PATH just in case
        if BIN_DIR not in os.environ["PATH"]:
            os.environ["PATH"] += os.pathsep + BIN_DIR
    # ---------------------------------------------------------

    # 1. Voice Input (Always visible)
    from audiorecorder import audiorecorder
    
    col_mic, col_label = st.columns([1, 3])
    with col_mic:
        st.write("") # Spacer
    
    st.markdown("##### 🎙️ Voice Search")
    audio = audiorecorder("Click to Speak", "Stop Recording", key="voice_recorder")
    
    if len(audio) > 0:
        # Save audio to temp file
        import tempfile
        import sys
        
        # Ensure path is set (safe to do multiple times)
        sys_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
        if sys_path not in sys.path:
            sys.path.append(sys_path)
            
        from voice_assistant import transcribe_with_auto_language, cleanup_temp_files, detect_language_from_text, text_to_speech
        
        temp_audio = tempfile.NamedTemporaryFile(delete=False, suffix='.wav')
        audio.export(temp_audio.name, format='wav')
        
        with st.spinner("🎧 Listening & Detecting Language..."):
            # Auto-detect language during transcription
            text, lang, error = transcribe_with_auto_language(temp_audio.name)
            
            if text:
                st.success(f"✅ Heard ({lang}): {text}")
                user_query = text
                detected_lang = lang
            else:
                st.error(f"❌ {error}")
        
        # Cleanup temp file
        cleanup_temp_files(temp_audio.name)

    # 2. Text Input (Always visible)
    text_input = st.chat_input("...or type your question here")
    if text_input:
        user_query = text_input
        # For text input, we'll detect language from the text itself
        # This is imported inside the voice block, so we need to ensure import availability or move import up
        # Ideally imports should be at top, but for now we'll do inline or assume it's available if we hit this.
        # Let's import safely here too.
        sys_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
        if sys_path not in sys.path:
             sys.path.append(sys_path)
        from voice_assistant import detect_language_from_text, text_to_speech
        
        detected_lang = detect_language_from_text(user_query)

    # Process Query
    if user_query:
        st.session_state.messages.append({"role": "user", "content": user_query})
        
        with st.spinner("Thinking..."):
            db_session = SessionLocal()
            response = ask_ai(user_query, db_session)
            db_session.close()
        
        st.session_state.messages.append({"role": "assistant", "content": response})
        
        # Generate voice response (Auto-detect language of response)
        # We use the response text to decide the voice language, usually matching the input language logic 
        # but if the AI replies in English to a Hindi query (unlikely but possible), let's trust the response content.
        # Actually, let's trust the detected input language for continuity, UNLESS the response is clearly in another script.
        # But safest is to detect from response text.
        
        resp_lang = detect_language_from_text(response)
        
        # Create audio
        audio_path, error = text_to_speech(response, resp_lang)
        
        if audio_path:
            st.session_state.last_audio_response = audio_path
        else:
            st.warning(f"Voice synthesis failed: {error}")

    # Display History vs Current
    if st.session_state.messages:
        # 1. Previous History (Popover)
        previous_messages = st.session_state.messages[:-2]
        if previous_messages:
            with st.popover("🕒 View History"):
                for msg in previous_messages:
                    with st.chat_message(msg["role"]):
                        st.write(msg["content"])
        
        # 2. Current/Last Interaction (Directly in Sidebar)
        last_pair = st.session_state.messages[-2:]
        for msg in last_pair:
            with st.chat_message(msg["role"]):
                st.write(msg["content"])
        
        # 3. Play Voice Response Button
        if st.session_state.last_audio_response and os.path.exists(st.session_state.last_audio_response):
            st.audio(st.session_state.last_audio_response, format='audio/mp3')
    else:
        st.caption("No interactions yet.")
        
# Load data from Database
db = SessionLocal()
try:
    claims = db.query(Claim).all()
    print(f"DEBUG: Fetched {len(claims)} claims from DB")
except Exception as e:
    print(f"DEBUG: DB Error: {e}")
    claims = []
finally:
    db.close()

# Define columns first for consistent ordering
cols = ["UID", "Patient", "Insurance", "TPA", "Claim No", "Status", 
        "Total Bill", "Claim Amount", "Approved Amt", "Settled Amt", "Rejected Amt", "Ins. Balance",
        "Admission Date", "Settlement Date", "Email Date", "Processed At", "Claim Type"]

# Convert to DataFrame
data_list = []
for c in claims:
    # Determine Status
    status = c.claim_status
    if not status or str(status).lower() in ['null', 'none']:
        status = "Pending"

    try:
        data_list.append({
            "UID": c.email_uid, # Hidden ID for lookup
            "Patient": c.patient_name,
            "Insurance": c.insurance_company or "Unknown",
            "TPA": getattr(c, "tpa_name", None) or c.insurance_company or "-", # Fallback to Insurance

            "Claim No": c.claim_number,
            "Status": status.title() if status else "Pending",
            "Total Bill": c.total_bill_amount or 0.0,
            "Claim Amount": c.claim_amount or c.total_bill_amount or 0.0,  # Use claim_amount if available, fallback to total_bill
            "Approved Amt": c.approved_amount or c.settled_amount or 0.0,
            "Settled Amt": c.settled_amount or 0.0,
            "Rejected Amt": c.rejected_amount or 0.0,
            "Ins. Balance": c.balance_amount or 0.0,
            "Admission Date": c.claim_date or c.submitted_date,  # Use new claim_date field, fallback to old
            "Settlement Date": c.settlement_date or c.submitted_date,  # Use new settlement_date field, fallback to old
            "Email Date": c.email_date,
            "Processed At": c.processed_at,
            "Claim Type": getattr(c, "claim_type", "General") or "General"
        })
    except Exception as e:
        print(f"Error processing claim {c.id}: {e}")

# Create DataFrame with explicit column order
df = pd.DataFrame(data_list, columns=cols) if data_list else pd.DataFrame(columns=cols)

# --- GLOBAL SEARCH ENGINE ---
if search_term:
    # Filter: Check if ANY column contains the search term (case-insensitive)
    mask = df.astype(str).apply(lambda x: x.str.contains(search_term, case=False, na=False)).any(axis=1)
    df = df[mask]


# --- FILTERS ---
with st.sidebar.expander("🔍 Filter Options", expanded=False):
    # Date Filter (Moved to Top as requested)
    # Parse Settlement Date to datetime for Min/Max
    if "Settlement Date" in df.columns and not df["Settlement Date"].isnull().all():
        df["Settlement Date_Dt"] = pd.to_datetime(df["Settlement Date"], errors='coerce')
        min_date = df["Settlement Date_Dt"].min()
        max_date = df["Settlement Date_Dt"].max()
        
        if pd.notnull(min_date) and pd.notnull(max_date):
            min_date = min_date.date()
            max_date = max_date.date()
            date_range = st.date_input("Filter by Settlement Date", [min_date, max_date], min_value=min_date, max_value=max_date)
        else:
            date_range = []
    else:
        # st.warning("No Settlement Dates found.")
        date_range = []

    st.divider() # Visual separator

    filter_status = st.multiselect("Filter by Status", options=df['Status'].unique(), default=df['Status'].unique())
    filter_insurance = st.multiselect("Filter by Insurance", options=df['Insurance'].unique(), default=df['Insurance'].unique())
    filter_tpa = st.multiselect("Filter by TPA", options=df['TPA'].unique(), default=df['TPA'].unique())

    # Apply filters
    if not df.empty:
        if filter_status:
            df = df[df['Status'].isin(filter_status)]
        if filter_insurance:
            df = df[df['Insurance'].isin(filter_insurance)]
        if filter_tpa:
            df = df[df['TPA'].isin(filter_tpa)]
        
        # Apply Date Filter
        if date_range and len(date_range) == 2:
            start_date, end_date = date_range
            # Include records where Date is NaT OR within range
            df = df[(df["Settlement Date_Dt"].isnull()) | ((df["Settlement Date_Dt"].dt.date >= start_date) & (df["Settlement Date_Dt"].dt.date <= end_date))]

# --- KPIs ---
st.subheader("📊 Key Performance Indicators")

# Custom HTML Card Function
def kpi_card(title, value, color="#ffffff"):
    st.markdown(f"""
    <div style="
        background-color: {color};
        padding: 15px;
        border-radius: 10px;
        box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
        margin-bottom: 20px;
        text-align: center;
    ">
        <h4 style="margin: 0; color: #7f8c8d; font-size: 12px;">{title}</h4>
        <h2 style="margin: 5px 0 0 0; color: #2c3e50; font-size: 20px; word-wrap: break-word;">{value}</h2>
    </div>
    """, unsafe_allow_html=True)

col1, col2, col3, col4 = st.columns(4)

with col1:
    kpi_card("Total Claims", len(df))
with col2:
    kpi_card("Total Outstanding", f"₹{df['Ins. Balance'].sum():,.2f}")
with col3:
    kpi_card("Total Approved", f"₹{df['Approved Amt'].sum():,.2f}")
with col4:
    approved_count = len(df[df['Status'].isin(['Approved', 'Settled'])])
    rate = (approved_count / len(df) * 100) if len(df) > 0 else 0
    kpi_card("Approval Rate", f"{rate:.1f}%")

col5, col6, col7, col8 = st.columns(4)

with col5:
    pending = len(df[df['Status'] == 'Pending'])
    kpi_card("Pending Claims", pending)
with col6:
    avg_val = df['Total Bill'].mean() if not df.empty else 0
    kpi_card("Avg. Claim Value", f"₹{avg_val:,.2f}")
with col7:
    rejected_total = df['Rejected Amt'].sum() 
    kpi_card("Total Rejected", f"₹{rejected_total:,.2f}")
# Removed Patient Payable KPI as requested

# --- TABLE ---
st.subheader("📄 Recent Claims Data")
st.info("💡 Tip: Select a row to view the original document.")

# Sort by Admission Date_Dt if available, otherwise by Email Date
if "Admission Date_Dt" in df.columns:
    sorted_df = df.sort_values(by="Admission Date_Dt", ascending=False)
else:
    sorted_df = df.sort_values(by="Email Date", ascending=False) if "Email Date" in df.columns else df

# Add a hidden UID column for selection logic (if not already there)
# We need to map back to the original claim list or keep UID in the DF
# Let's ensure 'UID' is in the DF.
# data_list already has 'UID' if we add it below. I need to update the generator loop first.

# 1. Update Generator Loop to include UID (Hidden) (DO THIS IN A SEPARATE STEP IF NEEDED, BUT I CAN DO IT HERE IF I REWRITE THE LOOP... 
# actually the loop IS above. I will just rely on index matching for now or assume UID addition. 
# Wait, I need to add UID to data_list.
# I will patch the loop separately. For now, let's assume index alignment since sorted_df is derived.)

# Actually, index alignment is risky if sorted.
# BETTER: Add UID to the DataFrame creation in the previous step, but I can't easily jump back.
# I will use 'Claim No' as the key.

event = st.dataframe(
    sorted_df,
    use_container_width=True,
    hide_index=True,
    column_config={
        "View": st.column_config.TextColumn("View", width="small", help="Click row to view document"),
        "Admission Date": st.column_config.DateColumn(format="D MMM YYYY"),
        "Settlement Date": st.column_config.DateColumn(format="D MMM YYYY"),
        "Email Date": st.column_config.DatetimeColumn(format="D MMM YYYY, h:mm a"),
        "Approved Amt": st.column_config.NumberColumn(format="₹%.2f"),
        "Total Bill": st.column_config.NumberColumn(format="₹%.2f"),
        "Claim Amount": st.column_config.NumberColumn(format="₹%.2f"),
        "Settled Amt": st.column_config.NumberColumn(format="₹%.2f"),
        "Rejected Amt": st.column_config.NumberColumn(format="₹%.2f"),
        "Ins. Balance": st.column_config.NumberColumn(format="₹%.2f"),
        "UID": None, # Hide UID
    },
    selection_mode="single-row",
    on_select="rerun"
)

# --- DOCUMENT VIEWER ---
if len(event.selection["rows"]) > 0:
    selected_index = event.selection["rows"][0]
    selected_row = sorted_df.iloc[selected_index]
    uid = selected_row.get("UID")
    
    if uid:
        st.divider()
        st.subheader(f"📄 Document for: {selected_row.get('Patient', 'Unknown')}")
        
        # 1. Look for files in attachments/
        # Format: {uid}_{filename}
        # Note: uid might be just '123' or 'FILE:xyz'
        
        search_uid = str(uid).replace("FILE:", "") # Clean for matching
        attachment_dir = os.path.join(os.path.dirname(__file__), "..", "data", "attachments")

        # Fallback if specific data dir not found
        if not os.path.exists(attachment_dir):
             attachment_dir = os.path.join(os.path.dirname(__file__), "..", "attachments")
             
        found_file = None
        if os.path.exists(attachment_dir):
            for f in os.listdir(attachment_dir):
                if f.startswith(f"{search_uid}_"):
                    found_file = os.path.join(attachment_dir, f)
                    break
        
        if found_file:
            st.success(f"Found File: {os.path.basename(found_file)}")
            ext = os.path.splitext(found_file)[1].lower()
            
            # Display based on type
            if ext in [".png", ".jpg", ".jpeg"]:
                st.image(found_file, use_container_width=True)
            elif ext == ".pdf":
                 # PDF Embed
                import base64
                with open(found_file, "rb") as f:
                    base64_pdf = base64.b64encode(f.read()).decode('utf-8')
                pdf_display = f'<iframe src="data:application/pdf;base64,{base64_pdf}" width="100%" height="600" type="application/pdf"></iframe>'
                st.markdown(pdf_display, unsafe_allow_html=True)
            else:
                 # Download Button for others
                 with open(found_file, "rb") as f:
                     st.download_button("Download Document", f, file_name=os.path.basename(found_file))

            # --- ONE-TIME AUTO-FIX ---
            if str(selected_row.get("Claim No")) == "134317936" and ("Sep" in str(selected_row.get("Claim Date")) or "09-11" in str(selected_row.get("Claim Date"))):
                 with st.spinner("Correcting Date..."):
                     try:
                         text_content = extract_content_from_file(found_file)
                         if text_content:
                             new_data = extract_claim_data(text_content)
                             if new_data and new_data.get("submitted_date"):
                                 db_sess = SessionLocal()
                                 c_upd = db_sess.query(Claim).filter(Claim.claim_number == "134317936").first()
                                 if c_upd:
                                     c_upd.submitted_date = new_data["submitted_date"]
                                     if new_data.get("settled_amount"): c_upd.settled_amount = new_data["settled_amount"]
                                     db_sess.commit()
                                     st.success("Fixed!")
                                     time.sleep(1)
                                     st.rerun()
                                 db_sess.close()
                     except Exception as e: pass

            

        else:
             st.warning(f"No document found for UID: {search_uid}")
    else:
        st.error("Could not determine Claim UID.")

# --- CHARTS SECTION ---
st.markdown("---")
st.subheader("📈 Performance & Analytics Overview")

# First Row of Charts
col1, col2 = st.columns(2)
with col1:
    # 1. Approved vs Rejected Amount by Insurance (Bar Chart)
    st.markdown("<h3 style='text-align: center; color: #2c3e50;'>📉 Approved vs Rejected by Insurance</h3>", unsafe_allow_html=True)
    if not df.empty:
        fin_df = df.groupby("Insurance")[["Approved Amt", "Rejected Amt"]].sum().reset_index()
        fin_df_long = fin_df.melt(id_vars="Insurance", var_name="Type", value_name="Amount")
        fig_fin = px.bar(
            fin_df_long, x='Insurance', y='Amount', color='Type', barmode='group',
            color_discrete_map={"Approved Amt": "#2ecc71", "Rejected Amt": "#e74c3c"}
        )
        fig_fin.update_layout(xaxis_title=None, yaxis_title="Amount (₹)", height=400,
                             legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1))
        st.plotly_chart(fig_fin, use_container_width=True)
    else:
        st.info("No data available.")

with col2:
    # 2. Total Bill Composition (Treemap)
    st.markdown("<h3 style='text-align: center; color: #2c3e50;'>🌳 Bill Value Distribution</h3>", unsafe_allow_html=True)
    if not df.empty:
        fig_tree = px.treemap(
            df, path=[px.Constant("All Claims"), 'Insurance', 'Status'], values='Total Bill',
            color='Total Bill', color_continuous_scale='RdYlGn'
        )
        fig_tree.update_layout(height=400, margin=dict(t=30, b=0, l=0, r=0))
        st.plotly_chart(fig_tree, use_container_width=True)
    else:
        st.info("No data.")

# Second Row of Charts
col3, col4 = st.columns(2)
with col3:
    # 3. Claims by Status (Pie Chart)
    st.markdown("<h3 style='text-align: center; color: #2c3e50;'>🍕 Claims by Status</h3>", unsafe_allow_html=True)
    if not df.empty:
        status_df = df.groupby("Status").size().reset_index(name='Count')
        fig_pie = px.pie(status_df, values='Count', names='Status', hole=0.5, color_discrete_sequence=px.colors.sequential.RdBu)
        fig_pie.update_traces(textinfo='percent+label')
        fig_pie.update_layout(height=400, margin=dict(t=30, b=0, l=0, r=0), showlegend=False)
        st.plotly_chart(fig_pie, use_container_width=True)
    else:
        st.info("No data.")

with col4:
    # 4. Daily Claim Trend Chart (Total Bill Amount)
    st.markdown("<h3 style='text-align: center; color: #2c3e50;'>📈 Daily Claim Trend</h3>", unsafe_allow_html=True)
    date_col = "Settlement Date" if "Settlement Date" in df.columns and not df["Settlement Date"].isnull().all() else "Email Date"
    if not df.empty and date_col in df.columns:
        trend_df = df.copy()
        # Group by specific Date (Day) instead of Month
        trend_df['Date'] = pd.to_datetime(trend_df[date_col], errors='coerce').dt.date
        daily_stats = trend_df.groupby('Date')['Total Bill'].sum().reset_index().sort_values('Date')
        
        fig_trend = px.line(daily_stats, x='Date', y='Total Bill', markers=True, line_shape='spline')
        fig_trend.update_xaxes(tickformat="%d %b %Y")
        fig_trend.update_traces(line_color='#3498db', line_width=3)
        fig_trend.update_layout(height=400, margin=dict(t=30, b=0, l=0, r=0))
        st.plotly_chart(fig_trend, use_container_width=True)
    else:
        st.info("Insufficient date data for trend analysis.")

# --- SETTLEMENT EFFICIENCY ---
st.markdown("---")
if not df.empty:
    total_requested = df["Total Bill"].sum()
    total_approved = df["Approved Amt"].sum()
    efficiency = (total_approved / total_requested * 100) if total_requested > 0 else 0

    # Align Headers in one line
    col_h1, col_h2 = st.columns([1, 2])
    with col_h1:
        st.subheader("💰 Settlement Efficiency Analysis")
    with col_h2:
        st.subheader("🏆 Approval Performance Analysis")
    
    col_eff1, col_eff2 = st.columns([1, 2])
    with col_eff1:
        st.metric("Recovery Rate", f"{efficiency:.1f}%", help="Percentage of total bill approved by insurance.")
        st.progress(efficiency / 100)
    
    with col_eff2:
        # NEW Performance Chart: Approval Performance by Insurance
        if not df.empty:
            perf_df = df.groupby("Insurance").agg({
                "Total Bill": "sum",
                "Approved Amt": "sum"
            }).reset_index()
            perf_df["Approval Ratio"] = (perf_df["Approved Amt"] / perf_df["Total Bill"] * 100).round(1)
            
            fig_perf = px.bar(
                perf_df, 
                x="Approval Ratio", 
                y="Insurance", 
                orientation='h',
                text="Approval Ratio",
                color="Approval Ratio",
                labels={"Approval Ratio": "Approval Rate (%)"},
                color_continuous_scale='Greens'
            )
            fig_perf.update_traces(texttemplate='%{text}%', textposition='inside')
            fig_perf.update_layout(height=300, margin=dict(t=10, b=0, l=0, r=0), coloraxis_showscale=False)
            st.plotly_chart(fig_perf, use_container_width=True)


# Dashboard footer or padding
st.markdown("<br><br>", unsafe_allow_html=True)

