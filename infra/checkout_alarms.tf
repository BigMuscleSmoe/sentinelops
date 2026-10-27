# The three alarms on checkout-api's EMF metrics (namespace checkout-api, no
# dimensions). The metrics are high resolution, so periods are 10 s: ALARM once
# 2 of the last 3 breach. Missing data counts as healthy, since a period holds
# only 2-5 requests at baseline. No actions: state changes reach EventBridge by
# themselves, and Phase 2's trigger listens there. The text names symptoms
# only, never a cause. High-resolution alarms are $0.30/month each.

locals {
  checkout_alarms = {
    pool-wait = {
      metric      = "pool_wait_time_ms"
      statistic   = "p99"
      threshold   = 100
      description = "checkout-api p99 wait for a database connection is over 100 ms"
    }
    errors = {
      metric      = "error"
      statistic   = "Average"
      threshold   = 0.05
      description = "Over 5% of checkout-api requests fail with a 500"
    }
    latency = {
      metric      = "latency_ms"
      statistic   = "p99"
      threshold   = 1500
      description = "checkout-api p99 latency is over 1.5 s"
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "checkout" {
  for_each = local.checkout_alarms

  alarm_name          = "sentinelops-checkout-${each.key}"
  alarm_description   = each.value.description
  namespace           = "checkout-api"
  metric_name         = each.value.metric
  statistic           = each.value.statistic == "Average" ? "Average" : null
  extended_statistic  = each.value.statistic == "Average" ? null : each.value.statistic
  period              = 10
  evaluation_periods  = 3
  datapoints_to_alarm = 2
  comparison_operator = "GreaterThanThreshold"
  threshold           = each.value.threshold
  treat_missing_data  = "notBreaching"
  # Percentiles only: with 2-5 samples a period, the p99 is still evaluated.
  evaluate_low_sample_count_percentiles = each.value.statistic == "Average" ? null : "evaluate"
}
