# Continuous Benchmarks & Performance History

This document tracks performance across Cliffracer releases and the active commit baseline.
CI automatically measures latency, throughput, serialization, and extension overheads,
failing the build if any key performance metric regresses beyond tolerance (> 15% on median of runs).

> **Last Generated**: `2026-09-10T20:22:16.111759+00:00`  
> **Baseline Commit**: `f961c4db8a`  
> **Environment**: Python 3.13.2 on Linux (x86_64)

## Node Specifications & Environment Context

Execution node environment and broker topology captured to normalize future CI migrations:

| Category | Specification | Recorded Setting / Measurement |
| :--- | :--- | :--- |
| **Runner Hardware** | CPU Cores / Architecture | 20 cores (x86_64) |
| **Runner Hardware** | Total System RAM | 125.47 GB |
| **Runner Hardware** | Operating System / Kernel | Linux 7.0.0-30-generic |
| **Runner Hardware** | Python Runtime | Python 3.13.2 |
| **Network Topology** | Target Broker Endpoint | `nats://localhost:4222` |
| **Network Topology** | Topology Classification | Localhost / Loopback (collocated zero-hop IPC/TCP) |
| **NATS Broker** | Server Version | NATS v2.10.29 |
| **NATS Broker** | JetStream Engine | Enabled (Active) |
| **NATS Broker** | Maximum Frame / Payload | 1,048,576 bytes (1MB) |

## Current Release Baseline

### Core RPC Latency & Throughput

| Concurrency | Throughput (msgs/sec) | p50 Latency (ms) | p95 Latency (ms) | p99 Latency (ms) |
| :--- | :--- | :--- | :--- | :--- |
| 10 | 21,701.6 | 0.336 | 0.725 | 0.766 |
| 100 | 37,160.7 | 2.017 | 3.059 | 3.194 |
| 1000 | 45,201.6 | 14.244 | 15.900 | 16.075 |

### Serialization: JSON vs MessagePack

| Payload Size | JSON Ser (ms) | JSON Deser (ms) | MsgPack Ser (ms) | MsgPack Deser (ms) | MsgPack Speedup |
| :--- | :--- | :--- | :--- | :--- | :--- |
| 1KB | 0.0037 | 0.0025 | 0.0008 | 0.0009 | **4.6x** (ser) / **2.9x** (deser) |
| 100KB | 0.1446 | 0.0740 | 0.0047 | 0.0058 | **30.7x** (ser) / **12.6x** (deser) |
| 1MB | 1.4869 | 0.7537 | 0.0877 | 0.0686 | **16.9x** (ser) / **11.0x** (deser) |

### Extension Overheads & Reliability

| Component / Extension | Metric | Baseline Measurement | Guarantee / Invariant |
| :--- | :--- | :--- | :--- |
| Core JetStream Pull | Batch throughput | 50,292.1 msgs/sec | p50 batch fetch: 4.556 ms |
| `cliffracer-http` AutoGateway | Route translation overhead | 0.1796 ms | Raw RPC: 0.0074 ms |
| `cliffracer-faststream` | ACK Hypervisor overhead | 0.00619 ms/msg | Hypervisor: 160,059.9 msgs/sec |
| `cliffracer-auth` | Token validation throughput | 79,407.1 ops/sec | PBKDF2 hash verif: 54.9 ops/sec |
| `cliffracer-kv` | Bulk Get/Put throughput | Put: 40,780.4 / Get: 29,873.6 ops/sec | Graceful failure & recovery: Verified |
