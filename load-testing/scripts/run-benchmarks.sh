#!/bin/bash

# Comprehensive Cliffracer Performance Benchmarks
# Runs multiple load test scenarios and generates performance reports

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
RESULTS_DIR="./reports/performance"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
REPORT_PREFIX="cliffracer-benchmark-${TIMESTAMP}"

echo -e "${BLUE}🚀 Cliffracer Performance Benchmark Suite${NC}"
echo "=" * 60

# Create results directory
mkdir -p $RESULTS_DIR

# Change to load-testing directory
cd "$(dirname "$0")/.."

# Export Python path
export PYTHONPATH="${PWD}:${PWD}/services:${PWD}/shared:${PYTHONPATH}"

# Benchmark scenarios
declare -A SCENARIOS=(
    ["baseline"]="Basic performance baseline with minimal load"
    ["high-throughput"]="High throughput test with complex validation"
    ["error-resilience"]="Error handling performance under failure conditions"
    ["large-payloads"]="Memory efficiency with large payload processing"
    ["mixed-workload"]="Realistic mixed workload simulation"
    ["stress-test"]="Maximum capacity stress testing"
)

# Test configurations
declare -A TEST_CONFIGS=(
    ["baseline"]="--users 10 --spawn-rate 2 --run-time 60s"
    ["high-throughput"]="--users 100 --spawn-rate 10 --run-time 120s"
    ["error-resilience"]="--users 50 --spawn-rate 5 --run-time 90s"
    ["large-payloads"]="--users 20 --spawn-rate 2 --run-time 180s"
    ["mixed-workload"]="--users 80 --spawn-rate 8 --run-time 150s"
    ["stress-test"]="--users 200 --spawn-rate 20 --run-time 300s"
)

# Check prerequisites
check_prerequisites() {
    echo "🔍 Checking prerequisites..."
    
    # Check if NATS is running
    if ! nc -z localhost 4222 2>/dev/null; then
        echo -e "${RED}❌ NATS server not running on localhost:4222${NC}"
        echo "Please run: ./scripts/start-test-services.sh"
        exit 1
    fi
    
    # Check if Cliffracer service is responsive
    python -c "
import asyncio
import nats
import json
import sys

async def test_service():
    try:
        nc = await nats.connect('nats://localhost:4222')
        response = await nc.request(
            'order_processing_service.get_service_metrics',
            json.dumps({}).encode(),
            timeout=5.0
        )
        await nc.close()
        return True
    except:
        return False

if not asyncio.run(test_service()):
    print('❌ Cliffracer service not responding')
    sys.exit(1)
    " 2>/dev/null
    
    if [ $? -ne 0 ]; then
        echo -e "${RED}❌ Cliffracer service not responding${NC}"
        echo "Please ensure services are running: ./scripts/start-test-services.sh"
        exit 1
    fi
    
    # Check if locust is installed
    if ! command -v locust &> /dev/null; then
        echo -e "${RED}❌ Locust not installed${NC}"
        echo "Installing locust..."
        pip install locust
    fi
    
    echo -e "${GREEN}✅ Prerequisites satisfied${NC}"
}

# Run a single benchmark scenario
run_benchmark() {
    local scenario=$1
    local description=$2
    local config=$3
    
    echo
    echo -e "${YELLOW}📊 Running ${scenario} benchmark...${NC}"
    echo "Description: $description"
    echo "Configuration: $config"
    echo
    
    local output_file="${RESULTS_DIR}/${REPORT_PREFIX}-${scenario}"
    
    # Run Locust test
    python -m locust \
        -f tests/locust/cliffracer_load_test.py \
        --host=nats://localhost:4222 \
        --headless \
        $config \
        --csv="${output_file}" \
        --html="${output_file}.html" \
        --logfile="${output_file}.log" \
        --loglevel=INFO
    
    local exit_code=$?
    
    if [ $exit_code -eq 0 ]; then
        echo -e "${GREEN}✅ ${scenario} benchmark completed successfully${NC}"
        
        # Extract key metrics from results
        if [ -f "${output_file}_stats.csv" ]; then
            echo "📈 Key metrics:"
            python -c "
import pandas as pd
import sys

try:
    df = pd.read_csv('${output_file}_stats.csv')
    overall = df[df['Name'] == 'Aggregated']
    if not overall.empty:
        row = overall.iloc[0]
        print(f'   Requests: {row[\"Request Count\"]}')
        print(f'   Failures: {row[\"Failure Count\"]}')
        print(f'   Median RT: {row[\"Median Response Time\"]}ms')
        print(f'   95th percentile: {row[\"95%\"]}ms')
        print(f'   RPS: {row[\"Requests/s\"]:.1f}')
        print(f'   Failure rate: {(row[\"Failure Count\"]/row[\"Request Count\"]*100):.1f}%')
except Exception as e:
    print(f'Could not parse results: {e}')
            "
        fi
    else
        echo -e "${RED}❌ ${scenario} benchmark failed${NC}"
        return 1
    fi
}

