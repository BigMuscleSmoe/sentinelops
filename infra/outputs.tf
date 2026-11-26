output "checkout_api_url" {
  value = aws_apigatewayv2_api.checkout.api_endpoint
}

output "checkout_api_function_name" {
  value = module.checkout_api.function_name
}

output "fault_controller_function_name" {
  value = module.fault_controller.function_name
}

output "traffic_generator_function_name" {
  value = module.traffic_generator.function_name
}

output "traffic_burst_function_name" {
  value = module.traffic_burst.function_name
}

output "tools_gateway_url" {
  value = aws_bedrockagentcore_gateway.tools.gateway_url
}

output "agent_runtime_arn" {
  value = aws_bedrockagentcore_agent_runtime.agent.agent_runtime_arn
}

output "trigger_function_name" {
  value = module.trigger.function_name
}

# The Console rule's subject for local runs, and SENTINELOPS_ANTHROPIC_ROLE_ARN.
output "local_sign_in_role_arn" {
  value = aws_iam_role.local_sign_in.arn
}

# For the deploy's ingestion step and the runbook search's checks.
output "runbooks_knowledge_base_id" {
  value = aws_bedrockagent_knowledge_base.runbooks.id
}

output "runbooks_data_source_id" {
  value = aws_bedrockagent_data_source.runbooks.data_source_id
}

output "remediation_function_name" {
  value = aws_lambda_function.remediation.function_name
}

# What the trigger invokes; the kill switch's commands name the function.
output "remediation_alias_arn" {
  value = aws_lambda_alias.remediation_live.arn
}
