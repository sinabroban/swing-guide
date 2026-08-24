@echo off
rem ============================================================
rem SwingGuide 일간 배치 실행 스크립트
rem - 프로젝트 루트의 app.py 실행 -> data.json 갱신
rem - 성공/실패 로그를 batch\log.txt 에 누적 기록
rem - 작업 스케줄러 등록 예시(관리자 권한 cmd):
rem   schtasks /create /tn "SwingGuide Daily" /tr "C:\Users\SEHO6\sige\batch\run_daily.bat" /sc daily /st 18:00
rem ============================================================
setlocal
chcp 65001 >nul
cd /d "%~dp0.."

set "LOGFILE=%~dp0log.txt"
set "PYTHON_CMD="

where python >nul 2>nul && set "PYTHON_CMD=python"
if not defined PYTHON_CMD where py >nul 2>nul && set "PYTHON_CMD=py"
if not defined PYTHON_CMD (
    echo [%date% %time%] [FAIL] Python not found in PATH. >> "%LOGFILE%"
    exit /b 1
)

echo [%date% %time%] ========== RUN START ==========>> "%LOGFILE%"
%PYTHON_CMD% -u app.py >> "%LOGFILE%" 2>&1

if errorlevel 1 (
    echo [%date% %time%] [FAIL] app.py exited with error. >> "%LOGFILE%"
    endlocal
    exit /b 1
)

if not exist "data.json" (
    echo [%date% %time%] [FAIL] data.json not created. >> "%LOGFILE%"
    endlocal
    exit /b 1
)

echo [%date% %time%] [OK] data.json updated successfully. >> "%LOGFILE%"
endlocal
exit /b 0
