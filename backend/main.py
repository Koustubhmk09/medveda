import os
import logging
import json
from pathlib import Path
from fastapi import FastAPI, HTTPException, Depends, File, Form, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
from jose import JWTError, jwt
import threading
import time
from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
import bcrypt
from typing import Optional

from src.helper import download_hugging_face_embeddings
from src.prompt import system_prompt, title_generation_prompt
from src.workflow import create_workflow
from src.database import get_db, Base, engine
from src.models import Admin, Patient
from fastapi.security import OAuth2PasswordBearer

from langchain_pinecone import PineconeVectorStore
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.tools import create_retriever_tool, tool
from langchain_core.messages import HumanMessage, AIMessage
from duckduckgo_search import DDGS
from uuid import uuid4

# Audio Transcript Services
from src.services.audio_transcript import (
    get_language_list,
    process_audio,
    process_multi_speaker_audio,
)
from src.services.ask_anything_service import answer_question as answer_audio_question

# Load environment variables
load_dotenv()

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="MedVeda AI")

# Enable CORS
raw_origins = os.environ.get("ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173,https://medveda-ai.onrender.com")
origins = [o.strip() for o in raw_origins.split(",")]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins, 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

PINECONE_API_KEY = os.environ.get('PINECONE_API_KEY')
PINECONE_INDEX_NAME = os.environ.get('PINECONE_INDEX_NAME')
GOOGLE_API_KEY = os.environ.get('GOOGLE_API_KEY')
GEMINI_MODEL = os.environ.get('GEMINI_MODEL', 'gemini-2.5-flash')

# AI Initialization State Tracking
class AIStatus:
    def __init__(self):
        self.status = "initializing" # initializing, ready, failed
        self.error = None
        self.last_ping = None
        self.start_time = time.time()

ai_status = AIStatus()

# Password hashing setup (Custom Bcrypt implementation to fix compatibility and 72-byte limit issues)
class PwdContext:
    def hash(self, password: str) -> str:
        # Bcrypt has a 72-byte limit. We truncate manually to avoid ValueError in newer bcrypt versions.
        truncated_password = password.encode('utf-8')[:72]
        return bcrypt.hashpw(truncated_password, bcrypt.gensalt()).decode('utf-8')
    
    def verify(self, password: str, hashed: str) -> bool:
        if not hashed:
            return False
        truncated_password = password.encode('utf-8')[:72]
        # Ensure hashed is in bytes for checkpw
        hashed_bytes = hashed.encode('utf-8') if isinstance(hashed, str) else hashed
        return bcrypt.checkpw(truncated_password, hashed_bytes)

pwd_context = PwdContext()

# Global variable for the AI workflow
app_workflow = None
llm = None

# JSON Storage Setup
DATA_DIR = Path("data/chats")
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Audio Uploads and Logs Setup
UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR = Path("logs")
LOGS_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".webm"}
MIME_EXTENSION_MAP = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/wav": ".wav",
    "audio/wave": ".wav",
    "audio/x-wav": ".wav",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/ogg": ".ogg",
    "audio/flac": ".flac",
    "audio/webm": ".webm",
}

def get_chats_file(user_id):
    return DATA_DIR / f"chats_{user_id}.json"

def load_user_chats(user_id):
    file_path = get_chats_file(user_id)
    if not file_path.exists():
        return []
    try:
        with open(file_path, "r") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading chats for user {user_id}: {e}")
        return []

