<#
.SYNOPSIS
  Despliega annie en el servidor de AWS creado por infra/ (Terraform).

.DESCRIPTION
  1. Construye las imágenes Docker de la API y del panel.
  2. Las sube a ECR con el tag :latest y el commit actual.
  3. Le pide al servidor, vía SSM Run Command (sin SSH), que corra
     /opt/annie/refresh.sh: arma el .env desde SSM, baja las imágenes,
     corre las migraciones y reinicia los contenedores.

.EXAMPLE
  .\deploy\deploy.ps1              # construir, subir y desplegar
  .\deploy\deploy.ps1 -SkipBuild   # solo reiniciar con lo que ya está en ECR
#>
param(
    [switch]$SkipBuild
)

$ErrorActionPreference = 'Stop'

function Invoke-Checked {
    param([scriptblock]$Command, [string]$What)
    & $Command
    if ($LASTEXITCODE -ne 0) { throw "Falló: $What (código $LASTEXITCODE)" }
}

function Get-TfOutput {
    param([string]$Name)
    $value = terraform -chdir=infra output -raw $Name
    if ($LASTEXITCODE -ne 0 -or -not $value) {
        throw "No se pudo leer '$Name' de Terraform. ¿Corriste 'terraform -chdir=infra apply'?"
    }
    return $value
}

$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    $region   = Get-TfOutput 'region'
    $apiRepo  = Get-TfOutput 'ecr_api_url'
    $panelRepo = Get-TfOutput 'ecr_panel_url'
    $instance = Get-TfOutput 'instance_id'
    $registry = $apiRepo.Split('/')[0]
    $tag      = (git rev-parse --short HEAD).Trim()

    if (-not $SkipBuild) {
        Write-Host "==> Login en ECR ($registry)"
        Invoke-Checked { aws ecr get-login-password --region $region | docker login --username AWS --password-stdin $registry } 'login en ECR'

        foreach ($target in @(@{ Name = 'api'; Repo = $apiRepo }, @{ Name = 'panel'; Repo = $panelRepo })) {
            $repo = $target.Repo
            Write-Host "==> Construyendo $($target.Name) ($tag)"
            Invoke-Checked { docker build --platform linux/amd64 --target $target.Name -t "${repo}:latest" -t "${repo}:$tag" . } "build de $($target.Name)"
            Write-Host "==> Subiendo $($target.Name)"
            Invoke-Checked { docker push "${repo}:latest" } "push de $($target.Name):latest"
            Invoke-Checked { docker push "${repo}:$tag" } "push de $($target.Name):$tag"
        }
    }

    Write-Host "==> Desplegando en $instance vía SSM"
    $commandId = aws ssm send-command `
        --region $region `
        --instance-ids $instance `
        --document-name AWS-RunShellScript `
        --comment "annie deploy $tag" `
        --parameters commands=/opt/annie/refresh.sh `
        --query 'Command.CommandId' --output text
    if ($LASTEXITCODE -ne 0) { throw 'No se pudo enviar el comando SSM' }

    # El waiter falla si el comando falla: igual se muestra la salida.
    # (Sin redirigir stderr: en Windows PowerShell 5.1, "2>" convierte cada línea
    # en un error y, con ErrorActionPreference=Stop, corta el script.)
    aws ssm wait command-executed --region $region --command-id $commandId --instance-id $instance
    $ok = ($LASTEXITCODE -eq 0)

    aws ssm get-command-invocation `
        --region $region `
        --command-id $commandId `
        --instance-id $instance `
        --query '[Status,StandardOutputContent,StandardErrorContent]' `
        --output text

    if (-not $ok) { throw 'El despliegue falló en el servidor (ver la salida de arriba).' }

    Write-Host ''
    Write-Host "Listo: $(Get-TfOutput 'public_url')"
}
finally {
    Pop-Location
}
