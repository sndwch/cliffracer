#!/usr/bin/env python3
"""
Cliffracer LocalStack Demo - Multi-Service E-commerce System

This demo showcases Cliffracer running with AWS services via LocalStack:
- SNS for event broadcasting
- SQS for reliable message queues
- DynamoDB for data storage
- CloudWatch for monitoring
- S3 for file storage
- Lambda for serverless processing

Architecture:
- Order Service: Creates orders, stores in DynamoDB, publishes to SNS
- Inventory Service: Listens to SQS, manages stock in DynamoDB
- Payment Service: Processes payments, logs to CloudWatch
- Fulfillment Service: Handles shipping, stores receipts in S3
- Analytics Service: Collects metrics in CloudWatch
"""

import asyncio
import json
import time
import uuid
from datetime import datetime, UTC
from decimal import Decimal
from typing import Dict, List, Optional

import boto3
from pydantic import BaseModel, Field

# Configure boto3 for LocalStack
import os
os.environ['AWS_DEFAULT_REGION'] = 'us-east-1'
os.environ['AWS_ACCESS_KEY_ID'] = 'test'
os.environ['AWS_SECRET_ACCESS_KEY'] = 'test'

# LocalStack endpoint
LOCALSTACK_ENDPOINT = 'http://localhost:4566'

# AWS clients pointing to LocalStack
sns = boto3.client('sns', endpoint_url=LOCALSTACK_ENDPOINT, region_name='us-east-1')
sqs = boto3.client('sqs', endpoint_url=LOCALSTACK_ENDPOINT, region_name='us-east-1')
dynamodb = boto3.resource('dynamodb', endpoint_url=LOCALSTACK_ENDPOINT, region_name='us-east-1')
s3 = boto3.client('s3', endpoint_url=LOCALSTACK_ENDPOINT, region_name='us-east-1')
cloudwatch = boto3.client('cloudwatch', endpoint_url=LOCALSTACK_ENDPOINT, region_name='us-east-1')
logs = boto3.client('logs', endpoint_url=LOCALSTACK_ENDPOINT, region_name='us-east-1')

class Product(BaseModel):
    product_id: str
    name: str
    price: Decimal
    stock: int

class OrderItem(BaseModel):
    product_id: str
    name: str
    quantity: int
    price: Decimal

class Order(BaseModel):
    order_id: str
    customer_id: str
    items: List[OrderItem]
    total_amount: Decimal
    status: str
    created_at: str

