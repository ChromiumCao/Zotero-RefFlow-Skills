param(
    [Parameter(Mandatory=$true)][string]$Document,
    [int]$TimeoutSeconds = 45,
    [string]$Python = 'python'
)
$ErrorActionPreference = 'Stop'
$targetPath = (Resolve-Path -LiteralPath $Document).Path
if ([IO.Path]::GetExtension($targetPath) -ne '.docx') { throw 'A generated DOCX copy is required.' }
if (-not (Test-Path -LiteralPath ($targetPath + '.manifest.json'))) { throw 'Build manifest missing; refresh only a generated output copy.' }
$manifest = Get-Content -Raw -LiteralPath ($targetPath + '.manifest.json') | ConvertFrom-Json
if ((Get-FileHash -LiteralPath $targetPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $manifest.sha256) { throw 'Output changed since build; verify the new version before refreshing.' }
$word = $null
$doc = $null
$created = $false
try {
    try { $word = [Runtime.InteropServices.Marshal]::GetActiveObject('Word.Application') }
    catch { $word = New-Object -ComObject Word.Application; $created = $true }
    if ($word.Name -ne 'Microsoft Word') { throw 'Actual Microsoft Word is required; another compatible application is insufficient.' }
    foreach ($openDoc in $word.Documents) {
        if ($openDoc.FullName -eq $targetPath) { throw 'Output is already open. Save/close that copy or refresh it interactively.' }
    }
    # Leave Word running on error so that any Zotero dialog can be resolved.
    $doc = $word.Documents.Open($targetPath)
    $doc.Activate()
    $word.Run('ZoteroRefresh')
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $hydrated = $false
    do {
        $citationCount = 0
        $hydratedCount = 0
        foreach ($story in $doc.StoryRanges) {
            $range = $story
            while ($null -ne $range) {
                foreach ($field in $range.Fields) {
                    $code = [string]$field.Code.Text
                    if ($code -match 'ZOTERO_ITEM CSL_CITATION') {
                        $citationCount++
                        if ($code -match '"plainCitation"\s*:' -and $code -match '"formattedCitation"\s*:') { $hydratedCount++ }
                    }
                }
                $range = $range.NextStoryRange
            }
        }
        $hydrated = $citationCount -gt 0 -and $citationCount -eq $hydratedCount
        if (-not $hydrated) { Start-Sleep -Milliseconds 500 }
    } while (-not $hydrated -and [DateTime]::UtcNow -lt $deadline)
    if (-not $hydrated) { throw 'Zotero refresh has not produced hydrated citation fields; resolve dialogs in Word. Bibliography-only requires interactive validation.' }
    $doc.Save()
    $doc.Close(0)
    # Word can restore lastModifiedBy and application metadata when it saves.
    $metadataResult = & $Python -X utf8 -B (Join-Path $PSScriptRoot 'zotero_output.py') clean-metadata --document $targetPath
    if ($LASTEXITCODE -ne 0) { throw 'Final document metadata cleanup failed.' }
    $metadataReceipt = ($metadataResult -join "`n") | ConvertFrom-Json
    if (-not $metadataReceipt.metadata_clean) { throw 'Final document metadata was not verified clean.' }
    $receipt = [ordered]@{
        schema_version = 'zotero-output.word-refresh/1'
        document = $targetPath
        sha256 = (Get-FileHash -LiteralPath $targetPath -Algorithm SHA256).Hash.ToLowerInvariant()
        citation_fields = $citationCount
        hydrated_fields = $hydratedCount
        word_version = [string]$word.Version
        macro_returned = $true
        metadata_clean = $true
        library_links_verified = $false
        requires = 'Run structural verification again and check Open in My Library in Zotero citation UI.'
    }
    $receipt | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath ($targetPath + '.word-refresh.json') -Encoding utf8
    if ($created) { $word.Quit() }
    $receipt | ConvertTo-Json -Depth 5
}
catch {
    Write-Error ($_.Exception.Message + ' Word and its generated document are left open for inspection.')
    exit 2
}
finally {
    if ($null -ne $doc) { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($doc) }
    if ($null -ne $word) { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($word) }
}
