# ZilloAgent - Auto Reply Handler
# Reads Gmail replies, responds intelligently, alerts human on positive leads

$GMAIL_USER     = "yot70179@gmail.com"
$GMAIL_PASSWORD = "cgal sjne nusb hmoa"
$LOG_FILE       = "$PSScriptRoot\outreach_log.csv"
$REPLY_LOG      = "$PSScriptRoot\replies_log.csv"
$AGENT_EMAIL    = "yot70179@gmail.com"   # where HOT LEAD alerts go

if (-not (Test-Path $REPLY_LOG)) {
    "Timestamp,FromEmail,FromName,Subject,Sentiment,Action" | Out-File $REPLY_LOG -Encoding utf8
}

# ── Load outreach log (who we emailed) ────────────────────────────────────────
function Get-ContactedEmails {
    if (-not (Test-Path $LOG_FILE)) { return @{} }
    $map = @{}
    Import-Csv $LOG_FILE | ForEach-Object {
        if ($_.AgentEmail) {
            $map[$_.AgentEmail.ToLower()] = @{
                name    = $_.AgentName
                address = $_.Address
                price   = $_.Price
            }
        }
    }
    return $map
}

# ── Sentiment analysis ────────────────────────────────────────────────────────
function Get-Sentiment($text) {
    $lower = $text.ToLower()

    $positive = @("interested","yes","love to","sounds good","available","call me",
                  "tell me more","schedule","when can","sure","absolutely","definitely",
                  "great idea","please send","how about","want to know","open to",
                  "let's talk","lets talk","reach out","contact me","good timing",
                  "perfect","works for me","sounds interesting","would like")

    $negative = @("not interested","no thanks","no thank you","stop","unsubscribe",
                  "remove me","don't contact","do not contact","please stop","not looking",
                  "already have","wrong person","wrong email","no longer","taken off",
                  "off your list","not available","not selling","not for sale")

    foreach ($kw in $negative) {
        if ($lower -match [regex]::Escape($kw)) { return "NEGATIVE" }
    }
    foreach ($kw in $positive) {
        if ($lower -match [regex]::Escape($kw)) { return "POSITIVE" }
    }
    return "NEUTRAL"
}

# ── Send email via Gmail SMTP ─────────────────────────────────────────────────
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
        $mail.Dispose(); $smtp.Dispose()
        return $true
    } catch {
        Write-Host "  [SMTP Error] $($_.Exception.Message)" -ForegroundColor Red
        return $false
    }
}

# ── Alert human agent about hot lead ─────────────────────────────────────────
function Send-HotLeadAlert($agentName, $agentEmail, $address, $price, $replyText) {
    $subject = "HOT LEAD - $agentName replied about $address"
    $body    = @"
HOT LEAD ALERT - ACTION REQUIRED

Agent Name  : $agentName
Agent Email : $agentEmail
Property    : $address
Price       : `$$price

Their reply:
-----------
$replyText
-----------

They expressed INTEREST. Contact them within 24 hours.

-- ZilloAgent
"@
    Send-Email $AGENT_EMAIL "You" $subject $body | Out-Null
    Write-Host "  [ALERT] Hot lead alert sent to $AGENT_EMAIL" -ForegroundColor Magenta
}

# ── IMAP: Connect to Gmail ────────────────────────────────────────────────────
function Connect-IMAP {
    $tcp = New-Object System.Net.Sockets.TcpClient("imap.gmail.com", 993)
    $ssl = New-Object System.Net.Security.SslStream($tcp.GetStream(), $false, { $true })
    $ssl.AuthenticateAsClient("imap.gmail.com")
    $reader = New-Object System.IO.StreamReader($ssl)
    $writer = New-Object System.IO.StreamWriter($ssl)
    $writer.AutoFlush = $true
    $reader.ReadLine() | Out-Null  # greeting
    return @{ tcp=$tcp; ssl=$ssl; reader=$reader; writer=$writer; tag=1 }
}

function Invoke-IMAP($conn, $cmd) {
    $tag = "T$($conn.tag)"
    $conn.tag++
    $conn.writer.WriteLine("$tag $cmd")
    $lines = @()
    $timeout = [System.DateTime]::Now.AddSeconds(15)
    while ([System.DateTime]::Now -lt $timeout) {
        if ($conn.reader.Peek() -ge 0) {
            $line = $conn.reader.ReadLine()
            $lines += $line
            if ($line -match "^$tag (OK|NO|BAD)") { break }
        } else {
            Start-Sleep -Milliseconds 100
        }
    }
    return $lines
}

function Read-IMAPBody($conn, $uid) {
    $lines = Invoke-IMAP $conn "UID FETCH $uid (BODY[TEXT] RFC822.HEADER)"
    return ($lines -join "`n")
}

function Disconnect-IMAP($conn) {
    try {
        $conn.writer.WriteLine("T99 LOGOUT")
        $conn.ssl.Close()
        $conn.tcp.Close()
    } catch {}
}

# ── MAIN ──────────────────────────────────────────────────────────────────────
Write-Host "================================================"
Write-Host "  ZilloAgent - Checking Replies"
Write-Host "================================================"

$contactedEmails = Get-ContactedEmails
Write-Host "Loaded $($contactedEmails.Count) contacted agents from log."

