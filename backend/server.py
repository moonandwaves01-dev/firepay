"""Small local demo API for wallet signup and the protected admin page."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent
SESSIONS: dict[str, tuple[str, float]] = {}
USER_SESSIONS: dict[str, tuple[str, float]] = {}
COMMISSION_RATE_BPS = 30  # 0.30%


def load_env_file() -> None:
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file()
DB_PATH = Path(os.environ.get("DATABASE_PATH", str(BASE_DIR / "users.sqlite3"))).expanduser()
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
if len(ADMIN_USERNAME) < 3 or len(ADMIN_PASSWORD) < 10:
    raise SystemExit("Set ADMIN_USERNAME and a 10+ character ADMIN_PASSWORD in backend/.env")


def database() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database() -> None:
    with database() as connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                mobile TEXT NOT NULL UNIQUE,
                password_salt BLOB NOT NULL,
                password_hash BLOB NOT NULL,
                created_at TEXT NOT NULL,
                referral_code TEXT,
                referred_by_user_id TEXT
            )"""
        )
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(users)")}
        if "referral_code" not in columns:
            connection.execute("ALTER TABLE users ADD COLUMN referral_code TEXT")
        if "referred_by_user_id" not in columns:
            connection.execute("ALTER TABLE users ADD COLUMN referred_by_user_id TEXT")
        existing = connection.execute(
            "SELECT user_id FROM users WHERE referral_code IS NULL OR referral_code = ''"
        ).fetchall()
        for row in existing:
            connection.execute(
                "UPDATE users SET referral_code = ? WHERE user_id = ?",
                (new_referral_code(), row["user_id"]),
            )
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_referral_code_uq ON users(referral_code)")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS transactions (
                transaction_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(user_id),
                kind TEXT NOT NULL CHECK (kind IN ('deposit', 'withdrawal')),
                currency TEXT NOT NULL CHECK (currency IN ('INR', 'USDT')),
                amount_minor_units INTEGER NOT NULL CHECK (amount_minor_units > 0),
                status TEXT NOT NULL CHECK (status IN ('pending', 'confirmed', 'failed')),
                payment_reference TEXT UNIQUE,
                created_at TEXT NOT NULL,
                confirmed_at TEXT
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS commission_ledger (
                commission_id TEXT PRIMARY KEY,
                referrer_user_id TEXT NOT NULL REFERENCES users(user_id),
                referred_user_id TEXT NOT NULL REFERENCES users(user_id),
                transaction_id TEXT NOT NULL UNIQUE REFERENCES transactions(transaction_id),
                currency TEXT NOT NULL CHECK (currency IN ('INR', 'USDT')),
                transaction_amount_minor_units INTEGER NOT NULL,
                commission_amount_minor_units INTEGER NOT NULL CHECK (commission_amount_minor_units >= 0),
                rate_basis_points INTEGER NOT NULL CHECK (rate_basis_points = 30),
                created_at TEXT NOT NULL
            )"""
        )
        connection.execute("CREATE INDEX IF NOT EXISTS transactions_user_created_idx ON transactions(user_id, created_at)")
        connection.execute("CREATE INDEX IF NOT EXISTS commissions_referrer_created_idx ON commission_ledger(referrer_user_id, created_at)")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS app_settings (setting_key TEXT PRIMARY KEY, setting_value TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )


def new_referral_code() -> str:
    return f"WD{secrets.token_hex(4).upper()}"


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def amount_to_minor_units(raw_amount: object, currency: str) -> int:
    decimals = 2 if currency == "INR" else 6
    try:
        amount = Decimal(str(raw_amount))
        quantizer = Decimal("1").scaleb(-decimals)
        rounded = amount.quantize(quantizer, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        raise ValueError("Enter a valid transaction amount.") from None
    if not amount.is_finite() or amount <= 0 or rounded != amount:
        raise ValueError(f"Amount must be greater than zero and use at most {decimals} decimal places.")
    return int(rounded * (10**decimals))


def minor_units_to_amount(value: int, currency: str) -> str:
    decimals = 2 if currency == "INR" else 6
    return f"{Decimal(value) / (10**decimals):.{decimals}f}"


def user_record(row: sqlite3.Row) -> dict[str, str]:
    return {
        "userId": row["user_id"],
        "name": row["name"],
        "mobile": row["mobile"],
        "inviteCode": row["referral_code"],
        "invitedBy": row["referred_by_user_id"] or "",
        "createdAt": row["created_at"],
    }


def transaction_record(row: sqlite3.Row) -> dict[str, str | int]:
    return {
        "transactionId": row["transaction_id"],
        "userId": row["user_id"],
        "name": row["name"] if "name" in row.keys() else "",
        "kind": row["kind"],
        "currency": row["currency"],
        "amount": minor_units_to_amount(row["amount_minor_units"], row["currency"]),
        "amountMinorUnits": row["amount_minor_units"],
        "status": row["status"],
        "paymentReference": row["payment_reference"] or "",
        "createdAt": row["created_at"],
        "confirmedAt": row["confirmed_at"] or "",
    }


def commission_record(row: sqlite3.Row) -> dict[str, str | int]:
    return {
        "commissionId": row["commission_id"],
        "referrerUserId": row["referrer_user_id"],
        "referredUserId": row["referred_user_id"],
        "transactionId": row["transaction_id"],
        "currency": row["currency"],
        "transactionAmount": minor_units_to_amount(row["transaction_amount_minor_units"], row["currency"]),
        "commissionAmount": minor_units_to_amount(row["commission_amount_minor_units"], row["currency"]),
        "rate": "0.3%",
        "createdAt": row["created_at"],
    }


ADMIN_PAGE = (BASE_DIR / "admin.html").read_text(encoding="utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "WalletDemoAPI/1.0"

    def log_message(self, _format: str, *args: object) -> None:
        # Avoid logging signup payloads, mobile numbers, or admin credentials.
        return

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def send_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict[str, object]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 16_384:
                raise ValueError("Request is too large.")
            value = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError("Invalid JSON request.") from None
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object.")
        return value

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.end_headers()

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/health":
            self.send_json(200, {"ok": True, "environment": "local-development"})
        elif path == "/api/public/usdt-addresses":
            self.send_json(200, {"addresses": self.get_usdt_addresses()})
        elif path in ("/", "/admin"):
            body = ADMIN_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/admin/users":
            if not self.require_admin_session():
                return
            with database() as connection:
                rows = connection.execute(
                    "SELECT user_id, name, mobile, referral_code, referred_by_user_id, created_at FROM users ORDER BY created_at DESC"
                ).fetchall()
            self.send_json(200, {"users": [user_record(row) for row in rows]})
        elif path == "/api/admin/transactions":
            if not self.require_admin_session():
                return
            with database() as connection:
                rows = connection.execute(
                    "SELECT t.*, u.name FROM transactions t JOIN users u ON u.user_id = t.user_id ORDER BY t.created_at DESC LIMIT 500"
                ).fetchall()
            self.send_json(200, {"transactions": [transaction_record(row) for row in rows]})
        elif path == "/api/admin/commissions":
            if not self.require_admin_session():
                return
            with database() as connection:
                rows = connection.execute(
                    "SELECT * FROM commission_ledger ORDER BY created_at DESC LIMIT 500"
                ).fetchall()
            self.send_json(200, {"commissions": [commission_record(row) for row in rows]})
        elif path == "/api/admin/settings/usdt-addresses":
            if not self.require_admin_session():
                return
            self.send_json(200, {"addresses": self.get_usdt_addresses()})
        elif path == "/api/me":
            user_id = self.require_user_session()
            if user_id is None:
                return
            with database() as connection:
                row = connection.execute(
                    "SELECT user_id, name, mobile, referral_code, referred_by_user_id, created_at FROM users WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
            if not row:
                self.send_json(404, {"error": "User profile not found."})
                return
            self.send_json(200, {"user": user_record(row)})
        elif path == "/api/me/transactions":
            user_id = self.require_user_session()
            if user_id is None:
                return
            with database() as connection:
                rows = connection.execute(
                    "SELECT * FROM transactions WHERE user_id = ? ORDER BY created_at DESC LIMIT 500",
                    (user_id,),
                ).fetchall()
            self.send_json(200, {"transactions": [transaction_record(row) for row in rows]})
        elif path == "/api/me/commissions":
            user_id = self.require_user_session()
            if user_id is None:
                return
            with database() as connection:
                rows = connection.execute(
                    "SELECT * FROM commission_ledger WHERE referrer_user_id = ? ORDER BY created_at DESC LIMIT 500",
                    (user_id,),
                ).fetchall()
            self.send_json(200, {"commissions": [commission_record(row) for row in rows]})
        elif path == "/api/team/summary":
            user_id = self.require_user_session()
            if user_id is None:
                return
            with database() as connection:
                profile = connection.execute(
                    "SELECT referral_code FROM users WHERE user_id = ?", (user_id,)
                ).fetchone()
                team_count = connection.execute(
                    "SELECT COUNT(*) FROM users WHERE referred_by_user_id = ?", (user_id,)
                ).fetchone()[0]
                team_deposits = connection.execute(
                    "SELECT COUNT(*) FROM transactions t JOIN users u ON u.user_id = t.user_id "
                    "WHERE u.referred_by_user_id = ? AND t.kind = 'deposit' AND t.status = 'confirmed'",
                    (user_id,),
                ).fetchone()[0]
                commissions = connection.execute(
                    "SELECT currency, SUM(commission_amount_minor_units) AS total FROM commission_ledger "
                    "WHERE referrer_user_id = ? GROUP BY currency",
                    (user_id,),
                ).fetchall()
            totals = {row["currency"]: minor_units_to_amount(row["total"], row["currency"]) for row in commissions}
            self.send_json(200, {
                "inviteCode": profile["referral_code"] if profile else "",
                "teamMembers": team_count,
                "confirmedTeamDeposits": team_deposits,
                "commissionRate": "0.3%",
                "commissionTotals": totals,
            })
        else:
            self.send_json(404, {"error": "Not found."})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            body = self.read_json()
        except ValueError as error:
            self.send_json(400, {"error": str(error)})
            return

        if path == "/api/signup":
            self.signup(body)
        elif path == "/api/login":
            self.user_login(body)
        elif path == "/api/deposit-orders":
            self.create_deposit_order(body)
        elif path == "/api/logout":
            token = self.bearer_token()
            if token:
                USER_SESSIONS.pop(token, None)
            self.send_json(200, {"ok": True})
        elif path == "/api/admin/login":
            self.admin_login(body)
        elif path == "/api/admin/logout":
            token = self.bearer_token()
            if token:
                SESSIONS.pop(token, None)
            self.send_json(200, {"ok": True})
        elif path == "/api/admin/settings/usdt-addresses":
            self.save_usdt_addresses(body)
        elif re.fullmatch(r"/api/admin/transactions/[A-Za-z0-9_-]+/confirm", path):
            self.confirm_transaction(path.split("/")[4])
        else:
            self.send_json(404, {"error": "Not found."})

    def signup(self, body: dict[str, object]) -> None:
        name = body.get("name")
        mobile = re.sub(r"\D", "", str(body.get("mobile", "")))
        password = body.get("password")
        if not isinstance(name, str) or not 2 <= len(name.strip()) <= 60:
            self.send_json(400, {"error": "Enter a name with 2 to 60 characters."})
            return
        if not re.fullmatch(r"[6-9][0-9]{9}", mobile):
            self.send_json(400, {"error": "Enter a valid 10-digit Indian mobile number."})
            return
        if not isinstance(password, str) or not 8 <= len(password) <= 128:
            self.send_json(400, {"error": "Password must be 8 to 128 characters."})
            return

        invite_code = str(body.get("inviteCode", body.get("invite_code", ""))).strip().upper()
        referred_by_user_id = None
        if invite_code:
            with database() as connection:
                inviter = connection.execute(
                    "SELECT user_id FROM users WHERE referral_code = ?", (invite_code,)
                ).fetchone()
            if not inviter:
                self.send_json(400, {"error": "Invite code is invalid."})
                return
            referred_by_user_id = inviter["user_id"]

        salt = secrets.token_bytes(16)
        password_hash = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 310_000)
        record = {
            "user_id": f"WD-{secrets.token_hex(5).upper()}",
            "name": name.strip(),
            "mobile": mobile,
            "referral_code": new_referral_code(),
            "referred_by_user_id": referred_by_user_id,
            "password_salt": salt,
            "password_hash": password_hash,
            "created_at": utc_now(),
        }
        try:
            with database() as connection:
                connection.execute(
                    "INSERT INTO users (user_id, name, mobile, password_salt, password_hash, created_at, referral_code, referred_by_user_id) "
                    "VALUES (:user_id, :name, :mobile, :password_salt, :password_hash, :created_at, :referral_code, :referred_by_user_id)",
                    record,
                )
        except sqlite3.IntegrityError:
            self.send_json(409, {"error": "This mobile number is already registered."})
            return

        token = secrets.token_urlsafe(32)
        USER_SESSIONS[token] = (record["user_id"], time.time() + 24 * 60 * 60)
        self.send_json(
            201,
            {"token": token, "expiresIn": 86_400,
             "user": {"userId": record["user_id"], "name": record["name"],
                      "mobile": record["mobile"], "inviteCode": record["referral_code"],
                      "invitedBy": referred_by_user_id or "", "createdAt": record["created_at"]}},
        )

    def user_login(self, body: dict[str, object]) -> None:
        mobile = re.sub(r"\D", "", str(body.get("mobile", "")))
        password = body.get("password")
        if not re.fullmatch(r"[6-9][0-9]{9}", mobile) or not isinstance(password, str):
            self.send_json(401, {"error": "Mobile number or password is incorrect."})
            return
        with database() as connection:
            row = connection.execute(
                "SELECT user_id, name, mobile, password_salt, password_hash, referral_code, referred_by_user_id, created_at "
                "FROM users WHERE mobile = ?", (mobile,)
            ).fetchone()
        if not row:
            self.send_json(401, {"error": "Mobile number or password is incorrect."})
            return
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), row["password_salt"], 310_000)
        if not hmac.compare_digest(candidate, row["password_hash"]):
            self.send_json(401, {"error": "Mobile number or password is incorrect."})
            return
        token = secrets.token_urlsafe(32)
        USER_SESSIONS[token] = (row["user_id"], time.time() + 24 * 60 * 60)
        self.send_json(200, {"token": token, "expiresIn": 86_400, "user": user_record(row)})

    def create_deposit_order(self, body: dict[str, object]) -> None:
        user_id = self.require_user_session()
        if user_id is None:
            return
        currency = str(body.get("currency", "")).strip().upper()
        if currency not in ("INR", "USDT"):
            self.send_json(400, {"error": "Currency must be INR or USDT."})
            return
        try:
            amount_minor_units = amount_to_minor_units(body.get("amount"), currency)
        except ValueError as error:
            self.send_json(400, {"error": str(error)})
            return
        if currency == "INR" and amount_minor_units < 10_000:
            self.send_json(400, {"error": "Minimum INR deposit is ₹100."})
            return
        transaction_id = f"TX-{secrets.token_hex(8).upper()}"
        created_at = utc_now()
        with database() as connection:
            connection.execute(
                "INSERT INTO transactions (transaction_id, user_id, kind, currency, amount_minor_units, status, created_at) "
                "VALUES (?, ?, 'deposit', ?, ?, 'pending', ?)",
                (transaction_id, user_id, currency, amount_minor_units, created_at),
            )
            row = connection.execute(
                "SELECT * FROM transactions WHERE transaction_id = ?", (transaction_id,)
            ).fetchone()
        self.send_json(201, {"transaction": transaction_record(row)})

    def confirm_transaction(self, transaction_id: str) -> None:
        if not self.require_admin_session():
            return
        with database() as connection:
            connection.execute("BEGIN IMMEDIATE")
            transaction = connection.execute(
                "SELECT * FROM transactions WHERE transaction_id = ?", (transaction_id,)
            ).fetchone()
            if not transaction:
                self.send_json(404, {"error": "Deposit request not found."})
                return
            if transaction["status"] == "confirmed":
                self.send_json(200, {"transaction": transaction_record(transaction), "alreadyConfirmed": True})
                return
            if transaction["status"] != "pending":
                self.send_json(409, {"error": "Only pending deposit requests can be confirmed."})
                return

            confirmed_at = utc_now()
            connection.execute(
                "UPDATE transactions SET status = 'confirmed', confirmed_at = ? WHERE transaction_id = ?",
                (confirmed_at, transaction_id),
            )
            user = connection.execute(
                "SELECT referred_by_user_id FROM users WHERE user_id = ?", (transaction["user_id"],)
            ).fetchone()
            if user and user["referred_by_user_id"]:
                commission_minor_units = int((
                    Decimal(transaction["amount_minor_units"]) * Decimal(COMMISSION_RATE_BPS) / Decimal(10_000)
                ).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
                connection.execute(
                    "INSERT INTO commission_ledger (commission_id, referrer_user_id, referred_user_id, transaction_id, "
                    "currency, transaction_amount_minor_units, commission_amount_minor_units, rate_basis_points, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (f"CM-{secrets.token_hex(8).upper()}", user["referred_by_user_id"], transaction["user_id"],
                     transaction_id, transaction["currency"], transaction["amount_minor_units"],
                     commission_minor_units, COMMISSION_RATE_BPS, confirmed_at),
                )
            confirmed = connection.execute(
                "SELECT * FROM transactions WHERE transaction_id = ?", (transaction_id,)
            ).fetchone()
        self.send_json(200, {"transaction": transaction_record(confirmed)})

    def get_usdt_addresses(self) -> dict[str, str]:
        with database() as connection:
            rows = connection.execute(
                "SELECT setting_key, setting_value FROM app_settings WHERE setting_key IN ('TRC20', 'ERC20')"
            ).fetchall()
        addresses = {"TRC20": "", "ERC20": ""}
        addresses.update({row["setting_key"]: row["setting_value"] for row in rows})
        return addresses

    def save_usdt_addresses(self, body: dict[str, object]) -> None:
        if not self.require_admin_session():
            return
        patterns = {
            "TRC20": re.compile(r"T[1-9A-HJ-NP-Za-km-z]{33}"),
            "ERC20": re.compile(r"0x[a-fA-F0-9]{40}"),
        }
        addresses: dict[str, str] = {}
        for network, pattern in patterns.items():
            raw_address = body.get(network, "")
            if not isinstance(raw_address, str):
                self.send_json(400, {"error": f"{network} address must be text."})
                return
            address = raw_address.strip()
            if len(address) > 128 or (address and not pattern.fullmatch(address)):
                self.send_json(400, {"error": f"Enter a valid {network} USDT address, or leave it blank."})
                return
            addresses[network] = address

        with database() as connection:
            for network, address in addresses.items():
                connection.execute(
                    "INSERT INTO app_settings (setting_key, setting_value, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(setting_key) DO UPDATE SET setting_value = excluded.setting_value, updated_at = excluded.updated_at",
                    (network, address, utc_now()),
                )
        self.send_json(200, {"addresses": addresses})

    def admin_login(self, body: dict[str, object]) -> None:
        username = str(body.get("username", ""))
        password = str(body.get("password", ""))
        if not (
            hmac.compare_digest(username.encode("utf-8"), ADMIN_USERNAME.encode("utf-8"))
            and hmac.compare_digest(password.encode("utf-8"), ADMIN_PASSWORD.encode("utf-8"))
        ):
            self.send_json(401, {"error": "Admin username or password is incorrect."})
            return
        token = secrets.token_urlsafe(32)
        SESSIONS[token] = (username, time.time() + 8 * 60 * 60)
        self.send_json(200, {"token": token, "expiresIn": 28_800})

    def bearer_token(self) -> str | None:
        authorization = self.headers.get("Authorization", "")
        if authorization.startswith("Bearer "):
            return authorization[7:]
        return None

    def require_admin_session(self) -> bool:
        token = self.bearer_token()
        session = SESSIONS.get(token or "")
        if not session or session[1] < time.time():
            if token:
                SESSIONS.pop(token, None)
            self.send_json(401, {"error": "Admin session expired. Please sign in again."})
            return False
        return True

    def require_user_session(self) -> str | None:
        token = self.bearer_token()
        session = USER_SESSIONS.get(token or "")
        if not session or session[1] < time.time():
            if token:
                USER_SESSIONS.pop(token, None)
            self.send_json(401, {"error": "User session expired. Please sign in again."})
            return None
        return session[0]


if __name__ == "__main__":
    initialize_database()
    host = os.environ.get("BACKEND_HOST", "0.0.0.0")
    port = int(os.environ.get("BACKEND_PORT", "8000"))
    print(f"Wallet demo backend running on http://{host}:{port}")
    print("Admin dashboard: http://127.0.0.1:8000/admin")
    print("Development server only. Use HTTPS and production hosting before real/public use.")
    ThreadingHTTPServer((host, port), Handler).serve_forever()
