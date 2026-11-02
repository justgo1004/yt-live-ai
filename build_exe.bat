@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo ================================================
echo   라이브 AI 리액션 - 무설치 실행 파일 만들기
echo   (처음 한 번만 실행하면 됩니다. 5~10분 정도 걸립니다)
echo ================================================

echo.
echo [1/5] Python 가상환경 준비
if exist ".venv\Scripts\python.exe" goto :venv_ok
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (where python >nul 2>nul && set "PY=python")
if not defined PY goto :nopython
%PY% -m venv .venv
if errorlevel 1 goto :fail
:venv_ok
call ".venv\Scripts\activate.bat"

echo.
echo [2/5] 필요한 패키지 설치
python -m pip install -U pip >nul
python -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto :fail

echo.
echo [3/5] exe 만들기 (가장 오래 걸립니다)
if exist "dist\YouTubeLiveAI.exe" del /q "dist\YouTubeLiveAI.exe"
pyinstaller --noconfirm --clean --onefile --windowed --name YouTubeLiveAI ^
  --add-data "ui;ui" --collect-submodules deepgram ^
  --hidden-import main --hidden-import live_stt --hidden-import reaction --hidden-import chat_sender --hidden-import commands --hidden-import updater --hidden-import version ^
  app.py
if not exist "dist\YouTubeLiveAI.exe" goto :fail

echo.
echo [4/5] ffmpeg.exe 준비
if not exist "build" mkdir build
if exist "build\ffmpeg.exe" goto :ffmpeg_ok
echo ffmpeg 를 내려받는 중... (약 100MB)
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; try { Invoke-WebRequest 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' -OutFile 'build\ffmpeg.zip'; Expand-Archive 'build\ffmpeg.zip' 'build\ffmpeg' -Force; Get-ChildItem 'build\ffmpeg' -Recurse -Filter ffmpeg.exe | Select-Object -First 1 | Copy-Item -Destination 'build\ffmpeg.exe' } catch { Write-Host $_ }"
if exist "build\ffmpeg.exe" goto :ffmpeg_ok
echo 내려받기에 실패해서 이 PC 에 설치된 ffmpeg 를 복사합니다...
for /f "delims=" %%p in ('where ffmpeg 2^>nul') do if not exist "build\ffmpeg.exe" copy "%%p" "build\ffmpeg.exe" >nul
if not exist "build\ffmpeg.exe" goto :noffmpeg
:ffmpeg_ok

echo.
echo [5/5] 실행 폴더 만들기
set "OUT=release\YouTubeLiveAI"
if exist "release" rmdir /s /q "release"
mkdir "%OUT%"
copy "dist\YouTubeLiveAI.exe" "%OUT%\" >nul
copy "build\ffmpeg.exe" "%OUT%\" >nul
rem 다른 사람에게 줄 때 쓰는 배포용 압축 (API 키·로그인 정보 없음)
powershell -NoProfile -Command "Compress-Archive -Path '%OUT%\YouTubeLiveAI.exe','%OUT%\ffmpeg.exe' -DestinationPath 'release\YouTubeLiveAI_배포용.zip' -Force"
rem 내 설정 옮기기 (API 키, 유튜브 로그인)
if exist ".env" copy ".env" "%OUT%\.env" >nul
if exist "client_secret.json" copy "client_secret.json" "%OUT%\" >nul
if exist "token.json" copy "token.json" "%OUT%\" >nul
if exist ".chat_state.json" copy ".chat_state.json" "%OUT%\" >nul
if exist "commands.json" copy "commands.json" "%OUT%\" >nul

echo.
echo ================================================
echo   완료!  release\YouTubeLiveAI 폴더가 열립니다.
echo   YouTubeLiveAI.exe 를 더블클릭해서 실행하세요.
echo   - 이 폴더에는 내 API 키가 들어 있으니 다른 사람에게 주지 마세요.
echo   - 다른 사람에게는 release\YouTubeLiveAI_배포용.zip 을 주세요.
echo ================================================
explorer "%OUT%"
pause
exit /b 0

:nopython
echo Python 이 없습니다. https://www.python.org/downloads/ 에서 설치하세요.
echo 설치 첫 화면에서 "Add python.exe to PATH" 를 꼭 체크하세요.
pause
exit /b 1

:noffmpeg
echo ffmpeg.exe 를 준비하지 못했습니다.
echo https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip 을 직접 받아
echo 압축 안의 bin\ffmpeg.exe 를 이 폴더의 build 폴더에 넣고 다시 실행하세요.
pause
exit /b 1

:fail
echo.
echo 오류가 났습니다. 위의 오류 메시지를 복사해서 Claude 에게 보내 주세요.
pause
exit /b 1
