"""
Real-Time Multimodal Voice Receptionist
========================================
Production-grade AI Voice Agent powered by:
- FastAPI + WebSockets for real-time streaming
- Local Whisper (CPU/GPU) for Speech-to-Text
- Groq API (Llama 3.3 70B / 3.1 8B) for ultra-fast LLM inference
- Microsoft Edge-TTS for natural neural voice synthesis
- In-memory Calendar & CRM store with complete REST CRUD APIs
"""

import os
import re
import json
import time
import base64
import wave
import tempfile
import asyncio
from datetime import datetime, timedelta, date
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field, asdict
from enum import Enum

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel
import edge_tts
import groq
from dotenv import load_dotenv, find_dotenv

# ─── Load Environment ────────────────────────────────────────────────────────
load_dotenv(find_dotenv(usecwd=True), override=True)
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
DEFAULT_TTS_VOICE = os.getenv("TTS_VOICE", "en-US-AriaNeural")

# ─── FastAPI App ─────────────────────────────────────────────────────────────
app = FastAPI(title="Brightside AI Voice Receptionist", version="2.0.0")
app.mount("/static", StaticFiles(directory="static"), name="static")

# ─── App Settings State ──────────────────────────────────────────────────────
class AppConfig:
    def __init__(self):
        self.clinic_name = "Brightside Dental Clinic"
        self.clinic_phone = "(555) 234-5678"
        self.business_hours = ("09:00", "17:00")
        self.tts_voice = DEFAULT_TTS_VOICE
        self.groq_model = "llama-3.3-70b-versatile"
        self.groq_temperature = 0.2
        self.slot_duration = 30  # minutes

config = AppConfig()

# ─── Whisper Model Loading ───────────────────────────────────────────────────
print("🔄 Initializing local Whisper model...")
whisper_model = None
WHISPER_BACKEND = "none"

try:
    from faster_whisper import WhisperModel
    whisper_model = WhisperModel("tiny", device="cpu", compute_type="int8")
    WHISPER_BACKEND = "faster"
    print("✅ faster-whisper (tiny) loaded.")
except Exception as e:
    print(f"ℹ️  faster-whisper note: {e}. Trying openai-whisper...")
    try:
        import whisper
        whisper_model = whisper.load_model("tiny")
        WHISPER_BACKEND = "openai"
        print("✅ openai-whisper (tiny) loaded.")
    except Exception as e2:
        print(f"⚠️  Whisper loading failed: {e2}")

# ─── Groq Client ─────────────────────────────────────────────────────────────
groq_client = groq.Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

# ─── Service Catalog ─────────────────────────────────────────────────────────
SERVICES_CATALOG = [
    {
        "id": "checkup",
        "name": "Comprehensive Dental Checkup",
        "price": 50,
        "duration": "30 mins",
        "description": "Full oral examination, digital X-rays, and customized treatment planning."
    },
    {
        "id": "cleaning",
        "name": "Professional Teeth Cleaning",
        "price": 80,
        "duration": "45 mins",
        "description": "Deep plaque removal, tartar scaling, ultrasonic polishing, and enamel fluoride treatment."
    },
    {
        "id": "consultation",
        "name": "Initial Consultation",
        "price": 0,
        "duration": "20 mins",
        "description": "Free consultation with Dr. Ava & team for braces, implants, or cosmetic smile makeovers."
    },
    {
        "id": "whitening",
        "name": "Laser Teeth Whitening",
        "price": 150,
        "duration": "60 mins",
        "description": "In-office instant brightening up to 8 shades lighter using gentle LED laser activation."
    },
    {
        "id": "rootcanal",
        "name": "Root Canal Therapy",
        "price": 300,
        "duration": "60 mins",
        "description": "Painless endodontic treatment to save infected teeth and provide instant pain relief."
    },
    {
        "id": "emergency",
        "name": "Emergency Dental Care",
        "price": 120,
        "duration": "45 mins",
        "description": "Same-day priority care for severe toothaches, broken teeth, or acute oral trauma."
    }
]

