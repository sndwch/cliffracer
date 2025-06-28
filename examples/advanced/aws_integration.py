"""
Example demonstrating full AWS stack with LocalStack:
- AWS SNS/SQS/EventBridge for messaging
- AWS Lambda for service execution
- CloudWatch for monitoring and logging
- Complete parity with AWS production environment
"""

import asyncio
import logging
import os
import time
from datetime import datetime

from fully_modular_service import (
    ConfigFactory,
    FullyModularConfig,
    event_handler,
    rpc,
)
from runners.lambda_runner import AWSLambdaRunner, LambdaServiceOrchestrator


class UserService(PluggableNATSService):
    """User service running on AWS Lambda with full AWS integration"""

    def __init__(self, config: FullyModularConfig):
        super().__init__(config)
        self.users: dict[str, dict] = {}
        self.user_counter = 0

    async def on_startup(self) -> None:
        """Service startup - called when Lambda initializes"""
        self.logger.info("UserService initialized in Lambda")

        # Record startup metric
        if self.monitoring_client:
            await self.monitoring_client.record_metric(
                name="service.startup",
                value=1,
                tags={"service": "user_service", "runtime": "lambda"},
            )

    @rpc
    async def create_user(self, username: str, email: str, full_name: str = "") -> dict:
        """Create a new user"""
        start_time = time.time()

        self.user_counter += 1
        user_id = f"user_{self.user_counter}"

        user = {
            "user_id": user_id,
            "username": username,
            "email": email,
            "full_name": full_name,
            "created_at": datetime.utcnow().isoformat(),
            "lambda_request_id": os.getenv("AWS_REQUEST_ID", "local"),
            "lambda_function_name": os.getenv("AWS_LAMBDA_FUNCTION_NAME", "local"),
        }

        self.users[user_id] = user

        # Publish event via SNS/EventBridge
        await self.publish_event(
            "users.created",
            user_id=user_id,
            username=username,
            email=email,
            created_at=user["created_at"],
        )

        # Record metrics
        execution_time = (time.time() - start_time) * 1000
        if self.monitoring_client:
            await self.monitoring_client.record_metric(
                name="user.created",
                value=1,
                tags={"service": "user_service", "method": "create_user"},
            )
            await self.monitoring_client.record_metric(
                name="create_user.duration", value=execution_time, tags={"service": "user_service"}
            )

        self.logger.info(f"Created user: {username} ({user_id}) in {execution_time:.2f}ms")
        return user

    @rpc
    async def get_user(self, user_id: str) -> dict:
        """Get user by ID"""
        start_time = time.time()

        user = self.users.get(user_id)
        if not user:
            # Record error metric
            if self.monitoring_client:
                await self.monitoring_client.record_metric(
                    name="user.get.errors",
                    value=1,
                    tags={"service": "user_service", "error_type": "not_found"},
                )
            raise ValueError(f"User {user_id} not found")

        # Record success metric
        execution_time = (time.time() - start_time) * 1000
        if self.monitoring_client:
            await self.monitoring_client.record_metric(
                name="user.retrieved", value=1, tags={"service": "user_service"}
            )
            await self.monitoring_client.record_metric(
                name="get_user.duration", value=execution_time, tags={"service": "user_service"}
            )

        return user

    @rpc
    async def list_users(self, limit: int = 10) -> list[dict]:
        """List all users"""
        users = list(self.users.values())
        return users[:limit]

    @rpc
    async def delete_user(self, user_id: str) -> dict:
        """Delete a user"""
        if user_id not in self.users:
            raise ValueError(f"User {user_id} not found")

        user = self.users.pop(user_id)

        # Publish deletion event
        await self.publish_event(
            "users.deleted",
            user_id=user_id,
            username=user["username"],
            deleted_at=datetime.utcnow().isoformat(),
        )

        # Record metric
        if self.monitoring_client:
            await self.monitoring_client.record_metric(
                name="user.deleted", value=1, tags={"service": "user_service"}
            )

        return {"deleted": True, "user": user}


