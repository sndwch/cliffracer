"""
AWS Lambda runner implementation for serverless service execution
Supports both Lambda functions and LocalStack for development
"""

import json
import os
import tempfile
import time
import zipfile
from datetime import datetime
from typing import Any

import boto3
from botocore.exceptions import ClientError

from .abstract_runner import (
    AbstractServiceOrchestrator,
    AbstractServiceRunner,
    RunnerConfig,
    RunnerFactory,
    RunnerType,
    ServiceMetrics,
    ServiceRequest,
    ServiceResponse,
)


class AWSLambdaRunner(ServiceRunner):
    """AWS Lambda-based service runner"""

    def __init__(self, config: RunnerConfig):
        super().__init__(config)

        # AWS clients
        self.lambda_client = None
        self.logs_client = None
        self.iam_client = None

        # Lambda configuration
        self.function_prefix = config.environment_variables.get("LAMBDA_PREFIX", "cliffracer")
        self.runtime = config.environment_variables.get("LAMBDA_RUNTIME", "python3.11")
        self.role_arn = config.environment_variables.get("LAMBDA_ROLE_ARN")
        self.vpc_config = self._parse_vpc_config()
        self.layers = self._parse_layers()

        # LocalStack support
        self.endpoint_url = config.environment_variables.get("AWS_ENDPOINT_URL")
        self.use_localstack = bool(self.endpoint_url)

        # Service tracking
        self.deployed_functions: dict[str, dict] = {}
        self.metrics = ServiceMetrics()

    def _parse_vpc_config(self) -> dict | None:
        """Parse VPC configuration from environment"""
        subnet_ids = self.config.environment_variables.get("LAMBDA_SUBNET_IDS")
        security_group_ids = self.config.environment_variables.get("LAMBDA_SECURITY_GROUP_IDS")

        if subnet_ids and security_group_ids:
            return {
                "SubnetIds": subnet_ids.split(","),
                "SecurityGroupIds": security_group_ids.split(","),
            }
        return None

    def _parse_layers(self) -> list[str]:
        """Parse Lambda layers from environment"""
        layers = self.config.environment_variables.get("LAMBDA_LAYERS", "")
        return [layer.strip() for layer in layers.split(",") if layer.strip()]

    async def start(self) -> None:
        """Initialize AWS Lambda client"""
        if self._running:
            return

        # Initialize AWS clients
        session = boto3.Session(
            region_name=self.config.environment_variables.get("AWS_REGION", "us-east-1")
        )

        client_kwargs = {}
        if self.endpoint_url:
            client_kwargs["endpoint_url"] = self.endpoint_url

        self.lambda_client = session.client("lambda", **client_kwargs)
        self.logs_client = session.client("logs", **client_kwargs)
        self.iam_client = session.client("iam", **client_kwargs)

        # Create IAM role if using LocalStack and role doesn't exist
        if self.use_localstack and not self.role_arn:
            self.role_arn = await self._create_lambda_role()

        self._running = True
        self.logger.info(f"Lambda runner started (LocalStack: {self.use_localstack})")

    async def stop(self) -> None:
        """Cleanup Lambda functions"""
        if not self._running:
            return

        # Optionally clean up functions in development
        if self.use_localstack:
            for function_name in list(self.deployed_functions.keys()):
                try:
                    await self._delete_lambda_function(function_name)
                except Exception as e:
                    self.logger.warning(f"Failed to delete function {function_name}: {e}")

        self.deployed_functions.clear()
        self._running = False
        self.logger.info("Lambda runner stopped")

    async def execute_service_method(self, request: ServiceRequest) -> ServiceResponse:
        """Execute service method via Lambda invocation"""
        function_name = f"{self.function_prefix}-{request.service_name}"

        if function_name not in self.deployed_functions:
            return ServiceResponse(
                success=False,
                error=f"Service {request.service_name} not deployed",
                correlation_id=request.correlation_id,
            )

        try:
            # Prepare Lambda payload
            lambda_payload = {
                "service_name": request.service_name,
                "method_name": request.method_name,
                "payload": request.payload,
                "correlation_id": request.correlation_id,
                "headers": request.headers,
                "timestamp": datetime.utcnow().isoformat(),
            }

            # Invoke Lambda function
            start_time = time.time()

            response = self.lambda_client.invoke(
                FunctionName=function_name,
                InvocationType="RequestResponse",  # Synchronous
                Payload=json.dumps(lambda_payload),
            )

            execution_time_ms = (time.time() - start_time) * 1000

            # Parse response
            if response["StatusCode"] == 200:
                payload = json.loads(response["Payload"].read().decode())

                if "errorMessage" in payload:
                    # Lambda execution error
                    service_response = ServiceResponse(
                        success=False,
                        error=payload["errorMessage"],
                        execution_time_ms=execution_time_ms,
                        correlation_id=request.correlation_id,
                        metadata={
                            "lambda_request_id": response.get("ResponseMetadata", {}).get(
                                "RequestId"
                            ),
                            "billed_duration_ms": response.get("ResponseMetadata", {})
                            .get("HTTPHeaders", {})
                            .get("x-amzn-billing-duration"),
                            "memory_used_mb": response.get("ResponseMetadata", {})
                            .get("HTTPHeaders", {})
                            .get("x-amzn-max-memory-used"),
                        },
                    )
                else:
                    # Successful execution
                    service_response = ServiceResponse(
                        success=True,
                        result=payload.get("result"),
                        execution_time_ms=execution_time_ms,
                        correlation_id=request.correlation_id,
                        metadata={
                            "lambda_request_id": response.get("ResponseMetadata", {}).get(
                                "RequestId"
                            ),
                            "billed_duration_ms": response.get("ResponseMetadata", {})
                            .get("HTTPHeaders", {})
                            .get("x-amzn-billing-duration"),
                            "memory_used_mb": response.get("ResponseMetadata", {})
                            .get("HTTPHeaders", {})
                            .get("x-amzn-max-memory-used"),
                        },
                    )
            else:
                service_response = ServiceResponse(
                    success=False,
                    error=f"Lambda invocation failed with status {response['StatusCode']}",
                    execution_time_ms=execution_time_ms,
                    correlation_id=request.correlation_id,
                )

            # Record metrics
            self.metrics.record_request(service_response)
            return service_response

        except Exception as e:
            self.logger.error(
                f"Failed to execute {request.service_name}.{request.method_name}: {e}"
            )
            error_response = ServiceResponse(
                success=False, error=str(e), correlation_id=request.correlation_id
            )
            self.metrics.record_request(error_response)
            return error_response

    async def register_service(self, service_class: type, service_config: Any) -> str:
        """Deploy service as Lambda function"""
        service_name = service_config.name
        function_name = f"{self.function_prefix}-{service_name}"

        try:
            # Create deployment package
            zip_content = await self._create_deployment_package(service_class, service_config)

            # Lambda function configuration
            function_config = {
                "FunctionName": function_name,
                "Runtime": self.runtime,
                "Role": self.role_arn,
                "Handler": "lambda_handler.handler",
                "Code": {"ZipFile": zip_content},
                "Description": f"Cultku service: {service_name}",
                "Timeout": self.config.timeout_seconds,
                "MemorySize": self.config.memory_mb,
                "Environment": {
                    "Variables": {
                        **self.config.environment_variables,
                        "SERVICE_NAME": service_name,
                        "SERVICE_CONFIG": json.dumps(
                            service_config.__dict__
                            if hasattr(service_config, "__dict__")
                            else str(service_config)
                        ),
                    }
                },
                "Tags": {
                    **self.config.tags,
                    "cliffracer:service": service_name,
                    "cliffracer:runner": "lambda",
                },
            }

            # Add VPC configuration if specified
            if self.vpc_config:
                function_config["VpcConfig"] = self.vpc_config

            # Add layers if specified
            if self.layers:
                function_config["Layers"] = self.layers

            # Create or update function
            try:
                response = self.lambda_client.create_function(**function_config)
                self.logger.info(f"Created Lambda function: {function_name}")
            except ClientError as e:
                if e.response["Error"]["Code"] == "ResourceConflictException":
                    # Function exists, update it
                    self.lambda_client.update_function_code(
                        FunctionName=function_name, ZipFile=zip_content
                    )
                    self.lambda_client.update_function_configuration(
                        FunctionName=function_name,
                        Runtime=function_config["Runtime"],
                        Role=function_config["Role"],
                        Handler=function_config["Handler"],
                        Description=function_config["Description"],
                        Timeout=function_config["Timeout"],
                        MemorySize=function_config["MemorySize"],
                        Environment=function_config["Environment"],
                    )
                    response = self.lambda_client.get_function(FunctionName=function_name)
                    self.logger.info(f"Updated Lambda function: {function_name}")
                else:
                    raise

            # Store deployment info
            self.deployed_functions[function_name] = {
                "service_class": service_class,
                "service_config": service_config,
                "function_arn": response["FunctionArn"],
                "deployed_at": datetime.utcnow().isoformat(),
            }

            return function_name

        except Exception as e:
            self.logger.error(f"Failed to deploy service {service_name}: {e}")
            raise

    async def unregister_service(self, service_id: str) -> None:
        """Delete Lambda function"""
        await self._delete_lambda_function(service_id)

    async def _delete_lambda_function(self, function_name: str) -> None:
        """Delete a Lambda function"""
        try:
            self.lambda_client.delete_function(FunctionName=function_name)
            if function_name in self.deployed_functions:
                del self.deployed_functions[function_name]
            self.logger.info(f"Deleted Lambda function: {function_name}")
        except ClientError as e:
            if e.response["Error"]["Code"] != "ResourceNotFoundException":
                raise

    async def _create_deployment_package(self, service_class: type, service_config: Any) -> bytes:
        """Create Lambda deployment package"""
        import inspect

        # Create temporary directory for package
        with tempfile.TemporaryDirectory() as temp_dir:
            zip_path = os.path.join(temp_dir, "deployment.zip")

            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zip_file:
                # Add Lambda handler
                handler_code = self._generate_lambda_handler(service_class, service_config)
                zip_file.writestr("lambda_handler.py", handler_code)

                # Add service class file
                service_file = inspect.getfile(service_class)
                service_filename = os.path.basename(service_file)
                zip_file.write(service_file, service_filename)

                # Add framework files
                framework_files = [
                    "modular_service.py",
                    "messaging/__init__.py",
                    "messaging/abstract_messaging.py",
                    "messaging/nats_messaging.py",
                    "messaging/aws_messaging.py",
                ]

                for file_path in framework_files:
                    if os.path.exists(file_path):
                        zip_file.write(file_path, file_path)

                # Add requirements (minimal set for Lambda)
                requirements = ["boto3", "botocore", "pydantic", "nats-py"]

                # For LocalStack, we assume dependencies are available
                # In production, you'd layer these or use Lambda layers
                if not self.use_localstack:
                    # Add site-packages for required modules
                    for req in requirements:
                        try:
                            module = __import__(req.replace("-", "_"))
                            module_path = os.path.dirname(module.__file__)

                            for root, dirs, files in os.walk(module_path):
                                for file in files:
                                    if file.endswith((".py", ".so")):
                                        file_path = os.path.join(root, file)
                                        arc_path = os.path.relpath(
                                            file_path, os.path.dirname(module_path)
                                        )
                                        zip_file.write(file_path, arc_path)
                        except ImportError:
                            pass

            # Read zip content
            with open(zip_path, "rb") as f:
                return f.read()

    def _generate_lambda_handler(self, service_class: type, service_config: Any) -> str:
        """Generate Lambda handler code"""
        return f'''
import json
import asyncio
import logging
from datetime import datetime

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def handler(event, context):
    """Lambda handler function"""
    try:
        # Parse event
        service_name = event.get("service_name")
        method_name = event.get("method_name") 
        payload = event.get("payload", {{}})
        correlation_id = event.get("correlation_id")
        
        logger.info(f"Executing {{service_name}}.{{method_name}} ({{correlation_id}})")
        
        # Import service class
        from {service_class.__module__} import {service_class.__name__}
        from modular_service import ModularServiceConfig
        from messaging import MessagingConfig
        
        # Create service configuration
        # Note: In real Lambda, this would come from environment or parameter store
        config = ModularServiceConfig(
            name=service_name,
            messaging=MessagingConfig.aws_sns_sqs()  # Default to AWS for Lambda
        )
        
        # Create service instance
        service = {service_class.__name__}(config)
        
        # Execute method
        method = getattr(service, method_name)
        if asyncio.iscoroutinefunction(method):
            result = asyncio.run(method(**payload))
        else:
            result = method(**payload)
        
        logger.info(f"Successfully executed {{service_name}}.{{method_name}}")
        
        return {{
            "result": result,
            "success": True,
            "correlation_id": correlation_id,
            "timestamp": datetime.utcnow().isoformat()
        }}
        
    except Exception as e:
        logger.error(f"Lambda execution failed: {{e}}")
        return {{
            "errorMessage": str(e),
            "errorType": type(e).__name__,
            "correlation_id": event.get("correlation_id"),
            "timestamp": datetime.utcnow().isoformat()
        }}
'''

    async def _create_lambda_role(self) -> str:
        """Create IAM role for Lambda (LocalStack)"""
        role_name = f"{self.function_prefix}-lambda-role"

        # Trust policy for Lambda
        trust_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "lambda.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }
            ],
        }

        try:
            # Create role
            response = self.iam_client.create_role(
                RoleName=role_name,
                AssumeRolePolicyDocument=json.dumps(trust_policy),
                Description="Lambda execution role for Cultku services",
            )

            # Attach basic Lambda execution policy
            self.iam_client.attach_role_policy(
                RoleName=role_name,
                PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
            )

            # Attach SNS/SQS permissions
            sns_sqs_policy = {
                "Version": "2012-10-17",
                "Statement": [
                    {"Effect": "Allow", "Action": ["sns:*", "sqs:*", "events:*"], "Resource": "*"}
                ],
            }

            self.iam_client.put_role_policy(
                RoleName=role_name,
                PolicyName="CultkuMessagingPolicy",
                PolicyDocument=json.dumps(sns_sqs_policy),
            )

            role_arn = response["Role"]["Arn"]
            self.logger.info(f"Created Lambda role: {role_arn}")
            return role_arn

        except ClientError as e:
            if e.response["Error"]["Code"] == "EntityAlreadyExists":
                response = self.iam_client.get_role(RoleName=role_name)
                return response["Role"]["Arn"]
            else:
                raise

    async def get_stats(self) -> dict[str, Any]:
        """Get Lambda runner statistics"""
        stats = {
            "runner_type": "lambda",
            "running": self._running,
            "deployed_functions": len(self.deployed_functions),
            "use_localstack": self.use_localstack,
            "metrics": self.metrics.get_stats(),
        }

        # Add function details
        function_stats = []
        for name, info in self.deployed_functions.items():
            try:
                # Get function configuration
                response = self.lambda_client.get_function(FunctionName=name)
                config = response["Configuration"]

                function_stats.append(
                    {
                        "function_name": name,
                        "service_name": info["service_config"].name,
                        "runtime": config["Runtime"],
                        "memory_size": config["MemorySize"],
                        "timeout": config["Timeout"],
                        "last_modified": config["LastModified"],
                        "code_size": config["CodeSize"],
                    }
                )
            except Exception as e:
                function_stats.append({"function_name": name, "error": str(e)})

        stats["functions"] = function_stats
        return stats

    async def health_check(self) -> dict[str, Any]:
        """Perform health check"""
        health = {
            "status": "healthy" if self._running else "stopped",
            "runner_type": "lambda",
            "lambda_client_available": self.lambda_client is not None,
            "deployed_functions": len(self.deployed_functions),
            "timestamp": datetime.utcnow().isoformat(),
        }

        # Test Lambda connectivity
        if self.lambda_client:
            try:
                self.lambda_client.list_functions(MaxItems=1)
                health["lambda_connectivity"] = "ok"
            except Exception as e:
                health["lambda_connectivity"] = f"error: {e}"
                health["status"] = "unhealthy"

        return health


