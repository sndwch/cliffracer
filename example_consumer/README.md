# Cliffracer consumer example

A standalone project that depends on cliffracer, so you can see what a service
looks like outside this repository.

## Setup

```bash
# Install dependencies, cliffracer included
uv sync

# Run the service
python simple_service.py
```

The service needs a NATS broker on `nats://localhost:4222`.

## Usage

`simple_service.py` registers two RPC handlers on the service name
`example_service`:

- `hello(name="World")` returns a greeting
- `add(a, b)` returns the sum

They answer on `example_service.rpc.hello` and `example_service.rpc.add`. Call
them from another cliffracer service:

```python
greeting = await self.call_rpc("example_service", "hello", name="Alice")
```
