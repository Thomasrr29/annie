terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.40"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.6"
    }
  }

  # Estado LOCAL a propósito para el MVP: un solo operador y sin costo extra.
  # El estado contiene secretos (contraseñas de la base generadas aquí): nunca
  # se sube al repo (ver infra/.gitignore). Con más de una persona, moverlo a
  # un backend S3 con bloqueo.
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = var.project
      ManagedBy = "terraform"
    }
  }
}
