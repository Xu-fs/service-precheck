param([string]$SourceRoot = (Join-Path $PSScriptRoot '..\作业提交'), [string]$NameFilter = '*')
$ErrorActionPreference = 'Stop'
$taskProject = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$taskRender = Join-Path $taskProject '.venv\assignment-qa\render'
New-Item -ItemType Directory -Force $taskRender | Out-Null
$taskWord = New-Object -ComObject Word.Application
$taskWord.Visible = $false
$taskWord.DisplayAlerts = 0
$taskResults = @()
if ($NameFilter -ne '*' -and (Test-Path -LiteralPath (Join-Path $taskRender 'render-index.json'))) {
    $taskResults = @(Get-Content -LiteralPath (Join-Path $taskRender 'render-index.json') -Raw | ConvertFrom-Json | Where-Object { [IO.Path]::GetFileNameWithoutExtension($_.source) -notlike $NameFilter })
}
try {
    $taskDocuments = Get-ChildItem -LiteralPath $SourceRoot -Filter '*.docx' -File -Recurse | Where-Object { $_.FullName -notmatch '[\\/]MVP项目[\\/]' -and $_.BaseName -like $NameFilter }
    foreach ($taskFile in $taskDocuments) {
        $taskOutput = Join-Path $taskRender $taskFile.BaseName
        New-Item -ItemType Directory -Force $taskOutput | Out-Null
        $taskPdf = Join-Path $taskOutput ($taskFile.BaseName + '.pdf')
        $taskDocument = $null
        try {
            $taskDocument = $taskWord.Documents.Open($taskFile.FullName, $false, $true, $false)
            $taskDocument.ExportAsFixedFormat($taskPdf, 17)
            $taskResults += [PSCustomObject]@{ source=$taskFile.FullName; pdf=$taskPdf; status='exported' }
            Write-Output ('Exported ' + $taskFile.Name)
        } finally {
            if ($null -ne $taskDocument) { $taskDocument.Close(0); [Runtime.InteropServices.Marshal]::ReleaseComObject($taskDocument) | Out-Null }
        }
    }
} finally {
    $taskWord.Quit()
    [Runtime.InteropServices.Marshal]::ReleaseComObject($taskWord) | Out-Null
}
$taskResults | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $taskRender 'render-index.json') -Encoding utf8
