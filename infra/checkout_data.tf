# checkout-api's state. All on demand, so idle they cost nothing.

# The connection pool every instance shares, as one item. See
# services/checkout-api/sim/pool.py.
resource "aws_dynamodb_table" "checkout_pool" {
  name         = "sentinelops-checkout-pool"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pool"

  attribute {
    name = "pool"
    type = "S"
  }
}

# The demo's fault switch. The service ignores an item past its expires_at;
# TTL only deletes it some time later.
resource "aws_dynamodb_table" "checkout_flags" {
  name         = "sentinelops-checkout-flags"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "flag"

  attribute {
    name = "flag"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
}

# Deploys and config changes, newest last, read by the agent's
# get_deploy_history tool.
resource "aws_dynamodb_table" "deploy_history" {
  name         = "sentinelops-deploy-history"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "service"
  range_key    = "ts"

  attribute {
    name = "service"
    type = "S"
  }

  attribute {
    name = "ts"
    type = "S"
  }
}

# The deploys before the demo begins, oldest first. The newest is more than a
# week old, so no retained log can contradict it. The fault controller adds the
# rest at each break: services/checkout-api/controller/faults.py, whose
# RELEASE_COMMIT is 1.42.0's commit here.
locals {
  seeded_deploys = {
    "2026-08-25T14:12:09.000Z" = { version = "1.38.0", previous = "1.37.2", commit = "b71c0e4", author = "ken", message = "Add the SUMMIT20 promo code" }
    "2026-09-03T16:40:51.000Z" = { version = "1.39.0", previous = "1.38.0", commit = "e2d94a0", author = "priya", message = "Return 404 for unknown products" }
    "2026-09-17T13:05:37.000Z" = { version = "1.40.0", previous = "1.39.0", commit = "5a0f3c8", author = "tomas", message = "Raise the payments-api client timeout to 2 s" }
    "2026-09-30T15:22:18.000Z" = { version = "1.41.0", previous = "1.40.0", commit = "c64b19e", author = "maya", message = "Read pool settings from the environment" }
    "2026-10-14T17:48:02.000Z" = { version = "1.42.0", previous = "1.41.0", commit = "3f8a2d1", author = "ken", message = "One structured log line per request" }
  }
}

resource "aws_dynamodb_table_item" "seeded_deploy" {
  for_each = local.seeded_deploys

  table_name = aws_dynamodb_table.deploy_history.name
  hash_key   = aws_dynamodb_table.deploy_history.hash_key
  range_key  = aws_dynamodb_table.deploy_history.range_key
  item = jsonencode({
    service = { S = "checkout-api" }
    ts      = { S = each.key }
    kind    = { S = "deploy" }
    version = { S = each.value.version }
    commit  = { S = each.value.commit }
    author  = { S = each.value.author }
    message = { S = each.value.message }
    changes = { L = [{ M = { key = { S = "version" }, old = { S = each.value.previous }, new = { S = each.value.version } } }] }
  })
}
