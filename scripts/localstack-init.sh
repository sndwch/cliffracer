#!/bin/bash

# LocalStack initialization script
# Sets up AWS resources for full Cultku stack emulation

set -e

echo "🚀 Initializing LocalStack for Cultku AWS stack..."

# Wait for LocalStack to be ready
echo "⏳ Waiting for LocalStack to be ready..."
while ! curl -s http://localstack:4566/_localstack/health | grep -q "running"; do
    echo "   Still waiting for LocalStack..."
    sleep 5
done

echo "✅ LocalStack is ready!"

# Set AWS CLI to use LocalStack
export AWS_DEFAULT_REGION=us-east-1
export AWS_ACCESS_KEY_ID=test
export AWS_SECRET_ACCESS_KEY=test
export AWS_ENDPOINT_URL=http://localstack:4566

echo "🔧 Setting up AWS resources..."

# 1. Create IAM role for Lambda
echo "📋 Creating Lambda execution role..."
aws iam create-role \
    --role-name cultku-lambda-role \
    --assume-role-policy-document '{
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "lambda.amazonaws.com"},
                "Action": "sts:AssumeRole"
            }
        ]
    }' \
    --endpoint-url $AWS_ENDPOINT_URL || echo "Role already exists"

# Attach policies to Lambda role
aws iam attach-role-policy \
    --role-name cultku-lambda-role \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole \
    --endpoint-url $AWS_ENDPOINT_URL || true

# Create custom policy for SNS/SQS/CloudWatch access
aws iam put-role-policy \
    --role-name cultku-lambda-role \
    --policy-name CultkuMessagingPolicy \
    --policy-document '{
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "sns:*",
                    "sqs:*",
                    "events:*",
                    "cloudwatch:*",
                    "logs:*"
                ],
                "Resource": "*"
            }
        ]
    }' \
    --endpoint-url $AWS_ENDPOINT_URL || true

# 2. Create SNS topics for messaging
echo "📢 Creating SNS topics..."
aws sns create-topic \
    --name cultku-user-service \
    --endpoint-url $AWS_ENDPOINT_URL || true

aws sns create-topic \
    --name cultku-notification-service \
    --endpoint-url $AWS_ENDPOINT_URL || true

aws sns create-topic \
    --name cultku-order-service \
    --endpoint-url $AWS_ENDPOINT_URL || true

aws sns create-topic \
    --name cultku-events \
    --endpoint-url $AWS_ENDPOINT_URL || true

# 3. Create SQS queues for request/response patterns
echo "📬 Creating SQS queues..."
aws sqs create-queue \
    --queue-name cultku-user-service-queue \
    --endpoint-url $AWS_ENDPOINT_URL || true

aws sqs create-queue \
    --queue-name cultku-notification-service-queue \
    --endpoint-url $AWS_ENDPOINT_URL || true

aws sqs create-queue \
    --queue-name cultku-order-service-queue \
    --endpoint-url $AWS_ENDPOINT_URL || true

# Create dead letter queues
aws sqs create-queue \
    --queue-name cultku-dlq \
    --endpoint-url $AWS_ENDPOINT_URL || true

# 4. Create EventBridge custom event bus
echo "🎭 Creating EventBridge resources..."
aws events create-event-bus \
    --name cultku-events \
    --endpoint-url $AWS_ENDPOINT_URL || true

# Create rules for event routing
aws events put-rule \
    --name cultku-user-events \
    --event-pattern '{
        "source": ["cultku.microservices"],
        "detail-type": ["Message: users.created", "Message: users.updated", "Message: users.deleted"]
    }' \
    --event-bus-name cultku-events \
    --endpoint-url $AWS_ENDPOINT_URL || true

aws events put-rule \
    --name cultku-order-events \
    --event-pattern '{
        "source": ["cultku.microservices"],
        "detail-type": ["Message: orders.created", "Message: orders.updated", "Message: orders.completed"]
    }' \
    --event-bus-name cultku-events \
    --endpoint-url $AWS_ENDPOINT_URL || true

# 5. Create CloudWatch Log Groups
echo "📊 Creating CloudWatch Log Groups..."
aws logs create-log-group \
    --log-group-name /aws/lambda/cultku-dev-user-service \
    --endpoint-url $AWS_ENDPOINT_URL || true

aws logs create-log-group \
    --log-group-name /aws/lambda/cultku-dev-notification-service \
    --endpoint-url $AWS_ENDPOINT_URL || true

aws logs create-log-group \
    --log-group-name /aws/lambda/cultku-dev-order-service \
    --endpoint-url $AWS_ENDPOINT_URL || true

aws logs create-log-group \
    --log-group-name /cultku/application \
    --endpoint-url $AWS_ENDPOINT_URL || true

# 6. Create S3 bucket for Lambda deployment packages
echo "🪣 Creating S3 bucket for deployments..."
aws s3 mb s3://cultku-lambda-deployments \
    --endpoint-url $AWS_ENDPOINT_URL || true

