param(
    [Parameter(Mandatory = $true)]
    [string]$ReleaseDirectory
)

$ErrorActionPreference = "Stop"
$resourcesDirectory = Join-Path $ReleaseDirectory "_internal\PySide6\resources"
if (-not (Test-Path -LiteralPath $resourcesDirectory)) {
    throw "未找到 PySide6 WebEngine 资源目录：$resourcesDirectory"
}

# Qt release builds never load Chromium's debug resource bundles. Keep every
# non-debug bundle so embedded YouTube login remains identical to the source run.
$debugFiles = Get-ChildItem -LiteralPath $resourcesDirectory -File -Filter "*.debug.*"
$removedBytes = ($debugFiles | Measure-Object Length -Sum).Sum
foreach ($file in $debugFiles) {
    Remove-Item -LiteralPath $file.FullName -Force
}

[pscustomobject]@{
    RemovedFiles = $debugFiles.Count
    RemovedMB = [math]::Round(($removedBytes / 1MB), 1)
    ReleaseDirectory = $ReleaseDirectory
} | Format-List
