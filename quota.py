"""
유튜브 API 할당량(하루 사용량) 측정 — Google Cloud 프로젝트(= OAuth 클라이언트)별로 센다.

같은 client_secret.json 을 쓰는 봇들은 하루 10,000 단위를 함께 쓴다. 봇마다 프로젝트를 따로 만들면 각자 10,000.
비용(단위)은 YouTube Data API 공식 표 기준의 추정치다. 실제 사용량은 Google Cloud 콘솔이 기준.
날짜는 할당량이 초기화되는 미국 태평양 시간 자정 기준.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

DAILY_LIMIT = 10_000
COST = {"insert": 50, "list_chat": 5, "videos": 1, "channels": 1}


def _pt_day() -> str:
    # 태평양 표준시(UTC-8) 기준 날짜 — 서머타임 1시간 차이는 무시해도 무방
    return (datetime.now(timezone.utc) - timedelta(hours=8)).date().isoformat()


def project_key(client_secret_path: str | Path) -> str:
    """client_secret.json → 프로젝트 식별자(client_id 앞부분 숫자). 읽을 수 없으면 파일 경로."""
    try:
        data = json.loads(Path(client_secret_path).read_text(encoding="utf-8"))
        cid = (data.get("installed") or data.get("web") or {}).get("client_id", "")
        return cid.split("-")[0] or str(client_secret_path)
    except (OSError, ValueError):
        return str(client_secret_path)


class QuotaMeter:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.day = _pt_day()
        self.used: dict[str, int] = {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("day") == self.day:
                self.used = {k: int(v) for k, v in data.get("used", {}).items()}
        except (OSError, ValueError):
            pass

    def _roll(self) -> None:
        d = _pt_day()
        if d != self.day:
            self.day, self.used = d, {}

    def add(self, project: str, units: int) -> None:
        with self._lock:
            self._roll()
            self.used[project] = self.used.get(project, 0) + units
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"day": self.day, "used": self.used}), encoding="utf-8")
            tmp.replace(self.path)

    def get(self, project: str) -> int:
        with self._lock:
            self._roll()
            return self.used.get(project, 0)

    def ratio(self, project: str) -> float:
        return self.get(project) / DAILY_LIMIT


_default: Optional[QuotaMeter] = None


def meter(path: str | Path = ".quota.json") -> QuotaMeter:
    """프로세스 전체에서 하나의 측정기를 공유."""
    global _default
    if _default is None or _default.path != Path(path):
        _default = QuotaMeter(path)
    return _default
