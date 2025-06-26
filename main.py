import os
import datetime

from fastapi import FastAPI
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.dialects.postgresql import insert as pg_insert

from models import Base, TimeSlot   # importa le tue tabelle

# -------------------------------------------------------------------
# CONFIGURAZIONE DATABASE
# -------------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL not set")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
Session = sessionmaker(bind=engine)

# Crea automaticamente le tabelle se non esistono
Base.metadata.create_all(engine)

# -------------------------------------------------------------------
# APP FASTAPI
# -------------------------------------------------------------------
app = FastAPI()


@app.get("/")
def read_root():
    return {"status": "ok", "message": "LedWall portal online ✔"}


# -------------------------------------------------------------------
# FUNZIONI DI SUPPORTO
# -------------------------------------------------------------------
def round_to_next_5(dt: datetime.datetime) -> datetime.datetime:
    """Arrotonda (in avanti) al blocco successivo di 5 minuti."""
    discard = datetime.timedelta(
        minutes=dt.minute % 5,
        seconds=dt.second,
        microseconds=dt.microsecond,
    )
    return dt - discard + datetime.timedelta(minutes=5)


def ensure_slots(db, start_dt: datetime.datetime, end_dt: datetime.datetime):
    """
    Inserisce tutti gli slot da 5′ tra start_dt ed end_dt se mancanti,
    usando INSERT … ON CONFLICT DO NOTHING per evitare duplicati.
    """
    ts = round_to_next_5(start_dt)
    rows = []
    while ts <= round_to_next_5(end_dt):
        rows.append({"start_utc": ts})
        ts += datetime.timedelta(minutes=5)

    if rows:
        stmt = (
            pg_insert(TimeSlot)
            .values(rows)
            .on_conflict_do_nothing(index_elements=["start_utc"])
        )
        db.execute(stmt)
        db.commit()


# -------------------------------------------------------------------
# ENDPOINT API
# -------------------------------------------------------------------
@app.get("/api/slots")
def get_free_slots(hours_ahead: int = 24):
    """
    Ritorna gli slot liberi (con posti < capacity) nelle prossime `hours_ahead` ore.
    Le date sono in UTC e nel formato ISO-8601.
    """
    now = datetime.datetime.utcnow()
    upper = now + datetime.timedelta(hours=hours_ahead)

    with Session() as db:
        # Crea eventuali slot mancanti in modo atomico
        ensure_slots(db, now, upper)

        # Estrae solo quelli non pieni
        stmt = (
            select(TimeSlot)
            .where(
                TimeSlot.booked < TimeSlot.capacity,
                TimeSlot.start_utc >= now,
                TimeSlot.start_utc <= upper,
            )
            .order_by(TimeSlot.start_utc)
        )
        slots = db.execute(stmt).scalars().all()

    return [
        {
            "id": s.id,
            "start_utc": s.start_utc.isoformat() + "Z",
            "free": s.capacity - s.booked,
        }
        for s in slots
    ]