def save_user_chats(user_id, chats):
    file_path = get_chats_file(user_id)
    try:
        with open(file_path, "w") as f:
            json.dump(chats, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving chats for user {user_id}: {e}")

def call_gemini_with_retry(func, *args, max_retries=3, **kwargs):
    """Simple retry logic for handling transient Gemini API errors (like 429)."""
    last_error = None
    for i in range(max_retries):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            last_error = e
            error_str = str(e).lower()
            # If it's a quota or rate limit error, wait and retry
            if "429" in error_str or "resource_exhausted" in error_str:
                wait_time = (i + 1) * 2 # Exponential wait
                logger.warning(f"Gemini Rate Limit (429). Retrying in {wait_time}s... (Attempt {i+1}/{max_retries})")
                time.sleep(wait_time)
                continue
            raise e
    raise last_error

def initialize_ai():
    global app_workflow, llm
    try:
        if not PINECONE_API_KEY or not PINECONE_INDEX_NAME or not GOOGLE_API_KEY:
            ai_status.status = "failed"
            ai_status.error = "Core API keys missing in .env"
            logger.warning("Core API keys missing. AI features will not work until .env is configured.")
            return

        logger.info("Background: Initializing MedVeda AI Core components...")
        
        # 1. Initialize Embeddings
        ai_status.status = "loading_embeddings"
        embeddings = download_hugging_face_embeddings()
        
        # 2. Connect to Pinecone
        ai_status.status = "connecting_vectorstore"
        vectorstore = PineconeVectorStore.from_existing_index(
            index_name=PINECONE_INDEX_NAME,
            embedding=embeddings
        )
        
        # Clinical Retriever (GALE + Merck)
        clinical_retriever = vectorstore.as_retriever(
            search_kwargs={
                'k': 4,
                'filter': {"knowledge_type": "clinical_knowledge"}
            }
        )
        
        # Medicine Retriever (Davis Drug Guide)
        medicine_retriever = vectorstore.as_retriever(
            search_kwargs={
                'k': 3,
                'filter': {"knowledge_type": "medicine_intelligence"}
            }
        )

        llm = ChatGoogleGenerativeAI(
            model=GEMINI_MODEL,
            temperature=0.4, # Lower temperature for clinical precision
            google_api_key=GOOGLE_API_KEY,
        )

        # 3. Define Tools
        clinical_tool = create_retriever_tool(
            clinical_retriever,
            "clinical_database_search",
            "Search for disease facts, symptoms, and diagnostic criteria from GALE and Merck manuals."
        )
        
        medicine_tool = create_retriever_tool(
            medicine_retriever,
            "medicine_database_search",
            "Search for drug dosage, contraindications, and safety rules from Davis Drug Guide."
        )
        
        @tool
        def web_search(query: str):
            """Search the web for real-time information."""
            try:
                with DDGS() as ddgs:
                    results = list(ddgs.text(query, max_results=5))
                    return "\n\n---\n\n".join([f"### {r.get('title')}\n{r.get('body')}" for r in results])
            except Exception as e:
                return f"Error: {str(e)}"
        
        tools = [clinical_tool, medicine_tool, web_search]
        app_workflow = create_workflow(llm, tools)

        ai_status.status = "ready"
        logger.info(f"AI components initialized successfully in {time.time() - ai_status.start_time:.2f}s")
    except Exception as e:
        ai_status.status = "failed"
        ai_status.error = str(e)
        logger.error(f"Startup error: {e}")

from fastapi.responses import JSONResponse
from fastapi import Request

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Global error caught: {exc}")
    return JSONResponse(
        status_code=500,
        content={"detail": str(exc)},
        headers={
            "Access-Control-Allow-Origin": "http://localhost:5173",
            "Access-Control-Allow-Credentials": "true"
        }
    )

@app.on_event("startup")
async def startup_event():
    # Start initialization in a separate thread so the server starts INSTANTLY
    # This resolves the 10-minute wait on Render by making the server responsive immediately
    thread = threading.Thread(target=initialize_ai)
    thread.daemon = True
    thread.start()
    
    # Create Database Tables (only Admin/Patient now)
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("Database tables verified.")
    except Exception as e:
        logger.error(f"Database initialization error: {e}")

class ChatRequest(BaseModel):
    message: str
    chat_id: Optional[str] = None
    patient_id: Optional[str] = None

class DoctorRegisterRequest(BaseModel):
    full_name: str
    email: str
    password: str
    specialty: Optional[str] = None
    license_number: Optional[str] = None
    hospital_name: Optional[str] = None

class DoctorLoginRequest(BaseModel):
    email: str
    password: str

# JWT Config
SECRET_KEY = os.environ.get("SECRET_KEY", "medveda_ai_secret_key_123456789")
ALGORITHM = os.environ.get("ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.environ.get("ACCESS_TOKEN_EXPIRE_MINUTES", 1440))

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token", auto_error=False)

async def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: str = payload.get("sub")
        admin = db.query(Admin).filter(Admin.id == int(user_id)).first()
        if admin is None:
            raise HTTPException(status_code=401, detail="Admin not found")
        return admin
    except JWTError:
        raise HTTPException(status_code=401, detail="Could not validate credentials")

# JSON Storage Endpoints
@app.get("/doctor/stats")
async def get_doctor_stats(current_user: Admin = Depends(get_current_user), db: Session = Depends(get_db)):
    # All doctors see all patients in the small clinic
    count = db.query(Patient).count()
    return {"patients_count": count}

@app.get("/chats")
async def get_chats(current_user: Admin = Depends(get_current_user)):
    chats = load_user_chats(current_user.id)
    # Return brief info for sidebar, including patient_id for data isolation
    return [{"id": c["id"], "title": c["title"], "created_at": c["created_at"], "patient_id": c.get("patient_id")} for c in chats]

@app.get("/chats/{chat_id}")
async def get_chat(chat_id: str, current_user: Admin = Depends(get_current_user)):
    chats = load_user_chats(current_user.id)
    chat = next((c for c in chats if c["id"] == chat_id), None)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    return chat

@app.delete("/chats/{chat_id}")
async def delete_chat(chat_id: str, current_user: Admin = Depends(get_current_user)):
    chats = load_user_chats(current_user.id)
    chats = [c for c in chats if c["id"] != chat_id]
    save_user_chats(current_user.id, chats)
    return {"message": "Deleted"}

@app.post("/chat")
async def chat(request: ChatRequest, current_user: Admin = Depends(get_current_user), db: Session = Depends(get_db)):
    if app_workflow is None:
        raise HTTPException(status_code=503, detail="AI components not initialized")
    
    chat_id = request.chat_id
    chats = load_user_chats(current_user.id)
    
    history = []
    active_chat = next((c for c in chats if c["id"] == chat_id), None) if chat_id else None
    
    if active_chat:
        # Pass last 10 messages for context to keep it snappy but aware
        for msg in active_chat.get("messages", [])[-10:]:
            if msg["role"] == "user":
                history.append(HumanMessage(content=msg["content"]))
            else:
                history.append(AIMessage(content=msg["content"]))
    
    # Process with Advanced LangGraph Workflow
    try:
        # Fetch Patient Data if provided
        patient_context = "N/A"
        if request.patient_id:
            patient = db.query(Patient).filter(Patient.patient_id == request.patient_id).first()
            if patient:
                patient_context = (
                    f"Patient: {patient.full_name}, Age: {patient.age}, Gender: {patient.gender}, "
                    f"Blood Group: {patient.blood_group or 'N/A'}, Contact: {patient.contact_no or 'N/A'}, "
                    f"Visit Date: {patient.visit_date or 'N/A'}, Visit Type: {patient.visit_type or 'N/A'}, "
                    f"Symptoms: {patient.symptoms}, History: {patient.primary_disease or 'N/A'}, "
                    f"Past Treatment: {patient.prescribed_medicine or 'N/A'}"
                )

        doc_specialty = current_user.specialty
        if not doc_specialty or doc_specialty == "Normal patients checking doctor":
            doc_specialty = "General Practitioner"

        doctor_context = (
            f"Dr. {current_user.full_name}, Specialty: {doc_specialty}, "
            f"Clinic: {current_user.hospital_name or 'Independent Clinic'}"
        )

        inputs = {
            "query": request.message, 
            "messages": history,
            "patient_context": patient_context,
            "doctor_context": doctor_context
        }
        # Use retry logic for the main workflow invocation
        result = call_gemini_with_retry(app_workflow.invoke, inputs)
        answer = result["final_answer"]
    except Exception as e:
        logger.error(f"AI error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    # Persistence logic
    chat_title = None
    new_chat_created = False
    
    if not active_chat:
        chat_id = str(int(datetime.now(timezone.utc).timestamp()))
        new_chat_created = True
        
        # LAZY TITLE GENERATION: Use message snippet first to save API quota
        chat_title = request.message[:30] + ( "..." if len(request.message) > 30 else "")
        
        # Only try LLM title generation if it's a non-greeting to save quota
        # and wrap it in a try-block so it doesn't crash the chat if quota is hit
        try:
            # We don't use retry here as title is non-critical
            title_response = llm.invoke(title_generation_prompt.format(query=request.message))
            if title_response and title_response.content:
                chat_title = title_response.content.strip().strip('"').strip("'")
        except Exception as e:
            logger.warning(f"Title generation skipped due to quota or error: {e}")

        active_chat = {
            "id": chat_id,
            "title": chat_title,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "patient_id": request.patient_id,
            "messages": []
        }
        chats.append(active_chat)

    active_chat["messages"].append({"role": "user", "content": request.message, "created_at": datetime.now(timezone.utc).isoformat()})
    active_chat["messages"].append({"role": "assistant", "content": answer, "created_at": datetime.now(timezone.utc).isoformat()})

    save_user_chats(current_user.id, chats)
    return {"answer": answer, "chat_id": chat_id, "title": chat_title if new_chat_created else active_chat.get("title")}

@app.post("/auth/doctor/register")
async def doctor_register(request: DoctorRegisterRequest, db: Session = Depends(get_db)):
    # Check if admin already exists
    existing_admin = db.query(Admin).filter(Admin.email == request.email).first()
    if existing_admin:
        raise HTTPException(status_code=400, detail="Admin with this email already exists")

    hashed_password = pwd_context.hash(request.password)
    new_admin = Admin(
        email=request.email,
        full_name=request.full_name,
        specialty=request.specialty,
        license_number=request.license_number,
        hospital_name=request.hospital_name,
        hashed_password=hashed_password
    )
    db.add(new_admin)
    db.commit()
    db.refresh(new_admin)
    return {"message": "Admin registered successfully", "admin_id": new_admin.id}

@app.post("/auth/doctor/login")
async def doctor_login(request: DoctorLoginRequest, db: Session = Depends(get_db)):
    admin = db.query(Admin).filter(Admin.email == request.email).first()
    if not admin or not admin.hashed_password:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    
    if not pwd_context.verify(request.password, admin.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    access_token = jwt.encode(
        {"sub": str(admin.id), "email": admin.email, "exp": datetime.now(timezone.utc) + timedelta(days=7)},
        SECRET_KEY, 
        algorithm=ALGORITHM
    )
    return {
        "access_token": access_token, 
        "user": {
            "id": admin.id, 
            "email": admin.email, 
            "full_name": admin.full_name,
            "specialty": admin.specialty,
            "license_number": admin.license_number,
            "hospital_name": admin.hospital_name
        }
    }

@app.get("/patients")
async def get_patients(current_user: Admin = Depends(get_current_user), db: Session = Depends(get_db)):
    # Simple list of all 20 patients for the clinic
    patients = db.query(Patient).order_by(Patient.patient_id).all()
    
    return [
        {
            "patient_id": p.patient_id,
            "full_name": p.full_name,
            "age": p.age,
            "gender": p.gender,
            "blood_group": p.blood_group,
            "contact_no": p.contact_no,
            "visit_date": p.visit_date,
            "symptoms": p.symptoms,
            "primary_disease": p.primary_disease,
            "prescribed_medicine": p.prescribed_medicine,
            "visit_type": p.visit_type,
        }
        for p in patients
    ]


@app.get("/")
async def root():
    return {"message": "Welcome to MedVeda AI"}

@app.get("/health")
async def health():
    # Update last ping to track cron-job activity
    ai_status.last_ping = datetime.now(timezone.utc).isoformat()
    
    return {
        "status": "online" if ai_status.status == "ready" else "initializing",
        "ai_state": ai_status.status,
        "error": ai_status.error,
        "uptime_seconds": int(time.time() - ai_status.start_time),
        "last_ping": ai_status.last_ping,
        "message": "MedVeda AI is ready" if ai_status.status == "ready" else f"AI is currently: {ai_status.status}"
    }

def get_upload_extension(file: UploadFile) -> str:
    extension = os.path.splitext(file.filename or "")[1].lower()
    if extension:
        return extension
    content_type = (file.content_type or "").split(";")[0].strip().lower()
    return MIME_EXTENSION_MAP.get(content_type, "")

def infer_audio_source(file: UploadFile) -> str:
    filename = (file.filename or "").strip().lower()
    if filename in {"recording.wav", "recording.webm", "recording.ogg"}:
        return "Recording"
    return "Upload"

def save_uploaded_file(file: UploadFile, extension: str) -> str:
    if extension not in ALLOWED_AUDIO_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{extension}'. Allowed: {', '.join(ALLOWED_AUDIO_EXTENSIONS)}",
        )
    filename = f"{uuid4().hex}{extension}"
    file_path = UPLOAD_DIR / filename
    with open(file_path, "wb") as buffer:
        buffer.write(file.file.read())
    return str(file_path)

# Audio Transcription Endpoints
@app.get("/languages")
async def get_languages():
    """Returns the full list of supported languages for source audio detection."""
    return {"languages": get_language_list()}

@app.post("/process-audio")
async def process_audio_api(
    file: UploadFile = File(..., description="Audio file to process"),
    task: str = Form(..., description="One of: 'Full Transcript', 'Summary', 'Keywords'"),
    language: str = Form(None, description="Source language code (or 'auto' for auto-detection)"),
    target_language: str = Form(None, description="Target language code for translation output"),
):
    valid_tasks = {"Full Transcript", "Summary", "Keywords"}
    if task not in valid_tasks:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid task '{task}'. Must be one of: {', '.join(valid_tasks)}",
        )

    try:
        extension = get_upload_extension(file)
        start_total = time.perf_counter()
        file_path = save_uploaded_file(file, extension)

        # We use process_audio (Gemini Direct) instead of process_multi_speaker_audio (Diarization Subprocess)
        # to avoid the need for a local ML environment and fixed [WinError 2] issues.
        result = process_audio(
            file_path=file_path,
            task=task,
            language=language,
            target_language=target_language,
        )

        total_time = time.perf_counter() - start_total
        logger.info(f"Audio processed in {total_time:.2f}s")

        return result
    except Exception as e:
        logger.error(f"Audio processing failed: {e}")
        raise HTTPException(status_code=500, detail=f"Processing failed: {str(e)}")

class AudioPersistRequest(BaseModel):
    transcript: str
    output: str
    patient_id: str
    chat_id: Optional[str] = None

@app.post("/audio/persist-chat")
async def persist_audio_chat(request: AudioPersistRequest, current_user: Admin = Depends(get_current_user)):
    chat_id = request.chat_id
    chats = load_user_chats(current_user.id)
    new_chat_created = False
    chat_title = "Audio Intelligence Session"

    if not chat_id:
        chat_id = str(uuid4())
        new_chat_created = True
        # Generate a short title from the transcript if possible
        if len(request.transcript) > 10:
            try:
                # Simple fallback for title if LLM not available or just use first few words
                chat_title = " ".join(request.transcript.split()[:5]) + "..."
            except:
                pass
        
        active_chat = {
            "id": chat_id,
            "title": chat_title,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "patient_id": request.patient_id,
            "messages": []
        }
        chats.append(active_chat)
    else:
        active_chat = next((c for c in chats if c["id"] == chat_id), None)
        if not active_chat:
             # Fallback if ID sent but not found
             chat_id = str(uuid4())
             new_chat_created = True
             active_chat = {
                "id": chat_id,
                "title": chat_title,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "patient_id": request.patient_id,
                "messages": []
            }
             chats.append(active_chat)

    # Append transcript as user message and output as AI response
    active_chat["messages"].append({
        "role": "user", 
        "content": f"[Audio Transcript]\n{request.transcript}", 
        "created_at": datetime.now(timezone.utc).isoformat()
    })
    active_chat["messages"].append({
        "role": "assistant", 
        "content": request.output, 
        "created_at": datetime.now(timezone.utc).isoformat()
    })

    save_user_chats(current_user.id, chats)
    return {
        "chat_id": chat_id, 
        "title": active_chat.get("title"),
        "new_chat": new_chat_created
    }

class AudioQuestionRequest(BaseModel):
    question: str
    transcript: str

@app.post("/audio/ask-question")
async def ask_audio_question_api(request: AudioQuestionRequest):
    try:
        result = answer_audio_question(
            question=request.question,
            transcript_text=request.transcript
        )
        return result
    except Exception as e:
        logger.error(f"Audio QA failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    # Use the PORT environment variable provided by Render
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
