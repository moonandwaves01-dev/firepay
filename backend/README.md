# Wallet Demo backend (local development)

This folder contains a Python standard-library HTTP API backed by SQLite. It is a development backend; it does not process or verify real bank or USDT payments.

## Run it

From the project root, run:

```sh
python3 backend/server.py
```

The admin dashboard is served at `http://127.0.0.1:8000/admin`. Keep `backend/.env` private; it contains the local admin credentials and is ignored by Git. The server binds to the host and port configured there.

To connect Expo Go on a phone on the same Wi-Fi, copy the root `.env.example` to `.env.local`, replace `YOUR_MAC_LAN_IP` with the Mac's Wi-Fi address (you can get it with `ipconfig getifaddr en0`), then restart Expo. Do not use `127.0.0.1` for a phone; that points to the phone itself.

## First API endpoints

- `POST /api/signup` — name, Indian mobile, password, and optional `inviteCode`; returns user ID, invite code, and a short-lived session token.
- `POST /api/login` — mobile and password; returns a session token.
- `GET /api/me` — current signed-in profile.
- `POST /api/deposit-orders` — creates a pending INR or USDT deposit request. The user cannot confirm it.
- `GET /api/me/transactions` and `GET /api/me/commissions` — current user's records.
- `GET /api/team/summary` — invite code, team count, confirmed team deposits, and commission totals by currency.
- `GET /api/admin/users`, `/api/admin/transactions`, `/api/admin/commissions` — admin-only lists.
- `POST /api/admin/transactions/{id}/confirm` — local admin confirmation for manual development review.

## Commission calculation

When an admin confirms a pending deposit, the backend creates at most one commission ledger row for the referring user: 0.3% of that deposit, rounded to the currency's smallest unit. A unique transaction constraint makes retries idempotent. Only confirmed **deposits** are eligible in this first version; withdrawals do not generate commission. The ledger records the commission but does not transfer it into a withdrawable wallet balance.

Manual confirmation is only for local development. Before any real payments, add an HTTPS deployment, phone OTP, provider-signed payment webhooks, durable auth/session storage, monitoring, rate limits, and payout controls. Do not expose the development server publicly or treat an uploaded transaction ID / screenshot as proof of payment.
