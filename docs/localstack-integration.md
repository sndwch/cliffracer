# 🌐 Cliffracer LocalStack Integration

LocalStack allows you to run AWS services locally for development and testing, providing a complete AWS-compatible environment without cloud costs.

## 🎯 What is LocalStack?

LocalStack is a cloud service emulator that runs AWS services on your local machine:
- **Full AWS API compatibility** - Same SDKs, same code
- **Zero cloud costs** during development
- **Faster iteration** - No network latency
- **Complete isolation** - No impact on production resources
- **30+ AWS services** supported (SNS, SQS, DynamoDB, S3, Lambda, etc.)

## 🚀 Quick Start

### Prerequisites
- Python 3.13+
- Docker running
- Cliffracer project set up

### 1. Install LocalStack
```bash
pip install localstack boto3
```

### 2. Start LocalStack
```bash
localstack start -d
```

### 3. Verify Installation
```bash
curl http://localhost:4566/_localstack/health
```

### 4. Run Demo
```bash
python3 test_localstack_simple.py
```

## 📊 Demo Architecture

The LocalStack demo showcases a complete e-commerce microservices system using AWS services:

```
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│   Order Service │    │Inventory Service│    │ Payment Service │
│                 │    │                 │    │                 │
│   DynamoDB ──── │    │   SQS Queue ─── │    │   CloudWatch ── │
│                 │    │                 │    │                 │
└─────────┬───────┘    └─────────┬───────┘    └─────────┬───────┘
          │                      │                      │
          └──────────────────────┼──────────────────────┘
                                 │
                    ┌─────────────┴──────────┐
                    │     SNS Topic          │
                    │   (Event Broadcasting) │
                    └─────────────┬──────────┘
                                 │
          ┌──────────────────────┼──────────────────────┐
          │                      │                      │
┌─────────┴───────┐    ┌─────────┴───────┐    ┌─────────┴───────┐
│Fulfillment Svc  │    │ Analytics Svc   │    │   S3 Storage    │
│                 │    │                 │    │                 │
│   S3 Receipts ── │    │   CloudWatch ── │    │   File Storage  │
└─────────────────┘    └─────────────────┘    └─────────────────┘
```

## 🛠️ AWS Services Used

### **SNS (Simple Notification Service)**
- **Purpose**: Event broadcasting to multiple services
- **Use Case**: When an order is created, broadcast to all interested services
- **Code Example**:
```python
# Publish order created event
sns.publish(
    TopicArn=topic_arn,
    Message=json.dumps({
        'event_type': 'order_created',
        'order_id': order_id,
        'total_amount': float(total_amount)
    })
)
```

### **SQS (Simple Queue Service)**
- **Purpose**: Reliable message queuing between services
- **Use Case**: Each service has its own queue for processing events
- **Code Example**:
```python
# Each service processes its queue
response = sqs.receive_message(
    QueueUrl=queue_url,
    MaxNumberOfMessages=10,
    WaitTimeSeconds=2
)
```

### **DynamoDB**
- **Purpose**: NoSQL database for orders and inventory
- **Use Case**: Store order data, product inventory, customer info
- **Code Example**:
```python
# Store order in DynamoDB
orders_table.put_item(Item=order.model_dump(mode='json'))

# Update inventory
inventory_table.update_item(
    Key={'product_id': product_id},
    UpdateExpression='SET stock = stock - :qty',
    ExpressionAttributeValues={':qty': quantity}
)
```

### **S3 (Simple Storage Service)**
- **Purpose**: File storage for receipts and documents
- **Use Case**: Store shipping receipts, invoices, customer documents
- **Code Example**:
```python
# Store receipt in S3
s3.put_object(
    Bucket='ecommerce-receipts',
    Key=f"receipts/{order_id}.json",
    Body=json.dumps(receipt, indent=2)
)
```

### **CloudWatch**
- **Purpose**: Monitoring and metrics collection
- **Use Case**: Track business metrics, performance, errors
- **Code Example**:
```python
# Send business metrics
cloudwatch.put_metric_data(
    Namespace='Cliffracer/ECommerce',
    MetricData=[
        {
            'MetricName': 'OrdersCreated',
            'Value': 1,
            'Unit': 'Count'
        },
        {
            'MetricName': 'OrderValue',
            'Value': float(total_amount),
            'Unit': 'None'
        }
    ]
)
```

