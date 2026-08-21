# Authenticode-sign Windows artifacts. The counterpart to sign-macos.sh.
#
# Signing is OPTIONAL and this script is a no-op without a certificate, so
# the build keeps working for anyone who forks the repo, opens a pull
# request, or runs it locally. Only the release workflow on the upstream
# repo will ever have the secrets.
#
# Configure by setting two environment variables (GitHub: repository
# secrets, mapped in .github/workflows/build.yml):
#
#   WINDOWS_CERT_PFX_BASE64   the .pfx, base64-encoded
#   WINDOWS_CERT_PASSWORD     its password
#
# What signing buys, concretely: Windows stops calling the publisher
# "Unknown", and SmartScreen starts accumulating reputation against the
# certificate rather than against each individual file — so a new release
# inherits the trust the previous ones earned. Note that this is a slow
# accrual: since Microsoft removed EV's instant-pass in 2024, no
# certificate grants an immediate clean SmartScreen verdict.
#
# Usage:  ./sign-windows.ps1 file1.exe file2.exe ...

[CmdletBinding()]
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Files)

$ErrorActionPreference = 'Stop'

if (-not $Files) { throw 'usage: sign-windows.ps1 <file> [file ...]' }

if (-not $env:WINDOWS_CERT_PFX_BASE64) {
    Write-Host 'sign-windows: no certificate configured — leaving artifacts unsigned.'
    Write-Host '              (set WINDOWS_CERT_PFX_BASE64 / WINDOWS_CERT_PASSWORD to enable)'
    exit 0
}

# signtool lives in the Windows SDK, whose directory carries the SDK build
# number — hard-coding one version breaks the day the runner image moves.
$signtool = Get-ChildItem 'C:\Program Files (x86)\Windows Kits\10\bin' `
    -Filter signtool.exe -Recurse -ErrorAction SilentlyContinue |
    Where-Object { $_.DirectoryName -match '\\x64$' } |
    Sort-Object FullName -Descending | Select-Object -First 1
if (-not $signtool) { throw 'signtool.exe not found — is the Windows SDK installed?' }

$pfx = Join-Path ([System.IO.Path]::GetTempPath()) 'vpad-signing.pfx'
try {
    [IO.File]::WriteAllBytes($pfx, [Convert]::FromBase64String($env:WINDOWS_CERT_PFX_BASE64))

    foreach ($f in $Files) {
        if (-not (Test-Path $f)) { throw "nothing to sign at $f" }
        Write-Host "sign-windows: signing $f"
        # /fd + /td sha256: SHA-1 file digests are rejected outright by
        # current Windows. /tr is an RFC3161 timestamp, and it is not
        # optional — without it every signature stops validating the day
        # the certificate expires, including on copies already installed.
        & $signtool.FullName sign `
            /f $pfx /p $env:WINDOWS_CERT_PASSWORD `
            /fd sha256 /tr http://timestamp.digicert.com /td sha256 `
            /d 'V-Pad Helper' /du 'https://vpadcontroller.com/' `
            $f
        if ($LASTEXITCODE -ne 0) { throw "signtool failed on $f (exit $LASTEXITCODE)" }
    }

    foreach ($f in $Files) {
        & $signtool.FullName verify /pa /v $f | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "signature does not verify on $f" }
    }
    Write-Host "sign-windows: $($Files.Count) file(s) signed and verified."
}
finally {
    # The .pfx is the private key. Leaving it on a runner's disk between
    # steps is the kind of thing that ends up in a cache or an artifact.
    if (Test-Path $pfx) { Remove-Item $pfx -Force }
}
