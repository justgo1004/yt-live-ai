@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\activate.bat" (
  echo 먼저 setup.bat 을 실행하세요.
  pause
  exit /b 1
)
call ".venv\Scripts\activate.bat"

echo ============== 시험 ==============
echo 1) 데모 - 샘플 대사로 리액션 시험
echo 2) 라이브 시험 - 채팅은 보내지 않고 화면에만 표시
echo 3) 점검 - 설정과 설치 상태 확인
echo ============== 실전 ==============
echo 4) 유튜브 로그인 (처음 한 번만)
echo 5) 채팅 1건 전송 테스트
echo 6) 라이브 실전 - 리액션을 채팅창에 실제로 전송
echo.
set "MODE="
set /p MODE=번호를 입력하고 엔터: 
if "%MODE%"=="1" goto :demo
if "%MODE%"=="2" goto :live
if "%MODE%"=="3" goto :check
if "%MODE%"=="4" goto :auth
if "%MODE%"=="5" goto :sendtest
if "%MODE%"=="6" goto :real
echo 잘못된 번호입니다.
goto :end

:demo
python reaction.py --demo
goto :end

:live
call :askurl
if "%URL%"=="" goto :end
echo 끝내려면 Ctrl+C 를 누르세요.
python main.py "%URL%" --chat console
goto :end

:check
python main.py --check
goto :end

:auth
if not exist "client_secret.json" (
  echo client_secret.json 이 이 폴더에 없습니다.
  echo Google Cloud 에서 받은 OAuth 파일을 이 폴더에 client_secret.json 이름으로 넣으세요.
  goto :end
)
echo 브라우저가 열리면 채팅을 보낼 구글 계정으로 로그인하고 허용을 누르세요.
python chat_sender.py auth
goto :end

:sendtest
if not exist "token.json" goto :needauth
call :askurl
if "%URL%"=="" goto :end
python chat_sender.py test "%URL%" "테스트 메시지입니다"
goto :end

:real
if not exist "token.json" goto :needauth
call :askurl
if "%URL%"=="" goto :end
echo.
echo [실전] 리액션이 실제 채팅창에 전송됩니다. (AI 표기 포함, 45~90초 간격, 방송당 최대 20건)
echo 끝내려면 Ctrl+C 를 누르세요.
python main.py "%URL%" --chat youtube-api -o "chatlog_%DATE:~0,4%%DATE:~5,2%%DATE:~8,2%.txt"
goto :end

:needauth
echo 아직 유튜브 로그인을 하지 않았습니다. 먼저 4번을 실행하세요.
goto :end

:askurl
set "URL="
set /p URL=유튜브 라이브 URL 을 붙여넣고 엔터: 
exit /b 0

:end
echo.
pause