# Generate summary report
generate_summary_report() {
    echo
    echo -e "${BLUE}📋 Generating comprehensive performance report...${NC}"
    
    local summary_file="${RESULTS_DIR}/${REPORT_PREFIX}-summary.md"
    
    cat > "$summary_file" << EOF
# Cliffracer Performance Benchmark Report

**Generated:** $(date)
**Test Suite:** $REPORT_PREFIX

## Executive Summary

This report contains comprehensive performance testing results for the Cliffracer microservices framework,
including complex object validation, error handling resilience, and throughput analysis.

## Test Environment

- **Framework:** Cliffracer $(python -c "from cliffracer import __version__; print(__version__)" 2>/dev/null || echo "development")
- **Python:** $(python --version)
- **NATS:** localhost:4222
- **Test Tool:** Locust $(locust --version 2>/dev/null | head -1)
- **Timestamp:** $TIMESTAMP

## Benchmark Results

EOF
    
    # Process each scenario's results
    for scenario in "${!SCENARIOS[@]}"; do
        local stats_file="${RESULTS_DIR}/${REPORT_PREFIX}-${scenario}_stats.csv"
        
        if [ -f "$stats_file" ]; then
            echo "### ${scenario^} Scenario" >> "$summary_file"
            echo >> "$summary_file"
            echo "**Description:** ${SCENARIOS[$scenario]}" >> "$summary_file"
            echo "**Configuration:** ${TEST_CONFIGS[$scenario]}" >> "$summary_file"
            echo >> "$summary_file"
            
            # Extract metrics using Python
            python -c "
import pandas as pd
import sys

try:
    df = pd.read_csv('$stats_file')
    overall = df[df['Name'] == 'Aggregated']
    if not overall.empty:
        row = overall.iloc[0]
        
        # Calculate derived metrics
        requests = row['Request Count']
        failures = row['Failure Count']
        success_rate = ((requests - failures) / requests * 100) if requests > 0 else 0
        
        print('| Metric | Value |')
        print('|--------|-------|')
        print(f'| Total Requests | {requests:,} |')
        print(f'| Failed Requests | {failures:,} |')
        print(f'| Success Rate | {success_rate:.1f}% |')
        print(f'| Median Response Time | {row[\"Median Response Time\"]}ms |')
        print(f'| 95th Percentile | {row[\"95%\"]}ms |')
        print(f'| Requests per Second | {row[\"Requests/s\"]:.1f} |')
        print(f'| Min Response Time | {row[\"Min Response Time\"]}ms |')
        print(f'| Max Response Time | {row[\"Max Response Time\"]}ms |')
        print()
        
        # Performance analysis
        if row['Median Response Time'] < 10:
            print('🚀 **Excellent**: Sub-10ms median response time')
        elif row['Median Response Time'] < 50:
            print('✅ **Good**: Sub-50ms median response time')
        elif row['Median Response Time'] < 100:
            print('⚠️ **Acceptable**: Sub-100ms median response time')
        else:
            print('❌ **Needs Improvement**: >100ms median response time')
        
        print()
        
        if success_rate >= 99.5:
            print('🎯 **Excellent**: >99.5% success rate')
        elif success_rate >= 99:
            print('✅ **Good**: >99% success rate')
        elif success_rate >= 95:
            print('⚠️ **Acceptable**: >95% success rate')
        else:
            print('❌ **Needs Improvement**: <95% success rate')
        
        print()
        
except Exception as e:
    print(f'Error processing results: {e}')
            " >> "$summary_file"
            
            echo >> "$summary_file"
        fi
    done
    
    # Add conclusions
    cat >> "$summary_file" << EOF

## Key Findings

### Performance Highlights

- **Sub-millisecond Claims**: [Analysis of response times vs. claimed performance]
- **Error Resilience**: [How well the system maintains performance during errors]
- **Validation Overhead**: [Impact of complex object validation on performance]
- **Throughput Capabilities**: [Maximum sustained request rates]

### Recommendations

1. **Production Readiness**: [Assessment of production readiness]
2. **Scaling Considerations**: [Recommendations for horizontal scaling]
3. **Optimization Opportunities**: [Areas for performance improvement]

### Comparison Baseline

These results provide a baseline for comparing Cliffracer against:
- Traditional REST APIs with Flask/FastAPI
- Message queue systems (RabbitMQ, Redis)
- Other microservice frameworks

## Detailed Reports

Individual scenario reports are available:
$(for scenario in "${!SCENARIOS[@]}"; do echo "- [${scenario^} Scenario](${REPORT_PREFIX}-${scenario}.html)"; done)

---

*Generated by Cliffracer Load Testing Suite*
EOF
    
    echo -e "${GREEN}✅ Summary report generated: $summary_file${NC}"
    echo -e "${BLUE}📊 HTML reports available in: $RESULTS_DIR${NC}"
}

