# Perpetual-run wrapper for startup_researcher.py.
#
# Restarts the script automatically whenever it crashes (Drive sync
# write race, Gemini session death, network blip, Chrome corruption,
# etc.). Each restart resumes from the most recent checkpoint, so
# nothing is lost.
#
# USAGE:
#   ./run_overnight.ps1                      # runs perpetually until you Ctrl+C
#   ./run_overnight.ps1 -MaxIterations 50    # cap restarts (defaults to unlimited)
#   ./run_overnight.ps1 -Headless:$false     # run with visible Chrome (so you can solve CAPTCHA)
#
# The first invocation needs --seed-urls + a prompt; subsequent
# invocations use --resume which picks up automatically.

param(
    [int]$MaxIterations = 0,                         # 0 = unlimited
    [switch]$Headless = $true,                       # default headless (CAPTCHAs auto-skip)
    [int]$SleepBetweenSeconds = 30,                  # cool-off between restarts
    [string]$OutputDir = "startup_output",
    [string]$LogDir = "junk"
)

Set-Location $PSScriptRoot
$env:PYTHONUTF8 = "1"

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

# First-iteration command needs the prompt + seed URLs (in case there's
# no checkpoint yet); subsequent iterations use --resume which is
# self-contained.
$Prompt = "Find every company where AT LEAST ONE founder is a Cornellian. Include any size - startups AND Fortune 500s."
$SeedUrls = "https://eship.cornell.edu/cornell-startups/high-profile-startups/,https://bigredai.org/startups"
$CheckpointFile = "startup_checkpoint.json"

$iter = 0
while ($true) {
    $iter++
    if ($MaxIterations -gt 0 -and $iter -gt $MaxIterations) {
        Write-Host "[wrapper] Reached max iterations ($MaxIterations). Stopping."
        break
    }

    $ts = Get-Date -Format "yyyyMMdd-HHmmss"
    $log = Join-Path $LogDir "perpetual-$ts.log"
    Write-Host ""
    Write-Host "============================================================"
    Write-Host "[wrapper] Iteration $iter - log: $log"
    Write-Host "============================================================"

    # Build args list
    $argsList = @("startup_researcher.py", "--output-dir", $OutputDir)
    if ($Headless) { $argsList += "--headless" }

    if (Test-Path $CheckpointFile) {
        # Continue from previous round; --resume reads the prompt + plan
        # from the checkpoint, no need to repeat them.
        $argsList += "--resume"
    } else {
        # First run - pass prompt and seed URLs explicitly.
        $argsList += @("--seed-urls", $SeedUrls, $Prompt)
    }

    # Stream output to console AND to the log so you can scroll back.
    & python @argsList 2>&1 | Tee-Object -FilePath $log

    $exit = $LASTEXITCODE
    Write-Host ""
    Write-Host "[wrapper] Iteration $iter exited with code $exit."

    if ($exit -eq 0) {
        Write-Host "[wrapper] Clean exit - script finished its plan. Sleeping briefly then restarting to keep discovering ..."
    } else {
        Write-Host "[wrapper] Non-zero exit. Sleeping $SleepBetweenSeconds s, then restarting from checkpoint."
    }

    # Kill any orphan undetected_chromedriver.exe processes AND their
    # Chrome child windows. The user hates having to clean these up
    # manually, so be thorough - match Chrome processes whose user-data-
    # dir argument points at our `gemini_ephemeral_*` temp profile.
    Get-WmiObject Win32_Process -Filter "Name = 'undetected_chromedriver.exe'" `
        -ErrorAction SilentlyContinue |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

    Get-WmiObject Win32_Process -Filter "Name = 'chrome.exe'" `
        -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'gemini_ephemeral_|undetected' } |
        ForEach-Object {
            Write-Host "[wrapper] Killing orphan Chrome PID $($_.ProcessId)"
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }

    Start-Sleep -Seconds $SleepBetweenSeconds
}
