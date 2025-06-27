# 🔧 Debugging Guide

Quick reference for debugging Cliffracer services in development and production.

## 🚀 Quick Start - Backdoor Debugging

### 1. Enable Backdoor (Default)
```python
from cliffracer import ServiceConfig, NATSService

config = ServiceConfig(
    name="my_service",
    backdoor_enabled=True,  # Default: True in development
)
```

### 2. Connect to Your Service
```bash
# Start your service - watch for backdoor port in logs
python my_service.py
# Output: "🔧 Backdoor server available on localhost:12345"

# Connect in another terminal
nc localhost 12345
```

### 3. Debug Live Service
```python
>>> inspect_service()    # See service details
>>> inspect_nats()      # Check NATS connection
>>> service.my_var      # Access service variables
>>> await service.my_method()  # Test methods live
```

## 🔐 Security & Production

### Disable Backdoor Globally
```bash
# Environment variable (recommended for production)
export CLIFFRACER_DISABLE_BACKDOOR=1

# Or in configuration
config = ServiceConfig(name="service", disable_backdoor=True)
```

## 📋 Essential Commands

| Command | Description |
|---------|-------------|
| `help_backdoor()` | Show all available commands |
| `inspect_service()` | Service details and state |
| `inspect_nats()` | NATS connection info |
| `list_workers()` | Active workers status |
| `service.attribute` | Access any service attribute |
| `await service.method()` | Call service methods |

## 📖 Full Documentation

- **[Complete Backdoor Guide](backdoor.md)** - Comprehensive documentation
- **[Example Service](../examples/debugging/backdoor_demo.py)** - Hands-on demo

## 💡 Quick Tips

✅ **Perfect for**: Production debugging, testing methods, inspecting state  
⚠️ **Caution**: Only binds to localhost, disabled in production by default  
🔧 **Best Practice**: Use environment variables to control backdoor in production