# ─── Data Models ─────────────────────────────────────────────────────────────
@dataclass
class Appointment:
    id: str
    name: str
    phone: str
    service: str
    datetime: datetime
    status: str = "confirmed"  # confirmed, completed, cancelled
    notes: str = ""
    created_at: datetime = field(default_factory=datetime.now)

@dataclass
class CRMLead:
    id: str
    name: str
    phone: str
    email: str
    source: str
    status: str = "new"  # new, qualified, booked, contacted
    appointments: List[str] = field(default_factory=list)
    notes: str = ""
    created_at: datetime = field(default_factory=datetime.now)

# ─── Mock Calendar & CRM ─────────────────────────────────────────────────────
class MockCalendar:
    def __init__(self):
        self.appointments: Dict[str, Appointment] = {}
        self._seed_data()

    def _seed_data(self):
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow = today + timedelta(days=1)
        day_after = today + timedelta(days=2)

        self.book("Sarah Jenkins", "555-0192", "Professional Teeth Cleaning", tomorrow.replace(hour=10, minute=0), notes="Routine 6-month cleaning")
        self.book("Michael Chen", "555-0144", "Comprehensive Dental Checkup", tomorrow.replace(hour=14, minute=0), notes="First visit patient")
        self.book("Elena Rostova", "555-0187", "Laser Teeth Whitening", day_after.replace(hour=11, minute=30), notes="Event preparation")

    def get_available_slots(self, dt: datetime) -> List[str]:
        target_date = dt.date()
        booked_times = {
            appt.datetime.strftime("%H:%M")
            for appt in self.appointments.values()
            if appt.datetime.date() == target_date and appt.status != "cancelled"
        }

        available = []
        start_hour, start_min = map(int, config.business_hours[0].split(":"))
        end_hour, end_min = map(int, config.business_hours[1].split(":"))

        current = dt.replace(hour=start_hour, minute=start_min, second=0, microsecond=0)
        end = dt.replace(hour=end_hour, minute=end_min, second=0, microsecond=0)

        while current < end:
            t_str = current.strftime("%H:%M")
            if t_str not in booked_times:
                available.append(t_str)
            current += timedelta(minutes=config.slot_duration)

        return available

    def book(self, name: str, phone: str, service: str, dt: datetime, notes: str = "") -> Optional[Appointment]:
        t_str = dt.strftime("%H:%M")
        available = self.get_available_slots(dt)

        if t_str not in available:
            return None

        appt_id = f"APPT-{len(self.appointments) + 1:04d}"
        appt = Appointment(
            id=appt_id,
            name=name.strip(),
            phone=phone.strip(),
            service=service.strip(),
            datetime=dt,
            status="confirmed",
            notes=notes
        )
        self.appointments[appt_id] = appt
        return appt

    def cancel(self, appt_id: str) -> bool:
        if appt_id in self.appointments:
            self.appointments[appt_id].status = "cancelled"
            return True
        return False

    def delete(self, appt_id: str) -> bool:
        if appt_id in self.appointments:
            del self.appointments[appt_id]
            return True
        return False

