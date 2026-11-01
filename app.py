import os
import json
import hmac
import hashlib
import secrets
import re
import base64
import hashlib
from io import BytesIO
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Optional

import jwt
import requests
from fastapi import FastAPI, Depends, HTTPException, Header, Request
from pydantic import BaseModel, Field, EmailStr
from sqlalchemy import create_engine, Column, Integer, String, DateTime, ForeignKey, Index, inspect, text, func, Boolean
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from sqlalchemy import UniqueConstraint
import openpyxl

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./tpg_dev.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
if DATABASE_URL.startswith("postgresql://") and "sslmode=" not in DATABASE_URL:
    DATABASE_URL += ("&" if "?" in DATABASE_URL else "?") + "sslmode=require"

APP_SECRET = os.getenv("APP_SECRET", "")
BREVO_API_KEY = os.getenv("BREVO_API_KEY", "")
BREVO_SENDER_EMAIL = os.getenv("BREVO_SENDER_EMAIL", "qpgeneduservices@gmail.com")
BREVO_SENDER_NAME = os.getenv("BREVO_SENDER_NAME", "Totally Paper Generator")
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "")
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")
MONTHLY_PRICE_PAISE = int(os.getenv("MONTHLY_PRICE_PAISE", "9900"))
PLAN_CATALOG = {
    "basic": {"name": "BASIC", "price_paise": 9900, "attempts": 30, "pdf_limit": 30, "docx_limit": 30},
    "standard": {"name": "STANDARD", "price_paise": 14900, "attempts": 60, "pdf_limit": 60, "docx_limit": 60},
    "premium": {"name": "PREMIUM", "price_paise": 19900, "attempts": 90, "pdf_limit": 90, "docx_limit": 90},
}
TOKEN_DAYS = int(os.getenv("TOKEN_DAYS", "30"))
OTP_TTL_MINUTES = int(os.getenv("OTP_TTL_MINUTES", "10"))

