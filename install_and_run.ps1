$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

$ProjectDir = $PSScriptRoot
Set-Location -LiteralPath $ProjectDir

Write-Host "========================================" -ForegroundColor Magenta
Write-Host "  星之卡比桌面去背精靈 - 安裝與啟動" -ForegroundColor Magenta
Write-Host "========================================" -ForegroundColor Magenta
Write-Host ""

$VenvDir    = Join-Path $ProjectDir ".venv"
$PythonExe  = Join-Path $VenvDir "Scripts\python.exe"
$PythonWExe = Join-Path $VenvDir "Scripts\pythonw.exe"
$MainPy     = Join-Path $ProjectDir "main.py"
$ReqTxt     = Join-Path $ProjectDir "requirements.txt"
$IconPath   = Join-Path $ProjectDir "assets\kirby.ico"

function Find-Uv {
    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $candidates = @(
        (Join-Path $env:USERPROFILE ".local\bin\uv.exe"),
        (Join-Path $env:LOCALAPPDATA "uv\bin\uv.exe"),
        (Join-Path $env:USERPROFILE ".cargo\bin\uv.exe")
    )
    foreach ($c in $candidates) {
        if (Test-Path -LiteralPath $c) {
            $binDir = Split-Path -Parent $c
            if ($env:Path -notlike "*$binDir*") {
                $env:Path = "$binDir;$env:Path"
            }
            return $c
        }
    }
    return $null
}

function Test-VenvReady {
    if (-not (Test-Path -LiteralPath $PythonWExe)) { return $false }
    if (-not (Test-Path -LiteralPath $PythonExe))  { return $false }
    try {
        & $PythonExe -c "import PySide6, rembg, PIL, numpy" 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

if (-not (Test-VenvReady)) {
    Write-Host "[1/3] 正在準備 Python 3.12 環境與安裝套件（首次需要 1~2 分鐘）..." -ForegroundColor Cyan
    $uvExe = Find-Uv
    if (-not $uvExe) {
        Write-Host "      未偵測到 uv，正在自動安裝 uv..." -ForegroundColor Yellow
        try {
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            Invoke-RestMethod -Uri "https://astral.sh/uv/install.ps1" | Invoke-Expression
            $uvExe = Find-Uv
        } catch {
            Write-Host "      自動安裝 uv 失敗，改為嘗試本機 Python 3.10~3.12..." -ForegroundColor Yellow
        }
    }

    if ($uvExe) {
        Write-Host "      使用 uv 建立 Python 3.12 虛擬環境..." -ForegroundColor Gray
        & $uvExe venv --python 3.12 --clear $VenvDir
        if ($LASTEXITCODE -ne 0) {
            throw "建立虛擬環境失敗（uv venv 退出碼：$LASTEXITCODE）。請確認網路連線正常後再試一次。"
        }
        Write-Host "      正在安裝相依套件（PySide6、rembg、Pillow、numpy）..." -ForegroundColor Gray
        & $uvExe pip install --python $PythonExe -r $ReqTxt
        if ($LASTEXITCODE -ne 0) {
            throw "安裝套件失敗（uv pip install 退出碼：$LASTEXITCODE）。請確認網路連線正常後再試一次。"
        }
    } else {
        # Fallback: strictly use Python 3.12 / 3.11 / 3.10 (onnxruntime does not support 3.15+)
        $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
        $created = $false
        if ($pyLauncher) {
            foreach ($ver in @("-3.12", "-3.11", "-3.10")) {
                & py $ver -m venv $VenvDir 2>$null
                if ($LASTEXITCODE -eq 0 -and (Test-Path -LiteralPath $PythonExe)) {
                    $created = $true
                    break
                }
            }
        }
        if (-not $created) {
            throw "找不到相容的 Python 3.10~3.12 或 uv（注意：Python 3.15 尚未支援 onnxruntime）。請連上網路重新執行以自動安裝 uv，或手動安裝 Python 3.12。"
        }
        & $PythonExe -m pip install --upgrade pip
        & $PythonExe -m pip install -r $ReqTxt
        if ($LASTEXITCODE -ne 0) {
            throw "pip 安裝套件失敗（退出碼：$LASTEXITCODE）。"
        }
    }

    if (-not (Test-Path -LiteralPath $PythonWExe)) {
        throw "安裝後仍找不到 .venv\Scripts\pythonw.exe，請重新執行一次。"
    }
} else {
    Write-Host "[1/3] 虛擬環境已就緒，跳過安裝。" -ForegroundColor Green
}

Write-Host "[2/3] 正在建立星之卡比捷徑（桌面與本資料夾）..." -ForegroundColor Cyan
$wsh = New-Object -ComObject WScript.Shell
$shortcutTargets = @(
    (Join-Path ([Environment]::GetFolderPath('Desktop')) "星之卡比去背.lnk"),
    (Join-Path $ProjectDir "星之卡比去背.lnk")
)
foreach ($lnkPath in $shortcutTargets) {
    $sc = $wsh.CreateShortcut($lnkPath)
    $sc.TargetPath       = $PythonWExe
    $sc.Arguments        = '"' + $MainPy + '"'
    $sc.WorkingDirectory = $ProjectDir
    $sc.IconLocation     = "$IconPath,0"
    $sc.Description      = "星之卡比桌面去背精靈（把圖片丟進卡比嘴巴即可去背）"
    $sc.Save()
}

Write-Host "[3/3] 正在啟動星之卡比去背精靈..." -ForegroundColor Green
Start-Process -FilePath $PythonWExe -ArgumentList ('"' + $MainPy + '"') -WorkingDirectory $ProjectDir
Write-Host "啟動完成！" -ForegroundColor Magenta