class MockCRM:
    def __init__(self):
        self.leads: Dict[str, CRMLead] = {}
        self._seed_data()

    def _seed_data(self):
        l1 = self.create_lead("Sarah Jenkins", "555-0192", "sarah.j@example.com", "Voice Receptionist")
        self.add_appointment_to_lead(l1.id, "APPT-0001")
        self.add_note(l1.id, "Booked Teeth Cleaning for tomorrow at 10:00 AM via Voice.")

        l2 = self.create_lead("Michael Chen", "555-0144", "mchen@techcorp.io", "Voice Receptionist")
        self.add_appointment_to_lead(l2.id, "APPT-0002")
        self.add_note(l2.id, "Inquired about general checkup. Booked for tomorrow 2:00 PM.")

        l3 = self.create_lead("Elena Rostova", "555-0187", "elena.r@designstudio.co", "Website Chat")
        self.add_appointment_to_lead(l3.id, "APPT-0003")

    def create_lead(self, name: str, phone: str, email: str = "", source: str = "Voice Call") -> CRMLead:
        lead_id = f"LEAD-{len(self.leads) + 1:04d}"
        lead = CRMLead(
            id=lead_id,
            name=name.strip() if name else "Caller",
            phone=phone.strip() if phone else "Unknown",
            email=email.strip(),
            source=source,
            status="new"
        )
        self.leads[lead_id] = lead
        return lead

    def find_by_phone(self, phone: str) -> Optional[CRMLead]:
        clean_phone = re.sub(r"[^\d]", "", phone)
        for lead in self.leads.values():
            if re.sub(r"[^\d]", "", lead.phone) == clean_phone and clean_phone != "":
                return lead
        return None

    def add_appointment_to_lead(self, lead_id: str, appt_id: str) -> bool:
        if lead_id in self.leads:
            if appt_id not in self.leads[lead_id].appointments:
                self.leads[lead_id].appointments.append(appt_id)
            self.leads[lead_id].status = "booked"
            return True
        return False

    def add_note(self, lead_id: str, note: str) -> bool:
        if lead_id in self.leads:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M")
            if self.leads[lead_id].notes:
                self.leads[lead_id].notes += f"\n[{ts}] {note}"
            else:
                self.leads[lead_id].notes = f"[{ts}] {note}"
            return True
        return False

# Global stores & call counter
calendar = MockCalendar()
crm = MockCRM()
total_calls_count = 28
start_timestamp = time.time()

# ─── Conversation State ──────────────────────────────────────────────────────
class ConversationState(Enum):
    GREETING = "greeting"
    COLLECTING_NAME = "collecting_name"
    COLLECTING_SERVICE = "collecting_service"
    COLLECTING_DATE_TIME = "collecting_date_time"
    CONFIRMING = "confirming"
    BOOKED = "booked"
    INQUIRY = "inquiry"

@dataclass
class Conversation:
    state: ConversationState = ConversationState.GREETING
    name: str = ""
    phone: str = ""
    service: str = ""
    preferred_date: str = ""   # YYYY-MM-DD
    preferred_time: str = ""   # HH:MM (24h)
    lead_id: Optional[str] = None
    appt_id: Optional[str] = None
    history: List[Dict[str, str]] = field(default_factory=list)

# ─── Robust Date & Time Helpers ──────────────────────────────────────────────
def normalize_time(time_input: str) -> Optional[str]:
    """Convert various time formats ('2pm', '2:00 PM', '14:00', '9:30am') to 'HH:MM'."""
    if not time_input:
        return None
    s = time_input.strip().upper()
    try:
        # Match '14:00' or '09:30'
        if re.match(r"^\d{1,2}:\d{2}$", s):
            parts = s.split(":")
            h, m = int(parts[0]), int(parts[1])
            if 0 <= h < 24 and 0 <= m < 60:
                return f"{h:02d}:{m:02d}"

        # Match '2 PM', '2:30 PM', '2PM', '9AM'
        match = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(AM|PM)$", s)
        if match:
            h = int(match.group(1))
            m = int(match.group(2) or 0)
            meridiem = match.group(3)
            if meridiem == "PM" and h < 12:
                h += 12
            elif meridiem == "AM" and h == 12:
                h = 0
            return f"{h:02d}:{m:02d}"

        # Match single digit '2' or '14'
        if s.isdigit():
            h = int(s)
            if 1 <= h <= 6:  # assume PM for typical business hours 1-6
                h += 12
            if 0 <= h < 24:
                return f"{h:02d}:00"
    except Exception:
        pass
    return None

