# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Initial release of Cliffracer microservices framework
- Core NATS-based messaging with RPC and pub/sub patterns
- HTTP/REST API support via FastAPI integration
- WebSocket support for real-time communication
- Pydantic-based schema validation
- Structured logging with Loguru
- Zabbix monitoring integration
- AWS messaging backend support (SQS, SNS, EventBridge)
- CloudWatch metrics integration
- Comprehensive test suite
- Docker and Docker Compose configurations
- Load testing framework
- Example e-commerce system
- Backdoor debugging capabilities
- Auto-restart functionality for services

### Security
- Added authentication middleware
- JWT token support
- Service-to-service authentication

## [0.1.0] - 2024-01-01

### Added
- Initial public release

[Unreleased]: https://github.com/sndwch/cliffracer/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/sndwch/cliffracer/releases/tag/v0.1.0