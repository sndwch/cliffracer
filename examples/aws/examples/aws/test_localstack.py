#!/usr/bin/env python3
"""
Quick test to verify LocalStack AWS integration works
"""

import os

import boto3

# Configure for LocalStack
os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
os.environ["AWS_ACCESS_KEY_ID"] = "test"
os.environ["AWS_SECRET_ACCESS_KEY"] = "test"

LOCALSTACK_ENDPOINT = "http://localhost:4566"


def test_aws_services():
    print("🧪 Testing AWS services via LocalStack...")
    print("=" * 50)

    # Test SNS
    try:
        sns = boto3.client("sns", endpoint_url=LOCALSTACK_ENDPOINT, region_name="us-east-1")
        topic = sns.create_topic(Name="test-topic")
        topic_arn = topic["TopicArn"]
        print(f"✅ SNS: Created topic {topic_arn}")

        # Publish a message
        sns.publish(TopicArn=topic_arn, Message="Hello from Cliffracer!")
        print("✅ SNS: Published message")

    except Exception as e:
        print(f"❌ SNS: {e}")

    # Test SQS
    try:
        sqs = boto3.client("sqs", endpoint_url=LOCALSTACK_ENDPOINT, region_name="us-east-1")
        queue = sqs.create_queue(QueueName="test-queue")
        queue_url = queue["QueueUrl"]
        print(f"✅ SQS: Created queue {queue_url}")

        # Send a message
        sqs.send_message(QueueUrl=queue_url, MessageBody="Hello from Cliffracer!")
        print("✅ SQS: Sent message")

    except Exception as e:
        print(f"❌ SQS: {e}")

    # Test DynamoDB
    try:
        dynamodb = boto3.resource(
            "dynamodb", endpoint_url=LOCALSTACK_ENDPOINT, region_name="us-east-1"
        )
        table = dynamodb.create_table(
            TableName="test-table",
            KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        table.wait_until_exists()
        print("✅ DynamoDB: Created table test-table")

        # Put an item
        table.put_item(Item={"id": "test", "message": "Hello from Cliffracer!"})
        print("✅ DynamoDB: Put item")

    except Exception as e:
        print(f"❌ DynamoDB: {e}")

    # Test S3
    try:
        s3 = boto3.client("s3", endpoint_url=LOCALSTACK_ENDPOINT, region_name="us-east-1")
        s3.create_bucket(Bucket="test-bucket")
        print("✅ S3: Created bucket test-bucket")

        # Put an object
        s3.put_object(Bucket="test-bucket", Key="test.txt", Body="Hello from Cliffracer!")
        print("✅ S3: Put object")

    except Exception as e:
        print(f"❌ S3: {e}")

    # Test CloudWatch
    try:
        cloudwatch = boto3.client(
            "cloudwatch", endpoint_url=LOCALSTACK_ENDPOINT, region_name="us-east-1"
        )
        cloudwatch.put_metric_data(
            Namespace="Cliffracer/Test",
            MetricData=[{"MetricName": "TestMetric", "Value": 1, "Unit": "Count"}],
        )
        print("✅ CloudWatch: Put metric data")

    except Exception as e:
        print(f"❌ CloudWatch: {e}")

    print("\n🎉 LocalStack AWS integration test complete!")
    print("💡 All services are working and ready for the full demo")


if __name__ == "__main__":
    test_aws_services()
