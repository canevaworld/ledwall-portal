from sqlalchemy import Column, Integer, String, DateTime, Boolean, ForeignKey
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()

class TimeSlot(Base):
    __tablename__ = "time_slots"
    id = Column(Integer, primary_key=True)
    start_utc = Column(DateTime, unique=True, nullable=False)
    booked = Column(Integer, default=0)
    capacity = Column(Integer, default=5)

class Video(Base):
    __tablename__ = "videos"
    id       = Column(Integer, primary_key=True)
    slot_id  = Column(Integer, ForeignKey("time_slots.id"))
    filename = Column(String, nullable=False)
    email    = Column(String, nullable=False)     # <— rimane
    status   = Column(String, default="pending")
    slot     = relationship("TimeSlot")
