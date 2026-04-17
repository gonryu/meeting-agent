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
    # contacts.readonly: 신규 등록자부터 적용 (oauth.py에만 추가)
    # 기존 토큰 refresh 시 scope 불일치 방지를 위해 여기서는 제외
]


def _fernet() -> Fernet:
    key = os.getenv("ENCRYPTION_KEY")
    if not key:
        raise RuntimeError("ENCRYPTION_KEY 환경변수가 설정되지 않았습니다.")
    return Fernet(key.encode() if isinstance(key, str) else key)


@contextmanager
def _conn():
    conn = sqlite3.connect(_DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")  # INF-08: 동시성 안정화
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
                    "dreamplus_password_enc TEXT",
                    "dreamplus_jwt TEXT",
                    "dreamplus_jwt_exp TEXT",
                    "trello_token_enc TEXT"):
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

        conn.execute("""
            CREATE TABLE IF NOT EXISTS feedback (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     TEXT NOT NULL,
                category    TEXT NOT NULL,
                content     TEXT NOT NULL,
                original    TEXT NOT NULL,
                notified    INTEGER DEFAULT 0,
                created_at  TEXT NOT NULL
            )
        """)

        # INF-10: 회의록 검색 인덱스
        conn.execute("""
            CREATE TABLE IF NOT EXISTS meeting_index (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id      TEXT NOT NULL,
                user_id       TEXT NOT NULL,
                date          TEXT NOT NULL,
                title         TEXT NOT NULL,
                company_name  TEXT,
                attendees     TEXT,
                drive_file_id TEXT,
                drive_link    TEXT,
                has_proposal  INTEGER DEFAULT 0,
                created_at    TEXT DEFAULT (datetime('now'))
            )
        """)
        # 인덱스 생성 (IF NOT EXISTS로 중복 방지)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_meeting_company ON meeting_index(company_name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_meeting_date ON meeting_index(date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_meeting_user ON meeting_index(user_id)")


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


# ── Dreamplus 자격증명 ────────────────────────────────────────

def save_dreamplus_credentials(slack_user_id: str, email: str, password: str) -> None:
    """드림플러스 이메일·비밀번호를 Fernet 암호화하여 저장"""
    enc = _fernet().encrypt(password.encode()).decode()
    with _conn() as conn:
        conn.execute(
            """UPDATE users
               SET dreamplus_email = ?, dreamplus_password_enc = ?,
                   dreamplus_jwt = NULL, dreamplus_jwt_exp = NULL
               WHERE slack_user_id = ?""",
            (email, enc, slack_user_id),
        )


def get_dreamplus_credentials(slack_user_id: str) -> tuple[str, str] | None:
    """(email, password) 복호화 반환. 미설정 시 None."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT dreamplus_email, dreamplus_password_enc FROM users WHERE slack_user_id = ?",
            (slack_user_id,),
        ).fetchone()
    if not row or not row["dreamplus_email"] or not row["dreamplus_password_enc"]:
        return None
    email = row["dreamplus_email"]
    password = _fernet().decrypt(row["dreamplus_password_enc"].encode()).decode()
    return email, password


def get_dreamplus_jwt(slack_user_id: str) -> tuple[str, str, int, int] | None:
    """캐시된 (jwt, public_key, member_id, company_id) 반환. 없거나 만료면 None."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT dreamplus_jwt, dreamplus_jwt_exp FROM users WHERE slack_user_id = ?",
            (slack_user_id,),
        ).fetchone()
    if not row or not row["dreamplus_jwt"]:
        return None
    if row["dreamplus_jwt_exp"]:
        try:
            exp = datetime.fromisoformat(row["dreamplus_jwt_exp"])
            if datetime.now() >= exp:
                return None
        except Exception:
            return None
    stored = row["dreamplus_jwt"]
    parts = stored.split("|||")
    jwt = parts[0]
    pub_key = parts[1] if len(parts) > 1 else ""
    member_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    company_id = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
    return jwt, pub_key, member_id, company_id


def save_dreamplus_jwt(slack_user_id: str, jwt: str, public_key: str,
                       member_id: int = 0, company_id: int = 0,
                       exp_dt: datetime = None) -> None:
    """JWT, 공개키, memberId, companyId를 함께 캐시 저장. exp_dt 기본값 = 6시간 후."""
    from datetime import timedelta
    if exp_dt is None:
        exp_dt = datetime.now() + timedelta(minutes=30)
    stored = f"{jwt}|||{public_key}|||{member_id}|||{company_id}"
    with _conn() as conn:
        conn.execute(
            "UPDATE users SET dreamplus_jwt = ?, dreamplus_jwt_exp = ? WHERE slack_user_id = ?",
            (stored, exp_dt.isoformat(), slack_user_id),
        )


# ── Trello ────────────────────────────────────────────────────

def save_trello_token(slack_user_id: str, token: str) -> None:
    """Trello 사용자 토큰을 Fernet 암호화하여 저장"""
    enc = _fernet().encrypt(token.encode()).decode()
    with _conn() as conn:
        conn.execute(
            "UPDATE users SET trello_token_enc = ? WHERE slack_user_id = ?",
            (enc, slack_user_id),
        )


