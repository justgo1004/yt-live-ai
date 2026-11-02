@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo ================================================
echo   YouTube Live AI 설치 (Windows)
echo ================================================

echo.
echo [1/5] Python 확인
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (where python >nul 2>nul && set "PY=python")
if not defined PY goto :nopython
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 goto :oldpython
%PY% --version

echo.
echo [2/5] ffmpeg 확인
where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo ffmpeg 가 없어서 winget 으로 설치합니다...
  winget install -e --id Gyan.FFmpeg --accept-package-agreements --accept-source-agreements
  echo.
  echo ffmpeg 설치가 끝났습니다. 이 창을 닫고 setup.bat 을 한 번 더 실행하세요.
  pause
  exit /b 0
)
echo ffmpeg OK

echo.
echo [3/5] 가상환경 만들기 (.venv)
if not exist ".venv\Scripts\python.exe" %PY% -m venv .venv
if errorlevel 1 goto :fail
call ".venv\Scripts\activate.bat"

echo.
echo [4/5] 패키지 설치 (몇 분 걸릴 수 있습니다)
python -m pip install -U pip
python -m pip install -r requirements.txt
if errorlevel 1 goto :fail

echo.
echo [5/5] 설정 파일 (.env)
if not exist ".env" copy ".env.example" ".env" >nul
echo 메모장이 열리면 GROQ_API_KEY= 와 ANTHROPIC_API_KEY= 뒤에 키를 붙여넣고 저장한 뒤 닫으세요.
notepad ".env"

echo.
echo ===== 점검 결과 =====
python main.py --check
echo.
echo 설치 완료. 실행은 run.bat 을 더블클릭하세요.
pause
exit /b 0

:nopython
echo Python 이 없습니다. https://www.python.org/downloads/ 에서 설치하세요.
echo 설치 화면 첫 페이지에서 "Add python.exe to PATH" 를 꼭 체크하세요.
pause
exit /b 1

:oldpython
echo Python 3.10 이상이 필요합니다. https://www.python.org/downloads/ 에서 최신 버전을 설치하세요.
pause
exit /b 1

:fail
echo.
echo 오류가 났습니다. 위의 빨간 오류 메시지를 복사해서 Claude 에게 보내 주세요.
pause
exit /b 1