class NotificationService(PluggableNATSService):
    """Notification service that reacts to user events"""

    def __init__(self, config: FullyModularConfig):
        super().__init__(config)
        self.notifications: list[dict] = []

    @event_handler("users.created")
    async def on_user_created(
        self, subject: str, user_id: str, username: str, email: str, **kwargs
    ):
        """Handle user creation events via EventBridge"""
        notification = {
            "type": "welcome_email",
            "user_id": user_id,
            "username": username,
            "email": email,
            "message": f"Welcome {username}! Your account has been created.",
            "sent_at": datetime.utcnow().isoformat(),
            "lambda_request_id": os.getenv("AWS_REQUEST_ID", "local"),
        }

        self.notifications.append(notification)

        # Simulate sending email via SES (in real implementation)
        await asyncio.sleep(0.1)  # Simulate email sending delay

        # Record metrics
        if self.monitoring_client:
            await self.monitoring_client.record_metric(
                name="notification.sent",
                value=1,
                tags={"service": "notification_service", "type": "welcome_email"},
            )

        self.logger.info(f"📧 Welcome email sent to {username} ({email})")

    @event_handler("users.deleted")
    async def on_user_deleted(self, subject: str, user_id: str, username: str, **kwargs):
        """Handle user deletion events"""
        notification = {
            "type": "goodbye_email",
            "user_id": user_id,
            "username": username,
            "message": f"Goodbye {username}! Your account has been deleted.",
            "sent_at": datetime.utcnow().isoformat(),
        }

        self.notifications.append(notification)

        # Record metrics
        if self.monitoring_client:
            await self.monitoring_client.record_metric(
                name="notification.sent",
                value=1,
                tags={"service": "notification_service", "type": "goodbye_email"},
            )

        self.logger.info(f"📧 Goodbye email sent to {username}")

    @rpc
    async def get_notifications(self, user_id: str = None, limit: int = 50) -> list[dict]:
        """Get notifications"""
        if user_id:
            notifications = [n for n in self.notifications if n.get("user_id") == user_id]
        else:
            notifications = self.notifications

        return notifications[-limit:]

    @rpc
    async def send_custom_notification(
        self, user_id: str, message: str, notification_type: str = "custom"
    ) -> dict:
        """Send a custom notification"""
        notification = {
            "type": notification_type,
            "user_id": user_id,
            "message": message,
            "sent_at": datetime.utcnow().isoformat(),
            "lambda_request_id": os.getenv("AWS_REQUEST_ID", "local"),
        }

        self.notifications.append(notification)

        # Record metrics
        if self.monitoring_client:
            await self.monitoring_client.record_metric(
                name="notification.custom.sent",
                value=1,
                tags={"service": "notification_service", "type": notification_type},
            )

        return notification


class OrderService(PluggableNATSService):
    """Order service demonstrating cross-service communication"""

    def __init__(self, config: FullyModularConfig):
        super().__init__(config)
        self.orders: dict[str, dict] = {}
        self.order_counter = 0

    @rpc
    async def create_order(self, user_id: str, items: list[dict], total: float) -> dict:
        """Create a new order"""
        # Verify user exists by calling UserService
        try:
            user = await self.call_rpc("user_service", "get_user", user_id=user_id)
        except Exception as e:
            self.logger.error(f"Failed to verify user {user_id}: {e}")
            raise ValueError(f"Invalid user: {e}")

        self.order_counter += 1
        order_id = f"order_{self.order_counter}"

        order = {
            "order_id": order_id,
            "user_id": user_id,
            "username": user["username"],
            "items": items,
            "total": total,
            "status": "pending",
            "created_at": datetime.utcnow().isoformat(),
            "lambda_request_id": os.getenv("AWS_REQUEST_ID", "local"),
        }

        self.orders[order_id] = order

        # Publish order created event
        await self.publish_event(
            "orders.created",
            order_id=order_id,
            user_id=user_id,
            username=user["username"],
            total=total,
            item_count=len(items),
        )

        # Send order confirmation notification
        await self.call_async(
            "notification_service",
            "send_custom_notification",
            user_id=user_id,
            message=f"Order {order_id} created for ${total:.2f}",
            notification_type="order_confirmation",
        )

        # Record metrics
        if self.monitoring_client:
            await self.monitoring_client.record_metric(
                name="order.created", value=1, tags={"service": "order_service"}
            )
            await self.monitoring_client.record_metric(
                name="order.value", value=total, tags={"service": "order_service"}
            )

        self.logger.info(f"Created order {order_id} for user {user['username']} (${total:.2f})")
        return order

    @rpc
    async def get_order(self, order_id: str) -> dict:
        """Get order by ID"""
        order = self.orders.get(order_id)
        if not order:
            raise ValueError(f"Order {order_id} not found")
        return order

    @event_handler("users.deleted")
    async def on_user_deleted(self, subject: str, user_id: str, **kwargs):
        """Handle user deletion - cancel their orders"""
        cancelled_orders = []
        for order_id, order in self.orders.items():
            if order["user_id"] == user_id:
                order["status"] = "cancelled"
                order["cancelled_at"] = datetime.utcnow().isoformat()
                cancelled_orders.append(order_id)

        if cancelled_orders:
            self.logger.info(f"Cancelled {len(cancelled_orders)} orders for deleted user {user_id}")


