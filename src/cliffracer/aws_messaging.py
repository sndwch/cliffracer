"""
AWS-based messaging implementation using SNS, SQS, and EventBridge
Provides cloud-native messaging with AWS services
"""

import asyncio
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import boto3
from botocore.exceptions import ClientError

from .abstract_messaging import (
    Message,
    MessageBroker,
    MessageClient,
    MessageClientFactory,
    MessageConfig,
    SubscriptionConfig,
)


class AWSClient(MessageClient):
    """AWS-based messaging client using SNS, SQS, and EventBridge"""

    def __init__(
        self,
        region: str = "us-east-1",
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        session_token: str | None = None,
        prefix: str = "cliffracer",
    ):
        self.region = region
        self.prefix = prefix
        self._connected = False
        self._subscriptions: dict[str, dict] = {}
        self._topics: dict[str, str] = {}  # subject -> topic_arn
        self._queues: dict[str, str] = {}  # subject -> queue_url
        self._event_rules: dict[str, str] = {}  # pattern -> rule_name

        # Initialize AWS clients
        session = boto3.Session(
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            aws_session_token=session_token,
            region_name=region,
        )

        self.sns = session.client("sns")
        self.sqs = session.client("sqs")
        self.events = session.client("events")
        self.lambda_client = session.client("lambda")

    async def connect(self, **kwargs) -> None:
        """Initialize AWS resources"""
        try:
            # Test connectivity
            self.sns.list_topics()
            self.sqs.list_queues()

            self._connected = True
            print(f"Connected to AWS messaging in region {self.region}")

        except Exception as e:
            raise ConnectionError(f"Failed to connect to AWS: {e}")

    async def disconnect(self) -> None:
        """Cleanup AWS resources"""
        # Stop any polling tasks
        for sub_id, sub_info in self._subscriptions.items():
            if "task" in sub_info:
                sub_info["task"].cancel()

        self._subscriptions.clear()
        self._connected = False
        print("Disconnected from AWS messaging")

    async def publish(
        self,
        subject: str,
        data: bytes,
        headers: dict[str, str] | None = None,
        config: MessageConfig | None = None,
    ) -> None:
        """Publish message using SNS or EventBridge"""
        if not self._connected:
            raise RuntimeError("Not connected to AWS")

        config = config or MessageConfig()
        message_attrs = headers or {}
        message_attrs.update(
            {
                "subject": subject,
                "timestamp": datetime.now(UTC).isoformat(),
                "delivery_mode": config.delivery_mode.value,
                "persistence": config.persistence.value,
            }
        )

        # Use EventBridge for event-driven patterns
        if "." in subject or "*" in subject or ">" in subject:
            await self._publish_to_eventbridge(subject, data, message_attrs)
        else:
            # Use SNS for direct messaging
            await self._publish_to_sns(subject, data, message_attrs)

    async def _publish_to_sns(self, subject: str, data: bytes, attrs: dict[str, str]):
        """Publish to SNS topic"""
        topic_arn = await self._ensure_topic(subject)

        # Convert attributes for SNS
        message_attributes = {}
        for key, value in attrs.items():
            message_attributes[key] = {"DataType": "String", "StringValue": str(value)}

        try:
            response = self.sns.publish(
                TopicArn=topic_arn,
                Message=data.decode("utf-8"),
                MessageAttributes=message_attributes,
            )
            print(f"Published to SNS topic {subject}: {response['MessageId']}")

        except ClientError as e:
            raise RuntimeError(f"Failed to publish to SNS: {e}")

    async def _publish_to_eventbridge(self, subject: str, data: bytes, attrs: dict[str, str]):
        """Publish to EventBridge"""
        try:
            # Parse data as JSON for EventBridge detail
            try:
                detail = json.loads(data.decode("utf-8"))
            except json.JSONDecodeError:
                detail = {"data": data.decode("utf-8")}

            # Add metadata to detail
            detail["_metadata"] = attrs

            response = self.events.put_events(
                Entries=[
                    {
                        "Source": f"{self.prefix}.microservices",
                        "DetailType": f"Message: {subject}",
                        "Detail": json.dumps(detail),
                        "EventBusName": "default",
                    }
                ]
            )

            if response["FailedEntryCount"] > 0:
                raise RuntimeError(f"Failed to publish to EventBridge: {response['Entries']}")

            print(f"Published to EventBridge: {subject}")

        except ClientError as e:
            raise RuntimeError(f"Failed to publish to EventBridge: {e}")

    async def request(
        self,
        subject: str,
        data: bytes,
        timeout: float = 30.0,
        headers: dict[str, str] | None = None,
    ) -> Message:
        """Send request and wait for response using SQS"""
        if not self._connected:
            raise RuntimeError("Not connected to AWS")

        # Create temporary response queue
        response_queue_name = f"{self.prefix}-response-{uuid.uuid4().hex[:8]}"
        response_queue_url = await self._create_queue(response_queue_name, temporary=True)

        try:
            # Add reply-to header
            headers = headers or {}
            headers["reply_to"] = response_queue_url
            headers["correlation_id"] = str(uuid.uuid4())

            # Send request
            await self.publish(subject, data, headers)

            # Wait for response
            start_time = asyncio.get_event_loop().time()
            while (asyncio.get_event_loop().time() - start_time) < timeout:
                response = self.sqs.receive_message(
                    QueueUrl=response_queue_url,
                    MaxNumberOfMessages=1,
                    WaitTimeSeconds=1,
                    MessageAttributeNames=["All"],
                )

                if "Messages" in response:
                    msg = response["Messages"][0]

                    # Delete message
                    self.sqs.delete_message(
                        QueueUrl=response_queue_url, ReceiptHandle=msg["ReceiptHandle"]
                    )

                    # Parse response
                    msg_attrs = {}
                    if "MessageAttributes" in msg:
                        for key, attr in msg["MessageAttributes"].items():
                            msg_attrs[key] = attr["StringValue"]

                    return Message(
                        subject=msg_attrs.get("subject", subject),
                        data=msg["Body"].encode(),
                        headers=msg_attrs,
                        correlation_id=headers["correlation_id"],
                    )

            raise TimeoutError(f"Request timeout after {timeout} seconds")

        finally:
            # Clean up temporary queue
            try:
                self.sqs.delete_queue(QueueUrl=response_queue_url)
            except ClientError:
                pass  # Queue might not exist

    async def subscribe(
        self, config: SubscriptionConfig, callback: Callable[[Message], Any]
    ) -> str:
        """Subscribe to messages using SQS"""
        if not self._connected:
            raise RuntimeError("Not connected to AWS")

        subscription_id = str(uuid.uuid4())

        # Create or get queue
        queue_name = config.durable_name or f"{self.prefix}-{config.subject}-{subscription_id[:8]}"
        queue_url = await self._create_queue(queue_name)

        # Subscribe queue to SNS topic if needed
        if not ("." in config.subject or "*" in config.subject):
            topic_arn = await self._ensure_topic(config.subject)
            await self._subscribe_queue_to_topic(queue_url, topic_arn)

        # Start polling task
        task = asyncio.create_task(self._poll_queue(queue_url, callback, config))

        self._subscriptions[subscription_id] = {
            "queue_url": queue_url,
            "config": config,
            "task": task,
        }

        return subscription_id

    async def _poll_queue(
        self, queue_url: str, callback: Callable[[Message], Any], config: SubscriptionConfig
    ):
        """Poll SQS queue for messages"""
        while True:
            try:
                response = self.sqs.receive_message(
                    QueueUrl=queue_url,
                    MaxNumberOfMessages=10,
                    WaitTimeSeconds=20,  # Long polling
                    MessageAttributeNames=["All"],
                )

                if "Messages" in response:
                    for msg in response["Messages"]:
                        try:
                            # Parse message
                            msg_attrs = {}
                            if "MessageAttributes" in msg:
                                for key, attr in msg["MessageAttributes"].items():
                                    msg_attrs[key] = attr["StringValue"]

                            message = Message(
                                subject=msg_attrs.get("subject", config.subject),
                                data=msg["Body"].encode(),
                                headers=msg_attrs,
                            )

                            # Call handler
                            if asyncio.iscoroutinefunction(callback):
                                await callback(message)
                            else:
                                callback(message)

                            # Delete message if auto-ack
                            if config.auto_ack:
                                self.sqs.delete_message(
                                    QueueUrl=queue_url, ReceiptHandle=msg["ReceiptHandle"]
                                )

                        except Exception as e:
                            print(f"Error processing message: {e}")

            except Exception as e:
                print(f"Error polling queue {queue_url}: {e}")
                await asyncio.sleep(5)  # Wait before retrying

    async def unsubscribe(self, subscription_id: str) -> None:
        """Unsubscribe from messages"""
        if subscription_id in self._subscriptions:
            sub_info = self._subscriptions[subscription_id]

            # Cancel polling task
            if "task" in sub_info:
                sub_info["task"].cancel()

            # Optionally delete queue if not durable
            if not sub_info["config"].durable_name:
                try:
                    self.sqs.delete_queue(QueueUrl=sub_info["queue_url"])
                except ClientError:
                    pass

            del self._subscriptions[subscription_id]

    async def create_stream(
        self, name: str, subjects: list[str], config: MessageConfig | None = None
    ) -> None:
        """Create EventBridge rule for stream-like behavior"""
        rule_name = f"{self.prefix}-stream-{name}"

        # Create EventBridge rule
        event_pattern = {
            "source": [f"{self.prefix}.microservices"],
            "detail-type": [f"Message: {subject}" for subject in subjects],
        }

        try:
            self.events.put_rule(
                Name=rule_name,
                EventPattern=json.dumps(event_pattern),
                State="ENABLED",
                Description=f"Stream for subjects: {', '.join(subjects)}",
            )

            self._event_rules[name] = rule_name
            print(f"Created EventBridge rule for stream: {name}")

        except ClientError as e:
            raise RuntimeError(f"Failed to create stream: {e}")

    async def delete_stream(self, name: str) -> None:
        """Delete EventBridge rule"""
        if name in self._event_rules:
            rule_name = self._event_rules[name]

            try:
                # Remove targets first
                targets = self.events.list_targets_by_rule(Rule=rule_name)
                if targets["Targets"]:
                    target_ids = [t["Id"] for t in targets["Targets"]]
                    self.events.remove_targets(Rule=rule_name, Ids=target_ids)

                # Delete rule
                self.events.delete_rule(Name=rule_name)
                del self._event_rules[name]

            except ClientError as e:
                raise RuntimeError(f"Failed to delete stream: {e}")

    async def get_stats(self) -> dict[str, Any]:
        """Get AWS messaging statistics"""
        return {
            "backend": "aws",
            "region": self.region,
            "connected": self._connected,
            "topics": len(self._topics),
            "queues": len(self._queues),
            "subscriptions": len(self._subscriptions),
            "event_rules": len(self._event_rules),
        }

    @property
    def is_connected(self) -> bool:
        return self._connected

    # Helper methods

    async def _ensure_topic(self, subject: str) -> str:
        """Create SNS topic if it doesn't exist"""
        if subject in self._topics:
            return self._topics[subject]

        topic_name = f"{self.prefix}-{subject.replace('.', '-').replace('*', 'wildcard').replace('>', 'all')}"

        try:
            response = self.sns.create_topic(Name=topic_name)
            topic_arn = response["TopicArn"]
            self._topics[subject] = topic_arn
            return topic_arn

        except ClientError as e:
            raise RuntimeError(f"Failed to create topic {topic_name}: {e}")

    async def _create_queue(self, queue_name: str, temporary: bool = False) -> str:
        """Create SQS queue"""
        attributes = {
            "VisibilityTimeoutSeconds": "30",
            "MessageRetentionPeriod": "1209600",  # 14 days
        }

        if temporary:
            attributes["MessageRetentionPeriod"] = "300"  # 5 minutes for temp queues

        try:
            response = self.sqs.create_queue(QueueName=queue_name, Attributes=attributes)
            return response["QueueUrl"]

        except ClientError as e:
            # Queue might already exist
            try:
                response = self.sqs.get_queue_url(QueueName=queue_name)
                return response["QueueUrl"]
            except ClientError:
                raise RuntimeError(f"Failed to create/get queue {queue_name}: {e}")

    async def _subscribe_queue_to_topic(self, queue_url: str, topic_arn: str):
        """Subscribe SQS queue to SNS topic"""
        # Get queue attributes
        queue_attrs = self.sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["QueueArn"])
        queue_arn = queue_attrs["Attributes"]["QueueArn"]

        # Subscribe queue to topic
        try:
            self.sns.subscribe(TopicArn=topic_arn, Protocol="sqs", Endpoint=queue_arn)

            # Set queue policy to allow SNS to deliver messages
            policy = {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"Service": "sns.amazonaws.com"},
                        "Action": "sqs:SendMessage",
                        "Resource": queue_arn,
                        "Condition": {"ArnEquals": {"aws:SourceArn": topic_arn}},
                    }
                ],
            }

            self.sqs.set_queue_attributes(
                QueueUrl=queue_url, Attributes={"Policy": json.dumps(policy)}
            )

        except ClientError as e:
            print(f"Warning: Failed to subscribe queue to topic: {e}")


class AWSMessageBroker(MessageBroker):
    """AWS-specific message broker"""

    async def setup_rpc_pattern(self, service_name: str) -> None:
        """Setup RPC pattern using SNS/SQS"""
        # Create service topic for RPC requests
        await self.client._ensure_topic(service_name)

    async def setup_pubsub_pattern(self, topics: list[str]) -> None:
        """Setup pub/sub using SNS"""
        for topic in topics:
            await self.client._ensure_topic(topic)

    async def setup_queue_pattern(self, queues: list[str]) -> None:
        """Setup queue pattern using SQS"""
        for queue in queues:
            await self.client._create_queue(f"{self.client.prefix}-{queue}")


# Register AWS client with factory
MessageClientFactory.register_client("aws", AWSClient)