class AWSInfrastructure:
    """Setup AWS infrastructure in LocalStack"""
    
    def __init__(self):
        self.topic_arn = None
        self.queue_urls = {}
        self.table_names = {}
        self.bucket_name = 'cliffracer-ecommerce'
        
    async def setup(self):
        """Initialize all AWS resources"""
        print("🏗️ Setting up AWS infrastructure in LocalStack...")
        
        # Create SNS topic for order events
        topic_response = sns.create_topic(Name='order-events')
        self.topic_arn = topic_response['TopicArn']
        print(f"✅ Created SNS topic: {self.topic_arn}")
        
        # Create SQS queues
        queues = ['inventory-queue', 'payment-queue', 'fulfillment-queue', 'analytics-queue']
        for queue_name in queues:
            response = sqs.create_queue(QueueName=queue_name)
            self.queue_urls[queue_name] = response['QueueUrl']
            
            # Subscribe queue to SNS topic
            queue_attributes = sqs.get_queue_attributes(
                QueueUrl=self.queue_urls[queue_name],
                AttributeNames=['QueueArn']
            )
            queue_arn = queue_attributes['Attributes']['QueueArn']
            
            sns.subscribe(
                TopicArn=self.topic_arn,
                Protocol='sqs',
                Endpoint=queue_arn
            )
            print(f"✅ Created SQS queue and subscription: {queue_name}")
        
        # Create DynamoDB tables
        tables = {
            'orders': {
                'KeySchema': [{'AttributeName': 'order_id', 'KeyType': 'HASH'}],
                'AttributeDefinitions': [{'AttributeName': 'order_id', 'AttributeType': 'S'}]
            },
            'products': {
                'KeySchema': [{'AttributeName': 'product_id', 'KeyType': 'HASH'}],
                'AttributeDefinitions': [{'AttributeName': 'product_id', 'AttributeType': 'S'}]
            },
            'inventory': {
                'KeySchema': [{'AttributeName': 'product_id', 'KeyType': 'HASH'}],
                'AttributeDefinitions': [{'AttributeName': 'product_id', 'AttributeType': 'S'}]
            }
        }
        
        for table_name, schema in tables.items():
            try:
                table = dynamodb.create_table(
                    TableName=table_name,
                    KeySchema=schema['KeySchema'],
                    AttributeDefinitions=schema['AttributeDefinitions'],
                    BillingMode='PAY_PER_REQUEST'
                )
                table.wait_until_exists()
                self.table_names[table_name] = table_name
                print(f"✅ Created DynamoDB table: {table_name}")
            except Exception as e:
                if 'ResourceInUseException' in str(e):
                    print(f"⚠️ DynamoDB table {table_name} already exists")
                    self.table_names[table_name] = table_name
                else:
                    raise
        
        # Create S3 bucket
        try:
            s3.create_bucket(Bucket=self.bucket_name)
            print(f"✅ Created S3 bucket: {self.bucket_name}")
        except Exception as e:
            if 'BucketAlreadyExists' in str(e):
                print(f"⚠️ S3 bucket {self.bucket_name} already exists")
            else:
                raise
        
        # Create CloudWatch log group
        try:
            logs.create_log_group(logGroupName='/cliffracer/ecommerce')
            print("✅ Created CloudWatch log group: /cliffracer/ecommerce")
        except Exception as e:
            if 'ResourceAlreadyExistsException' in str(e):
                print("⚠️ CloudWatch log group already exists")
            else:
                raise
        
        # Seed product data
        await self.seed_products()
        
        print("🎉 AWS infrastructure setup complete!")
        
    async def seed_products(self):
        """Seed initial product and inventory data"""
        products_table = dynamodb.Table('products')
        inventory_table = dynamodb.Table('inventory')
        
        products = [
            {'product_id': 'laptop-pro', 'name': 'Professional Laptop', 'price': Decimal('1299.99')},
            {'product_id': 'smartphone-x', 'name': 'Smartphone X', 'price': Decimal('899.99')},
            {'product_id': 'tablet-air', 'name': 'Tablet Air', 'price': Decimal('599.99')},
            {'product_id': 'headphones', 'name': 'Premium Headphones', 'price': Decimal('199.99')},
            {'product_id': 'keyboard', 'name': 'Mechanical Keyboard', 'price': Decimal('149.99')}
        ]
        
        for product in products:
            # Add to products table
            products_table.put_item(Item=product)
            
            # Add to inventory table
            inventory_table.put_item(Item={
                'product_id': product['product_id'],
                'stock': 100,
                'reserved': 0
            })
        
        print("✅ Seeded product and inventory data")

