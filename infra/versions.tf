terraform {
  required_version = ">= 1.11"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.27"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.7"
    }
  }

  # Bucket is passed at init time so the account ID isn't committed:
  #   terraform init -backend-config="bucket=sentinelops-tfstate-<account_id>"
  backend "s3" {
    key          = "sentinelops/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      cost_center = "sentinelops"
      managed_by  = "terraform"
    }
  }
}
