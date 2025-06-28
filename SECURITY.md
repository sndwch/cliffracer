# Security Policy

## Supported Versions

We release patches for security vulnerabilities. Currently supported versions:

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |

## Reporting a Vulnerability

If you discover a security vulnerability within Cliffracer, please send an email to security@cliffracer.dev. All security vulnerabilities will be promptly addressed.

Please do not report security vulnerabilities through public GitHub issues.

## What to Include

When reporting a vulnerability, please include:

- The nature of the vulnerability
- Steps to reproduce the issue
- Potential impact
- Any suggested fixes (if you have them)

## Response Timeline

- We will acknowledge receipt of your vulnerability report within 48 hours
- We will provide a more detailed response within 7 days
- We will work on a fix and coordinate the release with you

## Security Best Practices

When using Cliffracer:

1. **Never commit secrets**: Use environment variables or secure secret management
2. **Keep dependencies updated**: Regularly run `uv sync` to get security updates
3. **Use TLS**: Always use TLS for NATS connections in production
4. **Validate inputs**: Use Pydantic models for all service inputs
5. **Implement authentication**: Use the built-in auth middleware for sensitive services

## Known Security Considerations

- NATS connections should always use credentials in production
- The backdoor debugging feature should never be enabled in production
- Always validate and sanitize user inputs before processing