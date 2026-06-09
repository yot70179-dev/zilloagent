# ZilloAgent - Real Estate Outreach via Gmail SMTP
# Pure PowerShell - no Python needed

$GMAIL_USER     = "yot70179@gmail.com"
$GMAIL_PASSWORD = "cgal sjne nusb hmoa"
$RAPIDAPI_KEY   = "f7a8487fe3msh2563fd64ac14bbap13966cjsn1d484fa88b0d"
$RAPIDAPI_HOST  = "us-real-estate-listings.p.rapidapi.com"
$DAILY_LIMIT    = 10
$LOG_FILE       = "$PSScriptRoot\outreach_log.csv"

# Personal/free email providers to skip - not real business emails
$SKIP_DOMAINS = @(
    "gmail.com","hotmail.com","yahoo.com","aol.com","outlook.com",
    "icloud.com","me.com","msn.com","live.com","ymail.com",
    "hotmail.co.uk","yahoo.co.uk","cox.net","sbcglobal.net",
    "verizon.net","att.net","comcast.net","earthlink.net"
)

function Test-IsRealEmail($email) {
    # Must match valid email format
    if ($email -notmatch "^[\w\.\-\+]+@[\w\.\-]+\.[a-zA-Z]{2,}$") { return $false }
    $domain = $email.Split("@")[1].ToLower()
    # Skip personal providers
    if ($SKIP_DOMAINS -contains $domain) { return $false }
    # Must have a real business domain (dot + 2+ chars)
    if ($domain.Split(".").Count -lt 2) { return $false }
    return $true
}

Write-Host "================================================"
Write-Host "  ZilloAgent - LA Real Estate Outreach"
Write-Host "================================================"

if (-not (Test-Path $LOG_FILE)) {
    "Timestamp,AgentName,AgentEmail,Address,Price,Status" | Out-File $LOG_FILE -Encoding utf8
}

function Get-LAListings {
    $headers = @{
        "x-rapidapi-key"  = $RAPIDAPI_KEY
        "x-rapidapi-host" = $RAPIDAPI_HOST
        "Content-Type"    = "application/json"
    }
    $allListings = @()
    $offset = 0
    Write-Host "Fetching listings from Los Angeles..."

    while ($allListings.Count -lt $DAILY_LIMIT) {
        $url = "https://$RAPIDAPI_HOST/for-sale?location=Los%20Angeles%2C%20CA&offset=$offset&limit=50&sort=relevance&days_on=7"
        try {
            $r = Invoke-RestMethod -Uri $url -Headers $headers -Method GET -TimeoutSec 20
            $listings = $r.listings
            if (-not $listings -or $listings.Count -eq 0) { break }

            foreach ($listing in $listings) {
                $agent = $listing.advertisers | Where-Object { $_.type -eq "seller" } | Select-Object -First 1
                if (-not $agent) { $agent = $listing.advertisers | Select-Object -First 1 }
                if ($agent -and $agent.email -and (Test-IsRealEmail $agent.email)) {
                    $allListings += @{
                        address    = $listing.location.address.line
                        city       = $listing.location.address.city
                        price      = $listing.list_price
                        agentName  = $agent.name
                        agentEmail = $agent.email
                        beds       = $listing.description.beds
                        baths      = $listing.description.baths_consolidated
                    }
                }
                if ($allListings.Count -ge $DAILY_LIMIT) { break }
            }
            Write-Host "  Found $($allListings.Count) listings with agent email..."
            $offset += 50
            if ($offset -ge 200) { break }
            Start-Sleep -Milliseconds 800
        } catch {
            Write-Host "  API error: $($_.Exception.Message)"
            break
        }
    }
    return $allListings | Select-Object -First $DAILY_LIMIT
}

