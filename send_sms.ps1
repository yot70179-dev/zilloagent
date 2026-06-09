# ZilloAgent - Step 1: Send consent SMS to property owners
# Flow: SMS → wait for YES → then call
# NO YES = SMS only. YES = call allowed. STOP = DNC.

$TWILIO_SID    = $env:TWILIO_ACCOUNT_SID
$TWILIO_TOKEN  = $env:TWILIO_AUTH_TOKEN
$TWILIO_FROM   = $env:TWILIO_PHONE_NUMBER     # e.g. "+1XXXXXXXXXX"
$RAPIDAPI_KEY  = "f7a8487fe3msh2563fd64ac14bbap13966cjsn1d484fa88b0d"
$RAPIDAPI_HOST = "us-real-estate-listings.p.rapidapi.com"
$CONSENT_LOG   = "$PSScriptRoot\consent_log.csv"
$DAILY_LIMIT   = 15

$CITIES = @(
    @{ name="New York, NY";    slug="New+York%2C+NY"    },
    @{ name="Austin, TX";      slug="Austin%2C+TX"      },
    @{ name="Los Angeles, CA"; slug="Los+Angeles%2C+CA" }
)

if (-not $TWILIO_SID -or -not $TWILIO_TOKEN -or -not $TWILIO_FROM) {
    Write-Host "[ERROR] Missing Twilio credentials in .env" -ForegroundColor Red
    Write-Host "  Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER" -ForegroundColor Yellow
    exit 1
}

if (-not (Test-Path $CONSENT_LOG)) {
    "Timestamp,Phone,Name,Address,City,Price,Status,FollowUpSent,ConsentTime" | Out-File $CONSENT_LOG -Encoding utf8
}

# Load existing numbers to avoid duplicates
$existing = @{}
if (Test-Path $CONSENT_LOG) {
    Import-Csv $CONSENT_LOG | ForEach-Object { $existing[$_.Phone] = $_.Status }
}

$apiHeaders = @{
    "x-rapidapi-key"  = $RAPIDAPI_KEY
    "x-rapidapi-host" = $RAPIDAPI_HOST
}

function Get-Listings($citySlug, $limit) {
    $results = @()
    $offset  = 0
    while ($results.Count -lt $limit) {
        $url = "https://$RAPIDAPI_HOST/for-sale?location=$citySlug&offset=$offset&limit=50&sort=relevance&days_on=14"
        try {
            $r = Invoke-RestMethod -Uri $url -Headers $apiHeaders -Method GET -TimeoutSec 20
            if (-not $r.listings) { break }
            foreach ($l in $r.listings) {
                $adv = $l.advertisers | Where-Object { $_.type -eq "seller" } | Select-Object -First 1
                if (-not $adv) { $adv = $l.advertisers | Select-Object -First 1 }
                $ph = $adv.phones | Where-Object { $_.type -match "Mobile|Direct" } | Select-Object -First 1
                if (-not $ph) { $ph = $adv.phones | Select-Object -First 1 }
                if ($adv -and $ph -and $ph.number) {
                    $clean = $ph.number -replace "[^\d]",""
                    if ($clean.Length -eq 10) { $clean = "1$clean" }
                    if ($clean.Length -eq 11) {
                        $results += @{
                            name    = $adv.name
                            phone   = "+$clean"
                            address = $l.location.address.line
                            city    = $l.location.address.city
                            price   = $l.list_price
                        }
                    }
                }
            }
        } catch { break }
        $offset += 50
        if ($offset -ge 300) { break }
        Start-Sleep -Milliseconds 400
    }
    return $results | Select-Object -First $limit
}

function Send-ConsentSMS($phone, $name, $address, $price) {
    $first    = ($name -split " ")[0]
    $priceFmt = "{0:N0}" -f [int]$price
    $msg      = "Hi $first, I have pre-qualified buyers looking in your area. Interested in connecting them with your listing at $address ($priceFmt)? Reply YES for a quick call or STOP to opt out."

    $auth = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("${TWILIO_SID}:${TWILIO_TOKEN}"))
    $body = "To=$([Uri]::EscapeDataString($phone))&From=$([Uri]::EscapeDataString($TWILIO_FROM))&Body=$([Uri]::EscapeDataString($msg))"
    try {
        $r = Invoke-RestMethod -Uri "https://api.twilio.com/2010-04-01/Accounts/$TWILIO_SID/Messages.json" `
            -Method POST -Headers @{ Authorization = "Basic $auth" } `
            -Body $body -ContentType "application/x-www-form-urlencoded" -TimeoutSec 10
        return $r.sid
    } catch {
        return $null
    }
}

Write-Host "================================================"
Write-Host "  ZilloAgent - Consent SMS Outreach"
Write-Host "================================================"

$sent = 0
$skipped = 0

foreach ($city in $CITIES) {
    if ($sent -ge $DAILY_LIMIT) { break }
    Write-Host "`n── $($city.name) ──────────────────────"
    $listings = Get-Listings $city.slug 10
    $seenPhones = @{}

    foreach ($lead in $listings) {
        if ($sent -ge $DAILY_LIMIT) { break }
        if ($seenPhones[$lead.phone]) { continue }
        $seenPhones[$lead.phone] = $true

        # Skip DNC and already-contacted
        if ($existing[$lead.phone] -eq "OPTED_OUT") { $skipped++; continue }
        if ($existing[$lead.phone] -eq "CONSENTED") { $skipped++; continue }
        if ($existing[$lead.phone] -eq "PENDING")   { $skipped++; continue }

        $sid = Send-ConsentSMS $lead.phone $lead.name $lead.address $lead.price
        $ts  = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

        if ($sid) {
            $sent++
            Write-Host "  [SMS $sent] $($lead.name) $($lead.phone) — $($lead.address)" -ForegroundColor Cyan
            "$ts,$($lead.phone),`"$($lead.name)`",`"$($lead.address)`",$($lead.city),$($lead.price),PENDING,No," | Out-File $CONSENT_LOG -Append -Encoding utf8
        } else {
            Write-Host "  [FAIL] $($lead.name) $($lead.phone)" -ForegroundColor Red
        }
        Start-Sleep -Seconds 1
    }
}

Write-Host "`n================================================"
Write-Host "  Sent: $sent SMS  |  Skipped: $skipped"
Write-Host "  Consent log: $CONSENT_LOG"
Write-Host "================================================"
