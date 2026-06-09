# ZilloAgent - Automated Phone Calls via Bland.ai
# Calls real estate agents and speaks like a professional salesperson

$BLANDAI_KEY   = "org_ed8eb1111c2048a128c5cd35dbe2f256cb0430f51310cbcd2b0fddfdb4c1feec7558238acb59265bc98369"
$RAPIDAPI_KEY  = "f7a8487fe3msh2563fd64ac14bbap13966cjsn1d484fa88b0d"
$RAPIDAPI_HOST = "us-real-estate-listings.p.rapidapi.com"
$CALLS_LIMIT   = 15  # 5 per city x 3 cities
$LOG_FILE      = "$PSScriptRoot\calls_log.csv"

$YOUR_PHONE    = ""  # e.g. "+972501234567"
$FROM_NUMBER   = "+16503831655"

# 3 cities: 3 owners + 2 agents each = 5 per city
$CITIES = @(
    @{ name="New York, NY";    slug="New+York%2C+NY"    },
    @{ name="Austin, TX";      slug="Austin%2C+TX"      },
    @{ name="Los Angeles, CA"; slug="Los+Angeles%2C+CA" }
)

$CONSENT_LOG = "$PSScriptRoot\consent_log.csv"

if (-not (Test-Path $LOG_FILE)) {
    "Timestamp,AgentName,Phone,Address,Price,City,Role,CallId,Status" | Out-File $LOG_FILE -Encoding utf8
}

# Load consented phones — RULE: no YES = no call
$consentedPhones = @{}
if (Test-Path $CONSENT_LOG) {
    Import-Csv $CONSENT_LOG | ForEach-Object {
        $consentedPhones[$_.Phone] = $_.Status
    }
}

function Test-Consent($phone) {
    $status = $consentedPhones[$phone]
    if ($status -eq "CONSENTED") { return $true }
    if ($status -eq "OPTED_OUT") {
        Write-Host "  [DNC] $phone is opted out — skipping" -ForegroundColor Red
        return $false
    }
    # PENDING or unknown = no call allowed
    Write-Host "  [NO CONSENT] $phone has not replied YES — SMS only" -ForegroundColor Yellow
    return $false
}

Write-Host "================================================"
Write-Host "  ZilloAgent - Automated Phone Calls"
Write-Host "================================================"

# Check balance
$balHeaders = @{ "authorization" = $BLANDAI_KEY }
$balance = Invoke-RestMethod -Uri "https://api.bland.ai/v1/me" -Headers $balHeaders -Method GET
Write-Host "Bland.ai Balance: `$$($balance.billing.current_balance)"
if ($balance.billing.current_balance -lt 0.5) {
    Write-Host "Low balance! Add credits at app.bland.ai"
    exit
}

$apiHeaders = @{
    "x-rapidapi-key"  = $RAPIDAPI_KEY
    "x-rapidapi-host" = $RAPIDAPI_HOST
    "Content-Type"    = "application/json"
}

function Get-Listings($citySlug, $limit) {
    $listings = @()
    $offset = 0
    while ($listings.Count -lt ($limit * 3)) {
        $url = "https://$RAPIDAPI_HOST/for-sale?location=$citySlug&offset=$offset&limit=50&sort=relevance&days_on=30"
        $r = Invoke-RestMethod -Uri $url -Headers $apiHeaders -Method GET -TimeoutSec 20
        if (-not $r.listings) { break }

        foreach ($listing in $r.listings) {
            $agent = $listing.advertisers | Where-Object { $_.type -eq "seller" } | Select-Object -First 1
            if (-not $agent) { $agent = $listing.advertisers | Select-Object -First 1 }
            $phone = $agent.phones | Where-Object { $_.type -match "Mobile|Office|Direct" } | Select-Object -First 1
            if (-not $phone) { $phone = $agent.phones | Select-Object -First 1 }
            if ($agent -and $phone -and $phone.number) {
                $clean = $phone.number -replace "[^\d]",""
                if ($clean.Length -eq 10) { $clean = "1$clean" }
                if ($clean.Length -eq 11) {
                    $listings += @{
                        name    = $agent.name
                        phone   = "+$clean"
                        address = $listing.location.address.line
                        city    = $listing.location.address.city
                        price   = $listing.list_price
                        beds    = $listing.description.beds
                        type    = $agent.type
                        email   = $agent.email
                    }
                }
            }
        }
        $offset += 50
        if ($offset -ge 200) { break }
        Start-Sleep -Milliseconds 500
    }
    return $listings
}

