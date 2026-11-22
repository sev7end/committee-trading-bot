# Registers all three scheduled tasks. Safe to re-run; it replaces existing ones.
#   powershell -ExecutionPolicy Bypass -File scripts\schedule_all.ps1
# Times are LOCAL and assume a UTC+4 clock with no DST: edit them for your timezone. US market opens 09:30 New York = 17:30 local in summer, 18:30 in winter.
$python = (Get-Command python).Source
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if ($python -match " " -or $root -match " ") { throw "Paths with spaces are not supported by this scheduler script: $python / $root" }
New-Item -ItemType Directory -Force (Join-Path $root "results\paper"), (Join-Path $root "results\reports") | Out-Null
$common = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -ExecutionTimeLimit (New-TimeSpan -Hours 8) -MultipleInstances IgnoreNew -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries
$logon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$logon.Delay = "PT5M"   # give Wi-Fi time to come up after logon

# 1. Paper runner: weekdays 17:15 local (~15 min before the New York open in US summer, ~75 min in winter): market-on-open
#    orders, stop/target attached after the fill. Also at every logon (skips if today already ran).
$log = Join-Path $root "results\paper\runner.log"
$cmd = "$python -u scripts\run_paper.py --auction --skip-if-done --wait-for-open 150 --max-minutes-before-open 420 >> $log 2>&1"   # -u: log lines appear as they happen, not at exit
$act = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c $cmd" -WorkingDirectory $root
$weekdays = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 17:15
Register-ScheduledTask -TaskName "TradingBot Paper Runner" -Action $act -Trigger @($weekdays, $logon) -Settings $common -Description "Committee bot: daily Alpaca PAPER trading run" -Force | Out-Null

# 2. Autopilot: Saturdays 12:00, plus at logon if the weekly run was missed (skips if ran within 6 days)
$log2 = Join-Path $root "results\reports\autopilot_runner.log"
$cmd2 = "$python -u scripts\autopilot.py --skip-if-recent 6 >> $log2 2>&1"
$act2 = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c $cmd2" -WorkingDirectory $root
$sat = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Saturday -At 12:00
Register-ScheduledTask -TaskName "TradingBot Autopilot" -Action $act2 -Trigger @($sat, $logon) -Settings $common -Description "Committee bot: weekly self-grading and monthly self-retuning" -Force | Out-Null

# 3. Dashboard server: at every logon, hidden, http://localhost:8765
$log3 = Join-Path $root "results\dashboard.log"
$act3 = New-ScheduledTaskAction -Execute $python -Argument "scripts\serve.py --log" -WorkingDirectory $root
$dash = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Days 30) -MultipleInstances IgnoreNew -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries -RestartCount 99 -RestartInterval (New-TimeSpan -Minutes 2)
Register-ScheduledTask -TaskName "TradingBot Dashboard" -Action $act3 -Trigger $logon -Settings $dash -Description "Committee bot: live dashboard on localhost:8765" -Force | Out-Null

# 4. Telegram command listener: at every logon, restarts itself if it dies
$log4 = Join-Path $root "results\telegram_bot.log"
$act4 = New-ScheduledTaskAction -Execute $python -Argument "-u scripts\telegram_bot.py --log" -WorkingDirectory $root
$tg = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Days 30) -MultipleInstances IgnoreNew -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries -RestartCount 99 -RestartInterval (New-TimeSpan -Minutes 2)
Register-ScheduledTask -TaskName "TradingBot Telegram" -Action $act4 -Trigger $logon -Settings $tg -Description "Committee bot: Telegram command listener" -Force | Out-Null

Get-ScheduledTask -TaskName "TradingBot*" | Select-Object TaskName, State | Format-Table -AutoSize
