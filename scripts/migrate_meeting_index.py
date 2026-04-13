#!/usr/bin/env python3
"""INF-11: 기존 회의록 파일을 meeting_index 테이블로 일괄 인덱싱하는 마이그레이션 스크립트.

사용법:
    python scripts/migrate_meeting_index.py

동작:
    1. DB의 모든 등록 사용자 조회
    2. 각 사용자의 Drive Minutes 폴더에서 회의록 파일 목록 조회
    3. 파일명에서 날짜·제목·업체명 추출
    4. meeting_index 테이블에 INSERT (중복 방지: event_id 기준)
"""
import json
import logging
import os
import re
import sys

# 프로젝트 루트를 path에 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(override=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

from store import user_store
from tools import drive


def _parse_minutes_filename(name: str) -> dict | None:
    """파일명에서 날짜, 제목, 종류를 파싱.
    예: '2026-04-10_카카오 미팅_내부용.md' → {date: '2026-04-10', title: '카카오 미팅', kind: '내부용'}
    """
    # 패턴: YYYY-MM-DD_제목_종류.md
    m = re.match(r'^(\d{4}-\d{2}-\d{2})_(.+?)_(내부용|외부용)\.md$', name)
    if m:
        return {"date": m.group(1), "title": m.group(2), "kind": m.group(3)}

    # 패턴: YYYY-MM-DD_제목.md (종류 없음)
    m = re.match(r'^(\d{4}-\d{2}-\d{2})_(.+?)\.md$', name)
    if m:
        return {"date": m.group(1), "title": m.group(2), "kind": None}

    return None


def migrate_user(user_id: str) -> int:
    """단일 사용자의 회의록 마이그레이션. 인덱싱된 파일 수 반환."""
    creds = user_store.get_credentials(user_id)
    if not creds:
        log.warning(f"[{user_id}] 인증 정보 없음 — 건너뜀")
        return 0

    user = user_store.get_user(user_id)
    minutes_folder_id = user.get("minutes_folder_id") if user else None
    if not minutes_folder_id:
        log.warning(f"[{user_id}] minutes_folder_id 없음 — 건너뜀")
        return 0

    # Drive에서 회의록 파일 목록 조회
    try:
        files = drive.list_minutes(creds, minutes_folder_id)
    except Exception as e:
        log.error(f"[{user_id}] Drive 파일 목록 조회 실패: {e}")
        return 0

    count = 0
    for f in files:
        name = f.get("name", "")
        file_id = f.get("id", "")

        parsed = _parse_minutes_filename(name)
        if not parsed:
            log.info(f"  파싱 불가 (건너뜀): {name}")
            continue

        # 내부용만 인덱싱 (외부용은 같은 미팅의 중복이므로)
        if parsed["kind"] == "외부용":
            continue

        event_id = f"migrated_{file_id}"
        title = parsed["title"]
        date_str = parsed["date"]

        # 중복 확인
        existing = user_store.search_meetings(
            user_id=user_id,
            company=None,
            date_from=date_str,
            date_to=date_str,
        )
        if any(r.get("title") == title for r in existing):
            log.info(f"  이미 인덱싱됨 (건너뜀): {name}")
            continue

        try:
            user_store.save_meeting_index(
                event_id=event_id,
                user_id=user_id,
                date=date_str,
                title=title,
                company_name=None,  # 파일명에서 업체명 추론은 추후 LLM으로 가능
                attendees=None,
                drive_file_id=file_id,
                drive_link=f"https://drive.google.com/file/d/{file_id}/view",
            )
            count += 1
            log.info(f"  인덱싱: {name}")
        except Exception as e:
            log.error(f"  인덱싱 실패: {name} — {e}")

    return count


def main():
    user_store.init_db()
    users = user_store.all_users()
    total = 0

    for row in users:
        user_id = row["slack_user_id"]
        log.info(f"=== 사용자 {user_id} 마이그레이션 시작 ===")
        count = migrate_user(user_id)
        total += count
        log.info(f"=== 사용자 {user_id}: {count}건 인덱싱 완료 ===")

    log.info(f"\n마이그레이션 완료: 총 {total}건 인덱싱")


if __name__ == "__main__":
    main()
