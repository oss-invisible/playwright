# The counters of the machine, one row a second, for as long as the hunt runs (e2e-hunt.yml, job goto-after-launch):
#   \PhysicalDisk(*)\Avg. Disk sec/Transfer, \PhysicalDisk(*)\Current Disk Queue Length, \Processor(_Total)\% Processor Time,
#   \Process(MsMpEng)\% Processor Time (the antivirus, when it runs) and \Memory\Available MBytes.
# Each row is written and closed as it is taken, so that the file is whole whenever the process is stopped (typeperf's own output is
# buffered and loses its tail when it is killed). Columns are the counter paths of the first sample, without the machine's name; an
# instance that appears later is not added (a hunt's disks do not change), one that is missing is left empty.
param([Parameter(Mandatory = $true)][string]$OutFile)
$ErrorActionPreference = 'Stop'
$paths = @('\PhysicalDisk(*)\Avg. Disk sec/Transfer', '\PhysicalDisk(*)\Current Disk Queue Length',
           '\Processor(_Total)\% Processor Time', '\Memory\Available MBytes')
if (Get-Process -Name MsMpEng -ErrorAction SilentlyContinue) { $paths += '\Process(MsMpEng)\% Processor Time' }
$columns = $null
Get-Counter -Counter $paths -SampleInterval 1 -Continuous | ForEach-Object {
    $row = [ordered]@{}
    foreach ($s in $_.CounterSamples) {
        $row[($s.Path -replace '^\\\\[^\\]+', '')] = [string]::Format([cultureinfo]::InvariantCulture, '{0:G6}', $s.CookedValue)
    }
    if ($null -eq $columns) {
        $columns = @($row.Keys)
        [IO.File]::AppendAllText($OutFile, '"time","' + ($columns -join '","') + '"' + "`r`n")
    }
    $cells = foreach ($c in $columns) { if ($row.Contains($c)) { $row[$c] } else { '' } }
    [IO.File]::AppendAllText($OutFile, '"' + $_.Timestamp.ToUniversalTime().ToString('o') + '","' + ($cells -join '","') + '"' + "`r`n")
}