def normalize_date(date_input: str, reference_date: Optional[datetime] = None) -> Optional[str]:
    """Parse relative or absolute date to 'YYYY-MM-DD'."""
    if not date_input:
        return None
    ref = reference_date or datetime.now()
    s = date_input.strip().lower()

    if s in ["today"]:
        return ref.strftime("%Y-%m-%d")
    if s in ["tomorrow"]:
        return (ref + timedelta(days=1)).strftime("%Y-%m-%d")
    if s in ["day after tomorrow"]:
        return (ref + timedelta(days=2)).strftime("%Y-%m-%d")

    # Match day names e.g. "monday", "next tuesday", "this friday"
    weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    for i, w in enumerate(weekdays):
        if w in s:
            current_w = ref.weekday()
            delta_days = (i - current_w) % 7
            if delta_days == 0 and "next" in s:
                delta_days = 7
            elif delta_days == 0:
                delta_days = 0  # today
            return (ref + timedelta(days=delta_days)).strftime("%Y-%m-%d")

    # Check ISO format YYYY-MM-DD
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        try:
            datetime.strptime(s, "%Y-%m-%d")
            return s
        except ValueError:
            pass

    return None

# ─── Audio Helpers ───────────────────────────────────────────────────────────
def pcm_to_wav(pcm_data: bytes, sample_rate: int = 16000, channels: int = 1) -> str:
    """Convert raw 16-bit linear PCM bytes to a temporary WAV file."""
    temp_wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    temp_name = temp_wav.name
    temp_wav.close()
    with wave.open(temp_name, "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(2)  # 16-bit
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm_data)
    return temp_name

def _transcribe_sync(wav_path: str) -> str:
    """Synchronous Whisper transcription."""
    if not whisper_model:
        return ""
    try:
        if WHISPER_BACKEND == "faster":
            segments, _ = whisper_model.transcribe(wav_path, language="en", beam_size=1)
            return " ".join([segment.text for segment in segments]).strip()
        else:
            result = whisper_model.transcribe(wav_path, language="en")
            return result.get("text", "").strip()
    except Exception as e:
        print(f"Whisper transcription error: {e}")
        return ""

async def transcribe_audio(wav_path: str) -> str:
    """Run Whisper transcription in background thread."""
    return await asyncio.to_thread(_transcribe_sync, wav_path)

async def text_to_speech(text: str, voice: Optional[str] = None) -> bytes:
    """Convert text to speech using Microsoft Edge-TTS."""
    v = voice or config.tts_voice
    clean_text = re.sub(r"[\*\_`#\[\]]", "", text).strip()
    communicate = edge_tts.Communicate(clean_text, v)

    temp_mp3 = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    temp_name = temp_mp3.name
    temp_mp3.close()

    try:
        await communicate.save(temp_name)
        with open(temp_name, "rb") as f:
            audio_bytes = f.read()
        return audio_bytes
    finally:
        if os.path.exists(temp_name):
            try:
                os.unlink(temp_name)
            except OSError:
                pass

