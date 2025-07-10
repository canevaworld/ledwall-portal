# main.py – LedWall backend / e-mail, auto-release, IP-limit, fascia 09-18

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https://.*\.pages\.dev$",   # qualunque *.pages.dev
    allow_methods=["GET"],        # basta il GET per /api/slots
    allow_headers=["*"],
    allow_credentials=False,      # non mandiamo cookie
)

import os, datetime, secrets, smtplib, ipaddress
from email.message import EmailMessage

from fastapi import FastAPI, HTTPException, Depends, status, Request, Query, Path
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.middleware.cors import CORSMiddleware          #  ← già qui
from pydantic import BaseModel, EmailStr
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.dialects.postgresql import insert as pg_insert
from zoneinfo import ZoneInfo

TZ_IT = ZoneInfo("Europe/Rome")      # fuso “ufficiale” Italia

from models   import Base, TimeSlot, Video
from storage  import new_file_key, presign_put

# ------------------------------------------------------------------#
# CONFIG
# ------------------------------------------------------------------#
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL not set")

from sqlalchemy import create_engine, event
# rimuovi completamente qualsiasi connect_args={"options": ...} qui sotto
engine = create_engine(DATABASE_URL, pool_pre_ping=True)

@event.listens_for(engine, "connect")
def _set_timezone(dbapi_conn, connection_record):
    """
    Su ogni nuova connessione (anche sotto PgBouncer in transaction-pooling)
    imposta il timezone a Europe/Rome.
    """
    cursor = dbapi_conn.cursor()
    cursor.execute("SET TIME ZONE 'Europe/Rome';")
    cursor.close()

Session = sessionmaker(bind=engine)
Base.metadata.create_all(engine)

app                      = FastAPI()
security                 = HTTPBasic()
ADMIN_USER, ADMIN_PASS   = "admin", "Fossalta58@"

SMTP_HOST, SMTP_PORT     = "smtp.office365.com", 587
SMTP_USER = SMTP_FROM    = "noreply@canevaworld.it"
SMTP_PASS                = "Jyb7#NeALsYcWbnf"
SMTP_SUBJ                = "LedWall – stato video"

MAX_SLOTS_PER_IP         = 5
UPLOAD_GRACE_MIN         = 5                    # minuti per completare upload
OPEN_HOUR_LOCAL          = 9                    # fascia libera   09-18
CLOSE_HOUR_LOCAL         = 18


# ------------------------------------------------------------------#
# UTIL
# ------------------------------------------------------------------#
def send_mail(to_addr: str, body: str):
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = SMTP_SUBJ, SMTP_FROM, to_addr
    msg.set_content(body)
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as s:
            s.starttls(); s.login(SMTP_USER, SMTP_PASS); s.send_message(msg)
        print("MAIL OK →", to_addr)
    except Exception as e:
        print("MAIL ERR:", e)


def verify_admin(c: HTTPBasicCredentials = Depends(security)):
    ok = secrets.compare_digest
    if not (ok(c.username, ADMIN_USER) and ok(c.password, ADMIN_PASS)):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "Bad credentials",
                            {"WWW-Authenticate": "Basic"})
    return True


# ------------------------------------------------------------------#
# SLOT helpers
# ------------------------------------------------------------------#
def round5(dt: datetime.datetime) -> datetime.datetime:
    tr = datetime.timedelta
    return dt - tr(minutes=dt.minute % 5,
                   seconds=dt.second,
                   microseconds=dt.microsecond) + tr(minutes=5)


def ensure_slots(db, start_dt: datetime.datetime, end_dt: datetime.datetime):
    print(f"[DEBUG ensure_slots] start_dt={start_dt.isoformat()} end_dt={end_dt.isoformat()}")
    ts = round5(start_dt)
    rows = []
    while ts < end_dt:
        local = ts.astimezone(TZ_IT)
        is_open = OPEN_HOUR_LOCAL <= local.hour < CLOSE_HOUR_LOCAL
        rows.append({
            "start_utc": ts,
            "booked":    0 if is_open else TimeSlot.capacity.default.arg
        })
        ts += datetime.timedelta(minutes=5)
    if rows:
        db.execute(
            pg_insert(TimeSlot)
            .values(rows)
            .on_conflict_do_nothing(index_elements=["start_utc"])
        )
        db.commit()