### **CloudWatch Logs**
- **Purpose**: Centralized application logging
- **Use Case**: Structured logging across all services
- **Code Example**:
```python
# Send structured logs
logs.put_log_events(
    logGroupName='/cliffracer/ecommerce',
    logStreamName='order-service',
    logEvents=[{
        'timestamp': int(time.time() * 1000),
        'message': json.dumps({
            'service': 'order_service',
            'action': 'order_created',
            'order_id': order_id
        })
    }]
)
```

## 🔄 Event Flow Example

Here's how a complete order flows through the system:

### 1. Order Creation
```python
# Customer places order
order = await order_service.create_order(customer_id, items)

# Order stored in DynamoDB
orders_table.put_item(Item=order_data)

# Event published to SNS
sns.publish(TopicArn=topic_arn, Message=event_data)
```

### 2. Inventory Processing
```python
# Inventory service receives SNS message via SQS
message = sqs.receive_message(QueueUrl=inventory_queue_url)

# Check and reserve inventory
inventory_table.update_item(
    Key={'product_id': product_id},
    UpdateExpression='SET reserved = reserved + :qty'
)
```

### 3. Payment Processing
```python
# Payment service processes payment
payment_result = await process_payment(order_data)

# Log result to CloudWatch
logs.put_log_events(
    logGroupName='/cliffracer/ecommerce',
    logEvents=[payment_log_entry]
)
```

### 4. Fulfillment
```python
# Create shipping receipt
receipt = create_shipping_receipt(order_data)

# Store in S3
s3.put_object(
    Bucket='receipts',
    Key=f'{order_id}.json',
    Body=json.dumps(receipt)
)
```

### 5. Analytics
```python
# Send metrics to CloudWatch
cloudwatch.put_metric_data(
    Namespace='Cliffracer/ECommerce',
    MetricData=business_metrics
)
```

## 📈 Monitoring & Observability

### **CloudWatch Metrics**
Track business and technical metrics:
- Orders created per minute
- Payment success rate
- Inventory levels
- Response times
- Error rates

### **CloudWatch Logs**
Centralized logging with structured data:
- All service logs in one place
- Correlation IDs for tracing
- Searchable JSON format
- Real-time log streaming

### **AWS Console Access**
LocalStack provides web interfaces:
- **LocalStack UI**: http://localhost:4566/_localstack/s3 (S3 browser)
- **CloudWatch**: http://localhost:4566/_aws/cloudwatch
- **DynamoDB**: Tables visible via AWS CLI

## 🧪 Testing & Development

### **Benefits of LocalStack for Development**

1. **Fast Iteration**
   - No network latency
   - Instant service startup
   - No AWS account needed

2. **Cost Effective**
   - Zero AWS charges
   - Unlimited testing
   - No resource limits

3. **Isolated Environment**
   - No impact on production
   - Complete data isolation
   - Reset anytime

4. **Full AWS Compatibility**
   - Same boto3 code
   - Same APIs and responses
   - Easy migration to AWS

### **Development Workflow**

```bash
# 1. Start LocalStack
localstack start -d

# 2. Run tests
python3 test_localstack_simple.py

# 3. Develop features
python3 examples/aws/localstack_demo.py

# 4. Deploy to AWS (same code!)
# Just change endpoint URLs
```

## 🔧 Configuration

### **Environment Variables**
```bash
# AWS credentials (LocalStack accepts any values)
export AWS_DEFAULT_REGION=us-east-1
export AWS_ACCESS_KEY_ID=test
export AWS_SECRET_ACCESS_KEY=test

# LocalStack endpoint
export AWS_ENDPOINT_URL=http://localhost:4566
```

### **Boto3 Client Configuration**
```python
# Configure boto3 clients for LocalStack
import boto3

sns = boto3.client(
    'sns',
    endpoint_url='http://localhost:4566',
    region_name='us-east-1'
)

sqs = boto3.client(
    'sqs',
    endpoint_url='http://localhost:4566',
    region_name='us-east-1'
)

dynamodb = boto3.resource(
    'dynamodb',
    endpoint_url='http://localhost:4566',
    region_name='us-east-1'
)
```