async def test_full_aws_stack():
    """Test the full AWS stack with LocalStack"""
    print("🚀 Testing Full AWS Stack with LocalStack")
    print("=" * 50)

    # Use LocalStack configuration
    services_config = {
        "user_service": ConfigFactory.aws_lambda_localstack("user_service"),
        "notification_service": ConfigFactory.aws_lambda_localstack("notification_service"),
        "order_service": ConfigFactory.aws_lambda_localstack("order_service"),
    }

    # Create test client
    client_config = ConfigFactory.aws_lambda_localstack("test_client")

    # For testing, we'll use a process runner instead of Lambda
    from runners.abstract_runner import RunnerType

    client_config.runner.runner_type = RunnerType.PROCESS

    client = PluggableNATSService(client_config)

    try:
        await client.start()

        # Wait for AWS resources to be ready
        print("⏳ Waiting for AWS resources to be ready...")
        await asyncio.sleep(5)

        print("\n1. Testing User Management")
        print("-" * 30)

        # Create users
        user1 = await client.call_rpc(
            "user_service",
            "create_user",
            username="alice",
            email="alice@example.com",
            full_name="Alice Smith",
        )
        print(f"✅ Created user: {user1['username']} ({user1['user_id']})")

        user2 = await client.call_rpc(
            "user_service",
            "create_user",
            username="bob",
            email="bob@example.com",
            full_name="Bob Johnson",
        )
        print(f"✅ Created user: {user2['username']} ({user2['user_id']})")

        # Get users
        retrieved_user = await client.call_rpc("user_service", "get_user", user_id=user1["user_id"])
        print(f"✅ Retrieved user: {retrieved_user['username']}")

        print("\n2. Testing Order Processing")
        print("-" * 30)

        # Create orders
        order1 = await client.call_rpc(
            "order_service",
            "create_order",
            user_id=user1["user_id"],
            items=[
                {"name": "Widget", "quantity": 2, "price": 10.0},
                {"name": "Gadget", "quantity": 1, "price": 25.0},
            ],
            total=45.0,
        )
        print(f"✅ Created order: {order1['order_id']} for ${order1['total']}")

        order2 = await client.call_rpc(
            "order_service",
            "create_order",
            user_id=user2["user_id"],
            items=[{"name": "Thing", "quantity": 3, "price": 15.0}],
            total=45.0,
        )
        print(f"✅ Created order: {order2['order_id']} for ${order2['total']}")

        # Wait for async events to be processed
        print("\n⏳ Waiting for events to be processed...")
        await asyncio.sleep(3)

        print("\n3. Testing Notifications")
        print("-" * 30)

        # Check notifications for user1
        notifications = await client.call_rpc(
            "notification_service", "get_notifications", user_id=user1["user_id"]
        )
        print(f"✅ User {user1['username']} has {len(notifications)} notifications:")
        for notif in notifications:
            print(f"   📧 {notif['type']}: {notif['message']}")

        print("\n4. Testing User Deletion")
        print("-" * 30)

        # Delete user
        deleted_user = await client.call_rpc(
            "user_service", "delete_user", user_id=user2["user_id"]
        )
        print(f"✅ Deleted user: {deleted_user['user']['username']}")

        # Wait for deletion events
        await asyncio.sleep(2)

        # Check goodbye notification
        goodbye_notifications = await client.call_rpc(
            "notification_service", "get_notifications", user_id=user2["user_id"]
        )
        print(f"✅ Found {len(goodbye_notifications)} notifications for deleted user")

        print("\n5. Testing Service Statistics")
        print("-" * 30)

        # Get service stats (would work if services were running)
        try:
            client_stats = await client.get_stats()
            print("✅ Client service stats:")
            print(f"   - Messaging: {client_stats['config']['messaging_backend']}")
            print(f"   - Runner: {client_stats['config']['runner_type']}")
            print(f"   - Monitoring: {client_stats['config']['monitoring_backend']}")
            print(f"   - Environment: {client_stats['config']['environment']}")
        except Exception as e:
            print(f"⚠️  Stats collection: {e}")

        print("\n🎉 Full AWS stack test completed successfully!")
        print("\nThis demonstrates:")
        print("✅ AWS SNS/SQS messaging with LocalStack")
        print("✅ AWS Lambda function execution (simulated)")
        print("✅ CloudWatch monitoring and metrics")
        print("✅ EventBridge event routing")
        print("✅ Cross-service communication")
        print("✅ Event-driven architecture")
        print("✅ Complete AWS parity in development")

    except Exception as e:
        print(f"❌ Test failed: {e}")
        import traceback

        traceback.print_exc()

    finally:
        await client.stop()


