# デスクトップに Anima Prompt Translator のショートカットを作る。
# 右クリック →「PowerShell で実行」、または:  powershell -ExecutionPolicy Bypass -File create_shortcut.ps1
$ErrorActionPreference = 'Stop'

$appDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$target = Join-Path $appDir 'AnimaTranslator.pyw'
$exe    = Join-Path $appDir 'dist\AnimaTranslator.exe'

$pythonw = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
$link = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Anima Prompt Translator.lnk'

$shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($link)
if (Test-Path $exe) {
    # ビルド済みの .exe があればそちらを優先
    $shortcut.TargetPath = $exe
} elseif ($pythonw) {
    $shortcut.TargetPath = $pythonw
    $shortcut.Arguments  = "`"$target`""
} else {
    throw 'pythonw.exe が見つかりません。Python を導入するか build_exe.bat で .exe を作ってください。'
}
$shortcut.WorkingDirectory = $appDir
$shortcut.Description = '日本語プロンプトを Anima 用の英語に翻訳する'
$shortcut.Save()

Write-Host "作成しました: $link"
