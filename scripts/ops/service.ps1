# 守护进程：在后台运行 companion qq / web，崩溃后 5 秒自动重启。
# 由 start.ps1 以隐藏窗口启动；stop.ps1 创建 data\run\<名字>.stop 后，它会等服务自行退出再结束。
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('qq', 'web')]
    [string]$Name
)

$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$run = Join-Path $root 'data\run'
$logs = Join-Path $root 'data\logs'
New-Item -ItemType Directory -Force -Path $run, $logs | Out-Null

$exe = Join-Path $root '.venv\Scripts\companion.exe'
$flag = Join-Path $run "$Name.stop"
$servicePid = Join-Path $run "$Name.pid"
Set-Content -Path (Join-Path $run "$Name.watchdog.pid") -Value $PID

$envFile = Join-Path $root '.env'
if (-not $env:CONSOLE_PORT -and (Test-Path $envFile)) {
    $m = Select-String -Path $envFile -Pattern '^\s*CONSOLE_PORT\s*=\s*(\d+)' | Select-Object -First 1
    if ($m) { $env:CONSOLE_PORT = $m.Matches[0].Groups[1].Value }
}
$env:PYTHONUNBUFFERED = '1'
$env:PYTHONIOENCODING = 'utf-8'

function Write-Log([string]$log, [string]$message) {
    $line = '{0} 守护：{1}' -f (Get-Date -Format 'MM-dd HH:mm:ss'), $message
    Add-Content -Path $log -Value $line -Encoding UTF8
}

while (-not (Test-Path $flag)) {
    $log = Join-Path $logs ('{0}-{1}.log' -f $Name, (Get-Date -Format 'yyyyMMdd'))
    $arguments = @('--log-file', "`"$log`"", $Name)
    if ($Name -eq 'web') {
        $arguments += '--no-browser'
        if ($env:CONSOLE_PORT) { $arguments += @('--port', $env:CONSOLE_PORT) }
    }

    $p = Start-Process -FilePath $exe -ArgumentList $arguments -WorkingDirectory $root `
        -NoNewWindow -PassThru `
        -RedirectStandardOutput (Join-Path $logs "$Name.stdout.log") `
        -RedirectStandardError (Join-Path $logs "$Name.stderr.log")
    $null = $p.Handle  # 拿住句柄，退出后才能读到退出码
    Set-Content -Path $servicePid -Value $p.Id
    $p.WaitForExit()
    $code = $p.ExitCode

    if (Test-Path $flag) { break }
    if ($code -eq 3) {
        Write-Log $log '已经有一个实例在运行，守护退出'
        break
    }
    Write-Log $log ('进程退出（代码 {0}），5 秒后重启；错误输出见 {1}.stderr.log' -f $code, $Name)
    Start-Sleep -Seconds 5
}

Remove-Item -Path $servicePid, (Join-Path $run "$Name.watchdog.pid") -ErrorAction SilentlyContinue