if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
else:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    # phone is retained for compatibility with existing rows; new accounts use email.
    phone = Column(String(20), unique=True, nullable=True, index=True)
    email = Column(String(320), unique=True, nullable=True, index=True)
    subscription_expires_at = Column(DateTime(timezone=True), nullable=True)
    plan_id = Column(String(20), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    is_blocked = Column(Integer, default=0, nullable=False)
    admin_note = Column(String(2000), nullable=True)
    last_seen_at = Column(DateTime(timezone=True), nullable=True)
    pdf_limit_override = Column(Integer, nullable=True)
    docx_limit_override = Column(Integer, nullable=True)
    standard_limit_override = Column(Integer, nullable=True)
    subject_limit_override = Column(Integer, nullable=True)
    # Snapshot of the exact plan/offer purchased; survives later admin edits/deletion.
    current_scheme_id = Column(Integer, nullable=True)
    current_scheme_title = Column(String(120), nullable=True)
    current_scheme_description = Column(String(1000), nullable=True)
    current_scheme_price_paise = Column(Integer, nullable=True)
    subscription_validity_days = Column(Integer, nullable=True)

class PaymentOrder(Base):
    __tablename__ = "payment_orders"
    id = Column(Integer, primary_key=True)
    reference_id = Column(String(64), unique=True, nullable=False, index=True)
    razorpay_link_id = Column(String(64), unique=True, nullable=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    status = Column(String(32), default="created", nullable=False)
    amount_paise = Column(Integer, nullable=False, default=9900)
    plan_id = Column(String(20), nullable=False, default="basic")
    scheme_id = Column(Integer, nullable=True, index=True)
    pdf_limit_snapshot = Column(Integer, nullable=True)
    docx_limit_snapshot = Column(Integer, nullable=True)
    validity_days_snapshot = Column(Integer, nullable=True)
    scheme_title_snapshot = Column(String(120), nullable=True)
    scheme_description_snapshot = Column(String(1000), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    paid_at = Column(DateTime(timezone=True), nullable=True)

class GenerationUsage(Base):
    __tablename__ = "generation_usage"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    period = Column(String(7), nullable=False)  # UTC YYYY-MM
    pdf_used = Column(Integer, default=0, nullable=False)
    docx_used = Column(Integer, default=0, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

Index("uq_generation_usage_user_period", GenerationUsage.user_id, GenerationUsage.period, unique=True)

class QuestionBankRecord(Base):
    __tablename__ = "question_bank_records"
    id = Column(Integer, primary_key=True)
    sheet_name = Column(String(40), nullable=False, index=True)
    data_json = Column(String(12000), nullable=False)
    fingerprint = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    __table_args__ = (UniqueConstraint("sheet_name", "fingerprint", name="uq_question_bank_sheet_fingerprint"),)

class BlueprintPreset(Base):
    __tablename__ = "blueprint_presets"
    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False)
    exam_title = Column(String(120), nullable=False, index=True)
    standard = Column(String(60), nullable=False, index=True)
    subject = Column(String(100), nullable=False, index=True)
    items_json = Column(String(30000), nullable=False, default="[]")
    fingerprint = Column(String(64), nullable=False, unique=True, index=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

class EmailOTP(Base):
    __tablename__ = "email_otp_requests"
    id = Column(Integer, primary_key=True)
    email = Column(String(320), nullable=False, index=True)
    otp_hash = Column(String(64), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    attempts = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    used_at = Column(DateTime(timezone=True), nullable=True)

Index("ix_emailotp_email_created", EmailOTP.email, EmailOTP.created_at)

class AppSetting(Base):
    __tablename__ = "app_settings"
    key = Column(String(100), primary_key=True)
    value = Column(String(500), nullable=False, default="")


class PlanScheme(Base):
    __tablename__ = "plan_schemes"
    id = Column(Integer, primary_key=True)
    title = Column(String(120), nullable=False)
    plan_id = Column(String(20), nullable=False, index=True)
    price_paise = Column(Integer, nullable=False)
    pdf_limit = Column(Integer, nullable=False, default=30)
    docx_limit = Column(Integer, nullable=False, default=30)
    validity_days = Column(Integer, nullable=False, default=30)
    original_price_paise = Column(Integer, nullable=True)
    description = Column(String(1000), nullable=True)
    starts_at = Column(DateTime(timezone=True), nullable=True)
    ends_at = Column(DateTime(timezone=True), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    is_default = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)


Base.metadata.create_all(bind=engine)

def migrate_existing_database():
    """Add email column and allow new email-only users without dropping existing users/orders."""
    inspector = inspect(engine)
    if "users" in inspector.get_table_names():
        cols = {c["name"] for c in inspector.get_columns("users")}
        with engine.begin() as conn:
            if "email" not in cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN email VARCHAR(320)"))
            # PostgreSQL needs phone nullable for new email-only accounts. Existing values are preserved.
            if engine.dialect.name == "postgresql":
                conn.execute(text("ALTER TABLE users ALTER COLUMN phone DROP NOT NULL"))
            elif engine.dialect.name == "sqlite":
                # Older SQLite tables may still require phone; if so, we stop rather than silently damage data.
                phone_col = next((c for c in inspector.get_columns("users") if c["name"] == "phone"), None)
                if phone_col and phone_col.get("nullable") is False:
                    # SQLite cannot safely drop NOT NULL with a simple ALTER. Use a new SQLite database for local development.
                    raise RuntimeError("This existing SQLite database has a NOT NULL phone column. Use a fresh local test DB or migrate SQLite separately.")
        with engine.begin() as conn:
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_email_unique ON users (email)"))
migrate_existing_database()

# Add plan fields to existing deployments without dropping user/payment data.
def migrate_plan_columns():
    inspector = inspect(engine)
    with engine.begin() as conn:
        user_cols = {c["name"] for c in inspector.get_columns("users")}
        payment_cols = {c["name"] for c in inspector.get_columns("payment_orders")}
        if "plan_id" not in user_cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN plan_id VARCHAR(20)"))
        if "plan_id" not in payment_cols:
            conn.execute(text("ALTER TABLE payment_orders ADD COLUMN plan_id VARCHAR(20) NOT NULL DEFAULT 'basic'"))

migrate_plan_columns()

def migrate_scheme_columns():
    inspector = inspect(engine)
    with engine.begin() as conn:
        if "payment_orders" in inspector.get_table_names():
            cols = {c["name"] for c in inspector.get_columns("payment_orders")}
            definitions = {
                "scheme_id": "INTEGER",
                "pdf_limit_snapshot": "INTEGER",
                "docx_limit_snapshot": "INTEGER",
                "validity_days_snapshot": "INTEGER",
                "scheme_title_snapshot": "VARCHAR(120)",
                "scheme_description_snapshot": "VARCHAR(1000)",
            }
            for name, definition in definitions.items():
                if name not in cols:
                    conn.execute(text(f"ALTER TABLE payment_orders ADD COLUMN {name} {definition}"))

migrate_scheme_columns()

def migrate_plan_scheme_type():
    inspector = inspect(engine)
    if "plan_schemes" in inspector.get_table_names():
        cols = {c["name"] for c in inspector.get_columns("plan_schemes")}
        if "is_default" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE plan_schemes ADD COLUMN is_default BOOLEAN NOT NULL DEFAULT FALSE"))

migrate_plan_scheme_type()

def seed_initial_plan_cards():
    """Seed standard cards once; after that admin edits/deletes persist."""
    db = SessionLocal()
    try:
        seeded = db.query(AppSetting).filter(AppSetting.key == "initial_plan_cards_seeded").first()
        if seeded:
            return
        if db.query(PlanScheme).filter(PlanScheme.is_default.is_(True)).count() == 0:
            initial_cards = {
                "basic": {"name": "Basic", "price_paise": 9900, "pdf_limit": 15, "docx_limit": 10},
                "standard": {"name": "Standard", "price_paise": 14900, "pdf_limit": 30, "docx_limit": 15},
                "premium": {"name": "Premium", "price_paise": 19900, "pdf_limit": 30, "docx_limit": 15},
            }
            for plan_id, plan in initial_cards.items():
                db.add(PlanScheme(
                    title=plan["name"], plan_id=plan_id,
                    price_paise=int(plan["price_paise"]),
                    pdf_limit=int(plan["pdf_limit"]), docx_limit=int(plan["docx_limit"]),
                    validity_days=30, description="Monthly generation allowance",
                    is_active=True, is_default=True,
                    created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
                ))
        db.add(AppSetting(key="initial_plan_cards_seeded", value="true"))
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

seed_initial_plan_cards()

def migrate_admin_columns():
    """Add admin-management fields without dropping existing user data."""
    inspector = inspect(engine)
    if "users" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("users")}
    definitions = {
        "is_blocked": "INTEGER NOT NULL DEFAULT 0",
        "admin_note": "VARCHAR(2000)",
        "last_seen_at": "TIMESTAMP",
        "pdf_limit_override": "INTEGER",
        "docx_limit_override": "INTEGER",
        "standard_limit_override": "INTEGER",
        "subject_limit_override": "INTEGER",
        "current_scheme_id": "INTEGER",
        "current_scheme_title": "VARCHAR(120)",
        "current_scheme_description": "VARCHAR(1000)",
        "current_scheme_price_paise": "INTEGER",
        "subscription_validity_days": "INTEGER",
    }
    with engine.begin() as conn:
        for name, definition in definitions.items():
            if name not in cols:
                conn.execute(text(f"ALTER TABLE users ADD COLUMN {name} {definition}"))

migrate_admin_columns()

app = FastAPI(title="Totally Paper Generator API", version="2.0.0")

class SendOTPBody(BaseModel):
    email: EmailStr

class VerifyOTPBody(BaseModel):
    email: EmailStr
    otp: str = Field(min_length=6, max_length=6, pattern=r"^[0-9]{6}$")

def utcnow():
    return datetime.now(timezone.utc)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def require_config(*names):
    values = {
        "APP_SECRET": APP_SECRET,
        "BREVO_API_KEY": BREVO_API_KEY,
        "BREVO_SENDER_EMAIL": BREVO_SENDER_EMAIL,
        "RAZORPAY_KEY_ID": RAZORPAY_KEY_ID,
        "RAZORPAY_KEY_SECRET": RAZORPAY_KEY_SECRET,
    }
    missing = [name for name in names if not values.get(name)]
    if missing:
        raise HTTPException(status_code=503, detail="Server setup incomplete. Missing configuration: " + ", ".join(missing))

def normalize_email(value: str) -> str:
    return value.strip().lower()

def send_email(to_email: str, subject: str, text_content: str, html_content: str):
    require_config("BREVO_API_KEY", "BREVO_SENDER_EMAIL")
    try:
        response = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={"api-key": BREVO_API_KEY, "accept": "application/json", "content-type": "application/json"},
            json={
                "sender": {"name": BREVO_SENDER_NAME, "email": BREVO_SENDER_EMAIL},
                "to": [{"email": to_email}],
                "subject": subject,
                "textContent": text_content,
                "htmlContent": html_content,
            },
            timeout=20,
        )
    except requests.RequestException:
        raise HTTPException(status_code=502, detail="Email provider could not be reached. Please try again.")
    if response.status_code >= 300:
        # Avoid returning provider details to the app; inspect Render logs if configuration fails.
        raise HTTPException(status_code=502, detail="Email could not be sent. Check Brevo sender verification and API configuration.")
    return True

def otp_digest(email: str, otp: str) -> str:
    return hashlib.sha256((email + ":" + otp + ":" + APP_SECRET).encode("utf-8")).hexdigest()

def create_access_token(user: User) -> str:
    require_config("APP_SECRET")
    now = utcnow()
    payload = {"sub": str(user.id), "email": user.email, "iat": now, "exp": now + timedelta(days=TOKEN_DAYS)}
    return jwt.encode(payload, APP_SECRET, algorithm="HS256")

def current_user(authorization: Optional[str] = Header(default=None), db: Session = Depends(get_db)) -> User:
    require_config("APP_SECRET")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Please log in again.")
    token = authorization.split(" ", 1)[1].strip()
    try:
        payload = jwt.decode(token, APP_SECRET, algorithms=["HS256"])
        user_id = int(payload["sub"])
    except Exception:
        raise HTTPException(status_code=401, detail="Session expired or invalid. Please verify email OTP again.")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="User account not found.")
    if bool(user.is_blocked):
        raise HTTPException(status_code=403, detail="This account has been suspended. Contact the administrator.")
    user.last_seen_at = utcnow()
    db.commit()
    return user

def subscription_active(user: User) -> bool:
    expires = user.subscription_expires_at
    if not expires:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires > utcnow()

def razorpay_request(method: str, path: str, payload: Optional[dict] = None):
    require_config("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET")
    try:
        response = requests.request(
            method, "https://api.razorpay.com/v1" + path,
            json=payload, auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET), timeout=20
        )
    except requests.RequestException:
        raise HTTPException(status_code=502, detail="Could not contact the payment provider. Please try again.")
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="Payment provider rejected the request. Check Razorpay test-mode setup.")
    return data

