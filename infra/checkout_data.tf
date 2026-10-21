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
