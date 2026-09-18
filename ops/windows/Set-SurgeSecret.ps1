<#
.SYNOPSIS
    Store one worker credential or setting, encrypted to this Windows user.

.DESCRIPTION
    The worker reads its credentials and a few settings from process environment
    variables and nowhere else. This is where they come from on this machine:
    each one is encrypted with DPAPI for the current Windows user and written
    outside the repository, under %LOCALAPPDATA%\surge\secrets.
    Invoke-WithSurgeSecrets.ps1 decrypts them into the environment of the one
    process that needs them.

    So a credential is never in the repository, never in a .env file, never in
    plaintext on disk, and never printed - not by this script, not by the
    wrapper. Only this Windows user on this machine can decrypt the blob; copied
    anywhere else it is noise.

    Secrets are read from a hidden prompt or from the clipboard, never from the
    command line, where they would land in the shell history. Only the settings
    that are not secret - which model, and the date Zero Data Retention was seen
    switched on - may be given with -Value.

.PARAMETER Name
    Which credential or setting. Limited to the names the worker actually reads,
    so a typo fails here rather than creating something nothing will ever use.

.PARAMETER FromClipboard
    Take the value from the clipboard instead of prompting, and clear the
    clipboard afterwards so a key does not linger there.

.PARAMETER Value
    For the non-secret settings only.

.EXAMPLE
    .\Set-SurgeSecret.ps1 -Name GROQ_API_KEY
    .\Set-SurgeSecret.ps1 -Name APCA_API_KEY_ID -FromClipboard
    .\Set-SurgeSecret.ps1 -Name GROQ_ZDR_CONFIRMED_ON -Value 2026-09-18
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet(
        "GROQ_API_KEY", "APCA_API_KEY_ID", "APCA_API_SECRET_KEY",
        "GROQ_MODEL", "GROQ_ZDR_CONFIRMED_ON"
    )]
    [string] $Name,

    [switch] $FromClipboard,

    [string] $Value
)

$ErrorActionPreference = "Stop"

$Secrets = @("GROQ_API_KEY", "APCA_API_KEY_ID", "APCA_API_SECRET_KEY")

# The shape each value is documented to have. Checked so that pasting the wrong
# thing - a model id, an old key, half a key - fails now instead of at the first
# 401 at 16:30. Nothing about a secret value is printed either way.
$Shapes = @{
    "GROQ_API_KEY"          = '^gsk_[A-Za-z0-9]{20,}$'
    "APCA_API_KEY_ID"       = '^[A-Z0-9]{16,}$'
    "APCA_API_SECRET_KEY"   = '^[A-Za-z0-9/+]{30,}$'
    "GROQ_MODEL"            = '^[a-z0-9][a-z0-9._/-]{2,80}$'
    "GROQ_ZDR_CONFIRMED_ON" = '^\d{4}-\d{2}-\d{2}$'
}

$isSecret = $Secrets -contains $Name
if ($PSBoundParameters.ContainsKey("Value") -and $isSecret) {
    throw "$Name is a secret and is not accepted on the command line, where it would be kept in the shell history. Use the prompt or -FromClipboard"
}

$dir = Join-Path $env:LOCALAPPDATA "surge\secrets"
New-Item -ItemType Directory -Force -Path $dir | Out-Null

if ($PSBoundParameters.ContainsKey("Value")) {
    $plain = $Value.Trim()
} elseif ($FromClipboard) {
    $plain = Get-Clipboard -Raw
    if ($null -eq $plain) { $plain = "" }
    $plain = $plain.Trim()
} else {
    $prompted = Read-Host -AsSecureString -Prompt "Paste $Name (input is hidden)"
    $plain = [System.Net.NetworkCredential]::new("", $prompted).Password.Trim()
    $prompted = $null
}

try {
    if ($plain.Length -eq 0) {
        throw "nothing was given, so $Name was not stored"
    }
    if ($plain -notmatch $Shapes[$Name]) {
        if ($isSecret) {
            throw "that does not look like a $Name (the value was not stored and is not shown)"
        }
        throw "'$plain' does not look like a $Name, so it was not stored"
    }

    $secure = ConvertTo-SecureString -String $plain -AsPlainText -Force
    # No -Key: on Windows this is DPAPI, scoped to the current user.
    $blob = ConvertFrom-SecureString -SecureString $secure
    Set-Content -Path (Join-Path $dir "$Name.dpapi") -Value $blob -NoNewline -Encoding ascii
}
finally {
    $plain = $null
    $secure = $null
    if ($FromClipboard) { Set-Clipboard -Value " " }
}

if ($isSecret) {
    Write-Host "stored $Name, encrypted to $env:USERNAME on $env:COMPUTERNAME. The value was not printed."
} else {
    Write-Host "stored $Name, encrypted to $env:USERNAME on $env:COMPUTERNAME."
}