## 🚀 Production Deployment

### **Transitioning to Real AWS**

The same code works with real AWS by simply removing the `endpoint_url`:

```python
# Development (LocalStack)
sns = boto3.client(
    'sns',
    endpoint_url='http://localhost:4566',  # Remove this line
    region_name='us-east-1'
)

# Production (Real AWS)
sns = boto3.client(
    'sns',
    region_name='us-east-1'
)
```

### **Environment-based Configuration**
```python
import os

# Use LocalStack in development, real AWS in production
endpoint_url = os.getenv('AWS_ENDPOINT_URL')  # Set to LocalStack URL in dev

sns = boto3.client(
    'sns',
    endpoint_url=endpoint_url,  # None in production
    region_name='us-east-1'
)
```

## 🎯 Best Practices

### **1. Service Design**
- Use event-driven architecture
- Implement idempotent operations
- Handle partial failures gracefully
- Use correlation IDs for tracing

### **2. Error Handling**
```python
# Robust error handling
try:
    response = sns.publish(TopicArn=topic_arn, Message=message)
except ClientError as e:
    error_code = e.response['Error']['Code']
    if error_code == 'NotFound':
        # Handle missing topic
        logger.error(f"Topic not found: {topic_arn}")
    else:
        # Handle other errors
        logger.error(f"SNS error: {e}")
```

### **3. Resource Management**
```python
# Clean resource initialization
async def setup_infrastructure():
    """Setup AWS resources with proper error handling"""
    try:
        # Create topic
        topic_response = sns.create_topic(Name='order-events')
        topic_arn = topic_response['TopicArn']
        
        # Create queues
        for queue_name in queue_names:
            sqs.create_queue(QueueName=queue_name)
            
    except Exception as e:
        logger.error(f"Infrastructure setup failed: {e}")
        raise
```

### **4. Testing**
```python
# Test with LocalStack
@pytest.fixture
def aws_services():
    """Setup LocalStack services for testing"""
    # Start LocalStack container
    # Setup test resources
    # Return service clients
    pass

def test_order_creation(aws_services):
    """Test order creation with AWS services"""
    sns, sqs, dynamodb = aws_services
    
    # Test your services
    order = create_order(customer_data)
    assert order.status == 'pending'
    
    # Verify SNS message published
    messages = receive_sqs_messages(queue_url)
    assert len(messages) == 1
```

## 🔍 Troubleshooting

### **Common Issues**

1. **LocalStack not starting**
   ```bash
   # Check Docker is running
   docker ps
   
   # Check LocalStack logs
   localstack logs
   ```

2. **Connection refused**
   ```bash
   # Verify LocalStack is healthy
   curl http://localhost:4566/_localstack/health
   ```

3. **Permission errors**
   ```bash
   # LocalStack accepts any AWS credentials
   export AWS_ACCESS_KEY_ID=test
   export AWS_SECRET_ACCESS_KEY=test
   ```

4. **Service not available**
   ```bash
   # Check which services are running
   curl http://localhost:4566/_localstack/health | jq .services
   ```

### **Debugging Tips**

1. **Enable debug logging**
   ```python
   import logging
   logging.basicConfig(level=logging.DEBUG)
   ```

2. **Use LocalStack logs**
   ```bash
   localstack logs -f
   ```

3. **Check AWS CLI**
   ```bash
   aws --endpoint-url=http://localhost:4566 sns list-topics
   ```

## 📚 Further Reading

- [LocalStack Documentation](https://docs.localstack.cloud/)
- [AWS boto3 Documentation](https://boto3.amazonaws.com/v1/documentation/api/latest/index.html)
- [Cliffracer AWS Examples](../examples/aws/)

## 🎉 Summary

LocalStack integration with Cliffracer provides:

- **Complete AWS development environment** locally
- **Zero cloud costs** during development
- **Fast iteration** and testing
- **Production-ready architecture** patterns
- **Easy transition** to real AWS
- **Full observability** with CloudWatch

The demo showcases a real-world e-commerce system using multiple AWS services, demonstrating how Cliffracer applications can leverage the full power of AWS while maintaining local development efficiency.