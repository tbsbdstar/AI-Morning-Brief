# 注册 Windows 计划任务「AI早知道」：每天按 config.json 的 schedule.run_time 运行
# 时间按 config.json 的 timezone_utc_offset 换算成本机时间（例如本机时钟是 UTC+9 时，北京 08:30 = 本机 09:30）
# -Python：用哪个 Python 跑（程序界面会传入它自己正在用的那个，换电脑时不依赖 PATH）
param([string]$Python = '')
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$cfg  = Get-Content (Join-Path $root 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json

$hm       = $cfg.schedule.run_time.Split(':')
$cfgOff   = [double]$cfg.timezone_utc_offset
$localOff = [System.TimeZoneInfo]::Local.GetUtcOffset([DateTime]::Now).TotalHours
$at       = (Get-Date -Hour ([int]$hm[0]) -Minute ([int]$hm[1]) -Second 0).AddHours($localOff - $cfgOff)

$py = $Python
if (-not $py -or -not (Test-Path $py)) {
    $bundled = Join-Path $root 'runtime\python\pythonw.exe'
    if (Test-Path $bundled) { $py = $bundled } else { $py = (Get-Command python -ErrorAction Stop).Source }
}
$pyw = Join-Path (Split-Path $py) 'pythonw.exe'
if (-not (Test-Path $pyw)) { $pyw = $py }

$action   = New-ScheduledTaskAction -Execute $pyw -Argument ('"' + (Join-Path $root 'run_brief.py') + '" --by 定时任务') -WorkingDirectory $root
$trigger  = New-ScheduledTaskTrigger -Daily -At $at
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName 'AI早知道' -Action $action -Trigger $trigger -Settings $settings `
    -Description '每日检索 AI / 信息安全 / 产品经理 资讯，生成中文早报并推送微信' -Force | Out-Null

# 改名前的旧任务（AI早报）一并清掉，避免重复推送
if (Get-ScheduledTask -TaskName 'AI早报' -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName 'AI早报' -Confirm:$false
}

Write-Host ("已注册计划任务「AI早知道」：每天本机时间 {0:HH:mm} 运行（= {1} {2}）" -f $at, $cfg.timezone_label, $cfg.schedule.run_time)
Write-Host "电脑当时关机或睡眠的话，开机后会补跑一次。"
