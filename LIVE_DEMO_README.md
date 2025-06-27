# 🚀 Cliffracer Live Demo - E-commerce System

This comprehensive demo showcases a realistic e-commerce microservices system with full monitoring capabilities.

## 🎯 What This Demo Shows

### **Complete E-commerce System**
- **Order Service**: HTTP API + NATS messaging for order processing
- **Inventory Service**: Real-time inventory management and reservation
- **Payment Service**: Payment processing with 90% success rate simulation
- **Notification Service**: Email/SMS notifications for order events
- **Load Generator**: Automatic realistic traffic generation

### **Production-Grade Monitoring**
- **Zabbix**: Real-time dashboards with custom business metrics
- **Structured Logging**: JSON logs with correlation IDs
- **NATS Monitoring**: Built-in monitoring dashboard
- **Performance Metrics**: Response times, throughput, error rates

### **Real Business Logic**
- Order workflow: Create → Inventory Check → Payment → Notifications
- Error handling: Payment failures, insufficient inventory
- Event-driven architecture with NATS messaging
- Type-safe APIs with Pydantic validation

## 🏃‍♂️ Quick Start

### **Option 1: Automated Setup (Recommended)**

```bash
# 1. Clone and navigate to the repository
git clone https://github.com/sndwch/cliffracer.git
cd cliffracer

# 2. Run the automated setup (handles everything)
./setup_live_demo.sh
```

### **Option 2: Manual Setup**

```bash
# 1. Start infrastructure services
docker-compose -f docker-compose-live-demo.yml up -d

# 2. Wait for services to be ready (2-3 minutes)
# Check: http://localhost:8222 (NATS) and http://localhost:8080 (Zabbix)

# 3. Install Python dependencies
uv sync --extra all

# 4. Start the microservices
uv run python example_ecommerce_live.py
```

## 📊 Monitoring Dashboards

Once running, access these monitoring interfaces:

### **🌐 Zabbix Dashboard: http://localhost:8080**
- **Username**: Admin
- **Password**: zabbix
- **What to watch**: 
  - Service health and uptime
  - Order creation rate
  - Payment success/failure rates
  - Response time graphs
  - Custom business metrics

### **📈 NATS Monitoring: http://localhost:8222**
- **What to watch**:
  - Message throughput (msgs/sec)
  - Active connections
  - Subscription counts
  - JetStream statistics

### **🛒 Order Service API: http://localhost:8001/docs**
- **Interactive API documentation**
- **Test order creation manually**
- **View API response schemas**

## 🔍 What to Look For

### **In the Terminal (Structured Logs)**
Watch for these JSON log events:
```json
{
  "level": "INFO",
  "action": "order_created",
  "order_id": "order_abc123",
  "total_amount": 1299.99,
  "processing_time": 0.045
}
```

Key actions to monitor:
- `order_created`: New orders being processed
- `inventory_reserved`: Inventory being allocated
- `payment_success`/`payment_failed`: Payment outcomes
- `notification_sent`: Customer notifications

### **In Zabbix (Real-time Metrics)**
Navigate to: **Monitoring → Dashboards**

Key metrics:
- **orders.created**: Rate of order creation
- **payments.processed**: Payment processing rate
- **inventory.reserved**: Inventory operations
- **Service response times**: Performance monitoring

### **Load Generation Behavior**
The system automatically generates realistic e-commerce traffic:
- **Orders every 2-10 seconds**
- **1-3 items per order**
- **Random product selection**
- **Random customer data**
- **90% payment success rate**

## 🧪 Interactive Testing

### **Test the System Manually**

```bash
# In a separate terminal, run the test script
uv run python test_live_system.py
```

This will:
- Create test orders via HTTP API
- Retrieve order information
- Show response times
- Verify API documentation

### **Create Orders via API**

```bash
# Create a sample order
curl -X POST "http://localhost:8001/orders" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "demo_user",
    "items": [
      {
        "product_id": "laptop-pro",
        "name": "Professional Laptop",
        "quantity": 1,
        "price": 1299.99
      }
    ],
    "shipping_address": "123 Demo St, Demo City",
    "email": "demo@example.com"
  }'
```

### **Monitor Order Processing**

```bash
# Get all orders
curl http://localhost:8001/orders

# Get specific order
curl http://localhost:8001/orders/{order_id}
```

## 📈 Performance Monitoring