def auto_release_expired(db):
    """libera slot prenotati ma senza upload dopo UPLOAD_GRACE_MIN."""
    limit = datetime.datetime.utcnow() - datetime.timedelta(minutes=UPLOAD_GRACE_MIN)
    stale = (db.query(Video)
               .filter(Video.status == "pending",
                       Video.uploaded.is_(False),
                       Video.created_at <= limit)
               .all())
    for v in stale:
        slot = db.query(TimeSlot).filter_by(id=v.slot_id).first()
        if slot and slot.booked > 0:
            slot.booked -= 1
        db.delete(v)
    if stale:
        db.commit()


# ------------------------------------------------------------------#
# PUBLIC ENDPOINTS
# ------------------------------------------------------------------#
@app.get("/")
def root():
    return {"status": "ok", "message": "LedWall portal online ✔"}


# --- extra import una tantum in testa al file ---
from fastapi import Query
from zoneinfo import ZoneInfo          # Python ≥ 3.9
TZ_IT = ZoneInfo("Europe/Rome")        # fuso orario da usare nel JSON
# ----------------------------------------------

from base64 import b64decode

@app.get("/api/slots")
def free_slots(
    request: Request,
    days_ahead: int | None = Query(None, ge=0, le=30),
):
    """
    • Utente normale  → mostra solo gli slot del giorno corrente
    • Admin (Basic-Auth) → con ?days_ahead=N (0-30) mostra solo gli slot di (oggi+N)
    """
    # ———————————————— manual Basic auth parsing ————————————————
    is_admin = False
    auth = request.headers.get("authorization")
    if auth and auth.lower().startswith("basic "):
        try:
            creds = b64decode(auth.split(" ",1)[1]).decode()
            user, pw = creds.split(":",1)
            if secrets.compare_digest(user, ADMIN_USER) and secrets.compare_digest(pw, ADMIN_PASS):
                is_admin = True
        except Exception:
            pass

    # 2) calcola l’intervallo [start, end) in UTC basandosi sul giorno locale
    #    (da 00:00 a 24:00 ora di Roma)
    # giorno di partenza in locale
    local_midnight = (
        datetime.datetime.now(TZ_IT)
        .replace(hour=0, minute=0, second=0, microsecond=0)
    )
    # se admin e days_ahead, sposto il giorno locale
    target_local = (
        local_midnight + datetime.timedelta(days=days_ahead)
    ) if (is_admin and days_ahead is not None) else local_midnight
    # converto in UTC per i filtri del DB
    start = target_local.astimezone(datetime.timezone.utc)
    end   = (target_local + datetime.timedelta(days=1)).astimezone(datetime.timezone.utc)

    # ———————————————— DEBUG (opzionale) ————————————————
    print(f"[DEBUG free_slots] is_admin={is_admin} days_ahead={days_ahead}")
    print(f"[DEBUG free_slots] window start={start} end={end}")

    # ———————————————— crea/ripulisci slot ————————————————
    with Session() as db:
        print(f"[DEBUG ensure_slots] start_dt={start} end_dt={end}")
        ensure_slots(db, start, end)
        auto_release_expired(db)

        cond = [TimeSlot.start_utc >= start, TimeSlot.start_utc < end]
        if not is_admin:
            cond.append(TimeSlot.booked < TimeSlot.capacity)

        q = select(TimeSlot).where(*cond).order_by(TimeSlot.start_utc)
        slots = db.execute(q).scalars().all()

    # ———————————————— output ————————————————
    return [
        {
            "id":    s.id,
            "start": s.start_utc.astimezone(TZ_IT).isoformat(timespec="minutes"),
            "free":  s.capacity - s.booked,
        }
        for s in slots
    ]



# ------------------------------------------------------------------#
#  UPLOAD FLOW
# ------------------------------------------------------------------#
class InitRequest(BaseModel):
    slot_id: int
    email:   EmailStr
    original_name: str


@app.post("/api/upload_init")
def upload_init(body: InitRequest, request: Request):
    raw_ip = request.client.host or "0.0.0.0"
    try:
        client_ip = str(ipaddress.ip_address(raw_ip))
    except ValueError:
        client_ip = "0.0.0.0"

    with Session() as db:
        cnt = (db.query(func.count(Video.id))
                 .filter(Video.client_ip == client_ip,
                         Video.status.in_(("pending", "approved")))
                 .scalar())
        if cnt >= MAX_SLOTS_PER_IP:
            raise HTTPException(429, "Troppi slot da questo IP")

        slot = db.query(TimeSlot).with_for_update().filter_by(id=body.slot_id).first()
        if not slot:
            raise HTTPException(404, "Slot inesistente")
        if slot.booked >= slot.capacity:
            raise HTTPException(409, "Slot pieno")

        fkey  = new_file_key(body.original_name)
        video = Video(email=body.email, slot_id=slot.id, filename=fkey,
                      status="pending", uploaded=False, client_ip=client_ip)
        slot.booked += 1
        db.add(video); db.commit(); db.refresh(video)

    return {"video_id": video.id,
            "upload_url": presign_put(fkey),
            "file_key":   fkey}


