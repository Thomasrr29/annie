# ----------------------------------------------------------------------
# S3: imágenes que mandan los clientes por WhatsApp.
# Privado, cifrado y con borrado automático (retención, Ley 1581).
# ----------------------------------------------------------------------
resource "aws_s3_bucket" "media" {
  bucket = "${local.name}-media-${local.account_id}"
}

resource "aws_s3_bucket_public_access_block" "media" {
  bucket                  = aws_s3_bucket.media.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "media" {
  bucket = aws_s3_bucket.media.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "media" {
  bucket = aws_s3_bucket.media.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "media" {
  bucket = aws_s3_bucket.media.id

  rule {
    id     = "retencion-imagenes"
    status = "Enabled"

    filter {
      prefix = "media/"
    }

    expiration {
      days = var.media_retention_days
    }
  }
}

# ----------------------------------------------------------------------
# ECR: dónde viven las imágenes Docker de la API y del panel.
# ----------------------------------------------------------------------
resource "aws_ecr_repository" "api" {
  name                 = "${local.name}-api"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_repository" "panel" {
  name                 = "${local.name}-panel"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

# Guardar solo las últimas 5 imágenes de cada repositorio (costo).
locals {
  ecr_keep_last_5 = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Conservar solo las ultimas 5 imagenes"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 5
      }
      action = { type = "expire" }
    }]
  })
}

resource "aws_ecr_lifecycle_policy" "api" {
  repository = aws_ecr_repository.api.name
  policy     = local.ecr_keep_last_5
}

resource "aws_ecr_lifecycle_policy" "panel" {
  repository = aws_ecr_repository.panel.name
  policy     = local.ecr_keep_last_5
}