# ─── Dynamic System Prompt ───────────────────────────────────────────────────
def build_system_prompt(conv: Conversation) -> str:
    today = datetime.now()
    days_context = []
    for i in range(5):
        d = today + timedelta(days=i)
        d_str = d.strftime("%Y-%m-%d")
        d_name = "TODAY" if i == 0 else ("TOMORROW" if i == 1 else d.strftime("%A"))
        slots = calendar.get_available_slots(d)
        slots_str = ", ".join(slots[:8]) if slots else "No slots available"
        if len(slots) > 8:
            slots_str += f" (and {len(slots)-8} more)"
        days_context.append(f"- {d_name} ({d_str}): Available times: [{slots_str}]")

    services_text = "\n".join([f"- {s['name']}: ${s['price']} ({s['duration']}) — {s['description']}" for s in SERVICES_CATALOG])

    current_state_summary = f"""
CURRENT PATIENT STATE RECORD:
- Patient Name: {conv.name or 'Not provided yet'}
- Patient Phone: {conv.phone or 'Not provided yet'}
- Selected Service: {conv.service or 'Not chosen yet'}
- Requested Date: {conv.preferred_date or 'Not chosen yet'}
- Requested Time: {conv.preferred_time or 'Not chosen yet'}
- Booking Reference: {conv.appt_id or 'None'}
"""

    return f"""You are "Ava," a friendly, ultra-fast, and professional AI Voice Receptionist for "{config.clinic_name}."

CORE RESPONSIBILITIES:
1. Greet callers warmly and speak conversationally in natural, concise sentences (1 to 2 sentences per response).
2. Help book, check prices, reschedule, or answer dental inquiries.
3. Collect necessary details smoothly: Full Name, Phone Number, Service, Date, and Time.
4. ONLY offer and confirm slots that are strictly listed in the calendar below.
5. If a slot is unavailable or taken, apologize briefly and offer the nearest 2 open slots.
6. Once the caller agrees on a valid date, time, and has provided their name & phone, confirm the booking explicitly.

SERVICES & PRICING:
{services_text}

CLINIC CALENDAR AVAILABILITY:
{chr(10).join(days_context)}
Business Hours: Monday–Friday, 9:00 AM to 5:00 PM. Phone: {config.clinic_phone}.

{current_state_summary}

CRITICAL RULES:
- Output MUST be a strictly valid JSON object. Do not include markdown tags, code blocks, or extra text.
- Match date to exact YYYY-MM-DD format.
- Match time to exact HH:MM (24-hour) format.
- "is_confirmed": true ONLY when user explicitly confirms the booking and date/time/name/phone are established.

REQUIRED JSON FORMAT:
{{
  "response": "Your spoken conversational response to the caller.",
  "name": "Alex Smith or null",
  "phone": "555-123-4567 or null",
  "service": "Professional Teeth Cleaning or null",
  "preferred_date": "YYYY-MM-DD or null",
  "preferred_time": "HH:MM or null",
  "is_confirmed": false
}}"""

# ─── LLM Message Handler ────────────────────────────────────────────────────
async def generate_response(conv: Conversation, user_text: str) -> tuple[str, Dict[str, Any], int]:
    """Generate response using Groq with latency profiling."""
    t0 = time.time()
    if not groq_client:
        return "The AI engine is currently offline. Please ensure GROQ_API_KEY is configured.", {}, 0

    system_prompt = build_system_prompt(conv)
    messages = [{"role": "system", "content": system_prompt}]

    # Include recent dialogue history
    for msg in conv.history[-6:]:
        messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({"role": "user", "content": user_text})

    try:
        completion = await asyncio.to_thread(
            groq_client.chat.completions.create,
            model=config.groq_model,
            messages=messages,
            response_format={"type": "json_object"},
            temperature=config.groq_temperature,
            max_tokens=260,
        )
        llm_duration_ms = int((time.time() - t0) * 1000)
        raw_content = completion.choices[0].message.content.strip()

        # Clean any accidental code fences
        cleaned = re.sub(r"^```(?:json)?\s*", "", raw_content)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        data = json.loads(cleaned)

        # Extract & Normalize updates
        if data.get("name") and str(data["name"]).strip().lower() not in ["null", "none", ""]:
            conv.name = str(data["name"]).strip()
        if data.get("phone") and str(data["phone"]).strip().lower() not in ["null", "none", ""]:
            conv.phone = str(data["phone"]).strip()
        if data.get("service") and str(data["service"]).strip().lower() not in ["null", "none", ""]:
            conv.service = str(data["service"]).strip()

        raw_date = data.get("preferred_date")
        if raw_date and str(raw_date).strip().lower() not in ["null", "none", ""]:
            parsed_d = normalize_date(str(raw_date))
            if parsed_d:
                conv.preferred_date = parsed_d

        raw_time = data.get("preferred_time")
        if raw_time and str(raw_time).strip().lower() not in ["null", "none", ""]:
            parsed_t = normalize_time(str(raw_time))
            if parsed_t:
                conv.preferred_time = parsed_t

        if data.get("is_confirmed") is True:
            conv.state = ConversationState.BOOKED

        reply = data.get("response", "I'd be glad to help you with that.")
        return reply, data, llm_duration_ms

    except Exception as e:
        llm_duration_ms = int((time.time() - t0) * 1000)
        print(f"Groq generation error: {e}")
        return "I'm having a slight connection blip. How else can I assist you with your appointment?", {}, llm_duration_ms

