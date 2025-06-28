# ⚠️ BROKEN: Backend Migration Guide

> **THIS DOCUMENTATION IS BROKEN**
> 
> The backend switching functionality described here is not implemented. 
> See [IMPLEMENTATION_STATUS.md](../IMPLEMENTATION_STATUS.md) for current status.

## Current Status: NOT IMPLEMENTED

The messaging backend migration described in this document has the following issues:

1. **MessagingFactory not implemented**: Contains `NotImplementedError`
2. **AWS backend not integrated**: Client exists but not connected to framework
3. **Configuration switching broken**: Examples don't work with actual codebase
4. **Factory registration commented out**: AWS backend registration disabled

## What's Planned (Not Available)

- Configuration-based backend switching
- Zero-code-change migration between NATS/AWS/Redis
- Pluggable messaging architecture
- Production AWS backend support

## What Works Instead

Currently, only NATS backend is functional:

```python
# This works
from cliffracer import NATSService

# This doesn't work
from cliffracer import AWSService  # Not integrated
```

## Current Workaround

If you need AWS messaging, you can use the AWS client directly, but it won't integrate with the main framework:

```python
# Direct AWS client usage (not integrated with services)
from cliffracer.aws_messaging import AWSMessagingClient
# But this won't work with NATSService decorators
```

This document will be updated when backend switching is properly implemented.