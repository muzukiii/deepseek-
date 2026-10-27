param(
    [ValidateSet('status', 'start-all', 'start-panel', 'open-panel', 'stop', 'stop-panel', 'stop-all', 'start-bot', 'stop-bot')]
    [string]$Action = 'status',
    [switch]$DryRun
)

$ErrorActionPreference = 'Continue'

$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Python = Join-Path $Root '.venv-win\Scripts\python.exe'
$PanelScript = Join-Path $Root 'tools\dashboard\server.py'
$PanelLog = Join-Path $env:TEMP 'astrabot_dashboard.log'
# Bot 的控制台输出单独落盘：Start-Process 起的进程窗口是隐藏的，未捕获异常的
# traceback 只会打到那个看不见的 stdout 上；不重定向的话进程静默退出就无从查起
# （2026-10-27 就出现过日志停在正常一行、35 分钟后才发现进程早已不在）。
$BotLogDir = Join-Path $env:TEMP 'astrabot_bot'
$SlDir = 'C:\Users\li204\Documents\git\tools\SnowLuma'
$PanelPort = 8000
$BotPort = 8080
$BridgePort = 3001
$BridgeWebUi = 5099

function Get-PortPid([int]$Port) {
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($conn) { return [int]$conn.OwningProcess }
    return 0
}

function Wait-Port([int]$Port, [int]$Seconds) {
    for ($i = 0; $i -lt $Seconds; $i++) {
        if (Get-PortPid $Port) { return $true }
        Start-Sleep -Seconds 1
    }
    return $false
}

function Start-Panel {
    $busy = Get-PortPid $PanelPort
    if ($busy) {
        Write-Host ("  [跳过] 面板已在运行 (PID {0})  http://127.0.0.1:{1}/" -f $busy, $PanelPort)
        return $true
    }
    if (-not (Test-Path $Python)) { Write-Host "  [错误] 找不到 venv: $Python"; return $false }
    if (-not (Test-Path $PanelScript)) { Write-Host "  [错误] 找不到面板脚本: $PanelScript"; return $false }
    if ($DryRun) { Write-Host "  [干跑] 将启动面板"; return $true }
    Start-Process -FilePath $Python -ArgumentList @($PanelScript, '--port', "$PanelPort") `
        -WorkingDirectory $Root -WindowStyle Hidden `
        -RedirectStandardOutput $PanelLog -RedirectStandardError "$PanelLog.err" | Out-Null
    if (Wait-Port $PanelPort 15) {
        Write-Host ("  [OK] 面板已启动 http://127.0.0.1:{0}/  (日志 {1})" -f $PanelPort, $PanelLog)
        return $true
    }
    Write-Host "  [警告] 面板 15s 内没监听，看日志: $PanelLog.err"
    return $false
}

