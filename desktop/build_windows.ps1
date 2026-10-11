param(
    [string]$Repository = 'pumpkissster/Gravelle',
    [string]$OutputDirectory = 'dist/windows',
    [string]$Python = 'python',
    [string]$InnoCompiler = '',
    [switch]$SkipDependencyInstall,
    [switch]$PortableOnly
)

$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if ($Repository -notmatch '^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]+$') {
    throw 'Repository must be a GitHub owner/repository name.'
}
function Resolve-ProjectOutput([string]$Value) {
    $absolute = [IO.Path]::GetFullPath((Join-Path $projectRoot $Value))
    if (-not $absolute.StartsWith($projectRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Build output must stay inside this project.'
    }
    return $absolute
}
$artifactDirectory = Resolve-ProjectOutput $OutputDirectory
$buildDirectory = Resolve-ProjectOutput 'work/windows-build'
$portableDirectory = Join-Path $artifactDirectory 'portable'
New-Item -ItemType Directory -Path $artifactDirectory, $buildDirectory, $portableDirectory -Force | Out-Null
Push-Location $projectRoot
try {
    & $Python -c 'import sys; assert sys.platform == "win32" and sys.version_info >= (3, 12); assert sys.maxsize > 2**32'
    if ($LASTEXITCODE -ne 0) { throw 'Use 64-bit Python 3.12 or newer on Windows.' }
    if (-not $SkipDependencyInstall) {
        $venvDirectory = Join-Path $buildDirectory 'venv'
        if (-not (Test-Path -LiteralPath (Join-Path $venvDirectory 'Scripts/python.exe'))) {
            & $Python -m venv $venvDirectory
            if ($LASTEXITCODE -ne 0) { throw 'Unable to create build environment.' }
        }
        $Python = Join-Path $venvDirectory 'Scripts/python.exe'
        & $Python -m pip install --disable-pip-version-check -r (Join-Path $PSScriptRoot 'requirements-build.txt')
        if ($LASTEXITCODE -ne 0) { throw 'Unable to install build dependencies.' }
    }

    $prepareCode = @'
from pathlib import Path
import ast, io, json, sys
from PIL import Image
import resvg_py
root=Path.cwd()
tree=ast.parse((root/'server.py').read_text(encoding='utf-8'))
version=next(ast.literal_eval(node.value) for node in tree.body if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='VERSION' for t in node.targets))
repo=sys.argv[1]
build=Path(sys.argv[2])
config={'repository':repo,'asset_name':'Gravelle-Setup-{version}-x64.exe','sha256_asset_name':'SHA256SUMS.txt'}
(build/'desktop-release.json').write_text(json.dumps(config,indent=2)+'\n',encoding='utf-8')
(build/'release.json').write_text(json.dumps({'repository':repo},indent=2)+'\n',encoding='utf-8')
with Image.open(root/'static/gravelle-icon.png') as image:
    image.save(root/'desktop/gravelle.ico',sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])
# Render from the supplied SVG on every build, keeping the checked-in source
# launcher asset and frozen resources in sync without a runtime SVG dependency.
wordmark=root/'desktop/assets/gravelle-wordmark.png'
wordmark.parent.mkdir(exist_ok=True)
png=resvg_py.svg_to_bytes(svg_path=str(root/'static/gravelle-wordmark.svg'),width=600,skip_system_fonts=True)
with Image.open(io.BytesIO(png)) as image:
    if image.mode!='RGBA' or image.width!=600 or image.getextrema()[3]!=(0,255):
        raise ValueError('The Gravelle wordmark must be a transparent RGBA PNG, 600 pixels wide.')
wordmark.write_bytes(png)
(build/'version.txt').write_text(version,encoding='ascii')
print('Preparing Gravelle '+version+' for '+repo)
'@
    & $Python -c $prepareCode $Repository $buildDirectory
    if ($LASTEXITCODE -ne 0) { throw 'Unable to prepare resources.' }
    $version = (Get-Content -LiteralPath (Join-Path $buildDirectory 'version.txt') -Raw).Trim()
    if ($version -notmatch '^\d+\.\d+\.\d+$') { throw 'Invalid application version.' }
    $desktopResourceDirectory = Join-Path $buildDirectory 'resources/desktop'
    New-Item -ItemType Directory -Path $desktopResourceDirectory -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $buildDirectory 'desktop-release.json') -Destination (Join-Path $desktopResourceDirectory 'release.json') -Force
    Copy-Item -LiteralPath (Join-Path $buildDirectory 'release.json') -Destination (Join-Path $buildDirectory 'resources/release.json') -Force
    $previousResources = $env:GRAVELLE_BUILD_RESOURCES
    try {
        $env:GRAVELLE_BUILD_RESOURCES = Join-Path $buildDirectory 'resources'
        & $Python -m PyInstaller --noconfirm --clean --distpath $portableDirectory --workpath (Join-Path $buildDirectory 'pyinstaller') (Join-Path $PSScriptRoot 'gravelle.spec')
    } finally {
        $env:GRAVELLE_BUILD_RESOURCES = $previousResources
    }
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }
    $bundleDirectory = Join-Path $portableDirectory 'Gravelle'
    Copy-Item -LiteralPath (Join-Path $projectRoot 'LICENSE') -Destination $bundleDirectory -Force
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'THIRD_PARTY_NOTICES.md') -Destination $bundleDirectory -Force
    $licenseCode = @'
