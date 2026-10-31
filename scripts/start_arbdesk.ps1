# Starts the ArbDesk panel + scheduler. Asks for the vault master password.
# Autostart at login: Win+R -> shell:startup -> new shortcut with target
#   powershell.exe -NoExit -ExecutionPolicy Bypass -File "C:\Users\<you>\arbdesk\scripts\start_arbdesk.ps1"
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$env:PYTHONUTF8 = "1"
& "$root\.venv\Scripts\Activate.ps1"
$host.UI.RawUI.WindowTitle = "ArbDesk (keep this window open)"
python -m arbdesk panel
