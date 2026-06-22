from sqlalchemy import Column, Integer, String, DateTime, ForeignKey
from sqlalchemy.sql import func
from .database import Base

class Admin(Base):
    __tablename__ = "admins"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    full_name = Column(String(255), nullable=True)
    specialty = Column(String(255), nullable=True)
    license_number = Column(String(255), nullable=True)
    hospital_name = Column(String(255), nullable=True)
    hashed_password = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class Patient(Base):
    __tablename__ = "patients"

    id = Column(Integer, primary_key=True, index=True)
    patient_id = Column(String(50), unique=True, index=True, nullable=False)
    full_name = Column(String(255), nullable=False)
    age = Column(Integer, nullable=False)
    gender = Column(String(50), nullable=False)
    blood_group = Column(String(10), nullable=True)
    contact_no = Column(String(20), nullable=True)
    visit_date = Column(String(50), nullable=True)
    primary_disease = Column(String(255), nullable=True)
    symptoms = Column(String(500), nullable=True)
    prescribed_medicine = Column(String(500), nullable=True)
    visit_type = Column(String(50), nullable=True)  # New, Follow-up
    doctor_id = Column(Integer, ForeignKey("admins.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
