"""사용자 저장소 — SQLite + Fernet 암호화"""
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from cryptography.fernet import Fernet
from google.oauth2.credentials import Credentials

_DB_PATH = os.path.join(os.path.dirname(__file__), "users.db")

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/documents.readonly",
]


def _fernet() -> Fernet:
    key = os.getenv("ENCRYPTION_KEY")
    if not key:
        raise RuntimeError("ENCRYPTION_KEY 환경변수가 설정되지 않았습니다.")
    return Fernet(key.encode() if isinstance(key, str) else key)


@contextmanager
def _conn():
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """DB 및 테이블 초기화 — 앱 시작 시 1회 호출"""
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                slack_user_id           TEXT PRIMARY KEY,
                encrypted_token         TEXT NOT NULL,
                contacts_folder_id      TEXT,
                knowledge_file_id       TEXT,
                minutes_folder_id       TEXT,
                briefing_hour           INTEGER DEFAULT 9,
                registered_at           TEXT,
                last_active             TEXT,
                dreamplus_email         TEXT,
                dreamplus_password_enc  TEXT
            )
        """)
        # 기존 DB에 컬럼이 없을 경우 추가 (마이그레이션)
        for col in ("minutes_folder_id TEXT",
                    "dreamplus_email TEXT",
                    "dreamplus_password_enc TEXT"):
            try:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col}")
            except Exception:
                pass  # 이미 존재하면 무시

        conn.execute("""
            CREATE TABLE IF NOT EXISTS action_items (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id    TEXT NOT NULL,
                user_id     TEXT NOT NULL,
                assignee    TEXT,
                content     TEXT NOT NULL,
                due_date    TEXT,
                status      TEXT DEFAULT 'open',
                created_at  TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_drafts (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id      TEXT NOT NULL,
                user_id       TEXT NOT NULL,
                title         TEXT,
                external_body TEXT NOT NULL,
                recipients    TEXT,
                status        TEXT DEFAULT 'pending',
                created_at    TEXT NOT NULL
            )
        """)


def is_registered(slack_user_id: str) -> bool:
    with _conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM users WHERE slack_user_id = ?", (slack_user_id,)
        ).fetchone()
    return row is not None


def register(slack_user_id: str, token_dict: dict):
    """OAuth 토큰을 암호화하여 신규 사용자 등록"""
    encrypted = _fernet().encrypt(json.dumps(token_dict).encode()).decode()
    now = datetime.now().isoformat()
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO users (slack_user_id, encrypted_token, registered_at, last_active)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(slack_user_id) DO UPDATE SET
                encrypted_token = excluded.encrypted_token,
                last_active = excluded.last_active
            """,
            (slack_user_id, encrypted, now, now),
        )


def get_credentials(slack_user_id: str) -> Credentials:
    """복호화된 Google Credentials 반환"""
    with _conn() as conn:
        row = conn.execute(
            "SELECT encrypted_token FROM users WHERE slack_user_id = ?",
            (slack_user_id,),
        ).fetchone()
    if not row:
        raise ValueError(f"등록되지 않은 사용자: {slack_user_id}")
    token_dict = json.loads(_fernet().decrypt(row["encrypted_token"].encode()))
    return Credentials.from_authorized_user_info(token_dict, SCOPES)


def get_user(slack_user_id: str) -> dict:
    """사용자 설정 정보 반환"""
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE slack_user_id = ?", (slack_user_id,)
        ).fetchone()
    if not row:
        raise ValueError(f"등록되지 않은 사용자: {slack_user_id}")
    return dict(row)


def update_drive_config(slack_user_id: str, contacts_folder_id: str,
                        knowledge_file_id: str, minutes_folder_id: str = None):
    """Drive 폴더 ID 업데이트 (최초 Drive 셋업 완료 후 호출)"""
    with _conn() as conn:
        conn.execute(
            """
            UPDATE users
            SET contacts_folder_id = ?, knowledge_file_id = ?, minutes_folder_id = ?
            WHERE slack_user_id = ?
            """,
            (contacts_folder_id, knowledge_file_id, minutes_folder_id, slack_user_id),
        )


