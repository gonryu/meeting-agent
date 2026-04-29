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
        # Phase 3: action_items 풍부화 컬럼 추가 (idempotent 마이그레이션)
        for col in ("priority TEXT",
                    "severity TEXT",
                    "owner_side TEXT",
                    "risk_score INTEGER",
                    "escalation_path TEXT",
                    "success_indicator TEXT",
                    "monitoring_cadence TEXT",
                    "next_check_date TEXT",
                    "dependencies TEXT",
                    "source_excerpt TEXT",
                    "secondary_risks TEXT"):
            try:
                conn.execute(f"ALTER TABLE action_items ADD COLUMN {col}")
            except Exception:
                pass  # 이미 존재하면 무시

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
                resolution  TEXT DEFAULT 'pending',
                created_at  TEXT NOT NULL
            )
        """)
        # 기존 DB에 resolution 컬럼이 없을 경우 추가
        try:
            conn.execute("ALTER TABLE feedback ADD COLUMN resolution TEXT DEFAULT 'pending'")
        except Exception:
            pass  # 이미 존재하면 무시

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

        # 개인 Todo (FR-T1~T7) — 활성 라이브 + 히스토리 분리
        conn.execute("""
            CREATE TABLE IF NOT EXISTS todos (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       TEXT NOT NULL,
                task          TEXT NOT NULL,
                category      TEXT NOT NULL DEFAULT 'work',
                due_date      TEXT,
                status        TEXT NOT NULL DEFAULT 'open',
                opened_at     TEXT NOT NULL,
                last_seen_at  TEXT NOT NULL,
                closed_at     TEXT,
                source        TEXT,
                note          TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_todos_user_status ON todos(user_id, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_todos_due ON todos(due_date)")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS todo_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                todo_id     INTEGER NOT NULL,
                user_id     TEXT NOT NULL,
                event       TEXT NOT NULL,
                payload     TEXT,
                occurred_at TEXT NOT NULL,
                FOREIGN KEY (todo_id) REFERENCES todos(id)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_todo_history_todo ON todo_history(todo_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_todo_history_user ON todo_history(user_id)")


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
    """액션아이템 목록을 DB에 저장.

    레거시 형식: {assignee, content, due_date}
    Phase 3 풍부화 형식: 위 3개 + priority/severity/owner_side/risk_score/
        escalation_path/success_indicator/monitoring_cadence/next_check_date/
        dependencies/source_excerpt/secondary_risks
    풍부화 필드는 없으면 NULL로 저장됩니다.
    """
    now = datetime.now().isoformat()
    with _conn() as conn:
        conn.execute("DELETE FROM action_items WHERE event_id = ?", (event_id,))
        for item in items:
            esc = item.get("escalation_path")
            esc_str = json.dumps(esc, ensure_ascii=False) if isinstance(esc, dict) else esc
            sec = item.get("secondary_risks")
            sec_str = json.dumps(sec, ensure_ascii=False) if isinstance(sec, list) else sec
            conn.execute(
                """INSERT INTO action_items
                   (event_id, user_id, assignee, content, due_date, status, created_at,
                    priority, severity, owner_side, risk_score, escalation_path,
                    success_indicator, monitoring_cadence, next_check_date,
                    dependencies, source_excerpt, secondary_risks)
                   VALUES (?, ?, ?, ?, ?, 'open', ?,
                           ?, ?, ?, ?, ?,
                           ?, ?, ?,
                           ?, ?, ?)""",
                (event_id, user_id,
                 item.get("assignee"), item.get("content", ""),
                 item.get("due_date"), now,
                 item.get("priority"), item.get("severity"),
                 item.get("owner_side"), item.get("risk_score"),
                 esc_str,
                 item.get("success_indicator"), item.get("monitoring_cadence"),
                 item.get("next_check_date"),
                 item.get("dependencies"), item.get("source_excerpt"),
                 sec_str),
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


def list_all_feedback(notified: int | None = None,
                      resolution: str | None = None,
                      limit: int = 200) -> list[dict]:
    """전체 피드백 조회 (관리자용). 인자 미지정 시 전체 반환."""
    query = "SELECT * FROM feedback"
    conditions: list[str] = []
    params: list = []
    if notified is not None:
        conditions.append("notified = ?")
        params.append(notified)
    if resolution is not None:
        conditions.append("resolution = ?")
        params.append(resolution)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with _conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def update_feedback_resolution(feedback_id: int, resolution: str) -> bool:
    """피드백 반영 상태 갱신. 존재하지 않으면 False."""
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE feedback SET resolution = ? WHERE id = ?",
            (resolution, feedback_id),
        )
        return cur.rowcount > 0


# ── Todo (할일) ──────────────────────────────────────────────
# FR-T1~T7: 개인 Todo 관리. user_id 단위 스코프, 히스토리는 append-only.

def add_todo(user_id: str, task: str, category: str = "work",
             due_date: str | None = None, source: str | None = None,
             note: str | None = None) -> int:
    """Todo 추가. Returns: todo_id"""
    now = datetime.now().isoformat()
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO todos
               (user_id, task, category, due_date, status,
                opened_at, last_seen_at, source, note)
               VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?)""",
            (user_id, task, category, due_date, now, now, source, note),
        )
        return cur.lastrowid


def list_active_todos(user_id: str) -> list[dict]:
    """활성(open) Todo 목록을 due_date ASC NULLS LAST, opened_at ASC 순으로 조회."""
    with _conn() as conn:
        rows = conn.execute(
            """SELECT * FROM todos
               WHERE user_id = ? AND status = 'open'
               ORDER BY
                   CASE WHEN due_date IS NULL OR due_date = '' THEN 1 ELSE 0 END,
                   due_date ASC,
                   opened_at ASC""",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def list_recent_completed(user_id: str, limit: int = 5) -> list[dict]:
    """최근 완료된 Todo n건 (closed_at DESC). status='done'만."""
    with _conn() as conn:
        rows = conn.execute(
            """SELECT * FROM todos
               WHERE user_id = ? AND status = 'done'
               ORDER BY closed_at DESC
               LIMIT ?""",
            (user_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_todo(user_id: str, todo_id: int) -> dict | None:
    """ID로 Todo 단건 조회. 다른 사용자의 Todo는 반환하지 않음."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM todos WHERE id = ? AND user_id = ?",
            (todo_id, user_id),
        ).fetchone()
    return dict(row) if row else None


def find_todo_by_text(user_id: str, text_match: str,
                      include_closed: bool = False) -> list[dict]:
    """제목 부분 일치로 Todo 검색. 기본은 활성(open)만."""
    pattern = f"%{text_match}%"
    if include_closed:
        query = ("SELECT * FROM todos WHERE user_id = ? AND task LIKE ? "
                 "ORDER BY status='open' DESC, opened_at DESC")
        params = (user_id, pattern)
    else:
        query = ("SELECT * FROM todos WHERE user_id = ? AND status = 'open' AND task LIKE ? "
                 "ORDER BY opened_at ASC")
        params = (user_id, pattern)
    with _conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def update_todo(todo_id: int, **kwargs) -> bool:
    """Todo 필드 갱신. 허용 필드: task, due_date, category, note. last_seen_at도 갱신."""
    allowed = {"task", "due_date", "category", "note"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return False
    fields["last_seen_at"] = datetime.now().isoformat()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    params = list(fields.values()) + [todo_id]
    with _conn() as conn:
        cur = conn.execute(
            f"UPDATE todos SET {set_clause} WHERE id = ?",
            params,
        )
        return cur.rowcount > 0


def close_todo(todo_id: int, status: str, note: str | None = None) -> bool:
    """Todo를 종료 상태(done | cancelled | deleted)로 전환."""
    if status not in ("done", "cancelled", "deleted"):
        raise ValueError(f"잘못된 status: {status}")
    now = datetime.now().isoformat()
    with _conn() as conn:
        if note is not None:
            cur = conn.execute(
                """UPDATE todos
                   SET status = ?, closed_at = ?, last_seen_at = ?, note = ?
                   WHERE id = ?""",
                (status, now, now, note, todo_id),
            )
        else:
            cur = conn.execute(
                """UPDATE todos
                   SET status = ?, closed_at = ?, last_seen_at = ?
                   WHERE id = ?""",
                (status, now, now, todo_id),
            )
        return cur.rowcount > 0


def log_todo_history(todo_id: int, user_id: str, event: str,
                     payload: dict | None = None) -> int:
    """Todo 히스토리 append. event: created|updated|completed|cancelled|deleted"""
    now = datetime.now().isoformat()
    payload_str = json.dumps(payload, ensure_ascii=False) if payload else None
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO todo_history (todo_id, user_id, event, payload, occurred_at)
               VALUES (?, ?, ?, ?, ?)""",
            (todo_id, user_id, event, payload_str, now),
        )
        return cur.lastrowid


def get_todo_history(user_id: str, limit: int = 100) -> list[dict]:
    """사용자의 Todo 히스토리 조회 (최신순)."""
    with _conn() as conn:
        rows = conn.execute(
            """SELECT * FROM todo_history
               WHERE user_id = ?
               ORDER BY occurred_at DESC
               LIMIT ?""",
            (user_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]
