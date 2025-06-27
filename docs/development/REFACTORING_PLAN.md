# Class Name Refactoring Plan

## Overview
This document outlines the systematic refactoring of class names to improve clarity, consistency, and maintainability of Cliffracer.

## Current Problems
1. **Confusing inheritance hierarchy**: `Service` → `ExtendedService` doesn't indicate what each adds
2. **Inconsistent NATS capitalization**: `NatsService` vs `NATSClient`
3. **Generic names**: `Service`, `Message` are too vague
4. **Redundant suffixes**: `NATSMessagingClient` has redundant "Messaging"

## Refactoring Phases

### Phase 1: Core Service Hierarchy (CRITICAL)
**Impact**: High - affects most of the codebase
**Estimated time**: 2-3 hours

```python
# Before → After
NatsService           → BaseNATSService
Service              → NATSService  
ExtendedService      → ValidatedNATSService
HTTPService          → HTTPNATSService
WebSocketService     → WebSocketNATSService
ModularService       → ConfigurableNATSService
FullyModularService  → PluggableNATSService
```

**Rationale**: 
- `BaseNATSService`: Clear it's the foundation
- `NATSService`: Shows it adds decorator support to base NATS
- `ValidatedNATSService`: Clear what "extended" means (schema validation)
- Technology prefix pattern for HTTP/WebSocket variants

### Phase 2: Messaging & Infrastructure (MEDIUM)
**Impact**: Medium - affects messaging and monitoring modules
**Estimated time**: 1-2 hours

```python
# Messaging
AbstractMessagingClient → MessageClient
NATSMessagingClient    → NATSClient
AWSMessagingClient     → AWSClient
MessagingFactory       → MessageClientFactory

# Monitoring  
MetricsExporterService → ZabbixMetricsService
ZabbixSender          → ZabbixExporter
MetricsCollector      → SystemMetricsCollector
AbstractMonitoringClient → MonitoringClient
```

### Phase 3: Support Classes (LOW)
**Impact**: Low - mostly internal classes
**Estimated time**: 30 minutes

```python
ServiceMeta        → NATSServiceMeta
ExtendedServiceMeta → ValidatedServiceMeta
AbstractServiceRunner → ServiceRunner
LambdaServiceRunner → AWSLambdaRunner
```

## Execution Steps

### Step 1: Preparation (5 minutes)
```bash
# 1. Create a backup branch
git checkout -b refactor-class-names

# 2. Ensure all tests pass before refactoring
uv run pytest

# 3. Check current linting status
uv run ruff check .
```

### Step 2: Dry Run Analysis (10 minutes)
```bash
# Run dry-run to see what would change
python refactor_class_names.py --dry-run

# Check specific phases
python refactor_class_names.py --dry-run --phase 1
python refactor_class_names.py --dry-run --phase 2  
python refactor_class_names.py --dry-run --phase 3
```

### Step 3: Execute Phase 1 (30 minutes)
```bash
# Apply Phase 1 changes (core services)
python refactor_class_names.py --phase 1

# Run tests to check for issues
uv run pytest tests/unit/

# Fix any import issues manually
# Update any missed references in comments/docstrings
```

### Step 4: Execute Phase 2 (20 minutes)
```bash
# Apply Phase 2 changes (messaging/monitoring)
python refactor_class_names.py --phase 2

# Test again
uv run pytest tests/

# Check linting
uv run ruff check .
```

### Step 5: Execute Phase 3 (10 minutes)
```bash
# Apply Phase 3 changes (support classes)  
python refactor_class_names.py --phase 3

# Final test run
uv run pytest
```

### Step 6: Create Backward Compatibility (5 minutes)
```bash
# Create deprecated aliases for gradual migration
python refactor_class_names.py --create-aliases

# This creates deprecated_names.py with warnings
```

### Step 7: Update Documentation (15 minutes)
```bash
# Update README.md with new class names
# Update examples in docs/ directory
# Update code examples in README

# Search for any remaining old names in comments
rg "ExtendedService|NatsService" --type md
```

### Step 8: Verification (10 minutes)
```bash
# Run full test suite
uv run pytest

# Check linting passes
uv run ruff check .

# Verify examples still work
uv run python example_extended_services.py --help

# Test imports work with new names
uv run python -c "from cliffracer import NATSService, ValidatedNATSService; print('✅ New imports work')"
```

## File Update Priority

### High Priority (Core functionality)
1. `nats_service.py` - Core service classes
2. `nats_service_extended.py` - Extended services  
3. `example_*.py` - All example files
4. `tests/` - All test files

### Medium Priority (Infrastructure)
5. `messaging/` - All messaging classes
6. `monitoring/` - All monitoring classes
7. `runners/` - Service runner classes

### Low Priority (Documentation)
8. `README.md` - Update examples
9. `docs/` - Update documentation
10. `pyproject.toml` - Update class references

## Risk Mitigation

### Backward Compatibility
- Keep old class names as deprecated aliases
- Add deprecation warnings pointing to new names
- Gradual migration path for users

### Testing Strategy
- Run tests after each phase
- Keep git commits small and focused
- Easy rollback if issues arise

### Import Management
```python
# In __init__.py files, support both:
from .nats_service import NATSService, ValidatedNATSService
from .nats_service import Service as NATSService_DEPRECATED  # Remove in v2.0
```

## Expected Benefits

### Developer Experience
- **Clearer inheritance**: `BaseNATSService` → `NATSService` → `ValidatedNATSService`
- **Self-documenting**: `HTTPNATSService` immediately tells you it's NATS + HTTP
- **Consistent patterns**: All NATS services follow same naming convention

### Code Quality
- **Reduced cognitive load**: No more guessing what "ExtendedService" does
- **Better IDE support**: Clear names improve autocomplete and navigation
- **Maintainability**: Easier to understand and modify code

### Architecture Clarity
- **Technology alignment**: NATS prefix shows these are NATS-specific
- **Feature indication**: Names clearly show what capabilities each class adds
- **Inheritance understanding**: Clear progression of capabilities

## Rollback Plan

If issues arise:
```bash
# Quick rollback
git reset --hard HEAD~1

# Or selective rollback
git checkout HEAD~1 -- nats_service.py
```

## Success Metrics

- [ ] All tests pass after refactoring
- [ ] No linting errors introduced  
- [ ] All examples work with new names
- [ ] Documentation updated
- [ ] Backward compatibility preserved
- [ ] Developer feedback positive

## Timeline

**Total estimated time**: 3-4 hours
- Preparation & analysis: 15 minutes
- Phase 1 execution: 30 minutes  
- Phase 2 execution: 20 minutes
- Phase 3 execution: 10 minutes
- Documentation updates: 15 minutes
- Testing & verification: 30 minutes
- Buffer for issues: 60 minutes

## Post-Refactoring

1. **Update pyproject.toml** with new class names in examples
2. **Create migration guide** for users upgrading
3. **Plan deprecation timeline** for old names (remove in v2.0)
4. **Update CI/CD** to test both old and new names during transition