from pathlib import Path
import importlib.metadata, shutil, sys
target=Path(sys.argv[1])/'licenses'
target.mkdir(exist_ok=True)
python_license=Path(sys.base_prefix)/'LICENSE.txt'
if python_license.exists(): shutil.copyfile(python_license,target/'Python-LICENSE.txt')
for name in ('Pillow','PyInstaller'):
    dist=importlib.metadata.distribution(name)
    for entry in dist.files or []:
        if any(word in entry.name.lower() for word in ('license','copying')) and entry.suffix.lower() in ('.txt','.md',''):
            source=Path(dist.locate_file(entry))
            if source.is_file(): shutil.copyfile(source,target/(name+'-'+entry.name))
'@
    & $Python -c $licenseCode $bundleDirectory
    if ($LASTEXITCODE -ne 0) { throw 'Unable to collect license notices.' }
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'languages/LICENSE-Inno-Setup.txt') -Destination (Join-Path $bundleDirectory 'licenses/Inno-Setup-LICENSE.txt') -Force
    $archivePath = Join-Path $artifactDirectory ("Gravelle-Windows-$version-x64.zip")
    # zipfile handles Unicode paths and files larger than Compress-Archive's limit.
    & $Python -c 'from pathlib import Path; import sys,zipfile; root=Path(sys.argv[1]); output=Path(sys.argv[2]); z=zipfile.ZipFile(output,"w",zipfile.ZIP_DEFLATED); [z.write(p,"Gravelle/"+p.relative_to(root).as_posix()) for p in sorted(root.rglob("*")) if p.is_file()]; z.close()' $bundleDirectory $archivePath
    if ($LASTEXITCODE -ne 0) { throw 'Unable to package the portable archive.' }

    if (-not $PortableOnly) {
        if (-not $InnoCompiler) {
            $compilerCommand = Get-Command ISCC.exe -ErrorAction SilentlyContinue
            if ($compilerCommand) { $InnoCompiler = $compilerCommand.Source }
            foreach ($candidate in @(
                (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6/ISCC.exe'),
                (Join-Path $env:ProgramFiles 'Inno Setup 7/ISCC.exe'),
                (Join-Path $projectRoot 'work/tools/inno/ISCC.exe')
            )) {
                if (-not $InnoCompiler -and (Test-Path -LiteralPath $candidate)) { $InnoCompiler = $candidate }
            }
        }
        if (-not $InnoCompiler -or -not (Test-Path -LiteralPath $InnoCompiler)) {
            throw 'Install Inno Setup or pass -InnoCompiler. Use -PortableOnly only for a portable development build.'
        }
        & $InnoCompiler '/Qp' "/DAppVersion=$version" "/DBundleDirectory=$bundleDirectory" "/DArtifactDirectory=$artifactDirectory" "/DRepository=$Repository" (Join-Path $PSScriptRoot 'installer.iss')
        if ($LASTEXITCODE -ne 0) { throw 'Inno Setup compilation failed.' }
    }
    $checksumLines = @(Get-ChildItem -LiteralPath $artifactDirectory -File | Where-Object { $_.Name -match ('^Gravelle-(Setup|Windows)-' + [regex]::Escape($version) + '-x64\.(exe|zip)$') } | Sort-Object Name | ForEach-Object {
        (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant() + '  ' + $_.Name
    })
    [IO.File]::WriteAllText((Join-Path $artifactDirectory 'SHA256SUMS.txt'), ($checksumLines -join "`n") + "`n", [Text.UTF8Encoding]::new($false))
    Write-Output "Windows release files: $artifactDirectory"
} finally {
    Pop-Location
}
