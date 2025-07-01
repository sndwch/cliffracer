# Cliffracer Consumer Example

This is an example project that consumes the Cliffracer framework.

## Setup

```bash
# Install dependencies (includes Cliffracer)
uv sync

# Run the service
python simple_service.py
```

## Usage

The service provides two RPC methods:
- `hello(name)` - Returns a greeting
- `add(a, b)` - Returns the sum of two numbers

Test with another Cliffracer service or client.
