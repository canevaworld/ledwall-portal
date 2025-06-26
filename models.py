from sqlalchemy import Column, Integer, String, DateTime, Boolean, ForeignKey, UniqueConstraint
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
import datetime

Base = declarative_base()

class TimeSlot(Base):
    __tablename__ = "time_slots"
    id         = Column(Integer, primary_key=True)
    start_utc  = Column(DateTime, nullable=False, unique=True)   # in UTC, arrotondato al 5′
    booked     = Column(Integer, default=0)     # numero video prenotati
    capacity   = Column(Integer, default=5)     # max per slot

class TgChat(Base):
    __tablename__ = "tg_chats"
    id = Column(Integer, primary_key=True)
    phone = Column(String, unique=True, nullable=False)
    chat_id = Column(String, unique=True, nullable=False)


class Video(Base):
    __tablename__ = "videos"
    id          = Column(Integer, primary_key=True)
    phone       = Column(String, nullable=False)
    slot_id     = Column(Integer, ForeignKey("time_slots.id"), nullable=False)
    filename    = Column(String, nullable=False)   # “key” su Cloudflare R2 (lo metteremo più avanti)
    status      = Column(String, default="pending")  # pending / approved / rejected
    created_at  = Column(DateTime, default=datetime.datetime.utcnow)

    slot = relationship("TimeSlot")
