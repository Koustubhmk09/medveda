"""
Load patients from backend/data/gp_clinic_patients.pdf into the database.
Run from backend/: python ingest_patients.py
"""
import os
import re
from collections import Counter

import pdfplumber
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from src.database import SessionLocal, engine
from src.models import Base, Patient

NEW_COLUMNS = {
    "blood_group": "VARCHAR(10) NULL",
    "contact_no": "VARCHAR(20) NULL",
}

DATASET_PDF = os.path.join("data", "gp_clinic_patients.pdf")


def _clean_cell(value) -> str:
    if value is None:
        return ""
    return str(value).replace("\n", " ").strip()


def ensure_patient_columns():
    """Add new columns to existing MySQL/SQLite tables without dropping data."""
    inspector = inspect(engine)
    if "patients" not in inspector.get_table_names():
        return
    existing = {c["name"] for c in inspector.get_columns("patients")}
    with engine.begin() as conn:
        for col, col_type in NEW_COLUMNS.items():
            if col not in existing:
                conn.execute(text(f"ALTER TABLE patients ADD COLUMN {col} {col_type}"))
                print(f"Added column: {col}", flush=True)


def load_patients_from_pdf(pdf_path: str) -> list[dict]:
    patients = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            if not tables:
                continue
            for table in tables:
                for row in table:
                    if not row or not row[0]:
                        continue
                    pid = _clean_cell(row[0])
                    # Match P001, P002, etc.
                    if not re.match(r"^P\d{3}$", pid, re.IGNORECASE):
                        continue
                    
                    patients.append({
                        "patient_id": pid.upper(),
                        "patient_name": _clean_cell(row[1]) or "Unknown",
                        "age": int(_clean_cell(row[2])),
                        "gender": _clean_cell(row[3]),
                        "blood_group": _clean_cell(row[4]) or None,
                        "contact_no": _clean_cell(row[5]) or None,
                        "visit_date": _clean_cell(row[6]),
                        "disease": _clean_cell(row[7]) or "Under investigation",
                        "symptoms": _clean_cell(row[8]),
                        "treatment": _clean_cell(row[9]) or "None",
                        "visit_type": _clean_cell(row[10]),
                    })
    return patients


def ingest_patients():
    # DROP and CREATE to ensure the schema matches precisely in HeidiSQL
    print("Dropping and recreating patients table...", flush=True)
    Base.metadata.drop_all(bind=engine, tables=[Patient.__table__])
    Base.metadata.create_all(bind=engine)

    if not os.path.exists(DATASET_PDF):
        print(f"Error: Put your PDF at {DATASET_PDF}", flush=True)
        return

    print(f"Loading from PDF: {DATASET_PDF}", flush=True)
    patients_data = load_patients_from_pdf(DATASET_PDF)
    print(f"Loaded {len(patients_data)} patient records.", flush=True)

    if not patients_data:
        print("Error: No patient rows found in PDF.", flush=True)
        return

    db: Session = SessionLocal()
    try:
        for i, p in enumerate(patients_data):
            if i % 25 == 0:
                print(f"Progress: {i}/{len(patients_data)}", flush=True)
            db.add(Patient(
                patient_id=p["patient_id"],
                full_name=p["patient_name"],
                age=p["age"],
                gender=p["gender"],
                blood_group=p["blood_group"],
                contact_no=p["contact_no"],
                visit_date=p["visit_date"],
                symptoms=p["symptoms"],
                primary_disease=p["disease"],
                prescribed_medicine=p["treatment"],
                visit_type=p["visit_type"],
            ))

        db.commit()
        print(f"Success: {len(patients_data)} patients ingested.", flush=True)
    except Exception as e:
        print(f"Error: {e}", flush=True)
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    ingest_patients()
