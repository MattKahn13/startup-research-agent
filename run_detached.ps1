# Launch the research agent as a DETACHED background process that survives the
# parent shell / Claude session teardown. Start-Process creates an independent
# process (not a child of this shell), so killing the launcher does not kill it.
#
# Requires UNATTENDED=1 so the agent never blocks on the Enter prompt (cookies
# must already be loaded in browser_cookies.json).
#
# Usage:
#   powershell -File run_detached.ps1 -OutputDir startup_output_overnight -MaxRounds 30
#
# Writes:
#   <OutputDir>/run_detached.log   -- combined stdout/stderr
#   <OutputDir>/run_detached.pid   -- the spawned PID (for monitoring / kill)

param(
    [string]$OutputDir = "startup_output_overnight",
    [int]$MaxRounds = 30,
    [string]$SeedUrls = "https://eship.cornell.edu/cornell-startups/high-profile-startups/,https://bigredai.org/startups",
    [string]$Prompt = "Find every company where at least one founder is a Cornellian. Prioritize source pages that state the founder's name and Cornell affiliation in the same passage."
)

$ErrorActionPreference = "Stop"
$proj = "G:\My Drive\Cornell\Spring 2026\Agents\startup_research_agent"
Set-Location $proj

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$log = Join-Path $OutputDir "run_detached.log"
$pidFile = Join-Path $OutputDir "run_detached.pid"

# Environment for the child: unattended (no Enter prompt) + UTF-8.
$env:UNATTENDED = "1"
$env:PYTHONUTF8 = "1"

$pyArgs = @(
    "startup_researcher.py",
    "--max-rounds", "$MaxRounds",
    "--output-dir", "$OutputDir",
    "--seed-urls", "$SeedUrls",
    "$Prompt"
)

# -WindowStyle Hidden + redirected streams = fully detached, no console window.
$proc = Start-Process -FilePath "python" `
    -ArgumentList $pyArgs `
    -WorkingDirectory $proj `
    -WindowStyle Hidden `
    -RedirectStandardOutput $log `
    -RedirectStandardError (Join-Path $OutputDir "run_detached.err.log") `
    -PassThru

$proc.Id | Out-File -FilePath $pidFile -Encoding ascii
Write-Output "DETACHED_PID=$($proc.Id)"
Write-Output "LOG=$log"
