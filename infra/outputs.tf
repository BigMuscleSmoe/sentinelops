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
