output "hello_function_name" {
  value = aws_lambda_function.hello.function_name
}

output "checkout_api_url" {
  value = aws_apigatewayv2_api.checkout.api_endpoint
}

output "checkout_api_function_name" {
  value = module.checkout_api.function_name
}