class OrderService:
    """Handles order creation and management"""
    
    def __init__(self, infrastructure: AWSInfrastructure):
        self.infrastructure = infrastructure
        self.orders_table = dynamodb.Table('orders')
        self.metrics_sent = 0
        
    async def create_order(self, customer_id: str, items: List[OrderItem]) -> Order:
        """Create a new order"""
        order_id = f"order_{uuid.uuid4().hex[:8]}"
        total_amount = sum(item.price * item.quantity for item in items)
        
        order = Order(
            order_id=order_id,
            customer_id=customer_id,
            items=items,
            total_amount=total_amount,
            status="pending",
            created_at=datetime.now(UTC).isoformat()
        )
        
        # Store in DynamoDB
        self.orders_table.put_item(Item=order.model_dump(mode='json'))
        
        # Publish to SNS
        message = {
            'event_type': 'order_created',
            'order_id': order_id,
            'customer_id': customer_id,
            'total_amount': float(total_amount),
            'items': [item.model_dump(mode='json') for item in items],
            'timestamp': order.created_at
        }
        
        sns.publish(
            TopicArn=self.infrastructure.topic_arn,
            Message=json.dumps(message),
            Subject='Order Created'
        )
        
        # Send metrics to CloudWatch
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
        self.metrics_sent += 2
        
        # Log to CloudWatch
        logs.put_log_events(
            logGroupName='/cliffracer/ecommerce',
            logStreamName='order-service',
            logEvents=[
                {
                    'timestamp': int(time.time() * 1000),
                    'message': json.dumps({
                        'service': 'order_service',
                        'action': 'order_created',
                        'order_id': order_id,
                        'customer_id': customer_id,
                        'total_amount': float(total_amount),
                        'item_count': len(items)
                    })
                }
            ]
        )
        
        print(f"📦 Order created: {order_id} (${total_amount}) - Published to SNS")
        return order

class InventoryService:
    """Manages inventory and stock levels"""
    
    def __init__(self, infrastructure: AWSInfrastructure):
        self.infrastructure = infrastructure
        self.queue_url = infrastructure.queue_urls['inventory-queue']
        self.inventory_table = dynamodb.Table('inventory')
        self.processed_orders = 0
        
    async def process_messages(self):
        """Process inventory messages from SQS"""
        while True:
            try:
                # Poll SQS for messages
                response = sqs.receive_message(
                    QueueUrl=self.queue_url,
                    MaxNumberOfMessages=10,
                    WaitTimeSeconds=2
                )
                
                messages = response.get('Messages', [])
                for message in messages:
                    await self.handle_message(message)
                    
                    # Delete processed message
                    sqs.delete_message(
                        QueueUrl=self.queue_url,
                        ReceiptHandle=message['ReceiptHandle']
                    )
                    
            except Exception as e:
                print(f"❌ Error processing inventory messages: {e}")
                await asyncio.sleep(1)
                
    async def handle_message(self, message):
        """Handle individual inventory message"""
        try:
            # Parse SNS message
            body = json.loads(message['Body'])
            if 'Message' in body:
                sns_message = json.loads(body['Message'])
                
                if sns_message.get('event_type') == 'order_created':
                    await self.reserve_inventory(sns_message)
                    
        except Exception as e:
            print(f"❌ Error handling inventory message: {e}")
            
    async def reserve_inventory(self, order_data):
        """Reserve inventory for an order"""
        order_id = order_data['order_id']
        items = order_data['items']
        
        try:
            # Check and reserve inventory for each item
            reservations_made = []
            for item in items:
                product_id = item['product_id']
                quantity = item['quantity']
                
                # Get current inventory
                response = self.inventory_table.get_item(Key={'product_id': product_id})
                if 'Item' not in response:
                    raise Exception(f"Product {product_id} not found")
                    
                inventory = response['Item']
                available = inventory['stock'] - inventory['reserved']
                
                if available >= quantity:
                    # Reserve inventory
                    self.inventory_table.update_item(
                        Key={'product_id': product_id},
                        UpdateExpression='SET reserved = reserved + :qty',
                        ExpressionAttributeValues={':qty': quantity}
                    )
                    reservations_made.append({'product_id': product_id, 'quantity': quantity})
                    
            self.processed_orders += 1
            
            # Send metrics
            cloudwatch.put_metric_data(
                Namespace='Cliffracer/ECommerce',
                MetricData=[
                    {
                        'MetricName': 'InventoryReservations',
                        'Value': len(reservations_made),
                        'Unit': 'Count'
                    }
                ]
            )
            
            print(f"📋 Inventory reserved for order {order_id}: {len(reservations_made)} items")
            
        except Exception as e:
            print(f"❌ Failed to reserve inventory for order {order_id}: {e}")

