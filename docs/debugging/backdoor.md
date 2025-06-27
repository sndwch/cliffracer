# 🔧 Cliffracer Backdoor - Live Service Debugging

The Cliffracer backdoor provides a **live Python shell** for debugging running services, inspired by Nameko's backdoor feature but designed specifically for NATS-based microservices.

## 🎯 What is the Backdoor?

The backdoor is a **debugging server** that runs alongside your service, providing:

- **Interactive Python shell** with full access to your service instance
- **NATS connection inspection** and real-time monitoring  
- **Service state debugging** without stopping the service
- **Live performance analysis** and troubleshooting

**Perfect for production debugging!** 🚀

---

## 🚀 Quick Start

### 1. Enable Backdoor (Default)

```python
from cliffracer import ServiceConfig, NATSService

# Backdoor enabled by default
config = ServiceConfig(
    name="my_service",
    nats_url="nats://localhost:4222"
)

class MyService(NATSService):
    pass

service = MyService(config)
await service.connect()  # Backdoor starts automatically
```

### 2. Connect to Backdoor

```bash
# Service will show: "🔧 Backdoor server available on localhost:12345"
nc localhost 12345

# OR use telnet
telnet localhost 12345

# OR use the CLI
python -m cliffracer.cli.backdoor localhost:12345
```

### 3. Debug Your Service

```python
🚀 Cliffracer Backdoor - Live Service Debugging
==================================================
Service: my_service
Time: 2024-01-15 14:30:22

Available commands:
  help_backdoor()    - Show debugging commands
  inspect_service()  - Show service information
  inspect_nats()     - Show NATS connection details
  list_workers()     - Show active workers
  
Variables:
  service           - Your service instance

>>> inspect_service()
🔍 Service Information:
  service_name: my_service
  service_class: MyService
  nats_connected: True
  methods: ['my_rpc_method', 'process_order', ...]

>>> service.nc.is_connected
True

>>> await service.my_rpc_method("test_data")
{'result': 'success', 'data': 'processed'}
```

---

## ⚙️ Configuration

### Basic Configuration

```python
from cliffracer import ServiceConfig

config = ServiceConfig(
    name="my_service",
    
    # Backdoor settings
    backdoor_enabled=True,        # Enable/disable backdoor
    backdoor_port=0,              # 0 = auto-assign port
    disable_backdoor=False,       # Global disable flag
)
```

### Environment Variables

```bash
# Globally disable backdoor
export CLIFFRACER_DISABLE_BACKDOOR=1

# Set environment (backdoor disabled in production by default)
export CLIFFRACER_ENV=production  # Disables backdoor
export CLIFFRACER_ENV=development # Enables backdoor (default)
```

### Custom Port

```python
config = ServiceConfig(
    name="my_service",
    backdoor_port=9999,  # Fixed port
)
```

---

## 🔐 Security & Global Disable

### ❌ Disable Backdoor Globally

**Method 1: Environment Variable**
```bash
export CLIFFRACER_DISABLE_BACKDOOR=1
python my_service.py  # Backdoor disabled
```

**Method 2: Configuration**
```python
config = ServiceConfig(
    name="my_service",
    disable_backdoor=True,  # Completely disabled
)
```

**Method 3: Service Level**
```python
config = ServiceConfig(
    name="my_service", 
    backdoor_enabled=False,  # Disabled for this service
)
```

### 🔒 Production Security

- **Default**: Backdoor **disabled** in production (`CLIFFRACER_ENV=production`)
- **Binding**: Only binds to `localhost` (not accessible externally)
- **No Authentication**: Use firewall rules for additional security
- **Environment Detection**: Automatically disabled in production environments

---

## 🛠️ Debugging Commands

### Built-in Commands

| Command | Description |
|---------|-------------|
| `help_backdoor()` | Show all available debugging commands |
| `inspect_service()` | Display service configuration and state |
| `inspect_nats()` | Show NATS connection details and statistics |
| `list_workers()` | Display active workers and their status |
| `show_metrics()` | Show service performance metrics |

### Available Variables

| Variable | Description |
|----------|-------------|
| `service` | Your running service instance |
| `nats` | NATS connection (if available) |
| `service_inspector` | ServiceInspector utility |
| `nats_inspector` | NATSInspector utility |
| `pprint` | Pretty print utility |
| `datetime` | DateTime module |
| `asyncio` | AsyncIO module |

### Example Debugging Session

```python
>>> # Check service health
>>> inspect_service()
🔍 Service Information:
  service_name: order_service
  service_class: OrderService  
  nats_connected: True
  methods: process_order, validate_payment, ...

>>> # Examine NATS connection
>>> inspect_nats()
📡 NATS Connection Information:
  connected: True
  servers: 1 available
  current: nats://localhost:4222
  subscriptions: 5 active

>>> # Test RPC methods manually
>>> result = await service.process_order({
...     "customer_id": "123",
...     "items": [{"id": "widget", "qty": 2}]
... })
>>> pprint(result)
{'order_id': 'ord_abc123', 'status': 'processed', 'total': 29.98}

>>> # Check service configuration
>>> vars(service.config)
{'name': 'order_service', 'nats_url': 'nats://localhost:4222', ...}

>>> # Inspect active subscriptions
>>> list_subs()
📋 Active Subscriptions (5 total)
==================================================
 1. order.process
 2. order.validate
 3. payment.*.response (queue: payment_workers)
 ...
```

---

## 🔍 Advanced Debugging

### Service State Inspection

