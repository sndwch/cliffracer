# Load testing

Harness for benchmarking a cliffracer service against your own workload.

## Layout

- `tests/locust/cliffracer_load_test.py` — the Locust scenario
- `scripts/` — `run-benchmarks.sh`, `start-test-services.sh`, `generate-reports.sh`
- `config/test-config.yaml` — scenario configuration
- `services/` and `shared/` — the services the benchmark drives
- `simple_load_test.py` — raw NATS round-trip timing, no application logic
- `simple_test_service.py` — the service `simple_load_test.py` calls

## Running

```bash
cd load-testing
pip install -r requirements.txt

# Raw NATS round-trip latency
python simple_load_test.py

# Full benchmark suite
./scripts/run-benchmarks.sh
```

Measure against your own workload and environment. Throughput depends on
handler cost, payload size, JetStream settings and replica count, so a figure
measured here describes this machine running this scenario.