class PaymentService:
    """Processes payments with CloudWatch logging"""
    
    def __init__(self, infrastructure: AWSInfrastructure):
        self.infrastructure = infrastructure
        self.queue_url = infrastructure.queue_urls['payment-queue']
        self.processed_payments = 0
        self.successful_payments = 0
        
    async def process_messages(self):
        """Process payment messages from SQS"""
        while True:
            try:
                response = sqs.receive_message(
                    QueueUrl=self.queue_url,
                    MaxNumberOfMessages=10,
                    WaitTimeSeconds=2
                )
                
                messages = response.get('Messages', [])
                for message in messages:
                    await self.handle_message(message)
                    
                    sqs.delete_message(
                        QueueUrl=self.queue_url,
                        ReceiptHandle=message['ReceiptHandle']
                    )
                    
            except Exception as e:
                print(f"❌ Error processing payment messages: {e}")
                await asyncio.sleep(1)
                
    async def handle_message(self, message):
        """Handle individual payment message"""
        try:
            body = json.loads(message['Body'])
            if 'Message' in body:
                sns_message = json.loads(body['Message'])
                
                if sns_message.get('event_type') == 'order_created':
                    await self.process_payment(sns_message)
                    
        except Exception as e:
            print(f"❌ Error handling payment message: {e}")
            
    async def process_payment(self, order_data):
        """Process payment for an order"""
        order_id = order_data['order_id']
        amount = order_data['total_amount']
        
        # Simulate payment processing delay
        await asyncio.sleep(0.2)
        
        # Simulate 90% success rate
        import random
        success = random.random() < 0.9
        
        self.processed_payments += 1
        if success:
            self.successful_payments += 1
            
        # Log detailed payment info to CloudWatch
        logs.put_log_events(
            logGroupName='/cliffracer/ecommerce',
            logStreamName='payment-service',
            logEvents=[
                {
                    'timestamp': int(time.time() * 1000),
                    'message': json.dumps({
                        'service': 'payment_service',
                        'action': 'payment_processed',
                        'order_id': order_id,
                        'amount': amount,
                        'success': success,
                        'payment_id': f"pay_{uuid.uuid4().hex[:8]}"
                    })
                }
            ]
        )
        
        # Send metrics
        cloudwatch.put_metric_data(
            Namespace='Cliffracer/ECommerce',
            MetricData=[
                {
                    'MetricName': 'PaymentsProcessed',
                    'Value': 1,
                    'Unit': 'Count'
                },
                {
                    'MetricName': 'PaymentSuccessRate',
                    'Value': self.successful_payments / self.processed_payments * 100,
                    'Unit': 'Percent'
                }
            ]
        )
        
        status = "✅ SUCCESS" if success else "❌ FAILED"
        print(f"💳 Payment {status} for order {order_id}: ${amount}")