```python
>>> # Check all service attributes
>>> [attr for attr in dir(service) if not attr.startswith('_')]
['config', 'connect', 'disconnect', 'nc', 'process_order', ...]

>>> # Examine internal state
>>> service._rpc_handlers
{'process_order': <bound method>, 'validate_payment': <bound method>}

>>> # Check NATS statistics
>>> service.nc.stats()
{'in_msgs': 1547, 'out_msgs': 823, 'in_bytes': 45231, 'out_bytes': 23891}
```

### Performance Monitoring

```python
>>> # Monitor message processing
>>> import time
>>> start = time.time()
>>> result = await service.heavy_computation(large_data)
>>> elapsed = time.time() - start
>>> print(f"Processing took {elapsed:.3f} seconds")

>>> # Check memory usage (if psutil available)
>>> service_inspector.get_info()['runtime']['memory_usage']
{'rss': 52428800, 'vms': 134217728}  # Memory in bytes
```

### NATS Debugging

```python
>>> # Test NATS connectivity
>>> await service.nc.publish("test.subject", b"hello")

>>> # Check subscription status
>>> nats_inspector.list_subscriptions()
📋 Active Subscriptions (3 total)
1. order.process
2. order.*.status  
3. system.health

>>> # Monitor message flow
>>> sub = await service.nc.subscribe("debug.messages")
>>> async for msg in sub.messages:
...     print(f"Received: {msg.data}")
...     if some_condition:
...         break
```

---

## 🧪 Testing & Development

### Development Workflow

```python
# 1. Start service with backdoor
python my_service.py  # Shows: "🔧 Backdoor server available on localhost:12345"

# 2. Connect in another terminal  
nc localhost 12345

# 3. Test changes live
>>> # Modify service behavior on-the-fly
>>> service.debug_mode = True
>>> await service.test_new_feature()

# 4. Monitor performance
>>> inspect_service()
>>> show_metrics()
```

### Integration Testing

```python
>>> # Send test messages
>>> await service.nc.publish("order.process", json.dumps({
...     "customer_id": "test_123",
...     "items": [{"id": "test_widget", "qty": 1}]
... }).encode())

>>> # Verify results
>>> # Check logs, database state, etc.
```

---

## 📋 CLI Usage

### Backdoor Client CLI

```bash
# Connect to backdoor
python -m cliffracer.cli.backdoor localhost:9999

# With custom connection
python -m cliffracer.cli.backdoor production-server:12345
```

### Service Runner with Backdoor

```bash
# Run service with specific backdoor port
python -c "
from cliffracer import ServiceConfig, NATSService
config = ServiceConfig(name='debug_service', backdoor_port=9999)
# ... service setup
"
```

---

## ⚠️ Troubleshooting

### Common Issues

**❌ "Connection refused"**
```bash
# Check if service is running
ps aux | grep my_service

# Check backdoor port in logs
grep "Backdoor server" service.log
```

**❌ "Backdoor server disabled"**
```bash
# Check environment variables
echo $CLIFFRACER_DISABLE_BACKDOOR
echo $CLIFFRACER_ENV

# Check service configuration
>>> service.config.backdoor_enabled
>>> service.config.disable_backdoor
```

**❌ "No module named netcat"**
```bash
# Install netcat
brew install netcat       # macOS
apt-get install netcat    # Ubuntu
yum install nmap-ncat     # CentOS

# Or use telnet
telnet localhost 12345
```

### Debug Connection Issues

```python
>>> # Check if backdoor is running
>>> hasattr(service, '_backdoor_server')
True

>>> # Check backdoor server status
>>> service._backdoor_server.running if service._backdoor_server else None
True

>>> # Check port
>>> service._backdoor_server.port if service._backdoor_server else None
12345
```

---

## 💡 Best Practices

### ✅ Do

- **Use for debugging production issues** - safe and non-disruptive
- **Monitor performance** with the built-in inspection tools
- **Test RPC methods** manually for debugging
- **Check NATS connection state** when troubleshooting
- **Disable in sensitive environments** using environment variables

### ❌ Don't

- **Don't modify critical service state** in production
- **Don't leave backdoor sessions open** unnecessarily  
- **Don't expose backdoor ports** to external networks
- **Don't use for regular service administration**
- **Don't rely on backdoor for monitoring** - use proper observability tools

### 🔐 Security Guidelines

1. **Only bind to localhost** (default behavior)
2. **Use firewall rules** for additional protection
3. **Set `CLIFFRACER_DISABLE_BACKDOOR=1`** in sensitive environments
4. **Monitor backdoor connections** in production logs
5. **Use proper authentication** for production access

---

## 🚀 Example: E-commerce Debugging

```python
# Service running with backdoor on localhost:12345
# Connect: nc localhost 12345

>>> # Debug order processing issue
>>> inspect_service()
🔍 Service Information:
  service_name: ecommerce_service
  nats_connected: True
  methods: ['process_order', 'validate_payment', 'update_inventory']

>>> # Check recent orders
>>> orders = await service.get_recent_orders(limit=5)
>>> pprint(orders)

>>> # Test payment validation manually
>>> test_payment = {"method": "credit_card", "amount": 99.99}
>>> result = await service.validate_payment(test_payment)
>>> print(f"Payment validation: {result}")

>>> # Monitor NATS message flow
>>> inspect_nats()
📡 NATS Connection Information:
  connected: True
  subscriptions: 8 active
  
>>> # Check for stuck workers
>>> list_workers()
👷 Active Workers:
  (Worker tracking implementation in progress)

>>> # Exit backdoor
>>> exit()
```

**The backdoor is your Swiss Army knife for service debugging!** 🛠️

---

## 🔗 Related Documentation

- [Service Configuration](../configuration.md)
- [NATS Integration](../messaging/nats.md)
- [Monitoring & Observability](../monitoring/)
- [Production Deployment](../deployment/production.md)