def activate_order(db: Session, order: PaymentOrder):
    """Activate a newly paid order as a fresh 30-day subscription.

    Subscription time never stacks. Every newly paid order starts from the
    payment activation time, and the current calendar month's generation
    counters are reset so the newly purchased plan starts with fresh limits.
    Repeated webhook/status calls for an already-paid order are idempotent.
    """
    if order.status == "paid":
        return

    user = db.query(User).filter(User.id == order.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Payment user not found.")

    now = utcnow()
    new_plan_id = (order.plan_id or "basic").lower()

    # Replace any old subscription. Do NOT add the old remaining validity.
    user.plan_id = new_plan_id
    validity_days = int(order.validity_days_snapshot or 30)
    user.subscription_expires_at = now + timedelta(days=validity_days)
    user.current_scheme_id = order.scheme_id
    user.current_scheme_title = order.scheme_title_snapshot or PLAN_CATALOG.get(new_plan_id, {}).get("name", new_plan_id.title())
    user.current_scheme_description = order.scheme_description_snapshot or ""
    user.current_scheme_price_paise = int(order.amount_paise or 0)
    user.subscription_validity_days = validity_days
    # Apply the limits that were locked into the order at checkout time.
    if order.pdf_limit_snapshot is not None:
        user.pdf_limit_override = int(order.pdf_limit_snapshot)
    if order.docx_limit_snapshot is not None:
        user.docx_limit_override = int(order.docx_limit_snapshot)

    # Reset this month's usage on every new successful subscription purchase.
    # Older months remain in the database for history; the current month starts fresh.
    current_period = now.strftime("%Y-%m")
    usage = db.query(GenerationUsage).filter(
        GenerationUsage.user_id == user.id,
        GenerationUsage.period == current_period,
    ).with_for_update().first() if engine.dialect.name == "postgresql" else db.query(GenerationUsage).filter(
        GenerationUsage.user_id == user.id,
        GenerationUsage.period == current_period,
    ).first()
    if usage is not None:
        usage.pdf_used = 0
        usage.docx_used = 0
        usage.updated_at = now

    order.status = "paid"
    order.paid_at = now
    db.commit()

@app.get("/")
def root():
    return {"service": "Totally Paper Generator API", "status": "ok", "login": "email_otp"}

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/auth/send-otp")
def send_otp(body: SendOTPBody, db: Session = Depends(get_db)):
    require_config("APP_SECRET", "BREVO_API_KEY", "BREVO_SENDER_EMAIL")
    email = normalize_email(str(body.email))
    now = utcnow()
    cutoff = now - timedelta(hours=1)
    recent = db.query(EmailOTP).filter(EmailOTP.email == email, EmailOTP.created_at >= cutoff).count()
    if recent >= 5:
        raise HTTPException(status_code=429, detail="Too many OTP requests. Please try again later.")
    otp = f"{secrets.randbelow(1_000_000):06d}"
    record = EmailOTP(
        email=email,
        otp_hash=otp_digest(email, otp),
        expires_at=now + timedelta(minutes=OTP_TTL_MINUTES),
        attempts=0,
        created_at=now,
    )
    db.add(record)
    db.commit()
    try:
        send_email(
            email,
            "Your Totally Paper Generator verification code",
            f"Your verification code is {otp}. It expires in {OTP_TTL_MINUTES} minutes. Do not share it with anyone.",
            f"<p>Your Totally Paper Generator verification code is:</p><p style='font-size:28px;font-weight:bold;letter-spacing:5px'>{otp}</p><p>It expires in {OTP_TTL_MINUTES} minutes. Do not share it with anyone.</p>",
        )
    except Exception:
        db.delete(record)
        db.commit()
        raise
    return {"ok": True, "message": "OTP sent to your email address."}

@app.post("/auth/verify-otp")
def verify_otp(body: VerifyOTPBody, db: Session = Depends(get_db)):
    require_config("APP_SECRET", "BREVO_API_KEY", "BREVO_SENDER_EMAIL")
    email = normalize_email(str(body.email))
    now = utcnow()
    record = db.query(EmailOTP).filter(
        EmailOTP.email == email, EmailOTP.used_at.is_(None)
    ).order_by(EmailOTP.created_at.desc()).first()
    if not record:
        raise HTTPException(status_code=400, detail="No active OTP found. Request a new code.")
    expires = record.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < now:
        raise HTTPException(status_code=400, detail="OTP expired. Request a new code.")
    if record.attempts >= 5:
        raise HTTPException(status_code=429, detail="Too many incorrect attempts. Request a new OTP.")
    if not hmac.compare_digest(record.otp_hash, otp_digest(email, body.otp)):
        record.attempts += 1
        db.commit()
        raise HTTPException(status_code=400, detail="Incorrect OTP. Please try again.")
    record.used_at = now
    user = db.query(User).filter(User.email == email).first()
    if not user:
        user = User(email=email, phone=None)
        db.add(user)
    db.commit()
    db.refresh(user)
    if bool(user.is_blocked):
        raise HTTPException(
            status_code=403,
            detail="This account has been suspended. Contact the administrator."
        )
    return {
        "access_token": create_access_token(user),
        "token_type": "bearer",
        "subscription_active": subscription_active(user),
        "subscription_expires_at": user.subscription_expires_at.isoformat() if user.subscription_expires_at else None,
        "plan_id": user.plan_id,
        "email": user.email,
    }

def _as_utc(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)

def scheme_is_current(scheme: PlanScheme, now=None):
    now = now or utcnow()
    starts = _as_utc(scheme.starts_at)
    ends = _as_utc(scheme.ends_at)
    return bool(scheme.is_active and (starts is None or starts <= now) and (ends is None or ends >= now))

def scheme_dict(scheme: PlanScheme):
    now = utcnow()
    original = scheme.original_price_paise
    discount = 0
    if original and original > scheme.price_paise:
        discount = round((original - scheme.price_paise) * 100 / original)
    return {
        "scheme_id": scheme.id,
        "title": scheme.title,
        "plan_id": scheme.plan_id,
        "plan_name": PLAN_CATALOG[scheme.plan_id]["name"],
        "price_paise": int(scheme.price_paise),
        "price_inr": int(scheme.price_paise) / 100,
        "original_price_paise": original,
        "original_price_inr": (int(original) / 100) if original is not None else None,
        "discount_percent": discount,
        "pdf_limit": int(scheme.pdf_limit),
        "docx_limit": int(scheme.docx_limit),
        "validity_days": int(scheme.validity_days),
        "description": scheme.description or "",
        "starts_at": scheme.starts_at.isoformat() if scheme.starts_at else None,
        "ends_at": scheme.ends_at.isoformat() if scheme.ends_at else None,
        "is_active": bool(scheme.is_active),
        "is_default": bool(getattr(scheme, "is_default", False)),
        "currently_available": scheme_is_current(scheme, now),
        "availability_reason": (
            "inactive" if not scheme.is_active else
            "starts_later" if _as_utc(scheme.starts_at) and _as_utc(scheme.starts_at) > now else
            "expired" if _as_utc(scheme.ends_at) and _as_utc(scheme.ends_at) < now else
            "available"
        ),
    }

@app.get("/plans")
def list_available_plans(db: Session = Depends(get_db)):
    """Return all active database plans/offers with availability status.
    The client displays only currently_available entries; scheduled/expired
    entries remain visible in JSON for diagnosis and Admin date correction.
    """
    schemes = db.query(PlanScheme).filter(
        PlanScheme.is_active.is_(True)
    ).order_by(
        PlanScheme.is_default.desc(), PlanScheme.price_paise.asc(), PlanScheme.id.asc()
    ).all()
    rows = [scheme_dict(x) for x in schemes]
    return {
        "plans": rows,
        "available_count": sum(1 for x in rows if x["currently_available"]),
        "source": "database",
    }

class CreatePaymentBody(BaseModel):
    plan_id: str = Field(default="basic", pattern=r"^(basic|standard|premium)$")
    scheme_id: Optional[int] = Field(default=None, ge=1)

def purchased_plan_details(db: Session, user: User):
    """Return the saved purchase snapshot, with a safe fallback for older purchases."""
    latest_order = db.query(PaymentOrder).filter(
        PaymentOrder.user_id == user.id, PaymentOrder.status == "paid"
    ).order_by(PaymentOrder.id.desc()).first()
    scheme = None
    scheme_id = user.current_scheme_id or (latest_order.scheme_id if latest_order else None)
    if scheme_id:
        scheme = db.query(PlanScheme).filter(PlanScheme.id == scheme_id).first()
    plan = PLAN_CATALOG.get((user.plan_id or "").lower()) or {}
    return {
        "scheme_id": scheme_id,
        "scheme_title": (user.current_scheme_title or
                         (scheme.title if scheme else None) or
                         (latest_order.scheme_title_snapshot if latest_order else None) or
                         plan.get("name")),
        "scheme_description": (user.current_scheme_description or
                               (scheme.description if scheme else "") or
                               (latest_order.scheme_description_snapshot if latest_order else "")),
        "price_paise": (user.current_scheme_price_paise if user.current_scheme_price_paise is not None
                        else (latest_order.amount_paise if latest_order else None)),
        "validity_days": (user.subscription_validity_days or
                          (latest_order.validity_days_snapshot if latest_order else None) or 30),
        "standard_limit": user.standard_limit_override,
        "subject_limit": user.subject_limit_override,
    }

@app.get("/subscription/status")
def get_subscription(user: User = Depends(current_user), db: Session = Depends(get_db)):
    details = purchased_plan_details(db, user)
    active = subscription_active(user)
    plan = PLAN_CATALOG.get((user.plan_id or "").lower())
    pdf_limit = user.pdf_limit_override if user.pdf_limit_override is not None else (plan["pdf_limit"] if plan else 0)
    docx_limit = user.docx_limit_override if user.docx_limit_override is not None else (plan["docx_limit"] if plan else 0)
    return {
        "subscription_active": active,
        "active": active,
        "plan_id": user.plan_id,
        "plan_name": plan["name"] if plan else None,
        "plan_price_paise": plan["price_paise"] if plan else None,
        "login_attempt_limit": plan["attempts"] if plan else None,
        "pdf_limit": pdf_limit,
        "docx_limit": docx_limit,
        "expires_at": user.subscription_expires_at.isoformat() if user.subscription_expires_at else None,
        **details,
    }

# Online monthly generation counters used by the Android app.
def current_usage_period():
    # Monthly quota resets at midnight on the first day of the month in India.
    return datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m")

def get_or_create_usage(db: Session, user_id: int):
    period = current_usage_period()
    usage = db.query(GenerationUsage).filter(
        GenerationUsage.user_id == user_id,
        GenerationUsage.period == period,
    ).first()
    if usage is None:
        usage = GenerationUsage(user_id=user_id, period=period, pdf_used=0, docx_used=0, updated_at=utcnow())
        db.add(usage)
        try:
            db.commit()
        except Exception:
            db.rollback()
        usage = db.query(GenerationUsage).filter(
            GenerationUsage.user_id == user_id,
            GenerationUsage.period == period,
        ).first()
    return usage

class GenerationRequest(BaseModel):
    kind: str = Field(pattern=r"^(pdf|docx)$")

@app.get("/usage/status")
def generation_usage_status(user: User = Depends(current_user), db: Session = Depends(get_db)):
    # Status is readable even after expiry so the dashboard can explain why
    # generation is locked and offer renewal. Only /usage/consume authorizes use.
    plan = PLAN_CATALOG.get((user.plan_id or "").lower())
    if not plan:
        raise HTTPException(status_code=403, detail="No valid subscription plan is attached to this account.")
    usage = get_or_create_usage(db, user.id)
    details = purchased_plan_details(db, user)
    pdf_limit = user.pdf_limit_override if user.pdf_limit_override is not None else int(plan["pdf_limit"])
    docx_limit = user.docx_limit_override if user.docx_limit_override is not None else int(plan["docx_limit"])
    return {
        "plan_id": user.plan_id,
        "plan_name": plan["name"],
        "period": usage.period,
        "subscription_active": subscription_active(user),
        "expires_at": user.subscription_expires_at.isoformat() if user.subscription_expires_at else None,
        "pdf_used": int(usage.pdf_used or 0),
        "pdf_limit": int(pdf_limit),
        "pdf_remaining": max(0, int(pdf_limit) - int(usage.pdf_used or 0)),
        "docx_used": int(usage.docx_used or 0),
        "docx_limit": int(docx_limit),
        "docx_remaining": max(0, int(docx_limit) - int(usage.docx_used or 0)),
        **details,
    }

@app.post("/usage/consume")
def consume_generation(body: GenerationRequest, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not subscription_active(user):
        raise HTTPException(status_code=403, detail="An active subscription is required to generate papers.")
    plan = PLAN_CATALOG.get((user.plan_id or "").lower())
    if not plan:
        raise HTTPException(status_code=403, detail="No valid subscription plan is attached to this account.")
    usage = get_or_create_usage(db, user.id)
    # Lock the usage row on PostgreSQL so concurrent requests cannot exceed the plan limit.
    query = db.query(GenerationUsage).filter(GenerationUsage.id == usage.id)
    if engine.dialect.name == "postgresql":
        query = query.with_for_update()
    usage = query.first()
    field = "pdf_used" if body.kind == "pdf" else "docx_used"
    override = user.pdf_limit_override if body.kind == "pdf" else user.docx_limit_override
    limit = int(override if override is not None else (plan["pdf_limit"] if body.kind == "pdf" else plan["docx_limit"]))
    used = int(getattr(usage, field) or 0)
    if used >= limit:
        raise HTTPException(status_code=429, detail=f"Monthly {body.kind.upper()} generation limit reached ({limit}).")
    setattr(usage, field, used + 1)
    usage.updated_at = utcnow()
    db.commit()
    return {"kind": body.kind, "used": used + 1, "limit": limit, "remaining": max(0, limit - used - 1), "period": usage.period}

@app.post("/usage/release")
def release_generation(body: GenerationRequest, user: User = Depends(current_user), db: Session = Depends(get_db)):
    usage = get_or_create_usage(db, user.id)
    field = "pdf_used" if body.kind == "pdf" else "docx_used"
    query = db.query(GenerationUsage).filter(GenerationUsage.id == usage.id)
    if engine.dialect.name == "postgresql":
        query = query.with_for_update()
    usage = query.first()
    setattr(usage, field, max(0, int(getattr(usage, field) or 0) - 1))
    usage.updated_at = utcnow()
    db.commit()
    plan = PLAN_CATALOG.get((user.plan_id or "").lower()) or {"pdf_limit": 0, "docx_limit": 0}
    override = user.pdf_limit_override if body.kind == "pdf" else user.docx_limit_override
    limit = int(override if override is not None else (plan["pdf_limit"] if body.kind == "pdf" else plan["docx_limit"]))
    used = int(getattr(usage, field) or 0)
    return {"kind": body.kind, "used": used, "limit": limit, "remaining": max(0, limit - used), "period": usage.period}

@app.post("/payments/create")
def create_payment(body: CreatePaymentBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    require_config("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET")
    plan_id = body.plan_id.lower()
    plan = PLAN_CATALOG[plan_id]
    scheme = None
    if body.scheme_id is not None:
        scheme = db.query(PlanScheme).filter(PlanScheme.id == body.scheme_id).first()
        if not scheme or not scheme_is_current(scheme):
            raise HTTPException(status_code=400, detail="This offer is no longer available. Refresh the plans and select an active offer.")
        plan_id = scheme.plan_id
    else:
        # Compatibility for older clients: use the most recent active scheme for this plan.
        candidates = db.query(PlanScheme).filter(
            PlanScheme.plan_id == plan_id, PlanScheme.is_active.is_(True)
        ).order_by(PlanScheme.id.desc()).all()
        scheme = next((item for item in candidates if scheme_is_current(item)), None)
    if scheme:
        amount_paise = int(scheme.price_paise)
        pdf_limit_snapshot = int(scheme.pdf_limit)
        docx_limit_snapshot = int(scheme.docx_limit)
        validity_days_snapshot = int(scheme.validity_days)
        plan_name = scheme.title
        scheme_title_snapshot = scheme.title
        scheme_description_snapshot = scheme.description or ""
        scheme_id = scheme.id
    else:
        amount_paise = int(plan["price_paise"])
        pdf_limit_snapshot = int(plan["pdf_limit"])
        docx_limit_snapshot = int(plan["docx_limit"])
        validity_days_snapshot = 30
        plan_name = plan["name"]
        scheme_title_snapshot = plan["name"]
        scheme_description_snapshot = ""
        scheme_id = None
    reference = secrets.token_urlsafe(18)
    order = PaymentOrder(reference_id=reference, user_id=user.id, amount_paise=amount_paise,
                         plan_id=plan_id, scheme_id=scheme_id,
                         pdf_limit_snapshot=pdf_limit_snapshot, docx_limit_snapshot=docx_limit_snapshot,
                         validity_days_snapshot=validity_days_snapshot,
                         scheme_title_snapshot=scheme_title_snapshot,
                         scheme_description_snapshot=scheme_description_snapshot, status="creating")
    db.add(order)
    db.commit()
    db.refresh(order)
    payload = {
        "amount": amount_paise,
        "currency": "INR",
        "accept_partial": False,
        "description": f"Totally Paper Generator - {plan_name} - {validity_days_snapshot} days access",
        "reference_id": reference,
        "reminder_enable": False,
        "notes": {"user_id": str(user.id), "reference_id": reference,
                  "product": "Totally Paper Generator", "plan_id": plan_id, "scheme_id": str(scheme_id or "")},
    }
    data = razorpay_request("POST", "/payment_links", payload)
    link_id = data.get("id")
    payment_url = data.get("short_url")
    if not link_id or not payment_url:
        raise HTTPException(status_code=502, detail="Payment provider did not return a checkout link.")
    order.razorpay_link_id = link_id
    order.status = str(data.get("status", "created"))
    db.commit()
    return {"order_id": link_id, "payment_url": payment_url, "plan_id": plan_id,
            "scheme_id": scheme_id, "plan_name": plan_name, "amount_paise": amount_paise,
            "pdf_limit": pdf_limit_snapshot, "docx_limit": docx_limit_snapshot,
            "validity_days": validity_days_snapshot,
            "scheme_title": scheme_title_snapshot,
            "scheme_description": scheme_description_snapshot}

@app.get("/payments/status/{order_id}")
def payment_status(order_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    order = db.query(PaymentOrder).filter(
        PaymentOrder.razorpay_link_id == order_id, PaymentOrder.user_id == user.id
    ).first()
    if not order:
        raise HTTPException(status_code=404, detail="Payment order not found for this account.")
    if order.status != "paid":
        data = razorpay_request("GET", "/payment_links/" + order_id)
        if data.get("status") == "paid" and int(data.get("amount_paid", 0)) >= order.amount_paise:
            activate_order(db, order)
        else:
            order.status = str(data.get("status", order.status))
            db.commit()
    db.refresh(user)
    active = subscription_active(user)
    plan = PLAN_CATALOG.get((user.plan_id or "").lower())
    expiry = user.subscription_expires_at.isoformat() if user.subscription_expires_at else None
    return {"order_id": order_id, "payment_status": order.status, "subscription_active": active,
            "plan_id": user.plan_id, "plan_name": plan["name"] if plan else None,
            "subscription": {"active": active, "subscription_active": active,
                             "plan_id": user.plan_id, "plan_name": plan["name"] if plan else None,
                             "expires_at": expiry}}

@app.post("/payments/webhook")
async def razorpay_webhook(request: Request, db: Session = Depends(get_db)):
    if not RAZORPAY_WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="Webhook secret is not configured.")
    raw = await request.body()
    supplied = request.headers.get("X-Razorpay-Signature", "")
    expected = hmac.new(RAZORPAY_WEBHOOK_SECRET.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    if not supplied or not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=400, detail="Invalid webhook signature.")
    try:
        event = json.loads(raw.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid webhook payload.")
    if event.get("event") == "payment_link.paid":
        entity = event.get("payload", {}).get("payment_link", {}).get("entity", {})
        link_id = entity.get("id")
        order = db.query(PaymentOrder).filter(PaymentOrder.razorpay_link_id == link_id).first()
        if order and int(entity.get("amount_paid", 0)) >= order.amount_paise:
            activate_order(db, order)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Secure Admin API
# Configure ADMIN_USERNAME and ADMIN_PASSWORD in Render Environment.
# APP_SECRET must be a long, random secret. Never put admin credentials in this file.
# ---------------------------------------------------------------------------

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")

class AdminLoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)

class AdminUserUpdateBody(BaseModel):
    plan_id: Optional[str] = Field(default=None, pattern=r"^(basic|standard|premium)$")
    expires_at: Optional[str] = None
    is_blocked: Optional[bool] = None
    pdf_limit_override: Optional[int] = Field(default=None, ge=0)
    docx_limit_override: Optional[int] = Field(default=None, ge=0)
    standard_limit_override: Optional[int] = Field(default=None, ge=0)
    subject_limit_override: Optional[int] = Field(default=None, ge=0)
    admin_note: Optional[str] = Field(default=None, max_length=2000)

def require_admin_config():
    missing = []
    if not ADMIN_USERNAME:
        missing.append("ADMIN_USERNAME")
    if not ADMIN_PASSWORD:
        missing.append("ADMIN_PASSWORD")
    if not APP_SECRET:
        missing.append("APP_SECRET")
    if missing:
        raise HTTPException(
            status_code=503,
            detail="Admin setup incomplete. Set Render environment variables: " + ", ".join(missing)
        )

def admin_token(username: str) -> str:
    now = utcnow()
    return jwt.encode(
        {"sub": username, "role": "admin", "iat": now, "exp": now + timedelta(hours=8)},
        APP_SECRET,
        algorithm="HS256",
    )

def current_admin(authorization: Optional[str] = Header(default=None)):
    require_admin_config()
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Administrator login required.")
    token = authorization.split(" ", 1)[1].strip()
    try:
        payload = jwt.decode(token, APP_SECRET, algorithms=["HS256"])
    except Exception:
        raise HTTPException(status_code=401, detail="Admin session expired or invalid. Please log in again.")
    if payload.get("role") != "admin" or payload.get("sub") != ADMIN_USERNAME:
        raise HTTPException(status_code=403, detail="Administrator access required.")
    return payload


# ---------------------------------------------------------------------------
# Online question bank. Excel import is ADD-ONLY: duplicate rows are skipped.
# PlanRules is deliberately not imported; plans remain managed by Plan Schemes.
# ---------------------------------------------------------------------------
QUESTION_BANK_SHEETS = {
    "Schools": ["SchoolName"],
    "Standards": ["Standard"],
    "QuestionTypes": ["Question Type"],
    "Sections": ["Sections"],
    "Subjects": ["Subject"],
    "TotalMarks": ["TotalMarks"],
    "ExamTitles": ["ExamTitle"],
    "Questions": ["Section", "QuestionType", "Question", "Marks", "Subject", "Standard"],
    "Instructions": ["Question Type", "Instruction"],
    "Mark": ["Question Type", "Mark"],
    "Time": ["Time"],
}

def _qb_clean(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()

def _qb_fingerprint(sheet_name, values):
    canonical = json.dumps(
        [sheet_name.casefold()] + [re.sub(r"\s+", " ", _qb_clean(v)).casefold() for v in values],
        ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

def _qb_row_values(sheet_name, data):
    headers = QUESTION_BANK_SHEETS[sheet_name]
    return [_qb_clean(data.get(h, "")) for h in headers]

def _qb_record_dict(record):
    return {"id": record.id, "sheet": record.sheet_name, "values": json.loads(record.data_json)}

class QuestionBankImportBody(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content_base64: str = Field(min_length=20)

class QuestionBankRecordBody(BaseModel):
    values: dict

@app.post("/admin/question-bank/import")
def admin_import_question_bank(body: QuestionBankImportBody, admin=Depends(current_admin), db: Session = Depends(get_db)):
    if not body.filename.lower().endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="Please upload an .xlsx workbook.")
    try:
        raw = base64.b64decode(body.content_base64, validate=True)
        if len(raw) > 15 * 1024 * 1024:
            raise ValueError("Workbook is larger than 15 MB.")
        wb = openpyxl.load_workbook(BytesIO(raw), read_only=True, data_only=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Could not read the Excel workbook: " + str(exc))
    summary = {}
    try:
        for sheet_name, expected_headers in QUESTION_BANK_SHEETS.items():
            if sheet_name not in wb.sheetnames:
                summary[sheet_name] = {"inserted": 0, "skipped": 0, "invalid": 0, "message": "Sheet not found"}
                continue
            ws = wb[sheet_name]
            header_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), ())
            normalized = {re.sub(r"[^a-z0-9]", "", _qb_clean(v).casefold()): i for i, v in enumerate(header_row)}
            indexes = []
            missing = []
            for header in expected_headers:
                key = re.sub(r"[^a-z0-9]", "", header.casefold())
                idx = normalized.get(key)
                if idx is None and sheet_name == "Sections":
                    idx = normalized.get("section")
                if idx is None and sheet_name == "Mark" and header == "Mark":
                    idx = normalized.get("marks")
                if idx is None:
                    missing.append(header)
                indexes.append(idx)
            if missing:
                summary[sheet_name] = {"inserted": 0, "skipped": 0, "invalid": 0, "message": "Missing columns: " + ", ".join(missing)}
                continue
            inserted = skipped = invalid = 0
            for row in ws.iter_rows(min_row=2, values_only=True):
                vals = [_qb_clean(row[i]) if i < len(row) else "" for i in indexes]
                if not any(vals):
                    continue
                if sheet_name == "Questions" and (not vals[2] or not vals[5] or not vals[0] or not vals[1]):
                    invalid += 1
                    continue
                if sheet_name in ("Instructions", "Mark") and not vals[0]:
                    invalid += 1
                    continue
                data = dict(zip(expected_headers, vals))
                fingerprint = _qb_fingerprint(sheet_name, vals)
                exists = db.query(QuestionBankRecord.id).filter(
                    QuestionBankRecord.sheet_name == sheet_name,
                    QuestionBankRecord.fingerprint == fingerprint
                ).first()
                if exists:
                    skipped += 1
                    continue
                db.add(QuestionBankRecord(sheet_name=sheet_name,
                    data_json=json.dumps(data, ensure_ascii=False), fingerprint=fingerprint))
                inserted += 1
            db.flush()
            summary[sheet_name] = {"inserted": inserted, "skipped": skipped, "invalid": invalid}
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        wb.close()
    return {"ok": True, "mode": "add_only", "excluded_sheets": ["PlanRules"], "sheets": summary,
            "totals": {key: sum(v.get(key, 0) for v in summary.values()) for key in ("inserted", "skipped", "invalid")}}

@app.get("/admin/question-bank/{sheet_name}")
def admin_list_question_bank(sheet_name: str, admin=Depends(current_admin), db: Session=Depends(get_db)):
    if sheet_name not in QUESTION_BANK_SHEETS:
        raise HTTPException(status_code=404, detail="Unknown question-bank sheet.")
    rows = db.query(QuestionBankRecord).filter(QuestionBankRecord.sheet_name == sheet_name).order_by(QuestionBankRecord.id.asc()).all()
    return {"sheet": sheet_name, "headers": QUESTION_BANK_SHEETS[sheet_name], "records": [_qb_record_dict(x) for x in rows]}

@app.post("/admin/question-bank/{sheet_name}")
def admin_add_question_bank_record(sheet_name: str, body: QuestionBankRecordBody, admin=Depends(current_admin), db: Session=Depends(get_db)):
    if sheet_name not in QUESTION_BANK_SHEETS:
        raise HTTPException(status_code=404, detail="Unknown question-bank sheet.")
    vals = _qb_row_values(sheet_name, body.values)
    if not any(vals):
        raise HTTPException(status_code=400, detail="Enter at least one value.")
    if sheet_name == "Questions" and (not vals[0] or not vals[1] or not vals[2] or not vals[4] or not vals[5]):
        raise HTTPException(status_code=400, detail="Section, Question Type, Question, Subject and Standard are required.")
    fp = _qb_fingerprint(sheet_name, vals)
    if db.query(QuestionBankRecord.id).filter_by(sheet_name=sheet_name, fingerprint=fp).first():
        return {"ok": True, "duplicate": True, "message": "Duplicate skipped."}
    row = QuestionBankRecord(sheet_name=sheet_name, data_json=json.dumps(dict(zip(QUESTION_BANK_SHEETS[sheet_name], vals)), ensure_ascii=False), fingerprint=fp)
    db.add(row); db.commit(); db.refresh(row)
    return {"ok": True, "record": _qb_record_dict(row)}

@app.put("/admin/question-bank/{sheet_name}/{record_id}")
def admin_update_question_bank_record(sheet_name: str, record_id: int, body: QuestionBankRecordBody, admin=Depends(current_admin), db: Session=Depends(get_db)):
    if sheet_name not in QUESTION_BANK_SHEETS:
        raise HTTPException(status_code=404, detail="Unknown question-bank sheet.")
    row = db.query(QuestionBankRecord).filter_by(id=record_id, sheet_name=sheet_name).first()
    if not row: raise HTTPException(status_code=404, detail="Record not found.")
    vals = _qb_row_values(sheet_name, body.values); fp = _qb_fingerprint(sheet_name, vals)
    duplicate = db.query(QuestionBankRecord.id).filter(QuestionBankRecord.sheet_name==sheet_name, QuestionBankRecord.fingerprint==fp, QuestionBankRecord.id!=record_id).first()
    if duplicate: raise HTTPException(status_code=409, detail="Another identical record already exists.")
    row.data_json=json.dumps(dict(zip(QUESTION_BANK_SHEETS[sheet_name], vals)), ensure_ascii=False)
    row.fingerprint=fp; row.updated_at=utcnow(); db.commit(); db.refresh(row)
    return {"ok": True, "record": _qb_record_dict(row)}

@app.delete("/admin/question-bank/{sheet_name}/{record_id}")
def admin_delete_question_bank_record(sheet_name: str, record_id: int, admin=Depends(current_admin), db: Session=Depends(get_db)):
    if sheet_name not in QUESTION_BANK_SHEETS:
        raise HTTPException(status_code=404, detail="Unknown question-bank sheet.")
    row=db.query(QuestionBankRecord).filter_by(id=record_id, sheet_name=sheet_name).first()
    if not row: raise HTTPException(status_code=404, detail="Record not found.")
    db.delete(row); db.commit()
    return {"ok": True, "deleted_id": record_id}

class BlueprintBody(BaseModel):
    exam_title: str = Field(min_length=1, max_length=120)
    standard: str = Field(min_length=1, max_length=60)
    subject: str = Field(min_length=1, max_length=100)
    total_marks: float = Field(gt=0)
    sections: list = Field(min_length=1)

def _blueprint_norm(value):
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()

def _blueprint_fingerprint(exam_title, standard, subject):
    raw = "|".join(_blueprint_norm(x) for x in (exam_title, standard, subject))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def _blueprint_dict(row):
    data = json.loads(row.items_json)
    return {"id": row.id, "name": row.exam_title, "exam_title": row.exam_title,
            "standard": row.standard, "subject": row.subject,
            "total_marks": data.get("total_marks", 0),
            "sections": data.get("sections", [])}

def _validate_blueprint_structure(sections, total_marks):
    if not sections:
        raise HTTPException(status_code=400, detail="Add at least one section.")
    clean, seen_sections = [], set()
    calculated = 0.0
    for section in sections:
        section_name = str(section.get("section", "")).strip()
        if not section_name:
            raise HTTPException(status_code=400, detail="Every section must have a name.")
        skey = _blueprint_norm(section_name)
        if skey in seen_sections:
            raise HTTPException(status_code=409, detail="Duplicate section name in blueprint.")
        seen_sections.add(skey)
        clean_types, seen_types = [], set()
        for item in section.get("question_types", []):
            qtype = str(item.get("question_type", "")).strip()
            try:
                count = int(item.get("question_count", 0))
                marks = float(item.get("marks_per_question", 0))
            except (TypeError, ValueError):
                count, marks = 0, 0
            if not qtype or count <= 0 or marks <= 0:
                raise HTTPException(status_code=400, detail="Each question type needs a name, positive question count and positive marks per question.")
            tkey = _blueprint_norm(qtype)
            if tkey in seen_types:
                raise HTTPException(status_code=409, detail="Duplicate question type inside a section.")
            seen_types.add(tkey)
            clean_types.append({"question_type": qtype, "question_count": count,
                                "marks_per_question": marks})
            calculated += count * marks
        if not clean_types:
            raise HTTPException(status_code=400, detail="Each section must contain at least one question type.")
        clean.append({"section": section_name, "question_types": clean_types})
    if abs(calculated - float(total_marks)) > 0.001:
        raise HTTPException(status_code=400, detail=f"Blueprint total marks mismatch. The question pattern totals {calculated:g}, not {float(total_marks):g}.")
    return {"total_marks": float(total_marks), "sections": clean}

@app.get("/admin/blueprints")
def admin_list_blueprints(admin=Depends(current_admin), db: Session=Depends(get_db)):
    rows = db.query(BlueprintPreset).order_by(BlueprintPreset.exam_title, BlueprintPreset.standard, BlueprintPreset.subject).all()
    return {"records": [_blueprint_dict(x) for x in rows]}

@app.post("/admin/blueprints")
def admin_create_blueprint(body: BlueprintBody, admin=Depends(current_admin), db: Session=Depends(get_db)):
    fp = _blueprint_fingerprint(body.exam_title, body.standard, body.subject)
    if db.query(BlueprintPreset.id).filter_by(fingerprint=fp).first():
        raise HTTPException(status_code=409, detail="A blueprint already exists for this Exam Title, Standard and Subject.")
    structure = _validate_blueprint_structure(body.sections, body.total_marks)
    row = BlueprintPreset(name=body.exam_title.strip(), exam_title=body.exam_title.strip(),
        standard=body.standard.strip(), subject=body.subject.strip(),
        items_json=json.dumps(structure, ensure_ascii=False), fingerprint=fp)
    db.add(row); db.commit(); db.refresh(row)
    return {"ok": True, "record": _blueprint_dict(row)}

@app.put("/admin/blueprints/{blueprint_id}")
def admin_update_blueprint(blueprint_id: int, body: BlueprintBody, admin=Depends(current_admin), db: Session=Depends(get_db)):
    row = db.query(BlueprintPreset).filter_by(id=blueprint_id).first()
    if not row: raise HTTPException(status_code=404, detail="Blueprint not found.")
    fp = _blueprint_fingerprint(body.exam_title, body.standard, body.subject)
    duplicate = db.query(BlueprintPreset.id).filter(BlueprintPreset.fingerprint==fp, BlueprintPreset.id!=blueprint_id).first()
    if duplicate: raise HTTPException(status_code=409, detail="A blueprint already exists for this Exam Title, Standard and Subject.")
    structure = _validate_blueprint_structure(body.sections, body.total_marks)
    row.name=body.exam_title.strip(); row.exam_title=body.exam_title.strip()
    row.standard=body.standard.strip(); row.subject=body.subject.strip()
    row.items_json=json.dumps(structure, ensure_ascii=False); row.fingerprint=fp; row.updated_at=utcnow()
    db.commit(); db.refresh(row)
    return {"ok": True, "record": _blueprint_dict(row)}

@app.delete("/admin/blueprints/{blueprint_id}")
def admin_delete_blueprint(blueprint_id: int, admin=Depends(current_admin), db: Session=Depends(get_db)):
    row=db.query(BlueprintPreset).filter_by(id=blueprint_id).first()
    if not row: raise HTTPException(status_code=404, detail="Blueprint not found.")
    db.delete(row); db.commit()
    return {"ok": True, "deleted_id": blueprint_id}

@app.get("/blueprints")
def list_user_blueprints(exam_title: str, standard: str, subject: str,
                         user: User=Depends(current_user), db: Session=Depends(get_db)):
    rows=db.query(BlueprintPreset).all()
    matched=[x for x in rows if _blueprint_norm(x.exam_title)==_blueprint_norm(exam_title)
             and _blueprint_norm(x.standard)==_blueprint_norm(standard)
             and _blueprint_norm(x.subject)==_blueprint_norm(subject)]
    return {"records": [_blueprint_dict(x) for x in matched]}

@app.get("/question-bank/{sheet_name}")
def get_question_bank_sheet(sheet_name: str, user: User=Depends(current_user), db: Session=Depends(get_db)):
    if sheet_name not in QUESTION_BANK_SHEETS:
        raise HTTPException(status_code=404, detail="Unknown question-bank sheet.")
    rows=db.query(QuestionBankRecord).filter_by(sheet_name=sheet_name).order_by(QuestionBankRecord.id.asc()).all()
    return {"sheet": sheet_name, "headers": QUESTION_BANK_SHEETS[sheet_name], "records": [_qb_record_dict(x) for x in rows]}


def _dt_iso(value):
    return value.isoformat() if value else None

def _admin_user_dict(db: Session, user: User):
    usage = db.query(GenerationUsage).filter(
        GenerationUsage.user_id == user.id,
        GenerationUsage.period == current_usage_period(),
    ).first()
    plan = PLAN_CATALOG.get((user.plan_id or "").lower()) or {}
    pdf_used = int(usage.pdf_used or 0) if usage else 0
    docx_used = int(usage.docx_used or 0) if usage else 0
    pdf_limit = user.pdf_limit_override if user.pdf_limit_override is not None else int(plan.get("pdf_limit", 0))
    docx_limit = user.docx_limit_override if user.docx_limit_override is not None else int(plan.get("docx_limit", 0))
    seen = user.last_seen_at
    if seen and seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    online = bool(seen and seen >= utcnow() - timedelta(minutes=5))

    # Resolve the exact purchased offer from all available snapshots.
    # User fields are the durable subscription snapshot; PaymentOrder is the
    # fallback for accounts created before those fields were populated.
    latest_order = db.query(PaymentOrder).filter(
        PaymentOrder.user_id == user.id,
        PaymentOrder.status == "paid",
    ).order_by(PaymentOrder.id.desc()).first()

    scheme_id = user.current_scheme_id or (latest_order.scheme_id if latest_order else None)
    scheme = db.query(PlanScheme).filter(PlanScheme.id == scheme_id).first() if scheme_id else None

    scheme_title = (
        getattr(user, "current_scheme_title", None)
        or (scheme.title if scheme else None)
        or (latest_order.scheme_title_snapshot if latest_order else None)
    )
    # If no custom offer title was stored, display the plan catalog name,
    # but expose the base plan separately so Admin never mistakes it for an offer.
    if not scheme_title:
        scheme_title = plan.get("name") or (user.plan_id or "No plan").title()

    purchased_price_paise = getattr(user, "current_scheme_price_paise", None)
    if purchased_price_paise is None and latest_order:
        purchased_price_paise = latest_order.amount_paise
    if purchased_price_paise is None and scheme:
        purchased_price_paise = scheme.price_paise

    validity_days = (
        getattr(user, "subscription_validity_days", None)
        or (latest_order.validity_days_snapshot if latest_order else None)
        or (scheme.validity_days if scheme else None)
    )

    return {
        "id": user.id,
        "email": user.email,
        "phone": user.phone,
        "plan_id": user.plan_id,
        "plan_name": plan.get("name"),
        "base_plan_price_paise": plan.get("price_paise"),
        "scheme_id": scheme_id,
        "scheme_title": scheme_title,
        "purchased_offer_title": scheme_title,
        "scheme_description": (
            getattr(user, "current_scheme_description", None)
            or (scheme.description if scheme else None)
            or (latest_order.scheme_description_snapshot if latest_order else None)
            or ""
        ),
        "purchased_price_paise": purchased_price_paise,
        "purchased_validity_days": validity_days,
        "scheme_original_price_paise": (
            int(scheme.original_price_paise)
            if scheme and scheme.original_price_paise is not None
            else None
        ),
        "expires_at": _dt_iso(user.subscription_expires_at),
        "subscription_active": subscription_active(user),
        "is_blocked": bool(user.is_blocked),
        "online": online,
        "last_seen_at": _dt_iso(user.last_seen_at),
        "created_at": _dt_iso(user.created_at),
        "pdf_used": pdf_used,
        "pdf_limit": int(pdf_limit),
        "docx_used": docx_used,
        "docx_limit": int(docx_limit),
        "overrides": {
            "pdf": user.pdf_limit_override,
            "docx": user.docx_limit_override,
            "standard": user.standard_limit_override,
            "subject": user.subject_limit_override,
        },
        "admin_note": user.admin_note,
    }

@app.post("/admin/login")
def admin_login(body: AdminLoginBody):
    require_admin_config()
    username_ok = hmac.compare_digest(body.username.strip(), ADMIN_USERNAME)
    password_ok = hmac.compare_digest(body.password, ADMIN_PASSWORD)
    if not (username_ok and password_ok):
        # Do not reveal which credential was wrong.
        raise HTTPException(status_code=401, detail="Invalid administrator username or password.")
    return {"access_token": admin_token(ADMIN_USERNAME), "token_type": "bearer", "expires_in": 28800}

@app.get("/admin/dashboard")
def admin_dashboard(admin=Depends(current_admin), db: Session = Depends(get_db)):
    now = utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    total_users = db.query(func.count(User.id)).scalar() or 0
    users = db.query(User).all()
    active = sum(1 for u in users if subscription_active(u))
    expired = sum(1 for u in users if u.plan_id and not subscription_active(u))
    blocked = sum(1 for u in users if bool(u.is_blocked))
    online = 0
    for u in users:
        seen = u.last_seen_at
        if seen and seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        if seen and seen >= now - timedelta(minutes=5):
            online += 1
    pdf_month = db.query(func.coalesce(func.sum(GenerationUsage.pdf_used), 0)).filter(
        GenerationUsage.period == now.strftime("%Y-%m")
    ).scalar() or 0
    docx_month = db.query(func.coalesce(func.sum(GenerationUsage.docx_used), 0)).filter(
        GenerationUsage.period == now.strftime("%Y-%m")
    ).scalar() or 0
    return {
        "total_users": int(total_users),
        "online_users": int(online),
        "active_subscriptions": int(active),
        "expired_subscriptions": int(expired),
        "blocked_users": int(blocked),
        "pdf_generated_this_month": int(pdf_month),
        "docx_generated_this_month": int(docx_month),
    }

class SchemeCreateBody(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    plan_id: str = Field(pattern=r"^(basic|standard|premium)$")
    is_default: bool = False
    price_inr: int = Field(ge=0, le=1000000)
    pdf_limit: int = Field(ge=0, le=10000000)
    docx_limit: int = Field(ge=0, le=10000000)
    validity_days: int = Field(ge=1, le=3650)
    original_price_inr: Optional[int] = Field(default=None, ge=0, le=1000000)
    description: Optional[str] = Field(default="", max_length=1000)
    starts_at: Optional[str] = None
    ends_at: Optional[str] = None
    is_active: bool = True

def parse_optional_datetime(value, field_name):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        # Admin enters local Indian date/time values. Interpret values without
        # an explicit offset as Asia/Kolkata, then store in UTC.
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{field_name} must be an ISO date/time.")

def apply_scheme_body(scheme, body):
    scheme.title = body.title.strip()
    if not scheme.title:
        raise HTTPException(status_code=400, detail="Offer title cannot be blank.")
    scheme.plan_id = body.plan_id
    scheme.price_paise = int(body.price_inr) * 100
    scheme.pdf_limit = int(body.pdf_limit)
    scheme.docx_limit = int(body.docx_limit)
    scheme.validity_days = int(body.validity_days)
    scheme.original_price_paise = int(body.original_price_inr) * 100 if body.original_price_inr is not None else None
    scheme.description = (body.description or "").strip()
    scheme.starts_at = parse_optional_datetime(body.starts_at, "starts_at")
    scheme.ends_at = parse_optional_datetime(body.ends_at, "ends_at")
    if scheme.starts_at and scheme.ends_at and scheme.ends_at < scheme.starts_at:
        raise HTTPException(status_code=400, detail="Offer expiry must be after its start date.")
    scheme.is_active = bool(body.is_active)
    scheme.is_default = bool(body.is_default)
    scheme.updated_at = utcnow()

@app.get("/admin/schemes")
def admin_list_schemes(admin=Depends(current_admin), db: Session = Depends(get_db)):
    schemes = db.query(PlanScheme).order_by(PlanScheme.id.desc()).all()
    return {"schemes": [scheme_dict(x) for x in schemes], "count": len(schemes)}

@app.post("/admin/schemes")
def admin_create_scheme(body: SchemeCreateBody, admin=Depends(current_admin), db: Session = Depends(get_db)):
    if body.is_default:
        existing = db.query(PlanScheme).filter(
            PlanScheme.is_default.is_(True), PlanScheme.plan_id == body.plan_id
        ).first()
        if existing:
            raise HTTPException(status_code=409, detail="A standard plan already exists for this plan type. Edit that plan instead.")
    scheme = PlanScheme(
        title=body.title.strip(), plan_id=body.plan_id, price_paise=body.price_inr * 100,
        pdf_limit=body.pdf_limit, docx_limit=body.docx_limit, validity_days=body.validity_days,
        original_price_paise=body.original_price_inr * 100 if body.original_price_inr is not None else None,
        description=(body.description or "").strip(),
        starts_at=parse_optional_datetime(body.starts_at, "starts_at"),
        ends_at=parse_optional_datetime(body.ends_at, "ends_at"),
        is_active=body.is_active, is_default=body.is_default,
        created_at=utcnow(), updated_at=utcnow()
    )
    if scheme.starts_at and scheme.ends_at and scheme.ends_at < scheme.starts_at:
        raise HTTPException(status_code=400, detail="Offer expiry must be after its start date.")
    if not scheme.title:
        raise HTTPException(status_code=400, detail="Offer title cannot be blank.")
    db.add(scheme)
    db.commit()
    db.refresh(scheme)
    return {"ok": True, "scheme": scheme_dict(scheme)}

@app.put("/admin/schemes/{scheme_id}")
def admin_update_scheme(scheme_id: int, body: SchemeCreateBody, admin=Depends(current_admin), db: Session = Depends(get_db)):
    scheme = db.query(PlanScheme).filter(PlanScheme.id == scheme_id).first()
    if not scheme:
        raise HTTPException(status_code=404, detail="Plan or offer not found.")
    if body.is_default:
        duplicate = db.query(PlanScheme).filter(
            PlanScheme.is_default.is_(True),
            PlanScheme.plan_id == body.plan_id,
            PlanScheme.id != scheme_id
        ).first()
        if duplicate:
            raise HTTPException(status_code=409, detail="A standard plan already exists for this plan type.")
    apply_scheme_body(scheme, body)
    db.commit()
    db.refresh(scheme)
    return {"ok": True, "scheme": scheme_dict(scheme)}

@app.delete("/admin/schemes/{scheme_id}")
def admin_delete_scheme(scheme_id: int, admin=Depends(current_admin), db: Session = Depends(get_db)):
    scheme = db.query(PlanScheme).filter(PlanScheme.id == scheme_id).first()
    if not scheme:
        raise HTTPException(status_code=404, detail="Offer not found.")
    db.delete(scheme)
    db.commit()
    return {"ok": True, "deleted_id": scheme_id}


@app.get("/admin/users")
def admin_list_users(
    q: str = "",
    status: str = "all",
    limit: int = 250,
    admin=Depends(current_admin),
    db: Session = Depends(get_db),
):
    limit = max(1, min(int(limit), 250))
    query = db.query(User)
    search = q.strip()
    if search:
        like = f"%{search}%"
        query = query.filter((User.email.ilike(like)) | (User.phone.ilike(like)))
    users = query.order_by(User.id.desc()).limit(limit).all()
    now = utcnow()
    result = []
    for user in users:
        item = _admin_user_dict(db, user)
        if status == "blocked" and not item["is_blocked"]:
            continue
        if status == "online" and not item["online"]:
            continue
        if status == "active" and not item["subscription_active"]:
            continue
        if status == "expired" and (not user.plan_id or item["subscription_active"]):
            continue
        if status not in ("all", "online", "active", "expired", "blocked"):
            raise HTTPException(status_code=400, detail="Invalid status filter.")
        result.append(item)
    return {"users": result, "count": len(result)}

@app.get("/admin/users/{user_id}")
def admin_get_user(user_id: int, admin=Depends(current_admin), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    return _admin_user_dict(db, user)

@app.patch("/admin/users/{user_id}")
def admin_update_user(
    user_id: int,
    body: AdminUserUpdateBody,
    admin=Depends(current_admin),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    changes = body.model_dump(exclude_unset=True)
    if "plan_id" in changes:
        user.plan_id = changes["plan_id"]
    if "expires_at" in changes:
        raw = changes["expires_at"]
        if raw in (None, ""):
            user.subscription_expires_at = None
        else:
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                user.subscription_expires_at = parsed.astimezone(timezone.utc)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="expires_at must be a valid ISO date/time.")
    for field in (
        "is_blocked", "pdf_limit_override", "docx_limit_override",
        "standard_limit_override", "subject_limit_override", "admin_note"
    ):
        if field in changes:
            value = changes[field]
            if field == "is_blocked":
                value = 1 if value else 0
            setattr(user, field, value)
    db.commit()
    db.refresh(user)
    return {"ok": True, "user": _admin_user_dict(db, user)}