class FulfillmentService:
    """Handles order fulfillment and S3 storage"""
    
    def __init__(self, infrastructure: AWSInfrastructure):
        self.infrastructure = infrastructure
        self.queue_url = infrastructure.queue_urls['fulfillment-queue']
        self.fulfilled_orders = 0
        
    async def process_messages(self):
        """Process fulfillment messages from SQS"""
        while True:
            try:
                response = sqs.receive_message(
                    QueueUrl=self.queue_url,
                    MaxNumberOfMessages=10,
                    WaitTimeSeconds=2
                )
                
                messages = response.get('Messages', [])
                for message in messages:
                    await self.handle_message(message)
                    
                    sqs.delete_message(
                        QueueUrl=self.queue_url,
                        ReceiptHandle=message['ReceiptHandle']
                    )
                    
            except Exception as e:
                print(f"❌ Error processing fulfillment messages: {e}")
                await asyncio.sleep(1)
                
    async def handle_message(self, message):
        """Handle individual fulfillment message"""
        try:
            body = json.loads(message['Body'])
            if 'Message' in body:
                sns_message = json.loads(body['Message'])
                
                if sns_message.get('event_type') == 'order_created':
                    await self.fulfill_order(sns_message)
                    
        except Exception as e:
            print(f"❌ Error handling fulfillment message: {e}")
            
    async def fulfill_order(self, order_data):
        """Fulfill an order and store receipt in S3"""
        order_id = order_data['order_id']
        
        # Create shipping receipt
        receipt = {
            'order_id': order_id,
            'customer_id': order_data['customer_id'],
            'items': order_data['items'],
            'total_amount': order_data['total_amount'],
            'shipped_at': datetime.now(UTC).isoformat(),
            'tracking_number': f"TRK{uuid.uuid4().hex[:10].upper()}",
            'carrier': 'FastShip Express'
        }
        
        # Store receipt in S3
        s3.put_object(
            Bucket=self.infrastructure.bucket_name,
            Key=f"receipts/{order_id}.json",
            Body=json.dumps(receipt, indent=2),
            ContentType='application/json'
        )
        
        self.fulfilled_orders += 1
        
        # Send metrics
        cloudwatch.put_metric_data(
            Namespace='Cliffracer/ECommerce',
            MetricData=[
                {
                    'MetricName': 'OrdersFulfilled',
                    'Value': 1,
                    'Unit': 'Count'
                }
            ]
        )
        
        print(f"📦 Order fulfilled: {order_id} - Receipt stored in S3 (Tracking: {receipt['tracking_number']})")

class MetricsCollector:
    """Collects and displays system metrics"""
    
    def __init__(self):
        self.start_time = time.time()
        
    async def show_metrics(self, services):
        """Display current system metrics from CloudWatch"""
        try:
            # Get metrics from CloudWatch
            end_time = datetime.now(UTC)
            start_time = datetime.fromtimestamp(self.start_time, UTC)
            
            # Query CloudWatch metrics
            metrics_data = {}
            metric_names = ['OrdersCreated', 'PaymentsProcessed', 'InventoryReservations', 'OrdersFulfilled']
            
            for metric_name in metric_names:
                response = cloudwatch.get_metric_statistics(
                    Namespace='Cliffracer/ECommerce',
                    MetricName=metric_name,
                    StartTime=start_time,
                    EndTime=end_time,
                    Period=60,
                    Statistics=['Sum']
                )
                
                total = sum(point['Sum'] for point in response['Datapoints'])
                metrics_data[metric_name] = total
            
            # Display metrics
            uptime = time.time() - self.start_time
            print("\n" + "="*70)
            print("📊 CLIFFRACER AWS/LOCALSTACK METRICS")
            print("="*70)
            print(f"⏱️  System Uptime: {uptime:.1f}s")
            print(f"🏗️  AWS Services: SNS, SQS, DynamoDB, S3, CloudWatch")
            print()
            
            print("📈 CLOUDWATCH METRICS:")
            print(f"   Orders Created: {metrics_data.get('OrdersCreated', 0)}")
            print(f"   Payments Processed: {metrics_data.get('PaymentsProcessed', 0)}")
            print(f"   Inventory Reservations: {metrics_data.get('InventoryReservations', 0)}")
            print(f"   Orders Fulfilled: {metrics_data.get('OrdersFulfilled', 0)}")
            print()
            
            print("🔄 SERVICE STATUS:")
            print(f"   Order Service: {services['order'].metrics_sent} metrics sent")
            print(f"   Inventory Service: {services['inventory'].processed_orders} orders processed")
            print(f"   Payment Service: {services['payment'].processed_payments} payments processed")
            print(f"   Fulfillment Service: {services['fulfillment'].fulfilled_orders} orders fulfilled")
            print()
            
            print("🌐 AWS ENDPOINTS:")
            print(f"   LocalStack: http://localhost:4566")
            print(f"   CloudWatch Logs: http://localhost:4566/_aws/logs")
            print(f"   S3 Browser: http://localhost:4566/_aws/s3")
            print("="*70)
            
        except Exception as e:
            print(f"❌ Error collecting metrics: {e}")

