@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ========================================
echo   星之卡比桌面去背精靈 - 安裝與啟動
echo ========================================
echo.

if not exist ".venv\Scripts\pythonw.exe" (
    echo [1/3] 正在建立虛擬環境與安裝套件（首次需要 1~2 分鐘）...
    where uv >nul 2>nul
    if %errorlevel%==0 (
        uv venv --python 3.12 .venv
        uv pip install --python .venv\Scripts\python.exe -r requirements.txt
    ) else (
        where py >nul 2>nul
        if %errorlevel%==0 (
            py -3.12 -m venv .venv || py -3 -m venv .venv
        ) else (
            python -m venv .venv
        )
        .venv\Scripts\python.exe -m pip install --upgrade pip
        .venv\Scripts\python.exe -m pip install -r requirements.txt
    )
    if not exist ".venv\Scripts\pythonw.exe" (
        echo.
        echo 安裝失敗：請確認已安裝 Python 3.10~3.12 或 uv 後再試一次。
        pause
        exit /b 1
    )
)

echo [2/3] 正在建立桌面星之卡比捷徑...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$d = [Environment]::GetFolderPath('Desktop'); " ^
  "$s = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d '星之卡比去背.lnk')); " ^
  "$s.TargetPath = (Join-Path '%CD%' '.venv\Scripts\pythonw.exe'); " ^
  "$s.Arguments = '\"' + (Join-Path '%CD%' 'main.py') + '\"'; " ^
  "$s.WorkingDirectory = '%CD%'; " ^
  "$s.IconLocation = (Join-Path '%CD%' 'assets\kirby.ico') + ',0'; " ^
  "$s.Description = '星之卡比桌面去背精靈'; " ^
  "$s.Save()"

echo [3/3] 正在啟動星之卡比去背精靈...
start "" "%CD%\.venv\Scripts\pythonw.exe" "%CD%\main.py"
exit /b 0
