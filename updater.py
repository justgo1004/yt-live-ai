"""
자동 업데이트 — GitHub 릴리스에서 새 exe 를 받아 교체한다.

    1) check()    : api.github.com/repos/<저장소>/releases/latest → 버전 비교
    2) download() : YouTubeLiveAI.exe + .sha256 내려받기 → SHA-256 검증 (깨진 파일 차단)
    3) apply()    : 교체 스크립트를 띄우고 앱 종료
                    → 스크립트가 앱이 완전히 꺼질 때까지 기다린 뒤 exe 교체 → 새 exe 실행
                    → 교체 실패 시 이전 exe 로 되돌림 (update\\YouTubeLiveAI.old.exe 에 1개 보관)

설정·API 키·로그인 정보(.env, token.json 등)는 exe 와 별도 파일이라 그대로 유지된다.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import httpx

from version import EXE_NAME, UPDATE_REPO, __version__

API = f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest"
ALLOWED_HOSTS = ("github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com")


def parse_version(v: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums[:3]) or (0,)


@dataclass
class UpdateInfo:
    version: str
    notes: str
    exe_url: str
    sha_url: Optional[str]
    size: int
    page_url: str

    @property
    def newer(self) -> bool:
        return parse_version(self.version) > parse_version(__version__)


class UpdateError(Exception):
    pass


def check(timeout: float = 10.0) -> Optional[UpdateInfo]:
    """최신 릴리스 정보. 릴리스가 아직 없으면 None."""
    try:
        r = httpx.get(API, timeout=timeout, follow_redirects=True,
                      headers={"Accept": "application/vnd.github+json", "User-Agent": f"YouTubeLiveAI/{__version__}"})
    except httpx.HTTPError as e:
        raise UpdateError(f"업데이트 서버에 연결하지 못했습니다 ({type(e).__name__})") from e
    if r.status_code == 404:
        return None
    if r.status_code == 403 and "rate limit" in r.text.lower():
        raise UpdateError("업데이트 확인 횟수 제한에 걸렸습니다. 잠시 후 다시 시도하세요.")
    if not r.is_success:
        raise UpdateError(f"업데이트 확인 실패 (HTTP {r.status_code})")
    rel = r.json()
    assets = {a["name"]: a for a in rel.get("assets", [])}
    exe = assets.get(EXE_NAME)
    if not exe:
        return None                                   # 빌드가 아직 끝나지 않은 릴리스
    sha = assets.get(EXE_NAME + ".sha256")
    return UpdateInfo(
        version=str(rel.get("tag_name", "")).lstrip("v"),
        notes=(rel.get("body") or "").strip()[:2000],
        exe_url=exe["browser_download_url"], sha_url=sha["browser_download_url"] if sha else None,
        size=int(exe.get("size") or 0), page_url=rel.get("html_url", ""),
    )


def _check_host(url: str) -> None:
    host = httpx.URL(url).host
    if not any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS):
        raise UpdateError(f"허용되지 않은 다운로드 주소입니다: {host}")


def download(info: UpdateInfo, dest_dir: Path, progress: Callable[[float], None] = lambda p: None) -> Path:
    """새 exe 를 dest_dir/YouTubeLiveAI.new.exe 로 받고 SHA-256 을 검증한다."""
    _check_host(info.exe_url)
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / "YouTubeLiveAI.new.exe"
    part = out.with_suffix(".part")
    h = hashlib.sha256()
    got = 0
    headers = {"User-Agent": f"YouTubeLiveAI/{__version__}"}
    with httpx.stream("GET", info.exe_url, follow_redirects=True, timeout=60, headers=headers) as r:
        if not r.is_success:
            raise UpdateError(f"내려받기 실패 (HTTP {r.status_code})")
        total = int(r.headers.get("content-length") or info.size or 0)
        with part.open("wb") as f:
            for chunk in r.iter_bytes(1 << 16):
                f.write(chunk)
                h.update(chunk)
                got += len(chunk)
                if total:
                    progress(min(1.0, got / total))
    if info.size and got != info.size:
        part.unlink(missing_ok=True)
        raise UpdateError("내려받은 파일 크기가 맞지 않습니다. 다시 시도하세요.")
    if info.sha_url:
        _check_host(info.sha_url)
        expected = httpx.get(info.sha_url, follow_redirects=True, timeout=20, headers=headers).text.split()[0].lower()
        if h.hexdigest().lower() != expected:
            part.unlink(missing_ok=True)
            raise UpdateError("파일 검증(SHA-256)에 실패했습니다. 다시 시도하세요.")
    part.replace(out)
    return out


APPLY_BAT = r"""@echo off
chcp 65001 >nul
cd /d "%~dp0.."
rem 앱(파이썬 프로세스와 실행기)이 완전히 꺼질 때까지 대기 (창 없는 환경에서는 timeout 대신 ping 으로 1초 대기)
:wait
tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul
if not errorlevel 1 (ping -n 2 127.0.0.1 >nul & goto wait)
set /a tries=0
:swap
set /a tries+=1
if exist "update\YouTubeLiveAI.old.exe" del /f /q "update\YouTubeLiveAI.old.exe" >nul 2>nul
move /y "{exe}" "update\YouTubeLiveAI.old.exe" >nul 2>nul
if errorlevel 1 (
  if %tries% lss 20 (ping -n 2 127.0.0.1 >nul & goto swap)
  goto launch
)
move /y "update\YouTubeLiveAI.new.exe" "{exe}" >nul 2>nul
if errorlevel 1 move /y "update\YouTubeLiveAI.old.exe" "{exe}" >nul 2>nul
:launch
rem 이전 실행의 PyInstaller 환경 변수를 지우고 새로 시작 (남아 있으면 지워진 임시 폴더를 찾다가 실패)
for /f "delims==" %%v in ('set _PYI_ 2^>nul') do set "%%v="
set "_MEIPASS2="
set "PYINSTALLER_RESET_ENVIRONMENT=1"
start "" "{exe}" --after-update
del "%~f0"
"""


def apply(new_exe: Path, base: Path) -> None:
    """교체 스크립트를 실행한다. 호출한 쪽은 곧바로 앱을 종료해야 한다. (exe 로 실행 중일 때만)"""
    if not getattr(sys, "frozen", False) or os.name != "nt":
        raise UpdateError("자동 교체는 윈도우 exe 에서만 동작합니다.")
    exe = Path(sys.executable).name
    bat = base / "update" / "apply_update.bat"
    bat.write_text(APPLY_BAT.replace("{pid}", str(os.getpid())).replace("{exe}", exe), encoding="utf-8")
    # 숨김 콘솔 + 별도 프로세스 그룹: 앱이 꺼져도 계속 실행되고, 하위 명령(tasklist 등) 창도 뜨지 않음
    flags = 0x00000200 | 0x08000000   # CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    # PyInstaller 단일 exe 는 임시 폴더(_MEI…) 위치를 환경 변수로 자식에게 넘긴다. 그대로 물려주면
    # 새 exe 가 '이미 지워진 이전 임시 폴더'를 찾다가 "Failed to load Python DLL" 로 실패하므로 정리한다.
    env = {k: v for k, v in os.environ.items() if not k.startswith("_PYI_") and k != "_MEIPASS2"}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    subprocess.Popen(["cmd", "/c", str(bat)], cwd=str(base), creationflags=flags, close_fds=True, env=env)