def update_minutes_folder(slack_user_id: str, minutes_folder_id: str):
    """Minutes 폴더 ID 업데이트"""
    with _conn() as conn:
        conn.execute(
            "UPDATE users SET minutes_folder_id = ? WHERE slack_user_id = ?",
            (minutes_folder_id, slack_user_id),
        )


def update_last_active(slack_user_id: str):
    with _conn() as conn:
        conn.execute(
            "UPDATE users SET last_active = ? WHERE slack_user_id = ?",
            (datetime.now().isoformat(), slack_user_id),
        )


def all_users() -> list[dict]:
    """스케줄러용 전체 사용자 목록"""
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM users").fetchall()
    return [dict(r) for r in rows]


# ── Dreamplus 계정 ───────────────────────────────────────────

def save_dreamplus_credentials(slack_user_id: str, email: str, password: str) -> None:
    """Dreamplus 이메일 + 비밀번호(Fernet 암호화) 저장"""
    enc = _fernet().encrypt(password.encode()).decode()
    with _conn() as conn:
        conn.execute(
            """UPDATE users SET dreamplus_email = ?, dreamplus_password_enc = ?
               WHERE slack_user_id = ?""",
            (email, enc, slack_user_id),
        )


def get_dreamplus_credentials(slack_user_id: str) -> tuple[str, str] | None:
    """(email, password) 반환. 미등록 시 None."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT dreamplus_email, dreamplus_password_enc FROM users WHERE slack_user_id = ?",
            (slack_user_id,),
        ).fetchone()
    if not row or not row["dreamplus_email"] or not row["dreamplus_password_enc"]:
        return None
    password = _fernet().decrypt(row["dreamplus_password_enc"].encode()).decode()
    return row["dreamplus_email"], password


def has_dreamplus_credentials(slack_user_id: str) -> bool:
    with _conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM users WHERE slack_user_id = ? AND dreamplus_email IS NOT NULL",
            (slack_user_id,),
        ).fetchone()
    return row is not None


# ── Action Items ──────────────────────────────────────────────

def save_action_items(event_id: str, user_id: str, items: list[dict]) -> None:
    """액션아이템 목록을 DB에 저장"""
    now = datetime.now().isoformat()
    with _conn() as conn:
        for item in items:
            conn.execute(
                """INSERT INTO action_items
                   (event_id, user_id, assignee, content, due_date, status, created_at)
                   VALUES (?, ?, ?, ?, ?, 'open', ?)""",
                (event_id, user_id,
                 item.get("assignee"), item.get("content", ""),
                 item.get("due_date"), now),
            )


def get_action_items(event_id: str) -> list[dict]:
    """이벤트 ID로 액션아이템 목록 조회"""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM action_items WHERE event_id = ? ORDER BY id",
            (event_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_open_action_items_by_due(due_date: str) -> list[dict]:
    """특정 기한의 미완료 액션아이템 전체 조회 (리마인더용)"""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM action_items WHERE due_date = ? AND status = 'open' ORDER BY id",
            (due_date,),
        ).fetchall()
    return [dict(r) for r in rows]


def update_action_item_status(item_id: int, status: str) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE action_items SET status = ? WHERE id = ?",
            (status, item_id),
        )


# ── Pending Drafts ────────────────────────────────────────────

def save_pending_draft(event_id: str, user_id: str, title: str,
                       external_body: str, recipients: list[dict]) -> int:
    """외부 발송 Draft 저장. Returns: draft_id"""
    now = datetime.now().isoformat()
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO pending_drafts
               (event_id, user_id, title, external_body, recipients, status, created_at)
               VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
            (event_id, user_id, title,
             external_body, json.dumps(recipients, ensure_ascii=False), now),
        )
        return cur.lastrowid


def get_pending_draft(draft_id: int) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM pending_drafts WHERE id = ?", (draft_id,)
        ).fetchone()
    return dict(row) if row else None


def update_draft_status(draft_id: int, status: str) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE pending_drafts SET status = ? WHERE id = ?",
            (status, draft_id),
        )