# Main execution
main() {
    echo "🎯 Starting comprehensive performance benchmarks..."
    echo
    
    check_prerequisites
    
    echo
    echo "📋 Benchmark scenarios to run:"
    for scenario in "${!SCENARIOS[@]}"; do
        echo "   • $scenario: ${SCENARIOS[$scenario]}"
    done
    echo
    
    # Ask for confirmation
    read -p "Proceed with benchmarks? This will take approximately 15-20 minutes. (y/N): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Benchmark cancelled."
        exit 0
    fi
    
    echo
    echo -e "${YELLOW}🔥 Starting benchmark suite...${NC}"
    
    local success_count=0
    local total_count=${#SCENARIOS[@]}
    
    # Run each benchmark scenario
    for scenario in "${!SCENARIOS[@]}"; do
        if run_benchmark "$scenario" "${SCENARIOS[$scenario]}" "${TEST_CONFIGS[$scenario]}"; then
            ((success_count++))
        fi
        
        # Brief pause between tests
        if [ "$scenario" != "stress-test" ]; then
            echo "⏳ Cooling down for 30 seconds..."
            sleep 30
        fi
    done
    
    echo
    echo -e "${BLUE}📊 Benchmark Results Summary${NC}"
    echo "=" * 40
    echo "Completed: $success_count/$total_count scenarios"
    echo "Results location: $RESULTS_DIR"
    echo
    
    if [ $success_count -eq $total_count ]; then
        echo -e "${GREEN}🎉 All benchmarks completed successfully!${NC}"
        generate_summary_report
        
        echo
        echo -e "${YELLOW}📋 Next Steps:${NC}"
        echo "1. Review HTML reports in: $RESULTS_DIR"
        echo "2. Check summary report: ${RESULTS_DIR}/${REPORT_PREFIX}-summary.md"
        echo "3. Compare results with baseline performance claims"
        echo "4. Run comparison tests against other frameworks"
        
    else
        echo -e "${YELLOW}⚠️  Some benchmarks failed. Check individual logs for details.${NC}"
        exit 1
    fi
}

# Handle command line arguments
case "${1:-}" in
    "quick")
        # Quick test with reduced scenarios
        SCENARIOS=(
            ["baseline"]="Basic performance baseline"
            ["high-throughput"]="High throughput test"
        )
        TEST_CONFIGS=(
            ["baseline"]="--users 10 --spawn-rate 2 --run-time 30s"
            ["high-throughput"]="--users 50 --spawn-rate 5 --run-time 60s"
        )
        main
        ;;
    "stress")
        # Only run stress test
        SCENARIOS=(["stress-test"]="Maximum capacity stress testing")
        main
        ;;
    *)
        main
        ;;
esac