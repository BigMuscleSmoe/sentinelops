#!/usr/bin/env bash
# usage: live_run.sh <healthy|db_pool_exhaustion|bad_deploy>
# One live run against the deployed stack. A fault mode breaks the service,
# which also starts the burst, then clears the fault once the burst is over;
# "healthy" sends the burst alone, as a control. Prints each alarm transition
# in seconds after the invoke, and writes t0_<mode>.txt for analyze.py into
# the current directory. Prints no credentials or ARNs. See README.md.
set -u
export AWS_PROFILE=sentinelops AWS_PAGER="" MSYS_NO_PATHCONV=1 AWS_REGION=us-east-1
mode=${1:-}
case "$mode" in
  healthy | db_pool_exhaustion | bad_deploy) ;;
  *) echo "usage: live_run.sh <healthy|db_pool_exhaustion|bad_deploy>" >&2; exit 2 ;;
esac

alarm_states() {
  aws cloudwatch describe-alarms --alarm-name-prefix sentinelops-checkout \
    --query 'MetricAlarms[].[AlarmName,StateValue]' --output text | tr -d '\r'
}
controller() {
  printf '%s' "$1" > ctl.json
  aws lambda invoke --function-name sentinelops-fault-controller --cli-binary-format raw-in-base64-out \
    --payload file://ctl.json ctl_out.json --query StatusCode --output text > /dev/null
  cat ctl_out.json
  rm -f ctl.json ctl_out.json
}

not_ok=$(alarm_states | awk '$2 != "OK"')
if [ -n "$not_ok" ]; then echo "refusing to start: alarms not all OK:"; echo "$not_ok"; exit 1; fi

t0=$(date +%s.%N)
if [ "$mode" = healthy ]; then
  echo '{}' > burst_payload.json
  status=$(aws lambda invoke --function-name sentinelops-traffic-burst --invocation-type Event \
    --cli-binary-format raw-in-base64-out --payload file://burst_payload.json burst_out.json \
    --query StatusCode --output text)
  rm -f burst_payload.json burst_out.json
  echo "burst invoke status: $status"
else
  reply=$(controller "{\"action\": \"break\", \"mode\": \"$mode\"}")
  echo "break: $reply"
  case "$reply" in
    *'"burst": true'*) ;;
    *'"burst": false'*)
      # The fault is set but has no burst, so it would barely show.
      echo "clear: $(controller '{"action": "clear"}')"
      exit 1 ;;
    *) exit 1 ;;  # refused: another fault is active, so leave it alone
  esac
fi
t1=$(date +%s.%N)
echo "T0=$t0 T1=$t1" > "t0_$mode.txt"
echo "start $(date -d @"${t0%.*}" +%H:%M:%S) local, invoke took $(awk "BEGIN{printf \"%.2f\", $t1 - $t0}") s"

# The burst runs 360 s; the rest is time for the alarms to settle.
declare -A last
end=$(( ${t0%.*} + 440 ))
while [ "$(date +%s)" -lt "$end" ]; do
  now=$(date +%s.%N)
  while read -r name state; do
    [ -z "$name" ] && continue
    if [ "${last[$name]:-}" != "$state" ]; then
      printf '+%6.1fs %-10s %s\n' "$(awk "BEGIN{print $now - $t0}")" "${name#sentinelops-checkout-}" "$state"
      last[$name]=$state
    fi
  done <<< "$(alarm_states)"
  sleep 5
done

if [ "$mode" != healthy ]; then
  reply=$(controller '{"action": "clear"}')
  echo "clear at +$(awk "BEGIN{printf \"%.1f\", $(date +%s.%N) - $t0}")s: $reply"
fi
