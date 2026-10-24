# 一键停止：主程序（会先整理记忆再退出）、控制台、NapCat、Ollama。
# 用法：双击项目根目录的 stop.bat；保留某项可加 -KeepOllama / -KeepNapCat
param([switch]$KeepOllama, [switch]$KeepNapCat)

$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$run = Join-Path $root 'data\run'
New-Item -ItemType Directory -Force -Path $run | Out-Null

function Step([string]$text) { Write-Host ''; Write-Host "▸ $text" -ForegroundColor Magenta }
function Ok([string]$text) { Write-Host "  ✓ $text" -ForegroundColor Green }
function Warn([string]$text) { Write-Host "  ! $text" -ForegroundColor Yellow }

function Get-Id([string]$pidFile) {
    if (Test-Path $pidFile) { return (Get-Content -Path $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1) }
    return $null
}

function Stop-Companion([string]$name, [string]$label, [int]$graceSeconds) {
    Step $label
    $svc = Get-Id (Join-Path $run "$name.pid")
    $dog = Get-Id (Join-Path $run "$name.watchdog.pid")
    if (-not ($svc -and (Get-Process -Id $svc -ErrorAction SilentlyContinue)) -and -not ($dog -and (Get-Process -Id $dog -ErrorAction SilentlyContinue))) {
        Ok '没有在运行'
        Remove-Item -Path (Join-Path $run "$name.*pid") -ErrorAction SilentlyContinue
        return
    }
    New-Item -ItemType File -Force -Path (Join-Path $run "$name.stop") | Out-Null
    for ($i = 0; $i -lt $graceSeconds; $i++) {
        if (-not ($svc -and (Get-Process -Id $svc -ErrorAction SilentlyContinue))) { break }
        Start-Sleep -Seconds 1
    }
    if ($svc -and (Get-Process -Id $svc -ErrorAction SilentlyContinue)) {
        Warn "$graceSeconds 秒内没有自行退出，强制结束"
        & taskkill /T /F /PID $svc | Out-Null
    }
    Start-Sleep -Seconds 1
    if ($dog -and (Get-Process -Id $dog -ErrorAction SilentlyContinue)) { & taskkill /T /F /PID $dog | Out-Null }
    Remove-Item -Path (Join-Path $run "$name.*pid"), (Join-Path $run "$name.stop") -ErrorAction SilentlyContinue
    Ok '已停止'
}

Write-Host '白璎 · 一键停止' -ForegroundColor Magenta
Stop-Companion 'qq' '白璎主程序（先整理这次的对话，可能要几十秒）' 90
Stop-Companion 'web' '控制台' 15

if (-not $KeepNapCat) {
    Step 'NapCat'
    $values = @{}
    $envPath = Join-Path $root '.env'
    if (Test-Path $envPath) {
        foreach ($line in Get-Content -Path $envPath -Encoding UTF8) {
            if ($line -match '^\s*([A-Z0-9_]+)\s*=\s*(.*)$') { $values[$Matches[1]] = $Matches[2].Trim() }
        }
    }
    $napDir = $values['NAPCAT_DIR']
    if (-not $napDir) { $napDir = 'E:\07\NapCat.Shell.Windows.OneKey\NapCat.52230.Shell' }
    # 只结束 NapCat 目录里的 QQ，不动你平时用的 QQ
    $procs = @(Get-CimInstance Win32_Process | Where-Object {
        $_.ExecutablePath -and $_.ExecutablePath.StartsWith($napDir, [StringComparison]::OrdinalIgnoreCase)
    })
    if ($procs.Count -eq 0) { Ok '没有在运行' }
    else {
        foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
        Ok "已停止（$($procs.Count) 个进程）"
    }
}

if (-not $KeepOllama) {
    Step 'Ollama'
    $procs = @(Get-Process -Name 'ollama', 'ollama app' -ErrorAction SilentlyContinue)
    if ($procs.Count -eq 0) { Ok '没有在运行' }
    else { $procs | Stop-Process -Force -ErrorAction SilentlyContinue; Ok '已停止' }
}

Write-Host ''
Write-Host '全部已停止。' -ForegroundColor Magenta
