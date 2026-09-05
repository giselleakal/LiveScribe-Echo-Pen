# Livescribe Echo -> WSL2 USB bridge (run in an ADMIN PowerShell on Windows)
# Shares the pen with usbipd and attaches it to the default WSL distro.
$usbipd = 'C:\Program Files\usbipd-win\usbipd.exe'

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
& $usbipd attach --wsl -b $busid

Write-Host "Done. Verify inside WSL with: lsusb"