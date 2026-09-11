---
name: Bug Report
about: Create a report to help us improve Cliffracer
title: "[BUG] "
labels: ["bug"]
assignees: ""
---

## Description
A concise description of the bug.

## Steps to Reproduce
1. Define a service with ...
2. Start the service with ...
3. Dispatch request ...

## Minimal Reproducible Example
```python
from cliffracer import CliffracerService, ServiceConfig, rpc

class DemoService(CliffracerService):
    def __init__(self):
        super().__init__(ServiceConfig(name="demo"))

    @rpc
    async def ping(self) -> dict[str, str]:
        return {"status": "ok"}
```

## Expected Behavior
A concise description of what you expected to happen.

## Actual Behavior
A concise description of what actually happened, including any stack trace.

## Environment Context
- Cliffracer Version:
- Python Version:
- NATS Server Version:
- Operating System:
