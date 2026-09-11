#!/bin/bash

# Generate comprehensive performance analysis reports from load test results

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
REPORTS_DIR="./reports/performance"
ANALYSIS_DIR="./reports/analysis"

echo -e "${BLUE}📊 Cliffracer Performance Report Generator${NC}"
echo "=" * 50

# Create analysis directory
mkdir -p "$ANALYSIS_DIR"

# Change to load-testing directory
cd "$(dirname "$0")/.."

# Check if we have any results to analyze
if [ ! -d "$REPORTS_DIR" ] || [ -z "$(ls -A $REPORTS_DIR 2>/dev/null)" ]; then
    echo -e "${RED}❌ No performance results found in $REPORTS_DIR${NC}"
    echo "Please run benchmarks first: ./scripts/run-benchmarks.sh"
    exit 1
fi

echo "🔍 Found performance results in: $REPORTS_DIR"
echo

# Generate comprehensive analysis
generate_comprehensive_analysis() {
    echo -e "${YELLOW}📈 Generating comprehensive performance analysis...${NC}"
    
    python3 << 'EOF'
import os
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
from pathlib import Path
import json
from datetime import datetime
import numpy as np

# Set up plotting style
plt.style.use('seaborn-v0_8')
sns.set_palette("husl")

# Configuration
reports_dir = Path('./reports/performance')
analysis_dir = Path('./reports/analysis')
analysis_dir.mkdir(exist_ok=True)

def find_latest_results():
    """Find the most recent benchmark results."""
    csv_files = list(reports_dir.glob('*_stats.csv'))
    if not csv_files:
        print("❌ No CSV result files found")
        return None
    
    # Group by timestamp (extract from filename)
    timestamps = {}
    for file in csv_files:
        # Extract timestamp from filename like 'cliffracer-benchmark-20241215-143022-baseline_stats.csv'
        parts = file.stem.split('-')
        if len(parts) >= 4:
            timestamp = f"{parts[2]}-{parts[3]}"
            if timestamp not in timestamps:
                timestamps[timestamp] = []
            timestamps[timestamp].append(file)
    
    # Get the latest timestamp
    latest_timestamp = max(timestamps.keys())
    print(f"📅 Analyzing results from: {latest_timestamp}")
    
    return timestamps[latest_timestamp], latest_timestamp

def load_and_process_results(files):
    """Load and process all benchmark results."""
    results = {}
    
    for file in files:
        # Extract scenario name from filename
        parts = file.stem.split('-')
        scenario = parts[-2] if len(parts) >= 2 else file.stem
        
        try:
            df = pd.read_csv(file)
            # Get aggregated results (overall performance)
            overall = df[df['Name'] == 'Aggregated']
            if not overall.empty:
                results[scenario] = overall.iloc[0].to_dict()
                print(f"✅ Loaded {scenario}: {results[scenario]['Request Count']} requests")
            else:
                print(f"⚠️  No aggregated data in {file}")
        except Exception as e:
            print(f"❌ Error loading {file}: {e}")
    
    return results

def generate_performance_charts(results, timestamp):
    """Generate performance visualization charts."""
    print("📊 Generating performance charts...")
    
    # Prepare data for plotting
    scenarios = list(results.keys())
    metrics = {
        'requests_per_second': [results[s]['Requests/s'] for s in scenarios],
        'median_response_time': [results[s]['Median Response Time'] for s in scenarios],
        'percentile_95': [results[s]['95%'] for s in scenarios],
        'failure_rate': [(results[s]['Failure Count'] / results[s]['Request Count'] * 100) 
                        if results[s]['Request Count'] > 0 else 0 for s in scenarios]
    }
    
    # Create subplots
    fig, ((ax1, ax2), (ax3, ax4)) = plt.subplots(2, 2, figsize=(16, 12))
    fig.suptitle(f'Cliffracer Performance Analysis - {timestamp}', fontsize=16, fontweight='bold')
    
    # Requests per second
    bars1 = ax1.bar(scenarios, metrics['requests_per_second'], color='skyblue', alpha=0.8)
    ax1.set_title('Requests per Second (Higher is Better)', fontweight='bold')
    ax1.set_ylabel('RPS')
    ax1.tick_params(axis='x', rotation=45)
    for bar, value in zip(bars1, metrics['requests_per_second']):
        ax1.text(bar.get_x() + bar.get_width()/2, bar.get_height() + max(metrics['requests_per_second'])*0.01,
                f'{value:.1f}', ha='center', va='bottom', fontweight='bold')
    
    # Response time comparison
    x_pos = np.arange(len(scenarios))
    width = 0.35
    bars2a = ax2.bar(x_pos - width/2, metrics['median_response_time'], width, 
                     label='Median', color='lightgreen', alpha=0.8)
    bars2b = ax2.bar(x_pos + width/2, metrics['percentile_95'], width,
                     label='95th Percentile', color='orange', alpha=0.8)
    ax2.set_title('Response Times (Lower is Better)', fontweight='bold')
    ax2.set_ylabel('Time (ms)')
    ax2.set_xticks(x_pos)
    ax2.set_xticklabels(scenarios, rotation=45)
    ax2.legend()
    
    # Failure rate
    bars3 = ax3.bar(scenarios, metrics['failure_rate'], color='lightcoral', alpha=0.8)
    ax3.set_title('Failure Rate (Lower is Better)', fontweight='bold')
    ax3.set_ylabel('Failure Rate (%)')
    ax3.tick_params(axis='x', rotation=45)
    for bar, value in zip(bars3, metrics['failure_rate']):
        ax3.text(bar.get_x() + bar.get_width()/2, bar.get_height() + max(metrics['failure_rate'])*0.01,
                f'{value:.1f}%', ha='center', va='bottom', fontweight='bold')
    
    # Performance efficiency (RPS vs Response Time)
    scatter = ax4.scatter(metrics['median_response_time'], metrics['requests_per_second'], 
                         s=100, alpha=0.7, c=range(len(scenarios)), cmap='viridis')
    ax4.set_xlabel('Median Response Time (ms)')
    ax4.set_ylabel('Requests per Second')
    ax4.set_title('Performance Efficiency (Top-Right is Best)', fontweight='bold')
    
    # Add labels to scatter points
    for i, scenario in enumerate(scenarios):
        ax4.annotate(scenario, (metrics['median_response_time'][i], metrics['requests_per_second'][i]),
                    xytext=(5, 5), textcoords='offset points', fontsize=9)
    
    plt.tight_layout()
    chart_file = analysis_dir / f'performance_analysis_{timestamp}.png'
    plt.savefig(chart_file, dpi=300, bbox_inches='tight')
    print(f"✅ Performance charts saved: {chart_file}")
    plt.close()

def generate_detailed_report(results, timestamp):
    """Generate detailed markdown report."""
    print("📝 Generating detailed analysis report...")
    
    report_file = analysis_dir / f'detailed_analysis_{timestamp}.md'
    
    with open(report_file, 'w') as f:
        f.write(f"# Cliffracer Performance Analysis Report\n\n")
        f.write(f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"**Test Run:** {timestamp}\n\n")
        
        f.write("## Executive Summary\n\n")
        
        # Calculate overall metrics
        total_requests = sum(results[s]['Request Count'] for s in results)
        total_failures = sum(results[s]['Failure Count'] for s in results)
        avg_rps = sum(results[s]['Requests/s'] for s in results) / len(results)
        avg_response_time = sum(results[s]['Median Response Time'] for s in results) / len(results)
        
        f.write(f"- **Total Requests Processed:** {total_requests:,}\n")
        f.write(f"- **Overall Success Rate:** {((total_requests - total_failures) / total_requests * 100):.1f}%\n")
        f.write(f"- **Average RPS Across Scenarios:** {avg_rps:.1f}\n")
        f.write(f"- **Average Response Time:** {avg_response_time:.1f}ms\n\n")
        
        # Performance claims validation
        f.write("## Performance Claims Validation\n\n")
        
        sub_ms_scenarios = [s for s in results if results[s]['Median Response Time'] < 1]
        f.write(f"### Sub-millisecond Performance\n")
        if sub_ms_scenarios:
            f.write(f"✅ **ACHIEVED** in scenarios: {', '.join(sub_ms_scenarios)}\n\n")
        else:
            fastest_scenario = min(results.keys(), key=lambda s: results[s]['Median Response Time'])
            fastest_time = results[fastest_scenario]['Median Response Time']
            f.write(f"❌ **NOT ACHIEVED** - Fastest scenario: {fastest_scenario} ({fastest_time:.1f}ms)\n\n")
        
        # High throughput analysis
        high_throughput_scenarios = [s for s in results if results[s]['Requests/s'] > 1000]
        f.write(f"### High Throughput (>1000 RPS)\n")
        if high_throughput_scenarios:
            f.write(f"✅ **ACHIEVED** in scenarios: {', '.join(high_throughput_scenarios)}\n\n")
        else:
            highest_rps_scenario = max(results.keys(), key=lambda s: results[s]['Requests/s'])
            highest_rps = results[highest_rps_scenario]['Requests/s']
            f.write(f"❌ **NOT ACHIEVED** - Highest RPS: {highest_rps_scenario} ({highest_rps:.1f} RPS)\n\n")
        
        # Detailed scenario analysis
        f.write("## Detailed Scenario Analysis\n\n")
        
        for scenario in sorted(results.keys()):
            data = results[scenario]
            f.write(f"### {scenario.replace('_', ' ').title()}\n\n")
            
            f.write("| Metric | Value | Assessment |\n")
            f.write("|--------|-------|------------|\n")
            f.write(f"| Total Requests | {data['Request Count']:,} | - |\n")
            f.write(f"| Failed Requests | {data['Failure Count']:,} | ")
            
            failure_rate = (data['Failure Count'] / data['Request Count'] * 100) if data['Request Count'] > 0 else 0
            if failure_rate < 1:
                f.write("✅ Excellent\n")
            elif failure_rate < 5:
                f.write("✅ Good\n")
            elif failure_rate < 10:
                f.write("⚠️ Acceptable\n")
            else:
                f.write("❌ Needs Improvement\n")
            
            f.write(f"| Requests/Second | {data['Requests/s']:.1f} | ")
            if data['Requests/s'] > 1000:
                f.write("🚀 Excellent\n")
            elif data['Requests/s'] > 500:
                f.write("✅ Good\n")
            elif data['Requests/s'] > 100:
                f.write("⚠️ Acceptable\n")
            else:
                f.write("❌ Needs Improvement\n")
            
            f.write(f"| Median Response Time | {data['Median Response Time']:.1f}ms | ")
            if data['Median Response Time'] < 10:
                f.write("🚀 Excellent\n")
            elif data['Median Response Time'] < 50:
                f.write("✅ Good\n")
            elif data['Median Response Time'] < 100:
                f.write("⚠️ Acceptable\n")
            else:
                f.write("❌ Needs Improvement\n")
            
            f.write(f"| 95th Percentile | {data['95%']:.1f}ms | ")
            if data['95%'] < 50:
                f.write("🚀 Excellent\n")
            elif data['95%'] < 100:
                f.write("✅ Good\n")
            elif data['95%'] < 200:
                f.write("⚠️ Acceptable\n")
            else:
                f.write("❌ Needs Improvement\n")
            
            f.write("\n")
        
        f.write("## Recommendations\n\n")
        f.write("### Optimization Opportunities\n\n")
        
        # Find slowest scenarios
        slowest_scenario = max(results.keys(), key=lambda s: results[s]['Median Response Time'])
        f.write(f"1. **Focus on {slowest_scenario}**: Highest median response time ({results[slowest_scenario]['Median Response Time']:.1f}ms)\n")
        
        # Find scenarios with highest failure rates
        highest_failure_scenario = max(results.keys(), key=lambda s: 
            results[s]['Failure Count'] / results[s]['Request Count'] if results[s]['Request Count'] > 0 else 0)
        failure_rate = (results[highest_failure_scenario]['Failure Count'] / 
                       results[highest_failure_scenario]['Request Count'] * 100)
        if failure_rate > 5:
            f.write(f"2. **Improve error handling in {highest_failure_scenario}**: {failure_rate:.1f}% failure rate\n")
        
        f.write("3. **Profile memory usage**: Monitor for memory leaks during sustained load\n")
        f.write("4. **Optimize validation**: Complex object validation may be a bottleneck\n")
        f.write("5. **Database simulation**: Consider optimizing async I/O patterns\n\n")
        
        f.write("### Production Readiness\n\n")
        
        production_ready_scenarios = [s for s in results if 
            results[s]['Median Response Time'] < 100 and
            (results[s]['Failure Count'] / results[s]['Request Count'] * 100) < 5 and
            results[s]['Requests/s'] > 50
        ]
        
        if len(production_ready_scenarios) == len(results):
            f.write("✅ **All scenarios meet production readiness criteria**\n\n")
        elif len(production_ready_scenarios) > len(results) // 2:
            f.write(f"⚠️ **Most scenarios ready for production** ({len(production_ready_scenarios)}/{len(results)})\n\n")
        else:
            f.write(f"❌ **Needs optimization before production** ({len(production_ready_scenarios)}/{len(results)} scenarios ready)\n\n")
        
        f.write("---\n\n")
        f.write("*Generated by Cliffracer Load Testing Suite*\n")
    
    print(f"✅ Detailed report saved: {report_file}")

def main():
    files, timestamp = find_latest_results()
    if not files:
        return
    
    results = load_and_process_results(files)
    if not results:
        print("❌ No valid results to analyze")
        return
    
    print(f"📊 Analyzing {len(results)} scenarios...")
    
    generate_performance_charts(results, timestamp)
    generate_detailed_report(results, timestamp)
    
    print()
    print("🎉 Analysis complete!")
    print(f"📁 Results available in: {analysis_dir}")
    print(f"   • Performance charts: performance_analysis_{timestamp}.png")
    print(f"   • Detailed report: detailed_analysis_{timestamp}.md")

if __name__ == "__main__":
    main()
EOF

    if [ $? -eq 0 ]; then
        echo -e "${GREEN}✅ Comprehensive analysis generated successfully${NC}"
    else
        echo -e "${RED}❌ Failed to generate analysis${NC}"
        exit 1
    fi
}

