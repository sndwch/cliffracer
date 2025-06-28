#!/usr/bin/env python3
"""
Simple LocalStack demo to verify it works
"""

import asyncio
import json
import os
from datetime import UTC, datetime

import boto3

# Configure for LocalStack
os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
os.environ["AWS_ACCESS_KEY_ID"] = "test"
os.environ["AWS_SECRET_ACCESS_KEY"] = "test"

LOCALSTACK_ENDPOINT = "http://localhost:4566"


async def demo():
    print("🚀 Cliffracer LocalStack Simple Demo")
    print("=" * 50)

    # Create AWS clients
    sns = boto3.client("sns", endpoint_url=LOCALSTACK_ENDPOINT, region_name="us-east-1")
    sqs = boto3.client("sqs", endpoint_url=LOCALSTACK_ENDPOINT, region_name="us-east-1")
    dynamodb = boto3.resource("dynamodb", endpoint_url=LOCALSTACK_ENDPOINT, region_name="us-east-1")
    s3 = boto3.client("s3", endpoint_url=LOCALSTACK_ENDPOINT, region_name="us-east-1")
    cloudwatch = boto3.client(
        "cloudwatch", endpoint_url=LOCALSTACK_ENDPOINT, region_name="us-east-1"
    )

    # 1. Create SNS topic
    print("\n📢 Creating SNS topic...")
    topic_response = sns.create_topic(Name="ecommerce-events")
    topic_arn = topic_response["TopicArn"]
    print(f"✅ Created: {topic_arn}")

    # 2. Create SQS queue
    print("\n📮 Creating SQS queue...")
    queue_response = sqs.create_queue(QueueName="order-processing")
    queue_url = queue_response["QueueUrl"]
    print(f"✅ Created: {queue_url}")

    # 3. Subscribe queue to topic
    print("\n🔗 Subscribing SQS to SNS...")
    queue_attrs = sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["QueueArn"])
    queue_arn = queue_attrs["Attributes"]["QueueArn"]
    sns.subscribe(TopicArn=topic_arn, Protocol="sqs", Endpoint=queue_arn)
    print("✅ Subscription created")

    # 4. Create DynamoDB table
    print("\n🗄️ Creating DynamoDB table...")
    try:
        table = dynamodb.create_table(
            TableName="orders",
            KeySchema=[{"AttributeName": "order_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "order_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        table.wait_until_exists()
        print("✅ Created: orders table")
    except Exception as e:
        if "ResourceInUseException" in str(e):
            print("⚠️ Table already exists")
            table = dynamodb.Table("orders")
        else:
            raise

    # 5. Create S3 bucket
    print("\n🪣 Creating S3 bucket...")
    try:
        s3.create_bucket(Bucket="ecommerce-receipts")
        print("✅ Created: ecommerce-receipts bucket")
    except Exception as e:
        if "BucketAlreadyExists" in str(e):
            print("⚠️ Bucket already exists")
        else:
            raise

    # 6. Simulate e-commerce workflow
    print("\n🛒 Simulating e-commerce order workflow...")

    # Create order
    order_data = {
        "order_id": "order-123",
        "customer_id": "customer-456",
        "items": [
            {"product": "laptop", "quantity": 1, "price": "1299.99"},
            {"product": "mouse", "quantity": 1, "price": "49.99"},
        ],
        "total": "1349.98",
        "timestamp": datetime.now(UTC).isoformat(),
    }

    # Store in DynamoDB
    table.put_item(Item=order_data)
    print(f"✅ Order stored in DynamoDB: {order_data['order_id']}")

    # Publish to SNS
    message = {
        "event_type": "order_created",
        "order_id": order_data["order_id"],
        "total": order_data["total"],
    }
    sns.publish(TopicArn=topic_arn, Message=json.dumps(message))
    print("✅ Event published to SNS: order_created")

    # Send metrics to CloudWatch
    cloudwatch.put_metric_data(
        Namespace="Cliffracer/ECommerce",
        MetricData=[
            {"MetricName": "OrdersCreated", "Value": 1, "Unit": "Count"},
            {"MetricName": "OrderValue", "Value": float(order_data["total"]), "Unit": "None"},
        ],
    )
    print("✅ Metrics sent to CloudWatch")

    # Store receipt in S3
    receipt = {
        "order_id": order_data["order_id"],
        "receipt_number": "RCP-123",
        "processed_at": datetime.now(UTC).isoformat(),
        "items": order_data["items"],
    }
    s3.put_object(
        Bucket="ecommerce-receipts",
        Key=f"receipts/{order_data['order_id']}.json",
        Body=json.dumps(receipt, indent=2),
    )
    print("✅ Receipt stored in S3")

    # Check for messages in SQS
    print("\n📬 Checking for messages in SQS...")
    await asyncio.sleep(1)  # Give time for message delivery

    response = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=1)
    messages = response.get("Messages", [])

    if messages:
        message = messages[0]
        body = json.loads(message["Body"])
        sns_message = json.loads(body["Message"])
        print(f"✅ Received message: {sns_message}")

        # Delete message
        sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=message["ReceiptHandle"])
        print("✅ Message processed and deleted")
    else:
        print("⚠️ No messages received (this is normal for a quick test)")

    print("\n🎉 LocalStack Demo Complete!")
    print("\n💡 What we demonstrated:")
    print("  📢 SNS for event broadcasting")
    print("  📮 SQS for reliable message queues")
    print("  🗄️ DynamoDB for data storage")
    print("  🪣 S3 for file storage")
    print("  📊 CloudWatch for monitoring")
    print("  🔄 Event-driven architecture")
    print("\n✅ LocalStack provides full AWS compatibility!")


if __name__ == "__main__":
    asyncio.run(demo())
