# 一键启动：Ollama → NapCat（需要时弹出二维码）→ 主程序 → 控制台，全部在后台运行。
# 用法：双击项目根目录的 start.bat，或 powershell -ExecutionPolicy Bypass -File scripts\ops\start.ps1 [-NoBrowser]
param([switch]$NoBrowser)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$run = Join-Path $root 'data\run'
$logs = Join-Path $root 'data\logs'
New-Item -ItemType Directory -Force -Path $run, $logs | Out-Null

function Step([string]$text) { Write-Host ''; Write-Host "▸ $text" -ForegroundColor Magenta }
function Ok([string]$text) { Write-Host "  ✓ $text" -ForegroundColor Green }
function Info([string]$text) { Write-Host "  · $text" -ForegroundColor Gray }
function Warn([string]$text) { Write-Host "  ! $text" -ForegroundColor Yellow }

function Read-DotEnv {
    $values = @{}
    $path = Join-Path $root '.env'
    if (Test-Path $path) {
        foreach ($line in Get-Content -Path $path -Encoding UTF8) {
            if ($line -match '^\s*([A-Z0-9_]+)\s*=\s*(.*)$') { $values[$Matches[1]] = $Matches[2].Trim() }
        }
    }
    return $values
}

function Get-ListenerName([int]$port) {
    $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $conn) { return $null }
    $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
    if ($proc) { return $proc.ProcessName } else { return '未知进程' }
}