class CompleteBody(BaseModel):
    video_id: int


@app.post("/api/upload_complete")
def upload_complete(body: CompleteBody):
    with Session() as db:
        v = db.query(Video).filter_by(id=body.video_id).first()
        if not v:
            raise HTTPException(404, "video not found")
        if v.uploaded:
            return {"msg": "already flagged"}
        v.uploaded = True
        db.commit()
        slot_dt = db.query(TimeSlot).filter_by(id=v.slot_id).first().start_utc

    send_mail(v.email,
              f"Abbiamo ricevuto il tuo video.\n"
              f"Sarà revisionato a breve.\n"
              f"Slot selezionato: {slot_dt:%d/%m %H:%M} UTC.")
    return {"msg": "ok"}


# ------------------------------------------------------------------#
# ADMIN ENDPOINTS
# ------------------------------------------------------------------#
@app.get("/api/admin/videos", dependencies=[Depends(verify_admin)])
def list_videos(status: str = "pending", limit: int = 100):
    if status not in {"pending", "approved", "rejected"}:
        raise HTTPException(400, "status errato")
    with Session() as db:
        q = (db.query(Video, TimeSlot)
               .join(TimeSlot, Video.slot_id == TimeSlot.id)
               .filter(Video.status == status)
               .order_by(TimeSlot.start_utc)
               .limit(limit))
        return [{"video_id": v.id,
                 "file_key":  v.filename,
                 "status":    v.status,
                 "email":     v.email,
                 "slot_start_utc": s.start_utc.isoformat() + "Z"}
                for v, s in q]


class ValidateBody(BaseModel):
    video_id: int
    action:   str  # approve | reject


@app.post("/api/admin/validate", dependencies=[Depends(verify_admin)])
def validate_video(body: ValidateBody):
    if body.action not in {"approve", "reject"}:
        raise HTTPException(400, "action errata")

    with Session() as db:
        v = db.query(Video).filter_by(id=body.video_id).first()
        if not v:
            raise HTTPException(404, "Video mancante")
        if v.status != "pending":
            raise HTTPException(409, "già processato")
        if not v.uploaded:
            raise HTTPException(409, "file non caricato")

        s = db.query(TimeSlot).filter_by(id=v.slot_id).first()
        if not s:
            raise HTTPException(500, "slot mancante")

        if body.action == "approve":
            v.status = "approved"
            slot_it  = s.start_utc.astimezone(TZ_IT)
            mail_txt = (f"Il tuo video è stato APPROVATO e sarà trasmesso "
                        f"alle {slot_it:%H:%M} del {slot_it:%d/%m}.")
        else:
            v.status = "rejected"
            if s.booked > 0:
                s.booked -= 1
            mail_txt = ("Siamo spiacenti: il tuo video è stato RIFIUTATO "
                        "perché non conforme alle policy.")

        db.commit()
        vid, mail_to, status_now = v.id, v.email, v.status

    send_mail(mail_to, mail_txt)
    return {"video_id": vid, "status": status_now}


# ------------ slot manuali -----------------------------------------#
class SlotAction(BaseModel):
    slot_id: int
    action:  str  # block | free

@app.post("/api/admin/slot", dependencies=[Depends(verify_admin)])
def slot_admin(body: SlotAction):
    if body.action not in {"block", "free"}:
        raise HTTPException(400, "action deve essere block|free")
    with Session() as db:
        s = db.query(TimeSlot).filter_by(id=body.slot_id).first()
        if not s:
            raise HTTPException(404, "slot non trovato")
        if body.action == "block":
            s.booked = s.capacity
        else:
            s.booked = 0
        db.commit()
    return {"slot_id": body.slot_id, "status": body.action}


@app.post("/api/admin/slots/{slot_id}/free", dependencies=[Depends(verify_admin)])
def free_slot(slot_id: int = Path(..., ge=1)):
    with Session() as db:
        s = db.query(TimeSlot).filter_by(id=slot_id).first()
        if not s:
            raise HTTPException(404, "slot non trovato")
        s.booked = 0
        db.commit()
    return {"slot_id": slot_id, "booked": 0}
