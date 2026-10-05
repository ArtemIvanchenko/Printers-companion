# Compatibility entrypoint: no separate rebase/restart implementation.
param([Parameter(ValueFromRemainingArguments=$true)][string[]]$UpdateArguments)
& (Join-Path (Split-Path $PSScriptRoot -Parent) 'update.ps1') @UpdateArguments
exit $LASTEXITCODE