def get_trello_token(slack_user_id: str) -> str | None:
    """Trello 토큰 복호화 반환. 미설정 시 None."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT trello_token_enc FROM users WHERE slack_user_id = ?",
            (slack_user_id,),
        ).fetchone()
    if not row or not row["trello_token_enc"]:
        return None
    return _fernet().decrypt(row["trello_token_enc"].encode()).decode()


def clear_trello_token(slack_user_id: str) -> None:
    """Trello 연결 해제 (토큰 삭제)"""
    with _conn() as conn:
        conn.execute(
            "UPDATE users SET trello_token_enc = NULL WHERE slack_user_id = ?",
            (slack_user_id,),
        )


# ── Action Items ──────────────────────────────────────────────

def save_action_items(event_id: str, user_id: str, items: list[dict]) -> None:
    """액션아이템 목록을 DB에 저장"""
    now = datetime.now().isoformat()
    with _conn() as conn:
        conn.execute("DELETE FROM action_items WHERE event_id = ?", (event_id,))
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


# ── Feedback ─────────────────────────────────────────────────

def save_feedback(user_id: str, category: str, content: str, original: str) -> int:
    """사용자 피드백 저장. Returns: feedback_id"""
    now = datetime.now().isoformat()
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO feedback (user_id, category, content, original, notified, created_at)
               VALUES (?, ?, ?, ?, 0, ?)""",
            (user_id, category, content, original, now),
        )
        return cur.lastrowid


def get_pending_feedback() -> list[dict]:
    """아직 관리자에게 알림되지 않은 피드백 조회"""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM feedback WHERE notified = 0 ORDER BY created_at"
        ).fetchall()
    return [dict(r) for r in rows]


def mark_feedback_notified(feedback_ids: list[int]) -> None:
    """피드백 알림 완료 처리"""
    if not feedback_ids:
        return
    placeholders = ",".join("?" for _ in feedback_ids)
    with _conn() as conn:
        conn.execute(
            f"UPDATE feedback SET notified = 1 WHERE id IN ({placeholders})",
            feedback_ids,
        )


# ── 회의록 인덱스 (INF-10) ───────────────────────────────────


def save_meeting_index(*, event_id: str, user_id: str, date: str, title: str,
                       company_name: str = None, attendees: str = None,
                       drive_file_id: str = None, drive_link: str = None) -> None:
    """회의록 메타데이터를 인덱스에 저장"""
    with _conn() as conn:
        conn.execute(
            """INSERT INTO meeting_index
               (event_id, user_id, date, title, company_name, attendees, drive_file_id, drive_link)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (event_id, user_id, date, title, company_name, attendees,
             drive_file_id, drive_link),
        )


def search_meetings(*, user_id: str, company: str = None,
                     date_from: str = None, date_to: str = None,
                     limit: int = 20) -> list[dict]:
    """회의록 인덱스 검색 (업체명/기간 필터)"""
    query = "SELECT * FROM meeting_index WHERE user_id = ?"
    params: list = [user_id]

    if company:
        query += " AND company_name LIKE ?"
        params.append(f"%{company}%")
    if date_from:
        query += " AND date >= ?"
        params.append(date_from)
    if date_to:
        query += " AND date <= ?"
        params.append(date_to)

    query += " ORDER BY date DESC LIMIT ?"
    params.append(limit)

    with _conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def update_meeting_proposal_flag(event_id: str, user_id: str) -> None:
    """meeting_index의 has_proposal 플래그를 1로 갱신"""
    with _conn() as conn:
        conn.execute(
            "UPDATE meeting_index SET has_proposal = 1 WHERE event_id = ? AND user_id = ?",
            (event_id, user_id),
        )


# ── 관리자 페이지용 집계/조회 ────────────────────────────────

def admin_counts() -> dict:
    """관리자 대시보드 집계값"""
    with _conn() as conn:
        users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        meetings = conn.execute("SELECT COUNT(*) FROM meeting_index").fetchone()[0]
        feedback_total = conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
        feedback_pending = conn.execute(
            "SELECT COUNT(*) FROM feedback WHERE notified = 0"
        ).fetchone()[0]
        action_open = conn.execute(
            "SELECT COUNT(*) FROM action_items WHERE status = 'open'"
        ).fetchone()[0]
        action_total = conn.execute("SELECT COUNT(*) FROM action_items").fetchone()[0]
    return {
        "users": users,
        "meetings": meetings,
        "feedback_total": feedback_total,
        "feedback_pending": feedback_pending,
        "action_open": action_open,
        "action_total": action_total,
    }


def list_all_feedback(notified: int | None = None, limit: int = 200) -> list[dict]:
    """전체 피드백 조회 (관리자용). notified=None이면 전체."""
    query = "SELECT * FROM feedback"
    params: list = []
    if notified is not None:
        query += " WHERE notified = ?"
        params.append(notified)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with _conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]
