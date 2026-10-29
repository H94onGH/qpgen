import os
import json
import hmac
import hashlib
import secrets
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
import requests
from fastapi import FastAPI, Depends, HTTPException, Header, Request
from pydantic import BaseModel, Field, EmailStr
from sqlalchemy import create_engine, Column, Integer, String, DateTime, ForeignKey, Index, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker, Session

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

class PaymentOrder(Base):
    __tablename__ = "payment_orders"
    id = Column(Integer, primary_key=True)
    reference_id = Column(String(64), unique=True, nullable=False, index=True)
    razorpay_link_id = Column(String(64), unique=True, nullable=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    status = Column(String(32), default="created", nullable=False)
    amount_paise = Column(Integer, nullable=False, default=9900)
    plan_id = Column(String(20), nullable=False, default="basic")
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
    user.subscription_expires_at = now + timedelta(days=30)

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
    return {
        "access_token": create_access_token(user),
        "token_type": "bearer",
        "subscription_active": subscription_active(user),
        "subscription_expires_at": user.subscription_expires_at.isoformat() if user.subscription_expires_at else None,
        "plan_id": user.plan_id,
        "email": user.email,
    }

class CreatePaymentBody(BaseModel):
    plan_id: str = Field(default="basic", pattern=r"^(basic|standard|premium)$")

@app.get("/subscription/status")
def get_subscription(user: User = Depends(current_user)):
    active = subscription_active(user)
    plan = PLAN_CATALOG.get((user.plan_id or "").lower())
    return {
        "subscription_active": active,
        "active": active,
        "plan_id": user.plan_id,
        "plan_name": plan["name"] if plan else None,
        "plan_price_paise": plan["price_paise"] if plan else None,
        "login_attempt_limit": plan["attempts"] if plan else None,
        "pdf_limit": plan["pdf_limit"] if plan else 0,
        "docx_limit": plan["docx_limit"] if plan else 0,
        "expires_at": user.subscription_expires_at.isoformat() if user.subscription_expires_at else None,
    }

# Online monthly generation counters used by the Android app.
def current_usage_period():
    return utcnow().strftime("%Y-%m")

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
    if not subscription_active(user):
        raise HTTPException(status_code=403, detail="An active subscription is required to view usage.")
    plan = PLAN_CATALOG.get((user.plan_id or "").lower())
    if not plan:
        raise HTTPException(status_code=403, detail="No valid subscription plan is attached to this account.")
    usage = get_or_create_usage(db, user.id)
    return {
        "plan_id": user.plan_id,
        "plan_name": plan["name"],
        "period": usage.period,
        "subscription_active": subscription_active(user),
        "expires_at": user.subscription_expires_at.isoformat() if user.subscription_expires_at else None,
        "pdf_used": int(usage.pdf_used or 0),
        "pdf_limit": int(plan["pdf_limit"]),
        "pdf_remaining": max(0, int(plan["pdf_limit"]) - int(usage.pdf_used or 0)),
        "docx_used": int(usage.docx_used or 0),
        "docx_limit": int(plan["docx_limit"]),
        "docx_remaining": max(0, int(plan["docx_limit"]) - int(usage.docx_used or 0)),
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
    limit = int(plan["pdf_limit"] if body.kind == "pdf" else plan["docx_limit"])
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
    limit = int(plan["pdf_limit"] if body.kind == "pdf" else plan["docx_limit"])
    used = int(getattr(usage, field) or 0)
    return {"kind": body.kind, "used": used, "limit": limit, "remaining": max(0, limit - used), "period": usage.period}

@app.post("/payments/create")
def create_payment(body: CreatePaymentBody, user: User = Depends(current_user), db: Session = Depends(get_db)):
    require_config("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET")
    plan_id = body.plan_id.lower()
    plan = PLAN_CATALOG[plan_id]
    amount_paise = int(plan["price_paise"])
    reference = secrets.token_urlsafe(18)
    order = PaymentOrder(reference_id=reference, user_id=user.id, amount_paise=amount_paise,
                         plan_id=plan_id, status="creating")
    db.add(order)
    db.commit()
    db.refresh(order)
    payload = {
        "amount": amount_paise,
        "currency": "INR",
        "accept_partial": False,
        "description": f"Totally Paper Generator - {plan['name']} plan - 30 days access",
        "reference_id": reference,
        "reminder_enable": False,
        "notes": {"user_id": str(user.id), "reference_id": reference,
                  "product": "Totally Paper Generator", "plan_id": plan_id},
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
            "plan_name": plan["name"], "amount_paise": amount_paise}

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
