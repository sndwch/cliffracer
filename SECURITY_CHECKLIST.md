# Security Checklist for Deployment

## Pre-Deployment Security Checklist

### ✅ Credentials and Secrets
- [ ] Replace all hardcoded passwords in docker-compose.yml with environment variables
- [ ] Create .env file with strong passwords (do NOT commit)
- [ ] Ensure BACKDOOR_ENABLED=false in production
- [ ] Set strong BACKDOOR_PASSWORD if backdoor is needed
- [ ] Review AUTH_SECRET_KEY is sufficiently random (32+ chars)

### ✅ Code Security
- [ ] SQL injection protection enabled (SecureRepository)
- [ ] Input validation active on all endpoints
- [ ] JWT tokens have appropriate expiration
- [ ] Password hashing uses PBKDF2 with 100k iterations

### ✅ Infrastructure
- [ ] NATS server uses authentication in production
- [ ] PostgreSQL uses strong passwords
- [ ] Docker containers run as non-root user
- [ ] Firewall rules restrict access to internal ports

### ✅ Debug Features
- [ ] Backdoor service disabled (BACKDOOR_ENABLED=false)
- [ ] Debug logging disabled (LOG_LEVEL=INFO or higher)
- [ ] Test endpoints removed or protected
- [ ] Monitoring endpoints require authentication

### ✅ Documentation
- [ ] Remove any internal documentation from public repo
- [ ] Ensure no real IPs/hostnames in examples
- [ ] Review all TODO/FIXME comments
- [ ] Remove any embarrassing comments

## Environment Variables Template

```bash
# Production .env file (DO NOT COMMIT)

# Database
POSTGRES_PASSWORD=<generate-strong-password>
ZABBIX_DB_PASSWORD=<generate-strong-password>

# Authentication
AUTH_SECRET_KEY=<generate-32-char-random-string>
AUTH_TOKEN_EXPIRY_HOURS=24

# Debug (keep disabled)
BACKDOOR_ENABLED=false
CLIFFRACER_DISABLE_BACKDOOR=true

# NATS Security
NATS_USER=<nats-username>
NATS_PASSWORD=<nats-password>

# Monitoring
ZABBIX_API_USER=Admin
ZABBIX_API_PASSWORD=<change-from-default>
```

## Post-Deployment
- [ ] Verify backdoor is not accessible
- [ ] Check logs for any sensitive data leakage
- [ ] Monitor for unusual access patterns
- [ ] Regular security updates for dependencies