function Start-Bot {
    $busy = Get-PortPid $BotPort
    if ($busy) { Write-Host ("  [跳过] Bot 已在运行 (PID {0})" -f $busy); return $true }
    if (-not (Test-Path $Python)) { Write-Host "  [错误] 找不到 venv: $Python"; return $false }
    if (-not (Test-Path (Join-Path $Root '.env'))) { Write-Host "  [错误] 缺少 .env"; return $false }
    if ($DryRun) { Write-Host "  [干跑] 将启动 Bot"; return $true }
    if (-not (Test-Path $BotLogDir)) { New-Item -ItemType Directory -Path $BotLogDir | Out-Null }
    # 每次启动一份新文件：Start-Process 的重定向是覆盖写，复用同一个名字会在下次
    # 启动时把上一次的崩溃现场冲掉，那就等于白留。
    $botLog = Join-Path $BotLogDir ("bot_{0}.log" -f (Get-Date -Format 'yyyyMMdd_HHmmss'))
    Start-Process -FilePath $Python -ArgumentList 'bot.py' -WorkingDirectory $Root -WindowStyle Hidden `
        -RedirectStandardOutput $botLog -RedirectStandardError "$botLog.err" | Out-Null
    if (Wait-Port $BotPort 45) {
        Write-Host ("  [OK] Bot 已启动 (:{0})，控制台输出 -> {1}" -f $BotPort, $botLog)
        return $true
    }
    Write-Host ("  [警告] Bot 45s 内没监听 8080，看控制台输出: {0} / {0}.err" -f $botLog)
    return $false
}

function Start-Bridge {
    if (Get-PortPid $BridgePort) { Write-Host ("  [OK] 桥接已在监听 {0}" -f $BridgePort); return $true }
    if (-not (Get-PortPid $BridgeWebUi)) {
        $nodeExe = Join-Path $SlDir 'node.exe'
        $nodeScript = Join-Path $SlDir 'index.mjs'
        if (-not (Test-Path (Join-Path $SlDir 'config'))) {
            Write-Host "  [警告] $SlDir 没有 config，拒绝启动（不是已配置的实例）"
            return $false
        }
        if (-not (Test-Path $nodeExe)) { Write-Host "  [警告] 找不到 $nodeExe"; return $false }
        if ($DryRun) { Write-Host "  [干跑] 将启动 SnowLuma"; return $true }
        Write-Host '      未运行，正在启动 SnowLuma...'
        Start-Process -FilePath $nodeExe -ArgumentList $nodeScript -WorkingDirectory $SlDir -WindowStyle Minimized | Out-Null
    }
    if (Wait-Port $BridgePort 40) { Write-Host ("  [OK] OneBot 已监听 {0}" -f $BridgePort); return $true }
    Write-Host "  [警告] 40s 内没等到 3001：确认 QQ 已登录（SnowLuma 要 hook QQ）；Bot 会自己重连"
    return $false
}

function Get-TopPythonAncestor([int]$ProcessId) {
    # Start-Process 起的 python 常是一个小父进程再拉出真正的工作进程；
    # 只杀"占端口的那个"会把父进程留成孤儿，所以沿 python 祖先往上找到顶再整树杀。
    $current = $ProcessId
    for ($i = 0; $i -lt 3; $i++) {
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$current" -ErrorAction SilentlyContinue
        if (-not $proc -or -not $proc.ParentProcessId) { break }
        $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($proc.ParentProcessId)" -ErrorAction SilentlyContinue
        if ($parent -and $parent.Name -eq 'python.exe') { $current = [int]$parent.ProcessId } else { break }
    }
    return $current
}

function Resolve-BotPids {
    $found = New-Object System.Collections.Generic.List[int]
    $worker = Get-PortPid $BotPort
    if ($worker) {
        $found.Add($worker)
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$worker" -ErrorAction SilentlyContinue
        if ($proc -and $proc.ParentProcessId) { $found.Add([int]$proc.ParentProcessId) }
    }
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match 'bot\.py' } |
        ForEach-Object { $found.Add([int]$_.ProcessId) }
    return @($found | Sort-Object -Unique)
}

function Stop-Bot {
    $pids = Resolve-BotPids
    if (-not $pids) { Write-Host '  Bot 未在运行'; return }
    $tops = @($pids | ForEach-Object { Get-TopPythonAncestor $_ } | Sort-Object -Unique)
    if ($DryRun) { Write-Host ("  [干跑] 将结束 Bot 进程: {0}" -f ($tops -join ', ')); return }
    # 父进程先走：只杀 worker 的话 uvicorn 的父进程会把它再拉起来
    foreach ($p in $tops) { taskkill /PID $p /T /F > $null 2>&1 }
    Start-Sleep -Seconds 1
    if (Get-PortPid $BotPort) { Write-Host ("  [警告] 端口 {0} 仍被占用，可能有残留进程" -f $BotPort) }
    else { Write-Host ("  [OK] Bot 已停止 (处理 PID: {0})" -f ($tops -join ', ')) }
}

function Stop-Panel {
    $busy = Get-PortPid $PanelPort
    if (-not $busy) { Write-Host '  面板未在运行'; return }
    $top = Get-TopPythonAncestor $busy
    if ($DryRun) { Write-Host ("  [干跑] 将结束面板进程: {0}" -f $top); return }
    taskkill /PID $top /T /F > $null 2>&1
    Write-Host ("  [OK] 面板已停止 (PID {0})" -f $top)
}

function Show-Status {
    $bot = Get-PortPid $BotPort
    $panel = Get-PortPid $PanelPort
    $bridge = Get-PortPid $BridgePort
    Write-Host ("  Bot      :{0}  {1}" -f $BotPort, $(if ($bot) { "在线 (PID $bot)" } else { '离线' }))
    Write-Host ("  桥接     :{0}  {1}" -f $BridgePort, $(if ($bridge) { "在线 (PID $bridge)" } else { '离线' }))
    Write-Host ("  面板     :{0}  {1}" -f $PanelPort, $(if ($panel) { "在线 (PID $panel)  http://127.0.0.1:8000/" } else { '未启动' }))
    $logDir = Join-Path $Root 'astrabot\chat_service\data\logs'
    if (Test-Path $logDir) {
        $latest = Get-ChildItem $logDir -Filter 'astrabot_*.log' -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($latest) {
            $age = [int]((Get-Date) - $latest.LastWriteTime).TotalSeconds
            Write-Host ("  最新日志 : {0}  ({1}s 前)" -f $latest.Name, $age)
        }
    }
}

switch ($Action) {
    'status' { Show-Status }
    'start-all' {
        Write-Host '[1/3] 桥接'
        Start-Bridge | Out-Null
        Write-Host '[2/3] Bot'
        $ok = Start-Bot
        Write-Host '[3/3] 面板'
        $panelOk = Start-Panel
        if ($panelOk -and -not $DryRun) { Start-Process "http://127.0.0.1:$PanelPort/" | Out-Null }
        if (-not $ok) { exit 1 }
    }
    'start-panel' { Start-Panel | Out-Null }
    'start-bot' { Start-Bot | Out-Null }
    'stop-bot' { Stop-Bot }
    'open-panel' {
        if (Get-PortPid $PanelPort) {
            Start-Process "http://127.0.0.1:$PanelPort/" | Out-Null
            Write-Host "  已打开 http://127.0.0.1:$PanelPort/"
        } else {
            Write-Host '  面板未启动，先选「只启动面板」'
        }
    }
    'stop' { Stop-Panel; Stop-Bot }
    'stop-panel' { Stop-Panel }
    'stop-all' {
        Stop-Panel
        Stop-Bot
        $sl = Get-PortPid $BridgeWebUi
        if (-not $sl) { Write-Host '  SnowLuma 未在运行'; break }
        if ($DryRun) { Write-Host ("  [干跑] 将结束 SnowLuma: {0}" -f $sl) }
        else {
            taskkill /PID $sl /T /F > $null 2>&1
            Write-Host ("  [OK] SnowLuma 已停止 (PID {0})" -f $sl)
        }
    }
}
