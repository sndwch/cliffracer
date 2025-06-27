# 🌐 AWS Examples with LocalStack

This directory contains examples demonstrating Cliffracer integration with AWS services via LocalStack.

## 🚀 Quick Start

### 1. Start LocalStack
```bash
localstack start -d
```

### 2. Run Simple Demo
```bash
python3 examples/aws/test_localstack_simple.py
```

### 3. Run Full E-commerce Demo
```bash
python3 examples/aws/localstack_demo.py
```

## 📁 Files

### **test_localstack_simple.py**
A simple demonstration showing basic AWS service integration:
- SNS topic creation and messaging
- SQS queue setup and message processing
- DynamoDB table operations
- S3 file storage
- CloudWatch metrics

**Purpose**: Quick verification that LocalStack integration works
**Runtime**: ~10 seconds

### **localstack_demo.py**
A comprehensive e-commerce microservices system using AWS:
- Multiple services communicating via SNS/SQS
- DynamoDB for data storage
- S3 for file storage
- CloudWatch for monitoring and logging
- Real business logic with order processing

**Purpose**: Full demonstration of event-driven AWS architecture
**Runtime**: Runs continuously until stopped

## 🏗️ Architecture

Both demos showcase the same architectural patterns:

```
Order Service ──┐
                ├─── SNS Topic ──── SQS Queues ──── Processing Services
Payment Service ─┘                                  │
                                                     ├─── DynamoDB
                                                     ├─── S3
                                                     └─── CloudWatch
```

## 🔧 Configuration

All examples use these LocalStack settings:
- **Endpoint**: http://localhost:4566
- **Region**: us-east-1
- **Credentials**: test/test (LocalStack accepts any values)

## 📊 What You'll See

### **Simple Demo Output**
```
🚀 Cliffracer LocalStack Simple Demo
==================================================
✅ Created: arn:aws:sns:us-east-1:000000000000:ecommerce-events
✅ Created: http://sqs.us-east-1.localhost.localstack.cloud:4566/000000000000/order-processing
✅ Order stored in DynamoDB: order-123
✅ Event published to SNS: order_created
✅ Metrics sent to CloudWatch
✅ Receipt stored in S3
✅ Received message: {'event_type': 'order_created', 'order_id': 'order-123', 'total': '1349.98'}
🎉 LocalStack Demo Complete!
```

### **Full Demo Output**
```
🚀 Cliffracer LocalStack AWS Demo
==================================================
🏗️ Setting up AWS infrastructure in LocalStack...
✅ Created SNS topic: arn:aws:sns:us-east-1:000000000000:order-events
✅ Created SQS queue and subscription: inventory-queue
✅ Created DynamoDB table: orders
✅ Created S3 bucket: cliffracer-ecommerce
📦 Order created: order_abc123 ($1299.99) - Published to SNS
📋 Inventory reserved for order order_abc123: 1 items
💳 Payment ✅ SUCCESS for order order_abc123: $1299.99
📦 Order fulfilled: order_abc123 - Receipt stored in S3
```

## 🎯 Learning Objectives

After running these examples, you'll understand:

1. **Event-Driven Architecture**: How services communicate via SNS/SQS
2. **Data Storage**: Using DynamoDB for NoSQL data
3. **File Storage**: Storing documents in S3
4. **Monitoring**: Collecting metrics with CloudWatch
5. **Local Development**: Running AWS services locally with LocalStack
6. **Production Readiness**: Same code works with real AWS

## 🔍 Monitoring

### **CloudWatch Metrics**
View metrics at: http://localhost:4566/_aws/cloudwatch
- OrdersCreated
- PaymentSuccessRate
- InventoryReservations
- OrdersFulfilled

### **S3 Browser**
Browse files at: http://localhost:4566/_aws/s3
- Receipts stored in `cliffracer-ecommerce` bucket
- JSON formatted shipping receipts

### **DynamoDB Tables**
Access via AWS CLI:
```bash
aws --endpoint-url=http://localhost:4566 dynamodb scan --table-name orders
```

## 🛠️ Extending the Examples

### **Add New Services**
1. Create a new service class
2. Subscribe to the SNS topic
3. Process messages from your SQS queue
4. Store results in DynamoDB/S3
5. Send metrics to CloudWatch

### **Add New Events**
1. Define new event types
2. Publish to SNS topic
3. Handle in relevant services

### **Add New Storage**
1. Create new DynamoDB tables
2. Add new S3 buckets
3. Use different CloudWatch namespaces

## 🚀 Next Steps

1. **Explore the code** - See how each AWS service is used
2. **Modify the demos** - Add your own business logic
3. **Read the documentation** - Check `docs/localstack-integration.md`
4. **Deploy to AWS** - Same code works in production!

## 💡 Tips

- LocalStack logs: `localstack logs -f`
- Health check: `curl http://localhost:4566/_localstack/health`
- Stop LocalStack: `localstack stop`
- Reset data: `localstack stop && localstack start -d`