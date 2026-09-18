<#
.SYNOPSIS
    Run one command with the worker's credentials in its environment.

.DESCRIPTION
    Decrypts every credential stored by Set-SurgeSecret.ps1 into the environment
    of this process only, runs the command, and exits with its exit code. The
    values exist in memory for the life of that one process; they are not
    written to disk, not put in the user or machine environment, and not printed.

    `-List` as the only argument shows which credentials are stored - names
    only - so that "is the key there" can be answered without anyone looking at
    it.

    There is deliberately no param() block. With one, PowerShell tries to bind
    the wrapped command's own flags to this script's parameters: `python -c`
    would be read as an abbreviation of a `-Command` parameter. Reading $args
    verbatim passes the command through exactly as written.

.EXAMPLE
    .\Invoke-WithSurgeSecrets.ps1 -List
    .\Invoke-WithSurgeSecrets.ps1 python -m surge.jobs.readiness_cli --markets US
#>

$ErrorActionPreference = "Stop"

$dir = Join-Path $env:LOCALAPPDATA "surge\secrets"
$stored = @()
if (Test-Path $dir) {
    $stored = @(Get-ChildItem -Path $dir -Filter "*.dpapi" |
        Where-Object { $_.BaseName -match '^[A-Z][A-Z0-9_]*$' })
}

if ($args.Count -eq 1 -and $args[0] -eq "-List") {
    if ($stored.Count -eq 0) {
        Write-Host "no credentials are stored for $env:USERNAME"
    } else {
        foreach ($file in $stored) {
            Write-Host ("{0,-22} stored {1:yyyy-MM-dd HH:mm}" -f $file.BaseName, $file.LastWriteTime)
        }
    }
    exit 0
}

if ($args.Count -eq 0) {
    throw "nothing to run. Pass the command, e.g. python -m surge.jobs.readiness_cli"
}

# Anything this script sets it also removes. Run with `&` from a terminal, the
# script shares that terminal's process, so without this the keys would stay in
# the terminal's environment - visible to `Get-ChildItem env:` and inherited by
# everything run there afterwards - long after the one command that needed them.
$injected = @()
$code = 1
try {
    foreach ($file in $stored) {
        try {
            $secure = Get-Content -Path $file.FullName -Raw | ConvertTo-SecureString
        } catch {
            # Encrypted by another user or on another machine. Say which, not what.
            throw "could not decrypt $($file.BaseName); it was stored by a different Windows user or machine"
        }
        $plain = [System.Net.NetworkCredential]::new("", $secure).Password
        [Environment]::SetEnvironmentVariable($file.BaseName, $plain, "Process")
        $injected += $file.BaseName
        $plain = $null
    }

    $exe = $args[0]
    $rest = @()
    if ($args.Count -gt 1) { $rest = $args[1..($args.Count - 1)] }
    & $exe @rest
    $code = $LASTEXITCODE
}
finally {
    foreach ($name in $injected) {
        [Environment]::SetEnvironmentVariable($name, $null, "Process")
    }
}
exit $code