class LambdaServiceOrchestrator(AbstractServiceOrchestrator):
    """Orchestrator for Lambda-based services"""

    async def deploy_service(
        self, service_class: type, service_config: Any, deployment_config: dict[str, Any] = None
    ) -> str:
        """Deploy service to Lambda"""
        return await self.runner.register_service(service_class, service_config)

    async def undeploy_service(self, service_id: str) -> None:
        """Undeploy service from Lambda"""
        await self.runner.unregister_service(service_id)

    async def scale_service(self, service_id: str, target_instances: int) -> None:
        """Lambda auto-scales, but we can set concurrency"""
        if hasattr(self.runner, "lambda_client"):
            try:
                self.runner.lambda_client.put_reserved_concurrency_configuration(
                    FunctionName=service_id, ReservedConcurrencyLimit=target_instances
                )
                self.logger.info(f"Set reserved concurrency for {service_id}: {target_instances}")
            except Exception as e:
                self.logger.warning(f"Failed to set concurrency for {service_id}: {e}")

    async def get_service_logs(self, service_id: str, lines: int = 100) -> list[str]:
        """Get service logs from CloudWatch"""
        if not hasattr(self.runner, "logs_client"):
            return ["Logs client not available"]

        log_group_name = f"/aws/lambda/{service_id}"

        try:
            # Get log streams
            streams_response = self.runner.logs_client.describe_log_streams(
                logGroupName=log_group_name, orderBy="LastEventTime", descending=True, limit=5
            )

            logs = []
            for stream in streams_response["logStreams"]:
                try:
                    events_response = self.runner.logs_client.get_log_events(
                        logGroupName=log_group_name,
                        logStreamName=stream["logStreamName"],
                        limit=lines,
                    )

                    for event in events_response["events"]:
                        timestamp = datetime.fromtimestamp(event["timestamp"] / 1000)
                        logs.append(f"{timestamp.isoformat()} {event['message']}")

                except Exception as e:
                    logs.append(f"Error reading log stream {stream['logStreamName']}: {e}")

            return logs[-lines:] if logs else ["No logs found"]

        except Exception as e:
            return [f"Error reading logs: {e}"]


# Register Lambda runner
RunnerFactory.register_runner(RunnerType.LAMBDA, LambdaServiceRunner)