### **Expected Performance**
- **Order Creation**: < 50ms per order
- **NATS Message Latency**: < 1ms
- **End-to-end Processing**: < 200ms
- **Throughput**: 100+ orders/minute

### **What Impacts Performance**
- **Payment Processing**: Simulated 100-500ms delay
- **Notification Sending**: Simulated 50-200ms delay
- **Database Operations**: In-memory (very fast)
- **Message Routing**: NATS (sub-millisecond)

## 🔧 Troubleshooting

### **Services Won't Start**
```bash
# Check Docker services
docker-compose -f docker-compose-live-demo.yml ps

# Check specific service logs
docker-compose -f docker-compose-live-demo.yml logs nats
docker-compose -f docker-compose-live-demo.yml logs zabbix-server
```

### **Cannot Access Zabbix**
- Wait 2-3 minutes for initialization
- Check: `docker logs cliffracer-zabbix-server`
- Verify PostgreSQL is running: `docker logs cliffracer-postgres`

### **NATS Connection Issues**
- Verify NATS is running: `curl http://localhost:8222/varz`
- Check firewall settings for port 4222
- Look for NATS connection errors in service logs

### **Python Service Errors**
```bash
# Check Python environment
uv run python -c "import nats; print('NATS package OK')"

# Run with debug logging
uv run python example_ecommerce_live.py --log-level DEBUG
```

## 🛑 Clean Shutdown

### **Stop All Services**
```bash
# Stop Python services: Ctrl+C in the main terminal

# Stop Docker services
docker-compose -f docker-compose-live-demo.yml down

# Remove volumes (optional - clears all data)
docker-compose -f docker-compose-live-demo.yml down -v
```

## 📊 Understanding the Architecture

```
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│   Order Service │    │Inventory Service│    │ Payment Service │
│   (HTTP + NATS) │    │     (NATS)      │    │     (NATS)      │
└─────────┬───────┘    └─────────┬───────┘    └─────────┬───────┘
          │                      │                      │
          └──────────────────────┼──────────────────────┘
                                 │
                    ┌─────────────┴───────────┐
                    │     NATS Message Bus    │
                    │    (Sub-ms latency)     │
                    └─────────────┬───────────┘
                                 │
          ┌──────────────────────┼──────────────────────┐
          │                      │                      │
┌─────────┴───────┐    ┌─────────┴───────┐    ┌─────────┴───────┐
│Notification Svc │    │ Load Generator  │    │   Monitoring    │
│     (NATS)      │    │     (NATS)      │    │ (Zabbix/NATS)   │
└─────────────────┘    └─────────────────┘    └─────────────────┘
```

### **Message Flow Example**
1. **HTTP Request** → Order Service creates order
2. **NATS Event** → Order created event broadcast
3. **Inventory Service** → Receives event, reserves items
4. **Payment Service** → Processes payment (90% success)
5. **Notification Service** → Sends confirmation email
6. **Metrics** → All steps recorded in Zabbix

## 🎯 Key Takeaways

### **Performance Benefits**
- **NATS messaging**: Sub-millisecond service communication
- **No HTTP overhead**: Between internal services
- **Automatic load balancing**: Across service instances
- **Built-in monitoring**: Production-ready observability

### **Developer Experience**
- **Decorator-based APIs**: Simple service definition
- **Type safety**: Pydantic validation everywhere
- **Structured logging**: Easy debugging and monitoring
- **Hot reloading**: Fast development cycles

### **Production Readiness**
- **Health checks**: Built into all services
- **Graceful shutdown**: Proper cleanup on exit
- **Error handling**: Circuit breakers and retries
- **Comprehensive monitoring**: Business and technical metrics

## 🤝 Next Steps

After exploring the demo:

1. **Read the full documentation**: [`docs/why-cliffracer.md`](docs/why-cliffracer.md)
2. **Explore the code**: Start with `example_ecommerce_live.py`
3. **Try modifications**: Add new services or modify existing ones
4. **Performance testing**: Use the load generator or add your own
5. **Production deployment**: Check deployment guides in `docs/`

## 💡 Questions?

- **Documentation**: [Full docs](docs/)
- **Issues**: [GitHub Issues](https://github.com/sndwch/cliffracer/issues)
- **Discussions**: [GitHub Discussions](https://github.com/sndwch/cliffracer/discussions)