Write-Host "Connecting to Gmail IMAP..."
try {
    $conn = Connect-IMAP

    # Login
    $loginResp = Invoke-IMAP $conn "LOGIN `"$GMAIL_USER`" `"$GMAIL_PASSWORD`""
    if ($loginResp -notmatch "OK") {
        Write-Host "[ERROR] IMAP login failed. Check Gmail App Password." -ForegroundColor Red
        Disconnect-IMAP $conn; exit
    }
    Write-Host "Logged in successfully."

    # Select inbox
    Invoke-IMAP $conn "SELECT INBOX" | Out-Null

    # Search unseen emails from last 3 days
    $since = (Get-Date).AddDays(-3).ToString("dd-MMM-yyyy")
    $searchResp = Invoke-IMAP $conn "UID SEARCH UNSEEN SINCE $since"
    $uids = ($searchResp | Where-Object { $_ -match "^\* SEARCH" }) -replace "^\* SEARCH\s*", "" -split "\s+" | Where-Object { $_ -match "^\d+$" }

    if (-not $uids -or $uids.Count -eq 0) {
        Write-Host "No new unread emails found."
        Disconnect-IMAP $conn; exit
    }

    Write-Host "Found $($uids.Count) unread emails. Analyzing..."

    $processed = 0; $positive = 0; $negative = 0

    foreach ($uid in $uids) {
        $raw = Read-IMAPBody $conn $uid

        # Extract From header
        $fromLine = ($raw -split "`n" | Where-Object { $_ -match "^From:" }) | Select-Object -First 1
        $fromEmail = if ($fromLine -match "[\w\.\-\+]+@[\w\.\-]+\.\w+") { $Matches[0].ToLower() } else { "" }
        $fromName  = if ($fromLine -match "From:\s*(.+?)\s*<") { $Matches[1].Trim('"') } else { $fromEmail }

        # Extract Subject
        $subjectLine = ($raw -split "`n" | Where-Object { $_ -match "^Subject:" }) | Select-Object -First 1
        $subject = $subjectLine -replace "^Subject:\s*", ""

        # Only process replies from people we emailed
        if (-not $contactedEmails.ContainsKey($fromEmail)) { continue }

        $lead    = $contactedEmails[$fromEmail]
        $bodyIdx = $raw.IndexOf("`r`n`r`n")
        $body    = if ($bodyIdx -gt 0) { $raw.Substring($bodyIdx).Trim() } else { $raw }
        $body    = $body -replace "=\r?\n", "" -replace "=[\dA-F]{2}", ""  # decode quoted-printable basics

        $sentiment = Get-Sentiment "$subject $body"
        $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
        $processed++

        Write-Host "`n  From    : $fromName <$fromEmail>"
        Write-Host "  Subject : $subject"
        Write-Host "  Verdict : $sentiment" -ForegroundColor $(if ($sentiment -eq "POSITIVE") {"Green"} elseif ($sentiment -eq "NEGATIVE") {"Red"} else {"Yellow"})

        switch ($sentiment) {
            "POSITIVE" {
                $positive++
                # Reply to the agent
                $replyMsg = @"
Hi $($fromName.Split(" ")[0]),

Thank you for getting back to me! I'm glad to hear you're open to this.

I'll have our senior acquisition agent contact you directly within the next 24 hours to discuss the details and next steps for $($lead.address).

Looking forward to making this work for both sides!

Best regards,
Real Estate Acquisitions Team
"@
                Send-Email $fromEmail $fromName "Re: $subject" $replyMsg | Out-Null
                Write-Host "  [SENT] Positive reply sent to $fromEmail" -ForegroundColor Green

                # Alert human agent
                Send-HotLeadAlert $fromName $fromEmail $lead.address $lead.price $body

                "$ts,$fromEmail,`"$fromName`",`"$subject`",POSITIVE,REPLIED+ALERTED" | Out-File $REPLY_LOG -Append -Encoding utf8
            }
            "NEGATIVE" {
                $negative++
                $replyMsg = @"
Hi $($fromName.Split(" ")[0]),

Thank you for taking the time to respond. I completely understand and respect your decision.

We will remove you from our contact list immediately and will not reach out again.

Wishing you all the best with your listing.

Best regards,
Real Estate Acquisitions Team
"@
                Send-Email $fromEmail $fromName "Re: $subject" $replyMsg | Out-Null
                Write-Host "  [SENT] Thank you/goodbye sent to $fromEmail" -ForegroundColor Yellow

                "$ts,$fromEmail,`"$fromName`",`"$subject`",NEGATIVE,GOODBYE_SENT" | Out-File $REPLY_LOG -Append -Encoding utf8
            }
            "NEUTRAL" {
                # Ask clarifying question
                $replyMsg = @"
Hi $($fromName.Split(" ")[0]),

Thank you for getting back to me regarding $($lead.address).

Could you tell me a bit more about your thoughts? Are you open to connecting with a qualified buyer, or would you prefer we reach out at a different time?

Happy to work around your schedule.

Best regards,
Real Estate Acquisitions Team
"@
                Send-Email $fromEmail $fromName "Re: $subject" $replyMsg | Out-Null
                Write-Host "  [SENT] Follow-up question sent to $fromEmail" -ForegroundColor Cyan

                "$ts,$fromEmail,`"$fromName`",`"$subject`",NEUTRAL,FOLLOWUP_SENT" | Out-File $REPLY_LOG -Append -Encoding utf8
            }
        }

        # Mark as seen
        Invoke-IMAP $conn "UID STORE $uid +FLAGS (\Seen)" | Out-Null
    }

    Disconnect-IMAP $conn

    Write-Host "`n================================================"
    Write-Host "  Processed : $processed replies"
    Write-Host "  Positive  : $positive (alerts sent to you)"
    Write-Host "  Negative  : $negative (goodbye sent)"
    Write-Host "  Neutral   : $($processed - $positive - $negative) (follow-up sent)"
    Write-Host "  Reply log : $REPLY_LOG"
    Write-Host "================================================"

} catch {
    Write-Host "[ERROR] $($_.Exception.Message)" -ForegroundColor Red
}
