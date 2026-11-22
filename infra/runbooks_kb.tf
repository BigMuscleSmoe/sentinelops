# The runbooks' knowledge base: one document per runbook section in an S3
# bucket, embedded with Titan Text Embeddings V2 into an S3 Vectors index, and
# searched with Bedrock's Retrieve. Never OpenSearch Serverless: it bills by the
# hour for the same job.
#
# tools/build_runbooks.py writes the documents and their manifest into .build/
# before a plan, as agent/build_runtime.py does the Runtime's package. Without
# the manifest, file() fails the plan here, rather than planning the deletion
# of every section. The deploy ingests after the apply
# (tools/ingest_runbooks.py), and fails unless the index then holds exactly
# this build.

locals {
  runbook_sections_dir = "${path.module}/.build/runbook-sections"
  runbook_manifest     = jsondecode(file("${path.module}/.build/runbook-sections.json"))
  runbook_files = toset(flatten([
    for doc in local.runbook_manifest.documents : [doc, "${doc}.metadata.json"]
  ]))

  titan_v2_arn = "arn:aws:bedrock:${var.region}::foundation-model/amazon.titan-embed-text-v2:0"
}

# The documents. Nothing reads them but the knowledge base's role.
resource "aws_s3_bucket" "runbooks" {
  bucket_prefix = "sentinelops-runbooks-"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "runbooks" {
  bucket                  = aws_s3_bucket.runbooks.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "runbooks" {
  bucket = aws_s3_bucket.runbooks.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_object" "runbook_section" {
  for_each = local.runbook_files

  bucket       = aws_s3_bucket.runbooks.id
  key          = each.key
  source       = "${local.runbook_sections_dir}/${each.key}"
  etag         = filemd5("${local.runbook_sections_dir}/${each.key}")
  content_type = endswith(each.key, ".json") ? "application/json" : "text/markdown"

  lifecycle {
    # The folder and the manifest come from the same build: a folder holding
    # anything else was changed by hand, or by another build since.
    precondition {
      condition     = fileset(local.runbook_sections_dir, "*") == local.runbook_files
      error_message = "infra/.build/runbook-sections doesn't match its manifest: run python tools/build_runbooks.py again."
    }
  }
}

# The vectors. Bedrock stores each chunk's text and its own bookkeeping as
# metadata on its vector; filterable metadata is capped at 2 KB a vector on
# some indexes, and a section's text runs past that, so both are
# non-filterable. The sections' own metadata (runbook, title, section, corpus)
# stays filterable.
resource "aws_s3vectors_vector_bucket" "runbooks" {
  vector_bucket_name = "sentinelops-runbooks-vectors"
  force_destroy      = true
}

resource "aws_s3vectors_index" "runbooks" {
  vector_bucket_name = aws_s3vectors_vector_bucket.runbooks.vector_bucket_name
  index_name         = "runbook-sections"
  data_type          = "float32"
  dimension          = 1024
  distance_metric    = "cosine"

  metadata_configuration {
    non_filterable_metadata_keys = ["AMAZON_BEDROCK_TEXT", "AMAZON_BEDROCK_METADATA"]
  }
}

# The knowledge base's own role: it reads the documents, embeds them and each
# query, and writes and queries the vectors. Callers need only
# bedrock:Retrieve on the knowledge base.
resource "aws_iam_role" "runbooks_kb" {
  name                 = "sentinelops-runbooks-kb"
  assume_role_policy   = data.aws_iam_policy_document.runbooks_kb_assume.json
  permissions_boundary = data.aws_iam_policy.workload_boundary.arn
}

data "aws_iam_policy_document" "runbooks_kb_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["bedrock.amazonaws.com"]
    }

    # Only for a knowledge base in this account, never on another account's behalf.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:bedrock:${var.region}:${data.aws_caller_identity.current.account_id}:knowledge-base/*"]
    }
  }
}

data "aws_iam_policy_document" "runbooks_kb" {
  statement {
    sid       = "EmbedWithTitan"
    actions   = ["bedrock:InvokeModel"]
    resources = [local.titan_v2_arn]
  }

  # In AWS's policy for a knowledge base's role; neither can be scoped.
  statement {
    sid       = "ListModels"
    actions   = ["bedrock:ListFoundationModels", "bedrock:ListCustomModels"]
    resources = ["*"]
  }

  statement {
    sid       = "ListRunbookSections"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.runbooks.arn]

    condition {
      test     = "StringEquals"
      variable = "aws:ResourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }

  statement {
    sid       = "ReadRunbookSections"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.runbooks.arn}/*"]

    condition {
      test     = "StringEquals"
      variable = "aws:ResourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }

  statement {
    sid = "WriteAndQueryVectors"
    actions = [
      "s3vectors:PutVectors",
      "s3vectors:GetVectors",
      "s3vectors:DeleteVectors",
      "s3vectors:QueryVectors",
      "s3vectors:GetIndex",
    ]
    resources = [aws_s3vectors_index.runbooks.index_arn]
  }
}

resource "aws_iam_role_policy" "runbooks_kb" {
  name   = "permissions"
  role   = aws_iam_role.runbooks_kb.id
  policy = data.aws_iam_policy_document.runbooks_kb.json
}

resource "aws_bedrockagent_knowledge_base" "runbooks" {
  name        = "sentinelops-runbooks"
  description = "checkout-api's runbooks, one document per section, for the agent's runbook search"
  role_arn    = aws_iam_role.runbooks_kb.arn

  knowledge_base_configuration {
    type = "VECTOR"

    vector_knowledge_base_configuration {
      embedding_model_arn = local.titan_v2_arn

      embedding_model_configuration {
        bedrock_embedding_model_configuration {
          dimensions          = 1024
          embedding_data_type = "FLOAT32"
        }
      }
    }
  }

  storage_configuration {
    type = "S3_VECTORS"

    s3_vectors_configuration {
      index_arn = aws_s3vectors_index.runbooks.index_arn
    }
  }

  # The role's policy must exist before Bedrock checks what the role can reach.
  depends_on = [aws_iam_role_policy.runbooks_kb]
}

# Chunking NONE: each document is one section, so each is one chunk, and a
# search returns whole sections. Deleting the data source deletes its vectors.
resource "aws_bedrockagent_data_source" "runbooks" {
  name                 = "runbook-sections"
  knowledge_base_id    = aws_bedrockagent_knowledge_base.runbooks.id
  data_deletion_policy = "DELETE"

  data_source_configuration {
    type = "S3"

    s3_configuration {
      bucket_arn = aws_s3_bucket.runbooks.arn
    }
  }

  vector_ingestion_configuration {
    chunking_configuration {
      chunking_strategy = "NONE"
    }
  }
}
