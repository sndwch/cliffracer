# 🚀 Cliffracer Load Testing Suite

Comprehensive performance testing and benchmarking for Cliffracer using Locust.

## 📊 What We Test

### **Performance Scenarios**
- **High-throughput RPC calls** with complex object validation
- **Error handling resilience** - RPS maintained during failures
- **Concurrent service communication** patterns
- **Memory and latency under sustained load**

### **Comparison Benchmarks**  
- **Cliffracer vs FastAPI + Redis**
- **Cliffracer vs Flask + RabbitMQ**
- **NATS vs HTTP/REST performance**

### **Complex Data Validation**
- **Nested object hierarchies** (orders with items, addresses, payments)
- **Large payload processing** (file uploads, batch operations)
- **Schema validation overhead** measurement
- **Serialization performance** testing

## 🎯 Test Scenarios

### **1. E-commerce Load Test**
- Order processing with complex validation
- Payment processing with retry logic
- Inventory updates with concurrency
- Error injection (payment failures, validation errors)

### **2. Real-time Analytics**
- High-frequency event ingestion
- Complex aggregation operations
- Time-series data processing
- Concurrent read/write patterns

### **3. File Processing**
- Large file upload simulation
- Batch processing workflows
- Multi-step validation pipelines
- Error recovery testing

## 🏃‍♂️ Quick Start

```bash
# Install dependencies
pip install locust

# Start test services
./scripts/start-test-services.sh

# Run basic load test
locust -f tests/basic_load_test.py --host=nats://localhost:4222

# Run comparison benchmark
./scripts/run-benchmarks.sh

# Generate performance reports
./scripts/generate-reports.sh
```

## 📁 Directory Structure

```
load-testing/
├── README.md
├── requirements.txt
├── config/
│   ├── test-config.yaml
│   └── benchmark-config.yaml
├── services/
│   ├── cliffracer-services/    # Test services using Cliffracer
│   ├── fastapi-services/       # Comparison services using FastAPI
│   └── shared/                 # Shared data models and utilities
├── tests/
│   ├── locust/                 # Locust test files
│   ├── scenarios/              # Test scenario definitions
│   └── benchmarks/             # Comparison benchmarks
├── scripts/
│   ├── start-test-services.sh
│   ├── run-benchmarks.sh
│   └── generate-reports.sh
├── reports/
│   ├── performance/            # Generated performance reports
│   └── comparisons/            # Benchmark comparison results
└── data/
    ├── test-payloads/          # Sample data for testing
    └── schemas/                # Complex schema definitions
```

## 📈 Expected Results

Based on NATS performance characteristics, we expect:

- **Sub-millisecond latency** for simple RPC calls
- **10,000+ RPS** sustained throughput  
- **Minimal latency degradation** under load
- **Consistent performance** during error conditions
- **2-5x better performance** than HTTP-based alternatives

## 🔧 Test Configuration

All test parameters are configurable via YAML files:

```yaml
# config/test-config.yaml
load_test:
  users: 100
  spawn_rate: 10
  duration: 300
  
scenarios:
  ecommerce:
    order_rate: 50  # orders per second
    error_rate: 0.05  # 5% failure rate
    complex_validation: true
    
  analytics:
    event_rate: 1000  # events per second
    batch_size: 100
    concurrent_reads: 50
```