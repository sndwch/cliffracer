# 🏗️ Cliffracer Project Reorganization Plan

## Current Issues
- 44+ files in root directory (way too many!)
- Mixed concerns: framework code, examples, demos, configs all together
- No clear separation between library code and examples
- Multiple docker-compose files scattered around
- Hard to understand what's core vs. what's example

## Proposed Structure

```
cliffracer/
├── src/                          # Core library code
│   └── cliffracer/
│       ├── __init__.py
│       ├── core/                 # Core functionality
│       │   ├── __init__.py
│       │   ├── base_service.py  # (was: nats_service.py)
│       │   ├── extended_service.py  # (was: nats_service_extended.py)
│       │   └── service_config.py
│       ├── auth/                 # Authentication modules
│       │   ├── __init__.py
│       │   ├── framework.py     # (was: auth_framework.py)
│       │   └── middleware.py    # (was: auth_middleware.py)
│       ├── logging/             # Logging functionality
│       │   ├── __init__.py
│       │   ├── config.py        # (was: logging_config.py)
│       │   └── logged_service.py # (was: nats_service_logged.py)
│       ├── runners/             # Service runners
│       │   ├── __init__.py
│       │   └── orchestrator.py  # (was: nats_runner.py)
│       └── utils/               # Utilities
│           ├── __init__.py
│           └── deprecation.py   # (was: deprecated_names.py)
│
├── examples/                    # All example code
│   ├── basic/
│   │   ├── simple_service.py
│   │   └── async_patterns.py
│   ├── advanced/
│   │   ├── auth_patterns.py
│   │   ├── modular_services.py
│   │   └── aws_integration.py
│   ├── ecommerce/              # E-commerce demo
│   │   ├── README.md
│   │   ├── services/
│   │   │   ├── order_service.py
│   │   │   ├── inventory_service.py
│   │   │   ├── payment_service.py
│   │   │   └── notification_service.py
│   │   ├── main.py            # (was: example_ecommerce_live.py)
│   │   └── demo_simple.py     # (was: demo_without_docker.py)
│   └── monitoring/
│       └── metrics_exporter.py
│
├── deployment/                  # All deployment configs
│   ├── docker/
│   │   ├── Dockerfile
│   │   ├── docker-compose.yml
│   │   ├── docker-compose.dev.yml
│   │   └── docker-compose.monitoring.yml
│   ├── kubernetes/
│   │   └── (future k8s manifests)
│   └── localstack/
│       └── docker-compose.localstack.yml
│
├── scripts/                     # Utility scripts
│   ├── setup_live_demo.sh
│   ├── run_tests.sh
│   └── refactor_class_names.py
│
├── tests/                       # All tests
│   ├── unit/
│   ├── integration/
│   └── e2e/
│
├── docs/                        # Documentation
│   ├── getting-started.md
│   ├── api-reference.md
│   ├── why-cliffracer.md
│   └── deployment/
│
├── monitoring/                  # Monitoring configs
│   ├── zabbix/
│   ├── grafana/
│   └── prometheus/
│
├── .github/                     # GitHub specific
│   └── workflows/
│
├── pyproject.toml              # Python package config
├── README.md                   # Project readme
├── LICENSE                     # License file
├── .gitignore                  # Git ignore
└── .env.example               # Environment variables example
```

## Benefits of This Structure

1. **Clear Separation**: Library code (`src/cliffracer/`) vs examples vs deployment
2. **Pythonic**: Follows standard Python package structure
3. **Import Clarity**: `from cliffracer.core import BaseNATSService`
4. **Example Organization**: Easy to find examples by complexity/type
5. **Deployment Flexibility**: All deployment configs in one place
6. **Test Organization**: Clear test structure
7. **Documentation**: Centralized docs

## Migration Steps

### Phase 1: Create Directory Structure
```bash
# Create all directories
mkdir -p src/cliffracer/{core,auth,logging,runners,utils}
mkdir -p examples/{basic,advanced,ecommerce/services,monitoring}
mkdir -p deployment/{docker,kubernetes,localstack}
mkdir -p tests/{unit,integration,e2e}
```

### Phase 2: Move Core Library Files
- `nats_service.py` → `src/cliffracer/core/base_service.py`
- `nats_service_extended.py` → `src/cliffracer/core/extended_service.py`
- `nats_service_logged.py` → `src/cliffracer/logging/logged_service.py`
- `auth_framework.py` → `src/cliffracer/auth/framework.py`
- `auth_middleware.py` → `src/cliffracer/auth/middleware.py`
- `logging_config.py` → `src/cliffracer/logging/config.py`
- `nats_runner.py` → `src/cliffracer/runners/orchestrator.py`

### Phase 3: Move Example Files
- All `example_*.py` files → `examples/` (organized by type)
- `demo_without_docker.py` → `examples/ecommerce/demo_simple.py`
- `monitoring_exporter.py` → `examples/monitoring/metrics_exporter.py`

### Phase 4: Move Deployment Files
- All `docker-compose*.yml` → `deployment/docker/`
- Consolidate multiple docker-compose files into fewer, well-named ones

### Phase 5: Update Imports
- Update all imports to use new structure
- Add proper `__init__.py` files with public API exports

### Phase 6: Update Documentation
- Update README with new structure
- Update import examples in docs
- Add migration guide for users

## Implementation Priority

1. **High Priority**: 
   - Create directory structure
   - Move core library files
   - Update imports
   
2. **Medium Priority**:
   - Organize examples
   - Consolidate Docker files
   
3. **Low Priority**:
   - Documentation updates
   - Additional organization

Would you like me to proceed with this reorganization? I can do it step by step to ensure nothing breaks.