# Livescribe Echo -> WSL2 USB bridge (run in an ADMIN PowerShell on Windows)
# Shares the pen with usbipd and attaches it to the default WSL distro.
$usbipd = 'C:\Program Files\usbipd-win\usbipd.exe'

if (-not (Test-Path $usbipd)) {
    Write-Host "usbipd-win is not installed ($usbipd). Install it from https://github.com/dorssel/usbipd-win"
    exit 1
}

$busid = (& $usbipd list |
    Select-String -Pattern '1cfb:103[012]\s' |
    ForEach-Object { ($_ -split '\s+')[0] } |
    Select-Object -First 1)

if (-not $busid) {
    Write-Host "No Livescribe Echo pen found. Make sure it is plugged in with a DATA cable and powered on."
    exit 1
}
Write-Host "Found pen at busid $busid. Sharing and attaching..."

& $usbipd bind --force -b $busid
if ($LASTEXITCODE -ne 0) {
    Write-Host "usbipd bind failed (exit $LASTEXITCODE). Is this PowerShell running as Administrator?"
    exit 1
}

& $usbipd attach --wsl -b $busid
if ($LASTEXITCODE -ne 0) {
    Write-Host "usbipd attach failed (exit $LASTEXITCODE). Is WSL running? If the pen is already attached, 'lsusb' in WSL will show it."
    exit 1
}

Write-Host "Done. Verify inside WSL with: lsusb"