# Generate comparison with baseline claims
generate_claims_validation() {
    echo -e "${YELLOW}🎯 Validating performance claims...${NC}"
    
    python3 << 'EOF'
import pandas as pd
from pathlib import Path
import json

reports_dir = Path('./reports/performance')
analysis_dir = Path('./reports/analysis')

# Find latest results
csv_files = list(reports_dir.glob('*_stats.csv'))
if not csv_files:
    print("❌ No results found")
    exit(1)

# Load baseline scenario
baseline_file = None
for file in csv_files:
    if 'baseline' in file.name:
        baseline_file = file
        break

if not baseline_file:
    print("❌ No baseline results found")
    exit(1)

df = pd.read_csv(baseline_file)
overall = df[df['Name'] == 'Aggregated']

if overall.empty:
    print("❌ No aggregated data in baseline")
    exit(1)

baseline = overall.iloc[0]

print("🎯 Cliffracer Performance Claims Validation")
print("=" * 50)

# Claim 1: Sub-millisecond performance
median_ms = baseline['Median Response Time']
print(f"📊 Median Response Time: {median_ms:.2f}ms")
if median_ms < 1.0:
    print("✅ CLAIM VALIDATED: Sub-millisecond performance achieved")
else:
    print(f"❌ CLAIM NOT MET: {median_ms:.2f}ms > 1ms (but still very fast!)")

print()

# Claim 2: High throughput
rps = baseline['Requests/s']
print(f"📊 Requests per Second: {rps:.1f}")
if rps > 1000:
    print("✅ CLAIM VALIDATED: High throughput (>1000 RPS) achieved")
elif rps > 500:
    print("⚠️ CLAIM PARTIALLY MET: Good throughput but <1000 RPS")
else:
    print("❌ CLAIM NOT MET: Lower than expected throughput")

print()

# Claim 3: Error resilience
failure_rate = (baseline['Failure Count'] / baseline['Request Count'] * 100) if baseline['Request Count'] > 0 else 0
print(f"📊 Failure Rate: {failure_rate:.2f}%")
if failure_rate < 1:
    print("✅ CLAIM VALIDATED: Excellent error resilience (<1% failures)")
elif failure_rate < 5:
    print("✅ CLAIM VALIDATED: Good error resilience (<5% failures)")
else:
    print("❌ CLAIM NOT MET: Higher than expected failure rate")

print()

# Overall assessment
score = 0
if median_ms < 1.0:
    score += 1
if rps > 1000:
    score += 1
elif rps > 500:
    score += 0.5
if failure_rate < 1:
    score += 1
elif failure_rate < 5:
    score += 0.5

print("🏆 Overall Performance Score")
print(f"Score: {score:.1f}/3.0")

if score >= 2.5:
    print("🎉 EXCELLENT: Claims largely validated, production-ready performance")
elif score >= 2.0:
    print("✅ GOOD: Strong performance with minor areas for improvement")
elif score >= 1.5:
    print("⚠️ ACCEPTABLE: Decent performance but optimization needed")
else:
    print("❌ NEEDS WORK: Significant performance improvements required")
EOF
}

# Main execution
main() {
    echo "🎯 Analyzing Cliffracer performance results..."
    echo
    
    if [ ! -d "$REPORTS_DIR" ]; then
        echo -e "${RED}❌ No performance results found${NC}"
        echo "Please run benchmarks first: ./scripts/run-benchmarks.sh"
        exit 1
    fi
    
    generate_comprehensive_analysis
    echo
    generate_claims_validation
    
    echo
    echo -e "${GREEN}🎉 Performance analysis complete!${NC}"
    echo
    echo -e "${YELLOW}📋 Generated Reports:${NC}"
    echo "   📊 Performance charts (PNG)"
    echo "   📝 Detailed analysis (Markdown)"
    echo "   🎯 Claims validation summary"
    echo
    echo -e "${BLUE}📁 All reports available in: $ANALYSIS_DIR${NC}"
    echo
    echo -e "${YELLOW}💡 Next Steps:${NC}"
    echo "   1. Review performance charts for visual analysis"
    echo "   2. Read detailed report for optimization recommendations"
    echo "   3. Compare results with other frameworks"
    echo "   4. Use insights for production deployment planning"
}

# Handle command line arguments
case "${1:-}" in
    "claims")
        generate_claims_validation
        ;;
    *)
        main
        ;;
esac