async def generate_orders(order_service: OrderService):
    """Generate realistic orders for the demo"""
    products = [
        ('laptop-pro', 'Professional Laptop', Decimal('1299.99')),
        ('smartphone-x', 'Smartphone X', Decimal('899.99')),
        ('tablet-air', 'Tablet Air', Decimal('599.99')),
        ('headphones', 'Premium Headphones', Decimal('199.99')),
        ('keyboard', 'Mechanical Keyboard', Decimal('149.99'))
    ]
    
    order_count = 1
    
    while True:
        try:
            # Create random order
            num_items = random.randint(1, 3)
            items = []
            
            import random
            for _ in range(num_items):
                product_id, name, price = random.choice(products)
                quantity = random.randint(1, 2)
                items.append(OrderItem(
                    product_id=product_id,
                    name=name,
                    quantity=quantity,
                    price=price
                ))
            
            customer_id = f"customer_{random.randint(1000, 9999)}"
            order = await order_service.create_order(customer_id, items)
            
            print(f"🛒 Order #{order_count} created: {order.order_id} (${order.total_amount})")
            order_count += 1
            
            # Wait before next order
            await asyncio.sleep(random.uniform(2, 5))
            
        except Exception as e:
            print(f"❌ Error generating order: {e}")
            await asyncio.sleep(2)

async def main():
    """Run the LocalStack AWS demo"""
    print("🚀 Cliffracer LocalStack AWS Demo")
    print("=" * 50)
    print()
    print("This demo showcases:")
    print("  🌐 SNS for event broadcasting")
    print("  📮 SQS for reliable messaging")
    print("  🗄️  DynamoDB for data storage")
    print("  📊 CloudWatch for monitoring")
    print("  🪣 S3 for file storage")
    print("  📋 CloudWatch Logs for logging")
    print()
    print("💡 All AWS services running in LocalStack!")
    print("🔄 Watch the AWS services work together...")
    print()
    print("Press Ctrl+C to stop the demo")
    print("=" * 50)
    
    # Setup infrastructure
    infrastructure = AWSInfrastructure()
    await infrastructure.setup()
    
    # Create services
    services = {
        'order': OrderService(infrastructure),
        'inventory': InventoryService(infrastructure),
        'payment': PaymentService(infrastructure),
        'fulfillment': FulfillmentService(infrastructure),
        'metrics': MetricsCollector()
    }
    
    print("\n🚀 Starting microservices...")
    await asyncio.sleep(2)
    
    # Start all services
    tasks = [
        asyncio.create_task(generate_orders(services['order'])),
        asyncio.create_task(services['inventory'].process_messages()),
        asyncio.create_task(services['payment'].process_messages()),
        asyncio.create_task(services['fulfillment'].process_messages()),
    ]
    
    # Start metrics monitoring
    async def show_periodic_metrics():
        await asyncio.sleep(10)  # Wait for first order
        while True:
            await services['metrics'].show_metrics(services)
            await asyncio.sleep(15)
    
    tasks.append(asyncio.create_task(show_periodic_metrics()))
    
    try:
        # Run until interrupted
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        print("\n\n🛑 Demo stopped by user")
        
        # Show final metrics
        print("\n📊 FINAL METRICS:")
        await services['metrics'].show_metrics(services)
        
        print("\n💡 Key AWS Features Demonstrated:")
        print("  🌐 SNS Topics for event broadcasting")
        print("  📮 SQS Queues for reliable messaging")
        print("  🗄️  DynamoDB for NoSQL data storage")
        print("  📊 CloudWatch Metrics for monitoring")
        print("  📋 CloudWatch Logs for centralized logging")
        print("  🪣 S3 for file storage (receipts)")
        print("  ⚡ Event-driven architecture")
        
        print("\n🌟 LocalStack provides full AWS compatibility:")
        print("  ✅ Same APIs as real AWS")
        print("  ✅ Local development and testing")
        print("  ✅ No AWS costs during development")
        print("  ✅ Easy transition to production AWS")

if __name__ == "__main__":
    import random
    asyncio.run(main())