function Test-Alive([string]$pidFile) {
    if (-not (Test-Path $pidFile)) { return $false }
    $id = (Get-Content -Path $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    return [bool]($id -and (Get-Process -Id $id -ErrorAction SilentlyContinue))
}

$envs = Read-DotEnv
Write-Host "白璎 · 一键启动" -ForegroundColor Magenta
Remove-Item -Path (Join-Path $run '*.stop') -ErrorAction SilentlyContinue

# ---------- 1. 依赖同步 ----------
Step '检查依赖'
$running = (Test-Alive (Join-Path $run 'qq.watchdog.pid')) -or (Test-Alive (Join-Path $run 'web.watchdog.pid'))
if (-not $running -and (Get-Command uv -ErrorAction SilentlyContinue)) {
    Push-Location $root
    try { & uv sync --quiet; Ok '依赖已是最新' } catch { Warn "uv sync 失败：$_" } finally { Pop-Location }
} else {
    Info '服务已在运行，跳过依赖同步'
}

# ---------- 2. Ollama ----------
Step 'Ollama（记忆检索用的本地向量模型）'
if (Get-Process -Name 'ollama' -ErrorAction SilentlyContinue) {
    Ok '已在运行'
} else {
    $app = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama app.exe'
    if (Test-Path $app) { Start-Process -FilePath $app } else { Start-Process -FilePath 'ollama' -ArgumentList 'serve' -WindowStyle Hidden }
    Info '已启动，等待就绪……'
}
$ready = $false
for ($i = 0; $i -lt 60; $i++) {
    try { Invoke-RestMethod -Uri 'http://localhost:11434/api/tags' -TimeoutSec 2 | Out-Null; $ready = $true; break } catch { Start-Sleep -Seconds 1 }
}
if ($ready) { Ok 'Ollama 就绪' } else { Warn 'Ollama 60 秒内没有就绪（记忆检索会退化为关键词检索）' }

# ---------- 3. NapCat ----------
Step 'NapCat（QQ 协议端）'
$napDir = $envs['NAPCAT_DIR']
if (-not $napDir) { $napDir = 'E:\07\NapCat.Shell.Windows.OneKey\NapCat.52230.Shell' }
$account = $envs['QQ_BOT_ACCOUNT']
$wsUrl = $envs['QQ_WS_URL']
if (-not $wsUrl) { $wsUrl = 'ws://127.0.0.1:3001' }
$port = ([uri]$wsUrl).Port
$napExe = Join-Path $napDir 'NapCatWinBootMain.exe'

$listener = Get-ListenerName $port
$napRunning = [bool](Get-Process -Name 'NapCatWinBootMain' -ErrorAction SilentlyContinue)
if ($listener -and $listener -ne 'QQ') {
    Warn "端口 $port 被 $listener 占用，NapCat 的 WebSocket 服务会起不来。请先关掉它，或在 NapCat 和 .env 里改端口"
}
if ($napRunning -and $listener -eq 'QQ') {
    Ok "已在运行并已登录（端口 $port）"
} elseif (-not (Test-Path $napExe)) {
    Warn "找不到 $napExe，请在 .env 里设置 NAPCAT_DIR"
} else {
    $t0 = Get-Date
    if (-not $napRunning) {
        $napArgs = @()
        if ($account) { $napArgs = @($account) }  # 快速登录（该号需要登录过一次）
        if ($napArgs.Count -gt 0) {
            Start-Process -FilePath $napExe -ArgumentList $napArgs -WorkingDirectory $napDir -WindowStyle Hidden
        } else {
            Start-Process -FilePath $napExe -WorkingDirectory $napDir -WindowStyle Hidden
        }
        Info '已启动，等待登录……'
    } else {
        Info 'NapCat 已在运行，等待登录……'
    }
    $shownQr = $null
    $loggedIn = $false
    for ($i = 0; $i -lt 300; $i++) {
        if ((Get-ListenerName $port) -eq 'QQ') { $loggedIn = $true; break }
        $qr = Get-ChildItem -Path (Join-Path $napDir 'versions\*\resources\app\napcat\cache\qrcode.png') -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($qr -and $qr.LastWriteTime -gt $t0 -and $qr.LastWriteTime -ne $shownQr) {
            $shownQr = $qr.LastWriteTime
            Start-Process -FilePath $qr.FullName
            Warn '需要扫码登录：已打开二维码图片，请用手机 QQ 扫码（二维码过期会自动弹出新的）'
        }
        Start-Sleep -Seconds 1
    }
    if ($loggedIn) { Ok "已登录（端口 $port）" } else { Warn '5 分钟内没有登录成功；主程序会先启动，并不断尝试重连' }
}

# ---------- 4. 主程序与控制台 ----------
function Start-Watchdog([string]$name, [string]$label) {
    if (Test-Alive (Join-Path $run "$name.watchdog.pid")) { Ok "$label 已在运行"; return }
    $script = Join-Path $root 'scripts\ops\service.ps1'
    Start-Process -FilePath 'powershell.exe' -WindowStyle Hidden -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden', '-File', "`"$script`"", '-Name', $name
    )
    Ok "$label 已在后台启动（日志：data\logs\$name-日期.log）"
}

Step '白璎主程序'
Start-Watchdog 'qq' '主程序'
Step '控制台'
$urlFile = Join-Path $run 'console_url.txt'
$webWasRunning = Test-Alive (Join-Path $run 'web.watchdog.pid')
$t0w = Get-Date
Start-Watchdog 'web' '控制台'
$url = $null
for ($i = 0; $i -lt 30; $i++) {
    # 新启动时等它写出新地址；本来就在运行时直接读
    if ((Test-Path $urlFile) -and ($webWasRunning -or (Get-Item $urlFile).LastWriteTime -ge $t0w)) {
        $url = (Get-Content -Path $urlFile -Encoding UTF8 | Select-Object -First 1); break
    }
    Start-Sleep -Seconds 1
}
if ($url) {
    Ok "控制台地址（可收藏）：$url"
    if (-not $NoBrowser) { Start-Process $url }
} else {
    Warn '没拿到控制台地址，请查看 data\logs\web-日期.log'
}

Write-Host ''
Write-Host '全部启动完成。可以关掉这个窗口，服务会继续在后台运行；停止请运行 stop.bat。' -ForegroundColor Magenta