# ─── Booking Orchestrator ────────────────────────────────────────────────────
async def process_user_turn(conv: Conversation, user_text: str) -> tuple[str, Dict[str, Any], int]:
    """Process user turn, execute calendar booking and CRM updates."""
    ai_text, extracted_data, llm_ms = await generate_response(conv, user_text)

    # Check if booking conditions met
    if conv.state == ConversationState.BOOKED and not conv.appt_id:
        target_name = conv.name or "Valued Patient"
        target_phone = conv.phone or "Not Provided"
        target_service = conv.service or "Comprehensive Dental Checkup"

        # Resolve date/time
        book_dt = None
        if conv.preferred_date and conv.preferred_time:
            try:
                book_dt = datetime.strptime(f"{conv.preferred_date} {conv.preferred_time}", "%Y-%m-%d %H:%M")
            except Exception:
                book_dt = None

        if not book_dt:
            # Fallback to next day 11:00 AM
            next_day = datetime.now() + timedelta(days=1)
            book_dt = next_day.replace(hour=11, minute=0, second=0, microsecond=0)

        appt = calendar.book(target_name, target_phone, target_service, book_dt, notes="Booked automatically via Ava Voice Receptionist")

        if appt:
            conv.appt_id = appt.id
            lead = crm.find_by_phone(target_phone)
            if not lead:
                lead = crm.create_lead(target_name, target_phone, "", source="Voice Receptionist")
            conv.lead_id = lead.id
            crm.add_appointment_to_lead(lead.id, appt.id)
            crm.add_note(lead.id, f"Confirmed {target_service} on {appt.datetime.strftime('%b %d, %Y at %I:%M %p')}. Ref: #{appt.id}")
            ai_text += f" Your confirmation reference is #{appt.id}."
        else:
            conv.state = ConversationState.COLLECTING_DATE_TIME
            ai_text += " It appears that specific slot just got reserved. Let me check another time for you."

    return ai_text, extracted_data, llm_ms

