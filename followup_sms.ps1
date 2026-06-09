# ZilloAgent - Follow-up SMS after 3 days (PENDING only, once)

$TWILIO_SID   = $env:TWILIO_ACCOUNT_SID
$TWILIO_TOKEN = $env:TWILIO_AUTH_TOKEN
$TWILIO_FROM  = $env:TWILIO_PHONE_NUMBER
$CONSENT_LOG  = "$PSScriptRoot\consent_log.csv"

if (-not $TWILIO_SID -or -not $TWILIO_TOKEN -or -not $TWILIO_FROM) {
    Write-Host "[ERROR] Missing Twilio credentials" -ForegroundColor Red; exit 1
}
if (-not (Test-Path $CONSENT_LOG)) { Write-Host "No consent log found."; exit }

$rows  = Import-Csv $CONSENT_LOG
$now   = Get-Date
$sent  = 0

$updated = $rows | ForEach-Object {
    $row = $_
    if ($row.Status -eq "PENDING" -and $row.FollowUpSent -eq "No") {
        $ts = [datetime]::ParseExact($row.Timestamp, "yyyy-MM-dd HH:mm:ss", $null)
        if (($now - $ts).TotalDays -ge 3) {
            $first    = ($row.Name -split " ")[0]
            $priceFmt = "{0:N0}" -f [int]$row.Price
            $msg      = "Hi $first, just following up on $($row.Address) ($priceFmt). We still have qualified buyers ready. Reply YES for a call or STOP to opt out. This is our last message."

            $auth = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("${TWILIO_SID}:${TWILIO_TOKEN}"))
            $body = "To=$([Uri]::EscapeDataString($row.Phone))&From=$([Uri]::EscapeDataString($TWILIO_FROM))&Body=$([Uri]::EscapeDataString($msg))"
            try {
                Invoke-RestMethod -Uri "https://api.twilio.com/2010-04-01/Accounts/$TWILIO_SID/Messages.json" `
                    -Method POST -Headers @{ Authorization = "Basic $auth" } `
                    -Body $body -ContentType "application/x-www-form-urlencoded" -TimeoutSec 10 | Out-Null
                $row.FollowUpSent = "Yes"
                $sent++
                Write-Host "  [FOLLOWUP] $($row.Name) $($row.Phone)" -ForegroundColor Yellow
            } catch {
                Write-Host "  [FAIL] $($row.Phone): $($_.Exception.Message)" -ForegroundColor Red
            }
        }
    }
    $row
}

# Rewrite CSV
$updated | Export-Csv $CONSENT_LOG -NoTypeInformation -Encoding utf8
Write-Host "`nFollow-up SMS sent: $sent"
