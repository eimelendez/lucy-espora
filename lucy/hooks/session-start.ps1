# session-start.ps1 — envoltorio del gancho de arranque de Lucy.
#
# Lo ejecuta el bridge dsh-hooks-claude-code como comando del evento SessionStart:
#   pwsh -NoLogo -NoProfile -NonInteractive -Command "<esto>"
# El cwd es el workspace de la sesión y CLAUDE_PROJECT_DIR apunta al mismo sitio.
#
# Reglas:
#  - Si el workspace no tiene lucy/lucy.py, no hace nada (exit 0, sin salida).
#  - Resuelve un intérprete de Python en este orden: $env:LUCY_PYTHON, python del
#    PATH, runtime empaquetado de DSH, py.exe.
#  - Delega todo el cálculo en session_start.py; aquí solo se localiza Python.
#  - Nunca escribe en stderr ni falla con exit != 0 (un hook roto no debe romper
#    el arranque de la sesión).

$ErrorActionPreference = 'Stop'

try {
    $root = if ($env:CLAUDE_PROJECT_DIR) { $env:CLAUDE_PROJECT_DIR } else { (Get-Location).Path }
    $script = Join-Path $root 'lucy\hooks\session_start.py'
    $kernel = Join-Path $root 'lucy\lucy.py'

    if (-not (Test-Path -LiteralPath $script) -or -not (Test-Path -LiteralPath $kernel)) {
        exit 0
    }

    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

    $py = $null
    $pyArgs = @()

    if ($env:LUCY_PYTHON -and (Test-Path -LiteralPath $env:LUCY_PYTHON)) {
        $py = $env:LUCY_PYTHON
    }

    if (-not $py) {
        $cmd = Get-Command python.exe -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($cmd -and $cmd.Source -and (Test-Path -LiteralPath $cmd.Source)) { $py = $cmd.Source }
    }

    if (-not $py -and $env:USERPROFILE) {
        $candidates = Get-ChildItem -Path (Join-Path $env:USERPROFILE '.dsh\dsh-runtimes\*\dependencies\python\python.exe') -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending
        if ($candidates) { $py = $candidates[0].FullName }
    }

    if (-not $py) {
        $pyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($pyLauncher -and $pyLauncher.Source) { $py = $pyLauncher.Source; $pyArgs = @('-3') }
    }

    if (-not $py) {
        $msg = '<lucy-boot hook="SessionStart">Gancho de arranque sin Python: no se pudo localizar un intérprete (LUCY_PYTHON, PATH, runtime de DSH, py.exe). Sin telemetría verificada.</lucy-boot>'
        $payload = @{ hookSpecificOutput = @{ hookEventName = 'SessionStart'; additionalContext = $msg } } | ConvertTo-Json -Depth 5 -Compress
        [Console]::Out.Write($payload)
        exit 0
    }

    # El payload del hook llega por stdin; se reenvía (session_start.py lo ignora).
    $stdin = [Console]::In.ReadToEnd()
    $stdin | & $py @pyArgs $script
}
catch {
    exit 0
}

exit 0