# ─── WebSocket Endpoint ──────────────────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    global total_calls_count
    await websocket.accept()
    total_calls_count += 1

    conversation = Conversation()
    greeting = f"Hello! Thank you for calling {config.clinic_name}. I'm Ava, your virtual dental receptionist. How can I help you today?"

    try:
        t0_tts = time.time()
        tts_audio = await text_to_speech(greeting)
        tts_ms = int((time.time() - t0_tts) * 1000)

        await websocket.send_json({
            "type": "audio",
            "audio": base64.b64encode(tts_audio).decode("utf-8"),
            "text": greeting,
            "agent": "Ava",
            "state": conversation.state.value,
            "metrics": {
                "stt_ms": 0,
                "llm_ms": 0,
                "tts_ms": tts_ms,
                "total_ms": tts_ms
            }
        })
        conversation.history.append({"role": "assistant", "content": greeting})
    except Exception as e:
        print(f"WebSocket greeting error: {e}")

    try:
        while True:
            payload = await websocket.receive_json()
            msg_type = payload.get("type")
            turn_start = time.time()

            if msg_type == "audio":
                # Audio received
                pcm_b64 = payload.get("audio", "")
                sample_rate = int(payload.get("sample_rate", 16000))
                pcm_bytes = base64.b64decode(pcm_b64)

                wav_path = pcm_to_wav(pcm_bytes, sample_rate=sample_rate)

                t0_stt = time.time()
                user_text = await transcribe_audio(wav_path)
                stt_ms = int((time.time() - t0_stt) * 1000)

                try:
                    os.unlink(wav_path)
                except OSError:
                    pass

                if not user_text.strip():
                    continue

                # Broadcast transcription to frontend
                await websocket.send_json({
                    "type": "transcription",
                    "text": user_text,
                    "speaker": "caller",
                    "stt_ms": stt_ms
                })

                conversation.history.append({"role": "user", "content": user_text})

                # Process turn with Groq & Booking logic
                ai_response, extracted, llm_ms = await process_user_turn(conversation, user_text)

                # Generate TTS audio
                t0_tts = time.time()
                tts_audio = await text_to_speech(ai_response)
                tts_ms = int((time.time() - t0_tts) * 1000)
                total_ms = int((time.time() - turn_start) * 1000)

                await websocket.send_json({
                    "type": "audio",
                    "audio": base64.b64encode(tts_audio).decode("utf-8"),
                    "text": ai_response,
                    "agent": "Ava",
                    "state": conversation.state.value,
                    "extracted": {
                        "name": conversation.name,
                        "phone": conversation.phone,
                        "service": conversation.service,
                        "date": conversation.preferred_date,
                        "time": conversation.preferred_time,
                        "appt_id": conversation.appt_id
                    },
                    "metrics": {
                        "stt_ms": stt_ms,
                        "llm_ms": llm_ms,
                        "tts_ms": tts_ms,
                        "total_ms": total_ms
                    }
                })

                conversation.history.append({"role": "assistant", "content": ai_response})

            elif msg_type == "text":
                user_text = payload.get("text", "").strip()
                if not user_text:
                    continue

                conversation.history.append({"role": "user", "content": user_text})
                ai_response, extracted, llm_ms = await process_user_turn(conversation, user_text)

                t0_tts = time.time()
                tts_audio = await text_to_speech(ai_response)
                tts_ms = int((time.time() - t0_tts) * 1000)
                total_ms = int((time.time() - turn_start) * 1000)

                await websocket.send_json({
                    "type": "audio",
                    "audio": base64.b64encode(tts_audio).decode("utf-8"),
                    "text": ai_response,
                    "agent": "Ava",
                    "state": conversation.state.value,
                    "extracted": {
                        "name": conversation.name,
                        "phone": conversation.phone,
                        "service": conversation.service,
                        "date": conversation.preferred_date,
                        "time": conversation.preferred_time,
                        "appt_id": conversation.appt_id
                    },
                    "metrics": {
                        "stt_ms": 0,
                        "llm_ms": llm_ms,
                        "tts_ms": tts_ms,
                        "total_ms": total_ms
                    }
                })

                conversation.history.append({"role": "assistant", "content": ai_response})

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"WebSocket session error: {e}")

# ─── REST APIs ───────────────────────────────────────────────────────────────
class AppointmentCreate(BaseModel):
    name: str
    phone: str
    service: str
    datetime: str
    notes: Optional[str] = ""

class LeadCreate(BaseModel):
    name: str
    phone: str
    email: Optional[str] = ""
    source: Optional[str] = "Manual Entry"
    notes: Optional[str] = ""

class SettingsUpdate(BaseModel):
    clinic_name: Optional[str] = None
    clinic_phone: Optional[str] = None
    tts_voice: Optional[str] = None
    groq_model: Optional[str] = None
    groq_temperature: Optional[float] = None

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="8" fill="#000"/><path d="M8 8h16v16H8z" fill="none" stroke="#00E5FF" stroke-width="2"/><path d="M12 16h8M16 12v8" stroke="#fff" stroke-width="2" stroke-linecap="round"/></svg>'
    return Response(content=svg, media_type="image/svg+xml")

@app.get("/")
async def serve_ui():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@app.get("/api/appointments")
async def get_appointments():
    sorted_appts = sorted(calendar.appointments.values(), key=lambda x: x.datetime)
    return {
        "appointments": [
            {
                "id": a.id,
                "name": a.name,
                "phone": a.phone,
                "service": a.service,
                "datetime": a.datetime.isoformat(),
                "status": a.status,
                "notes": a.notes,
                "created_at": a.created_at.isoformat()
            }
            for a in sorted_appts
        ],
        "total": len(sorted_appts)
    }