function New-OutreachMessage($agentName, $address, $price, $city, $beds, $baths) {
    $firstName = ($agentName -split " ")[0]
    $priceFmt  = "{0:N0}" -f [int]$price
    $propDesc  = if ($beds) { "$beds bed/$baths bath home" } else { "property" }

    $t1 = "Hi $firstName,`n`nI came across your listing at $address ($priceFmt USD) and wanted to reach out directly.`n`nI represent qualified buyers actively searching for a $propDesc in $city this month. They are pre-approved and ready to move quickly.`n`nWould you be open to a brief conversation this week?`n`nBest regards,`nReal Estate Acquisitions Team`n`n---`nReply STOP to unsubscribe."
    $t2 = "Hello $firstName,`n`nYour $propDesc at $address caught our attention - it matches exactly what our buyers are looking for in $city.`n`nWe work with serious, pre-qualified buyers in the $$priceFmt range who are ready to act fast. Would you have 5 minutes to connect?`n`nWarm regards,`nReal Estate Acquisitions Team`n`n---`nReply STOP to unsubscribe."
    $t3 = "Hi $firstName,`n`nI specialize in connecting motivated sellers with qualified buyers in the $city area, and your listing at $address stood out to me.`n`nWe currently have buyers in the $$priceFmt range looking in your neighborhood. Would you be interested in discussing this?`n`nLooking forward to hearing from you,`nReal Estate Acquisitions Team`n`n---`nReply STOP to unsubscribe."

    $templates = @($t1, $t2, $t3)
    return $templates[(Get-Random -Min 0 -Max 3)]
}

function Send-Email($toEmail, $toName, $subject, $body) {
    try {
        $smtp = New-Object System.Net.Mail.SmtpClient("smtp.gmail.com", 587)
        $smtp.EnableSsl = $true
        $smtp.Credentials = New-Object System.Net.NetworkCredential($GMAIL_USER, $GMAIL_PASSWORD)
        $mail = New-Object System.Net.Mail.MailMessage
        $mail.From = New-Object System.Net.Mail.MailAddress($GMAIL_USER, "Real Estate Acquisitions Team")
        $mail.To.Add((New-Object System.Net.Mail.MailAddress($toEmail, $toName)))
        $mail.Subject    = $subject
        $mail.Body       = $body
        $mail.IsBodyHtml = $false
        $smtp.Send($mail)
        $mail.Dispose()
        $smtp.Dispose()
        return $true
    } catch {
        return $false
    }
}

# MAIN
$listings = Get-LAListings

if ($listings.Count -eq 0) {
    Write-Host "No listings found. Check API subscription."
    exit
}

Write-Host "`nFound $($listings.Count) listings. Starting email outreach...`n"

$sent = 0
$failed = 0
$seenEmails = @{}

foreach ($prop in $listings) {
    if ($seenEmails[$prop.agentEmail]) { continue }
    $seenEmails[$prop.agentEmail] = $true

    $message = New-OutreachMessage $prop.agentName $prop.address $prop.price $prop.city $prop.beds $prop.baths
    $subject  = "Qualified Buyers Interested in $($prop.address)"

    $ok = Send-Email $prop.agentEmail $prop.agentName $subject $message

    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    if ($ok) {
        $sent++
        Write-Host "  [OK] [$sent/$DAILY_LIMIT] $($prop.agentName) -> $($prop.agentEmail)" -ForegroundColor Green
        Write-Host "       $($prop.address) - `$$("{0:N0}" -f [int]$prop.price)" -ForegroundColor Gray
        "$ts,$($prop.agentName),$($prop.agentEmail),`"$($prop.address)`",$($prop.price),SENT" | Out-File $LOG_FILE -Append -Encoding utf8
    } else {
        $failed++
        Write-Host "  [FAIL] $($prop.agentName) -> $($prop.agentEmail)" -ForegroundColor Red
        "$ts,$($prop.agentName),$($prop.agentEmail),`"$($prop.address)`",$($prop.price),FAILED" | Out-File $LOG_FILE -Append -Encoding utf8
    }

    if ($sent -ge $DAILY_LIMIT) { break }
    Start-Sleep -Milliseconds 1500
}

Write-Host "`n================================================"
Write-Host "  DONE: Sent=$sent  Failed=$failed"
Write-Host "  Log: $LOG_FILE"
Write-Host "================================================"
