# 查看各服务是否在运行。
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$run = Join-Path $root 'data\run'

function Show([string]$label, [bool]$ok, [string]$detail) {
    $mark = if ($ok) { '●' } else { '○' }
    $color = if ($ok) { 'Green' } else { 'DarkGray' }
    Write-Host ("  {0} {1}  {2}" -f $mark, $label, $detail) -ForegroundColor $color
}

function Alive([string]$name) {
    $file = Join-Path $run "$name.pid"
    if (-not (Test-Path $file)) { return $false }
    $id = Get-Content -Path $file | Select-Object -First 1
    return [bool]($id -and (Get-Process -Id $id -ErrorAction SilentlyContinue))
}

Write-Host '白璎 · 运行状态' -ForegroundColor Magenta
Show 'Ollama  ' ([bool](Get-Process -Name 'ollama' -ErrorAction SilentlyContinue)) ''
Show 'NapCat  ' ([bool](Get-Process -Name 'NapCatWinBootMain' -ErrorAction SilentlyContinue)) ''
Show '主程序  ' (Alive 'qq') '日志：data\logs\qq-日期.log'
$url = ''
$urlFile = Join-Path $run 'console_url.txt'
if (Test-Path $urlFile) { $url = Get-Content -Path $urlFile -Encoding UTF8 | Select-Object -First 1 }
Show '控制台  ' (Alive 'web') $url