function Invoke-Call($lead, $task, $role) {
    $body = @{
        phone_number        = $lead.phone
        from                = $FROM_NUMBER
        task                = $task
        voice               = "nat"
        wait_for_greeting   = $true
        record              = $true
        max_duration        = 3
        answered_by_enabled = $true
    }
    if ($YOUR_PHONE -ne "") { $body.transfer_phone_number = $YOUR_PHONE }

    try {
        $callHeaders = @{ "authorization" = $BLANDAI_KEY; "Content-Type" = "application/json" }
        $resp = Invoke-RestMethod -Uri "https://api.bland.ai/v1/calls" -Headers $callHeaders -Method POST -Body ($body | ConvertTo-Json -Depth 3) -TimeoutSec 15
        $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
        "$ts,$($lead.name),$($lead.phone),`"$($lead.address)`",$($lead.price),$($lead.city),$role,$($resp.call_id),INITIATED" | Out-File $LOG_FILE -Append -Encoding utf8
        return $resp.call_id
    } catch {
        Write-Host "  [FAIL] $($lead.name): $($_.Exception.Message)" -ForegroundColor Red
        return $null
    }
}

$totalCalled = 0

foreach ($city in $CITIES) {
    Write-Host "`n── $($city.name) ──────────────────────────────────"
    $listings = Get-Listings $city.slug 20
    Write-Host "Found $($listings.Count) listings with phone numbers"

    $owners  = @($listings | Where-Object { $_.type -eq "seller" })
    $brokers = @($listings | Where-Object { $_.type -ne "seller" })

    $seenPhones  = @{}
    $ownerCalled = 0
    $agentCalled = 0

    # ── 3 owners per city ────────────────────────────────────────────────────
    foreach ($lead in $owners) {
        if ($ownerCalled -ge 3) { break }
        if ($seenPhones[$lead.phone]) { continue }
        $seenPhones[$lead.phone] = $true

        $firstName = ($lead.name -split " ")[0]
        $price     = "{0:N0}" -f [int]$lead.price

        # CONSENT CHECK — no call without YES
        if (-not (Test-Consent $lead.phone)) { continue }

        $task = "You are calling $firstName about their property at $($lead.address), listed at $price dollars. Ask if they have found a buyer yet or if they are still looking. If still looking, ask if they would like you to connect them with an agent who has qualified buyers right now. If yes, say you will have an agent call them shortly and end the call politely. If no, thank them and hang up. Keep it short and natural — under 60 seconds. No sales pitch, just a direct helpful question."

        $id = Invoke-Call $lead $task "OWNER"
        if ($id) {
            $ownerCalled++
            $totalCalled++
            Write-Host "  [OWNER $ownerCalled/3] $($lead.name) $($lead.phone)" -ForegroundColor Cyan
            Write-Host "           $($lead.address) — `$$price" -ForegroundColor Gray
        }
        Start-Sleep -Seconds 2
    }

    # ── 2 agents per city (connect with interested owners) ───────────────────
    foreach ($lead in $brokers) {
        if ($agentCalled -ge 2) { break }
        if ($seenPhones[$lead.phone]) { continue }
        $seenPhones[$lead.phone] = $true

        $firstName = ($lead.name -split " ")[0]

        # CONSENT CHECK — no call without YES
        if (-not (Test-Consent $lead.phone)) { continue }

        $task = "You are calling $firstName, a real estate agent in $($city.name). Say that you just spoke with property owners in the area who are looking for an agent with qualified buyers. Ask if they are currently taking new listings. If yes, say you will send their contact info to the owners today and end the call. If no, thank them and hang up. Under 45 seconds."

        $id = Invoke-Call $lead $task "AGENT"
        if ($id) {
            $agentCalled++
            $totalCalled++
            Write-Host "  [AGENT $agentCalled/2]  $($lead.name) $($lead.phone)" -ForegroundColor Yellow
        }
        Start-Sleep -Seconds 2
    }
}

Write-Host "`n================================================"
Write-Host "  DONE: $totalCalled / $CALLS_LIMIT calls initiated"
Write-Host "  Monitor: https://app.bland.ai/dashboard"
Write-Host "  Log: $LOG_FILE"
Write-Host "================================================"
