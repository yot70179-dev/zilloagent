# ZilloAgent Local API Server - serves dashboard data
$PORT = 8181
$BASE = "C:\Users\defaultuser0.DESKTOP-U646ES0.000\Documents\Claude Code\zilloagent"

$listener = New-Object System.Net.HttpListener
$listener.Prefixes.Add("http://localhost:$PORT/")
$listener.Start()
Write-Host "Dashboard running at: http://localhost:$PORT"
Write-Host "Press Ctrl+C to stop"

function Send-Response($ctx, $body, $type="application/json") {
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($body)
    $ctx.Response.ContentType = $type
    $ctx.Response.Headers.Add("Access-Control-Allow-Origin","*")
    $ctx.Response.ContentLength64 = $bytes.Length
    $ctx.Response.OutputStream.Write($bytes, 0, $bytes.Length)
    $ctx.Response.Close()
}

while ($listener.IsListening) {
    try {
        $ctx = $listener.GetContext()
        $path = $ctx.Request.Url.LocalPath

        if ($path -eq "/" -or $path -eq "/index.html") {
            $html = [System.IO.File]::ReadAllText("$BASE\dashboard.html")
            Send-Response $ctx $html "text/html; charset=utf-8"
        }
        elseif ($path -eq "/api/emails") {
            $logFile = "$BASE\outreach_log.csv"
            if (Test-Path $logFile) {
                $data = Import-Csv $logFile | Select-Object -Last 100 | ForEach-Object {
                    @{ time=$_.Timestamp; name=$_.AgentName; email=$_.AgentEmail; address=$_.Address; price=$_.Price; status=$_.Status }
                }
                Send-Response $ctx ($data | ConvertTo-Json -Depth 3)
            } else { Send-Response $ctx "[]" }
        }
        elseif ($path -eq "/api/replies") {
            $logFile = "$BASE\replies_log.csv"
            if (Test-Path $logFile) {
                $data = Import-Csv $logFile | Select-Object -Last 50 | ForEach-Object {
                    @{ time=$_.Timestamp; email=$_.FromEmail; name=$_.FromName; subject=$_.Subject; sentiment=$_.Sentiment; action=$_.Action }
                }
                Send-Response $ctx ($data | ConvertTo-Json -Depth 3)
            } else { Send-Response $ctx "[]" }
        }
        elseif ($path -eq "/api/calls") {
            $logFile = "$BASE\calls_log.csv"
            if (Test-Path $logFile) {
                $data = Import-Csv $logFile | Select-Object -Last 50 | ForEach-Object {
                    @{ time=$_.Timestamp; name=$_.AgentName; phone=$_.Phone; address=$_.Address; price=$_.Price; callId=$_.CallId; status=$_.Status }
                }
                Send-Response $ctx ($data | ConvertTo-Json -Depth 3)
            } else { Send-Response $ctx "[]" }
        }
        elseif ($path -eq "/api/consent") {
            $logFile = "$BASE\consent_log.csv"
            if (Test-Path $logFile) {
                $data = Import-Csv $logFile | Select-Object -Last 200 | ForEach-Object {
                    @{ time=$_.Timestamp; phone=$_.Phone; name=$_.Name; address=$_.Address; city=$_.City; price=$_.Price; status=$_.Status; followUpSent=$_.FollowUpSent; consentTime=$_.ConsentTime }
                }
                Send-Response $ctx ($data | ConvertTo-Json -Depth 3)
            } else { Send-Response $ctx "[]" }
        }
        elseif ($path -eq "/api/stats") {
            $emails  = if (Test-Path "$BASE\outreach_log.csv") { @(Import-Csv "$BASE\outreach_log.csv").Count } else { 0 }
            $replies = if (Test-Path "$BASE\replies_log.csv")  { @(Import-Csv "$BASE\replies_log.csv").Count  } else { 0 }
            $calls   = if (Test-Path "$BASE\calls_log.csv")    { @(Import-Csv "$BASE\calls_log.csv").Count    } else { 0 }
            $hot     = if (Test-Path "$BASE\replies_log.csv")  { @(Import-Csv "$BASE\replies_log.csv" | Where-Object { $_.Sentiment -eq "POSITIVE" }).Count } else { 0 }
            $stats   = @{ emails=$emails; replies=$replies; calls=$calls; hotLeads=$hot }
            Send-Response $ctx ($stats | ConvertTo-Json)
        }
        else { Send-Response $ctx "404" "text/plain" }
    } catch {}
}
