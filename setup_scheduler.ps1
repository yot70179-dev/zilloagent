# Register ZilloAgent reply checker to run every hour automatically

$scriptPath = "$PSScriptRoot\check_replies.ps1"
$action  = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NonInteractive -WindowStyle Hidden -File `"$scriptPath`""
$trigger = New-ScheduledTaskTrigger -RepetitionInterval (New-TimeSpan -Hours 1) -Once -At (Get-Date)
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -RestartCount 2

Register-ScheduledTask -TaskName "ZilloAgent-ReplyChecker" -Action $action -Trigger $trigger -Settings $settings -RunLevel Highest -Force

Write-Host "Scheduled task created - ZilloAgent will check replies every hour."
Write-Host "To run manually: .\check_replies.ps1"
Write-Host "To stop: Unregister-ScheduledTask -TaskName ZilloAgent-ReplyChecker -Confirm:`$false"
