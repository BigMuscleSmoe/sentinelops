# Each incident's trace, for the page (BUILD_PLAN.md, Phase 5 plan, "The live
# trace"; core/sentinelops_core/trace.py): one item per step, keyed by the
# incident's ID, written by the agent's Runtime as the run goes
# (agent/run_trace.py) and by the remediation flow as its PR goes. Both
# write best-effort, with one try and short timeouts; neither reads it. Items
# expire 7 days on, as the logs do; the recorded run's backfill sets no expiry
# and keeps (agent/backfill_trace.py).
#
# Cost: on demand, about 60 items a run, most well under 1 KB, and the demo's
# caps hold runs to 200 a month: cents.
resource "aws_dynamodb_table" "trace" {
  name         = "sentinelops-trace"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
}
