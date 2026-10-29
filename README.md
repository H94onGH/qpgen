# Totally Paper Generator — Email OTP Backend

FastAPI backend using Brevo transactional email OTP, Neon PostgreSQL, and Razorpay Payment Links.

## Render configuration

Build command:
`pip install -r requirements.txt`

Start command:
`uvicorn app:app --host 0.0.0.0 --port $PORT`

Environment variables (add in Render, never commit secrets):
- `DATABASE_URL` — existing Neon PostgreSQL connection string
- `APP_SECRET` — strong random secret (Render Generate button is suitable)
- `BREVO_API_KEY` — Brevo API key
- `BREVO_SENDER_EMAIL` — verified sender address; default in code is `qpgeneduservices@gmail.com`
- `BREVO_SENDER_NAME` — optional; defaults to `Totally Paper Generator`
- `RAZORPAY_KEY_ID` — Razorpay test key while testing
- `RAZORPAY_KEY_SECRET` — matching Razorpay test secret
- `RAZORPAY_WEBHOOK_SECRET` — webhook signing secret configured in Razorpay
- `MONTHLY_PRICE_PAISE=9900`
- `TOKEN_DAYS=30`
- `OTP_TTL_MINUTES=10` (optional)

Webhook URL:
`https://YOUR-RENDER-SERVICE.onrender.com/payments/webhook`

Subscribe to Razorpay event `payment_link.paid`.

## API contract

Send OTP:
`POST /auth/send-otp`
```json
{"email":"recipient@example.com"}
```

Verify OTP:
`POST /auth/verify-otp`
```json
{"email":"recipient@example.com","otp":"123456"}
```

The verification response includes `access_token`, `subscription_active`, and `subscription_expires_at`. The authenticated payment and subscription endpoints remain:
- `GET /subscription/status`
- `POST /payments/create`
- `GET /payments/status/{order_id}`
- `POST /payments/webhook`

## Database migration notes — read before deployment

The code adds an `email` column to the existing `users` table and makes `phone` nullable on PostgreSQL. It does not drop existing users or payment orders. Existing phone-only users and subscriptions remain in the database, but they are **not automatically linked** to a new email login. Existing subscribers must be linked to the correct email account through a deliberate, verified migration before production; do not match accounts by guess.

This is a starter implementation. Before real public launch, add abuse monitoring, stronger rate limiting (ideally IP + email), operational alerting, and test Razorpay webhooks and subscription activation. Keep Razorpay in test mode until end-to-end testing passes.

## Brevo notes

Brevo sends transactional email via `POST https://api.brevo.com/v3/smtp/email`. The sender must be registered/verified in Brevo. A verified sender address does not necessarily mean the domain is authenticated for best deliverability. See:
https://developers.brevo.com/docs/send-a-transactional-email
