# Arquitectura de la demo (bloque 7):
#
#   Internet ──► IP elástica ──► EC2 (Amazon Linux 2023, Docker Compose)
#                                  ├─ caddy   :80/:443  HTTPS automático
#                                  ├─ api     FastAPI (webhook de WhatsApp + /admin)
#                                  ├─ panel   Streamlit en /panel
#                                  ├─ db      Postgres 16 + pgvector (volumen local)
#                                  └─ migrate alembic upgrade head (corre y termina)
#   S3 (imágenes) · SES (avisos al asesor) · ECR (imágenes Docker) · SSM (secretos)
#   AWS Budgets (alerta de gasto)
#
# Sin NAT Gateway ni balanceador: son las dos piezas que más gastan en una
# cuenta pequeña. Sin SSH: se entra y se despliega con SSM.

data "aws_caller_identity" "current" {}

data "aws_availability_zones" "available" {
  state = "available"
}

# Amazon Linux 2023, la última publicada por AWS.
data "aws_ssm_parameter" "al2023" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

locals {
  name       = var.project
  account_id = data.aws_caller_identity.current.account_id
  domain     = var.domain != "" ? var.domain : "${replace(aws_eip.this.public_ip, ".", "-")}.sslip.io"
  ssm_prefix = "/${var.project}"
}

# ----------------------------------------------------------------------
# Red: una VPC propia con una sola subred pública (sin NAT).
# ----------------------------------------------------------------------
resource "aws_vpc" "this" {
  cidr_block           = "10.20.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = "${local.name}-vpc" }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "${local.name}-igw" }
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.this.id
  cidr_block              = "10.20.1.0/24"
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = false # la IP pública es la elástica
  tags                    = { Name = "${local.name}-public" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }

  tags = { Name = "${local.name}-public" }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

# Solo HTTP (para el certificado y la redirección) y HTTPS. Sin puerto 22.
resource "aws_security_group" "web" {
  name        = "${local.name}-web"
  description = "HTTP/HTTPS publico para el webhook de WhatsApp y el panel"
  vpc_id      = aws_vpc.this.id

  ingress {
    description = "HTTP (Let's Encrypt + redireccion a HTTPS)"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "HTTPS"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "Salida a internet: Meta, Anthropic y servicios de AWS"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# ----------------------------------------------------------------------
# Identidad del servidor: mínimo privilegio.
# ----------------------------------------------------------------------
data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "server" {
  name               = "${local.name}-server"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

# Entrar y desplegar con SSM (Session Manager / Run Command), sin SSH.
resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.server.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# Bajar las imágenes de ECR.
resource "aws_iam_role_policy_attachment" "ecr_read" {
  role       = aws_iam_role.server.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}

data "aws_iam_policy_document" "server" {
  # Leer SOLO los parámetros del proyecto para armar el .env.
  statement {
    sid       = "ReadProjectParameters"
    actions   = ["ssm:GetParametersByPath", "ssm:GetParameter", "ssm:GetParameters"]
    resources = ["arn:aws:ssm:${var.region}:${local.account_id}:parameter${local.ssm_prefix}/*"]
  }

  statement {
    sid       = "DecryptParametersViaSsm"
    actions   = ["kms:Decrypt"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["ssm.${var.region}.amazonaws.com"]
    }
  }

  # Imágenes de los clientes: solo este bucket.
  statement {
    sid       = "MediaObjects"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.media.arn}/*"]
  }

  statement {
    sid       = "MediaList"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.media.arn]
  }

  # Notas de voz: API síncrona de Amazon Transcribe (el audio va en la petición;
  # no usa S3 ni trabajos en cola). Proveedor intercambiable: app/core/transcription.py.
  statement {
    sid       = "TranscribeVoiceNotes"
    actions   = ["transcribe:TranscribeAudio"]
    resources = ["*"]
  }

  # Avisos al asesor: solo desde la identidad verificada.
  statement {
    sid       = "SendNotifications"
    actions   = ["ses:SendEmail", "ses:SendRawEmail"]
    resources = ["arn:aws:ses:${var.region}:${local.account_id}:identity/*"]
  }
}

resource "aws_iam_role_policy" "server" {
  name   = "${local.name}-server"
  role   = aws_iam_role.server.id
  policy = data.aws_iam_policy_document.server.json
}

resource "aws_iam_instance_profile" "server" {
  name = "${local.name}-server"
  role = aws_iam_role.server.name
}

# ----------------------------------------------------------------------
# El servidor
# ----------------------------------------------------------------------
resource "aws_eip" "this" {
  domain = "vpc"
  tags   = { Name = "${local.name}-ip" }
}

resource "aws_instance" "server" {
  ami                    = data.aws_ssm_parameter.al2023.value
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.web.id]
  iam_instance_profile   = aws_iam_instance_profile.server.name

  user_data = templatefile("${path.module}/templates/user_data.sh.tftpl", {
    compose = templatefile("${path.module}/templates/docker-compose.yml.tftpl", {
      api_image    = "${aws_ecr_repository.api.repository_url}:latest"
      panel_image  = "${aws_ecr_repository.panel.repository_url}:latest"
      domain       = local.domain
      region       = var.region
      media_bucket = aws_s3_bucket.media.bucket
      ses_sender   = var.owner_email
    })
    caddyfile = templatefile("${path.module}/templates/Caddyfile.tftpl", {
      domain = local.domain
    })
    refresh = templatefile("${path.module}/templates/refresh.sh.tftpl", {
      region     = var.region
      ssm_prefix = local.ssm_prefix
      registry   = split("/", aws_ecr_repository.api.repository_url)[0]
    })
  })

  metadata_options {
    http_tokens = "required" # IMDSv2
    # 2 saltos: los contenedores (red bridge de Docker) también necesitan
    # llegar a las credenciales del rol para usar S3 y SES.
    http_put_response_hop_limit = 2
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = var.root_volume_gb
    encrypted   = true
  }

  # Postgres vive en este disco: una AMI nueva NO debe reemplazar el servidor
  # (borraría la base). Las actualizaciones del SO se hacen con dnf, no recreando.
  lifecycle {
    ignore_changes = [ami]
  }

  tags = { Name = "${local.name}-server" }
}

resource "aws_eip_association" "this" {
  instance_id   = aws_instance.server.id
  allocation_id = aws_eip.this.id
}