# 7. Create DynamoDB tables for state management (optional)
echo "🗃️ Creating DynamoDB tables..."
aws dynamodb create-table \
    --table-name cultku-service-state \
    --attribute-definitions \
        AttributeName=service_name,AttributeType=S \
        AttributeName=instance_id,AttributeType=S \
    --key-schema \
        AttributeName=service_name,KeyType=HASH \
        AttributeName=instance_id,KeyType=RANGE \
    --billing-mode PAY_PER_REQUEST \
    --endpoint-url $AWS_ENDPOINT_URL || true

aws dynamodb create-table \
    --table-name cultku-metrics \
    --attribute-definitions \
        AttributeName=metric_name,AttributeType=S \
        AttributeName=timestamp,AttributeType=S \
    --key-schema \
        AttributeName=metric_name,KeyType=HASH \
        AttributeName=timestamp,KeyType=RANGE \
    --billing-mode PAY_PER_REQUEST \
    --endpoint-url $AWS_ENDPOINT_URL || true

# 8. Create API Gateway for HTTP endpoints (optional)
echo "🌐 Creating API Gateway..."
aws apigateway create-rest-api \
    --name cultku-api \
    --description "Cultku Microservices API" \
    --endpoint-url $AWS_ENDPOINT_URL || true

# 9. Create CloudWatch dashboards
echo "📈 Creating CloudWatch dashboards..."
aws cloudwatch put-dashboard \
    --dashboard-name "Cultku-Services-Overview" \
    --dashboard-body '{
        "widgets": [
            {
                "type": "metric",
                "properties": {
                    "metrics": [
                        ["Cultku/Services", "requests", "service", "user_service"],
                        [".", "execution_time", ".", "."],
                        [".", "errors", ".", "."]
                    ],
                    "period": 300,
                    "stat": "Average",
                    "region": "us-east-1",
                    "title": "User Service Metrics"
                }
            },
            {
                "type": "metric",
                "properties": {
                    "metrics": [
                        ["AWS/Lambda", "Invocations", "FunctionName", "cultku-dev-user-service"],
                        [".", "Duration", ".", "."],
                        [".", "Errors", ".", "."]
                    ],
                    "period": 300,
                    "stat": "Sum",
                    "region": "us-east-1",
                    "title": "Lambda Metrics"
                }
            }
        ]
    }' \
    --endpoint-url $AWS_ENDPOINT_URL || true

# 10. Create CloudWatch alarms
echo "🚨 Creating CloudWatch alarms..."
aws cloudwatch put-metric-alarm \
    --alarm-name "cultku-high-error-rate" \
    --alarm-description "High error rate detected" \
    --metric-name "errors" \
    --namespace "Cultku/Services" \
    --statistic "Sum" \
    --period 300 \
    --threshold 10 \
    --comparison-operator "GreaterThanThreshold" \
    --evaluation-periods 2 \
    --endpoint-url $AWS_ENDPOINT_URL || true

aws cloudwatch put-metric-alarm \
    --alarm-name "cultku-lambda-high-duration" \
    --alarm-description "Lambda function duration too high" \
    --metric-name "Duration" \
    --namespace "AWS/Lambda" \
    --statistic "Average" \
    --period 300 \
    --threshold 5000 \
    --comparison-operator "GreaterThanThreshold" \
    --evaluation-periods 2 \
    --dimensions Name=FunctionName,Value=cultku-dev-user-service \
    --endpoint-url $AWS_ENDPOINT_URL || true

# 11. Create Secrets Manager secrets for configuration
echo "🔐 Creating secrets..."
aws secretsmanager create-secret \
    --name cultku/database \
    --description "Database connection details" \
    --secret-string '{
        "host": "localhost",
        "port": "5432",
        "database": "cultku",
        "username": "cultku_user",
        "password": "cultku_password"
    }' \
    --endpoint-url $AWS_ENDPOINT_URL || true

aws secretsmanager create-secret \
    --name cultku/api-keys \
    --description "External API keys" \
    --secret-string '{
        "email_service_key": "test-key-123",
        "payment_gateway_key": "test-payment-key"
    }' \
    --endpoint-url $AWS_ENDPOINT_URL || true

echo "✅ LocalStack initialization complete!"
echo ""
echo "🎯 Available services:"
echo "   - SNS: http://localhost:4566 (topics: cultku-user-service, cultku-notification-service, cultku-order-service)"
echo "   - SQS: http://localhost:4566 (queues: cultku-*-service-queue, cultku-dlq)"
echo "   - Lambda: http://localhost:4566 (functions will be deployed by services)"
echo "   - CloudWatch: http://localhost:4566 (metrics, logs, alarms, dashboards)"
echo "   - EventBridge: http://localhost:4566 (event bus: cultku-events)"
echo "   - DynamoDB: http://localhost:4566 (tables: cultku-service-state, cultku-metrics)"
echo "   - S3: http://localhost:4566 (bucket: cultku-lambda-deployments)"
echo "   - Secrets Manager: http://localhost:4566 (secrets: cultku/database, cultku/api-keys)"
echo ""
echo "🔧 AWS CLI configuration:"
echo "   export AWS_ENDPOINT_URL=http://localhost:4566"
echo "   export AWS_DEFAULT_REGION=us-east-1"
echo "   export AWS_ACCESS_KEY_ID=test"
echo "   export AWS_SECRET_ACCESS_KEY=test"
echo ""
echo "🚀 Ready to deploy Cultku services!"