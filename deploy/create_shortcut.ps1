# Printer's Companion — create desktop shortcut
# Run once: .\deploy\create_shortcut.ps1

$ErrorActionPreference = 'Stop'
$ProjectDir = Split-Path -Parent $PSScriptRoot

Add-Type -AssemblyName System.Drawing

function New-AppIcon {
    param([string]$OutputPath)

    $sizes = @(256, 48, 32, 16)
    $bitmaps = @()

    foreach ($sz in $sizes) {
        $bmp = New-Object System.Drawing.Bitmap $sz, $sz
        $g   = [System.Drawing.Graphics]::FromImage($bmp)
        $g.SmoothingMode      = 'AntiAlias'
        $g.CompositingQuality = 'HighQuality'

        $g.Clear([System.Drawing.Color]::FromArgb(255, 15, 23, 42))

        $r      = [int]($sz * 0.14)
        $bgBrush = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(255, 30, 41, 59))
        $path   = New-Object System.Drawing.Drawing2D.GraphicsPath
        $path.AddArc(0, 0, $r*2, $r*2, 180, 90)
        $path.AddArc($sz - $r*2, 0, $r*2, $r*2, 270, 90)
        $path.AddArc($sz - $r*2, $sz - $r*2, $r*2, $r*2, 0, 90)
        $path.AddArc(0, $sz - $r*2, $r*2, $r*2, 90, 90)
        $path.CloseFigure()
        $g.FillPath($bgBrush, $path)

        $cyan  = [System.Drawing.Color]::FromArgb(255, 34, 211, 238)
        $white = [System.Drawing.Color]::FromArgb(255, 241, 245, 249)
        $green = [System.Drawing.Color]::FromArgb(255, 74, 222, 128)
        $cBrush = New-Object System.Drawing.SolidBrush($cyan)
        $wBrush = New-Object System.Drawing.SolidBrush($white)
        $gBrush = New-Object System.Drawing.SolidBrush($green)

        $m  = [int]($sz * 0.12)
        $pw = $sz - 2 * $m

        # Input paper (top)
        $paperW = [int]($pw * 0.62)
        $paperX = $m + [int](($pw - $paperW) / 2)
        $paperH = [int]($sz * 0.20)
        $paperY = [int]($sz * 0.18)
        $g.FillRectangle($wBrush, $paperX, $paperY, $paperW, $paperH)

        # Printer body
        $bodyH = [int]($sz * 0.25)
        $bodyY = $paperY + $paperH - [int]($sz * 0.04)
        $g.FillRectangle($cBrush, $m, $bodyY, $pw, $bodyH)

        # LED indicator
        if ($sz -ge 32) {
            $ledSz = [int]($sz * 0.07)
            $ledX  = $sz - $m - $ledSz - [int]($sz * 0.05)
            $ledY  = $bodyY + [int]($bodyH * 0.25)
            $g.FillEllipse($gBrush, $ledX, $ledY, $ledSz, $ledSz)
        }

        # Output paper (bottom)
        $outW = $paperW
        $outX = $paperX
        $outH = [int]($sz * 0.18)
        $outY = $bodyY + $bodyH - [int]($sz * 0.03)
        $g.FillRectangle($wBrush, $outX, $outY, $outW, $outH)

        # Print lines on output paper
        if ($sz -ge 32) {
            $lw   = [float]([Math]::Max(1, $sz / 64))
            $lPen = New-Object System.Drawing.Pen([System.Drawing.Color]::FromArgb(160, 34, 211, 238), $lw)
            $lx1  = $outX + [int]($outW * 0.12)
            $lx2  = $outX + [int]($outW * 0.88)
            $ly   = $outY + [int]($outH * 0.28)
            $g.DrawLine($lPen, $lx1, $ly, $lx2, $ly)
            $ly   = $outY + [int]($outH * 0.55)
            $g.DrawLine($lPen, $lx1, $ly, ($lx1 + [int](($lx2-$lx1)*0.65)), $ly)
            $lPen.Dispose()
        }

        # Bar chart (large sizes only)
        if ($sz -ge 48) {
            $barW    = [int]($sz * 0.055)
            $barBaseX = $outX + $outW + [int]($sz * 0.04)
            $baseY   = $outY + $outH
            $heights = @([int]($sz*0.10), [int]($sz*0.17), [int]($sz*0.24))
            for ($bi = 0; $bi -lt 3; $bi++) {
                $alpha    = 80 + $bi * 70
                $barColor = [System.Drawing.Color]::FromArgb($alpha, 34, 211, 238)
                $barBrush = New-Object System.Drawing.SolidBrush($barColor)
                $bx = $barBaseX + $bi * ($barW + [int]($sz * 0.02))
                $bh = $heights[$bi]
                $g.FillRectangle($barBrush, $bx, ($baseY - $bh), $barW, $bh)
                $barBrush.Dispose()
            }
        }

        $g.Dispose()
        $bitmaps += $bmp
    }

    # Write multi-size ICO (PNG-in-ICO, Vista+)
    $pngList = @()
    foreach ($b in $bitmaps) {
        $ms = New-Object System.IO.MemoryStream
        $b.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
        $pngList += ,$ms.ToArray()
        $ms.Dispose()
    }

    $count  = $pngList.Count
    $offset = 6 + 16 * $count

    $fs = [System.IO.File]::Open($OutputPath, 'Create', 'Write')
    $bw = New-Object System.IO.BinaryWriter($fs)

    $bw.Write([uint16]0)
    $bw.Write([uint16]1)
    $bw.Write([uint16]$count)

    for ($i = 0; $i -lt $count; $i++) {
        $sz = $sizes[$i]
        $w  = if ($sz -eq 256) { 0 } else { [byte]$sz }
        $h  = if ($sz -eq 256) { 0 } else { [byte]$sz }
        $bw.Write([byte]$w)
        $bw.Write([byte]$h)
        $bw.Write([byte]0)
        $bw.Write([byte]0)
        $bw.Write([uint16]1)
        $bw.Write([uint16]32)
        $bw.Write([uint32]$pngList[$i].Length)
        $bw.Write([uint32]$offset)
        $offset += $pngList[$i].Length
    }

    foreach ($data in $pngList) { $bw.Write($data) }
    $bw.Close()
    $fs.Close()
    foreach ($b in $bitmaps) { $b.Dispose() }
}

# Generate icon
$iconPath = Join-Path $ProjectDir "deploy\printer_companion.ico"
Write-Host "Generating icon..." -ForegroundColor Cyan
New-AppIcon -OutputPath $iconPath
Write-Host "  Icon saved: $iconPath" -ForegroundColor Green

# Create shortcut
$launchScript  = Join-Path $ProjectDir "deploy\launch.ps1"
$shortcutPath  = Join-Path ([Environment]::GetFolderPath('Desktop')) "Printer Companion.lnk"

$wsh = New-Object -ComObject WScript.Shell
$sc  = $wsh.CreateShortcut($shortcutPath)
$sc.TargetPath       = "powershell.exe"
$sc.Arguments        = "-ExecutionPolicy Bypass -NoProfile -File `"$launchScript`""
$sc.WorkingDirectory = $ProjectDir
$sc.IconLocation     = "$iconPath,0"
$sc.Description      = "Printer Companion - open dashboard"
$sc.WindowStyle      = 1
$sc.Save()

Write-Host "`nShortcut created: $shortcutPath" -ForegroundColor Green
Write-Host "Done! Icon is on the desktop." -ForegroundColor Cyan