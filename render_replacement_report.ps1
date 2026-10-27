$ErrorActionPreference = 'Stop'
$reportFile = 'G:\Codex\交换机测试\报告\WAGO替换Weidmuller综合数据分析与评估_20261026.docx'
$qaDir = 'G:\Codex\交换机测试\out\replacement_analysis_20261026\qa'
New-Item -ItemType Directory -Force -Path $qaDir | Out-Null
$renderScratch = 'C:\Users\LSC\.codex\visualizations\2026\10\26\01a1772b-4365-7051-8e5a-4773dd53c1e7\report_qa'
New-Item -ItemType Directory -Force -Path $renderScratch | Out-Null
$scratchDoc = Join-Path $renderScratch 'switch_assessment.docx'
$pdfFile = Join-Path $renderScratch 'switch_assessment.pdf'
Copy-Item -LiteralPath $reportFile -Destination $scratchDoc
$wordInstance = $null
$reportDocument = $null
try {
    $wordInstance = New-Object -ComObject Word.Application
    Write-Output 'Word initialized'
    $wordInstance.Visible = $false
    $wordInstance.DisplayAlerts = 0
    $wordInstance.AutomationSecurity = 3
    $wordInstance.Options.UpdateLinksAtOpen = $false
    $reportDocument = $wordInstance.Documents.Open($scratchDoc, $false, $true, $false)
    Write-Output 'Report opened'
    $reportDocument.SaveAs2($pdfFile, 17)
    Write-Output 'PDF exported'
    Copy-Item -LiteralPath $pdfFile -Destination (Join-Path $qaDir 'word_render.pdf')
    Write-Output $pdfFile
}
finally {
    if ($null -ne $reportDocument) {
        $reportDocument.Close(0)
        [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($reportDocument)
    }
    if ($null -ne $wordInstance) {
        $wordInstance.Quit()
        [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($wordInstance)
    }
}