@app.post("/api/appointments")
async def create_appointment(data: AppointmentCreate):
    try:
        dt = datetime.fromisoformat(data.datetime)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid datetime format. Expected ISO (YYYY-MM-DDTHH:MM)")

    appt = calendar.book(data.name, data.phone, data.service, dt, notes=data.notes or "")
    if not appt:
        raise HTTPException(status_code=409, detail="Selected time slot is unavailable")

    lead = crm.find_by_phone(data.phone)
    if not lead:
        lead = crm.create_lead(data.name, data.phone, "", source="Manual Booking")
    crm.add_appointment_to_lead(lead.id, appt.id)

    return {"status": "success", "appointment": asdict(appt)}

@app.delete("/api/appointments/{appt_id}")
async def cancel_appointment(appt_id: str):
    success = calendar.cancel(appt_id)
    if not success:
        raise HTTPException(status_code=404, detail="Appointment not found")
    return {"status": "success", "id": appt_id, "state": "cancelled"}

@app.get("/api/leads")
async def get_leads():
    sorted_leads = sorted(crm.leads.values(), key=lambda x: x.created_at, reverse=True)
    return {
        "leads": [
            {
                "id": l.id,
                "name": l.name,
                "phone": l.phone,
                "email": l.email,
                "source": l.source,
                "status": l.status,
                "appointments": l.appointments,
                "notes": l.notes,
                "created_at": l.created_at.isoformat()
            }
            for l in sorted_leads
        ],
        "total": len(sorted_leads)
    }

@app.post("/api/leads")
async def create_lead(data: LeadCreate):
    lead = crm.create_lead(data.name, data.phone, data.email or "", source=data.source or "Manual Entry")
    if data.notes:
        crm.add_note(lead.id, data.notes)
    return {"status": "success", "lead": asdict(lead)}

@app.get("/api/available-slots")
async def get_available_slots(date_str: Optional[str] = Query(None, alias="date")):
    if date_str:
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            dt = datetime.now() + timedelta(days=1)
    else:
        dt = datetime.now() + timedelta(days=1)

    slots = calendar.get_available_slots(dt)
    return {
        "date": dt.strftime("%Y-%m-%d"),
        "day_name": dt.strftime("%A"),
        "available_slots": slots,
        "total_available": len(slots)
    }

@app.get("/api/services")
async def get_services():
    return {"services": SERVICES_CATALOG}

@app.get("/api/stats")
async def get_stats():
    uptime_sec = int(time.time() - start_timestamp)
    total_appts = len(calendar.appointments)
    active_appts = len([a for a in calendar.appointments.values() if a.status == "confirmed"])
    total_leads = len(crm.leads)

    return {
        "total_calls": total_calls_count,
        "total_appointments": total_appts,
        "confirmed_appointments": active_appts,
        "total_leads": total_leads,
        "conversion_rate": f"{(active_appts / max(1, total_calls_count) * 100):.1f}%",
        "uptime_seconds": uptime_sec,
        "stt_engine": "Local Whisper (tiny)",
        "llm_engine": f"Groq ({config.groq_model})",
        "tts_engine": f"Microsoft Edge-TTS ({config.tts_voice})"
    }

@app.get("/api/health")
async def health_check():
    return {
        "status": "healthy",
        "whisper_backend": WHISPER_BACKEND,
        "groq_configured": bool(groq_client),
        "edge_tts_ready": True,
        "timestamp": datetime.now().isoformat()
    }

@app.get("/api/config")
async def get_config():
    return {
        "clinic_name": config.clinic_name,
        "clinic_phone": config.clinic_phone,
        "tts_voice": config.tts_voice,
        "groq_model": config.groq_model,
        "groq_temperature": config.groq_temperature,
        "business_hours": config.business_hours
    }

@app.post("/api/config")
async def update_config(data: SettingsUpdate):
    if data.clinic_name is not None:
        config.clinic_name = data.clinic_name
    if data.clinic_phone is not None:
        config.clinic_phone = data.clinic_phone
    if data.tts_voice is not None:
        config.tts_voice = data.tts_voice
    if data.groq_model is not None:
        config.groq_model = data.groq_model
    if data.groq_temperature is not None:
        config.groq_temperature = data.groq_temperature
    return {"status": "success", "config": await get_config()}

# ─── Main Runner ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