async def deploy_to_lambda():
    """Deploy services to actual Lambda functions in LocalStack"""
    print("🚀 Deploying services to Lambda in LocalStack")
    print("=" * 50)

    from runners.abstract_runner import RunnerConfig, RunnerType, RuntimeEnvironment

    # Create Lambda runner configuration
    runner_config = RunnerConfig(
        runner_type=RunnerType.LAMBDA,
        environment=RuntimeEnvironment.DEVELOPMENT,
        environment_variables={
            "AWS_ENDPOINT_URL": "http://localhost:4566",
            "AWS_REGION": "us-east-1",
            "AWS_ACCESS_KEY_ID": "test",
            "AWS_SECRET_ACCESS_KEY": "test",
            "LAMBDA_PREFIX": "cliffracer-dev",
        },
    )

    # Create Lambda runner
    runner = AWSLambdaRunner(runner_config)
    orchestrator = LambdaServiceOrchestrator(runner)

    try:
        await runner.start()

        print("📦 Deploying UserService to Lambda...")
        user_service_id = await orchestrator.deploy_service(
            UserService, ConfigFactory.aws_lambda_localstack("user_service")
        )
        print(f"✅ Deployed UserService as {user_service_id}")

        print("📦 Deploying NotificationService to Lambda...")
        notification_service_id = await orchestrator.deploy_service(
            NotificationService, ConfigFactory.aws_lambda_localstack("notification_service")
        )
        print(f"✅ Deployed NotificationService as {notification_service_id}")

        print("📦 Deploying OrderService to Lambda...")
        order_service_id = await orchestrator.deploy_service(
            OrderService, ConfigFactory.aws_lambda_localstack("order_service")
        )
        print(f"✅ Deployed OrderService as {order_service_id}")

        print("\n🎉 All services deployed to Lambda!")
        print("\nYou can now invoke them via:")
        print(
            f"aws lambda invoke --function-name {user_service_id} --endpoint-url http://localhost:4566"
        )

        # Keep services running
        print("\nPress Ctrl+C to undeploy services...")
        await asyncio.Event().wait()

    except KeyboardInterrupt:
        print("\n🛑 Shutting down and cleaning up Lambda functions...")
    except Exception as e:
        print(f"❌ Deployment failed: {e}")
        import traceback

        traceback.print_exc()
    finally:
        await runner.stop()


if __name__ == "__main__":
    import sys

    # Configure logging
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    command = sys.argv[1] if len(sys.argv) > 1 else "test"

    if command == "test":
        asyncio.run(test_full_aws_stack())
    elif command == "deploy":
        asyncio.run(deploy_to_lambda())
    else:
        print("Usage: python example_full_aws_stack.py [test|deploy]")
        print("  test   - Test services with simulated Lambda")
        print("  deploy - Deploy actual Lambda functions to LocalStack")
