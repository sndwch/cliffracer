# Continuous Benchmarks & Performance History

This document tracks performance across Cliffracer releases and the active commit baseline.
CI automatically measures latency, throughput, serialization, and extension overheads,
failing the build if any key performance metric regresses beyond tolerance (> 15% on median of runs).

The RPC figures are the cost of a call through `CliffracerService`: the dispatcher,
handler resolution and the reply path, not a bare NATS request and reply. The
serialization figures go through the `cliffracer.core.validation` pack and unpack
wrappers rather than calling a codec directly. Both therefore measure what a service
actually pays, and neither is comparable to a raw transport number.

One benchmark job runs at a time on a host. Two measuring together move these
figures by more than the 15% the gate allows, so a number recorded beside another
benchmark run is not a baseline.

> **Last Generated**: `2026-10-04T09:03:29.807792+00:00`  
> **Baseline Commit**: `0ec0a8c28f`  
> **Environment**: Python 3.13.2 on Linux (x86_64)

## Node Specifications & Environment Context

Execution node environment and broker topology captured to normalize future CI migrations:

| Category | Specification | Recorded Setting / Measurement |
| :--- | :--- | :--- |
| **Runner Hardware** | CPU Cores / Architecture | 20 cores (x86_64) |
| **Runner Hardware** | Total System RAM | 125.47 GB |
| **Runner Hardware** | Operating System / Kernel | Linux 7.0.0-30-generic |
| **Runner Hardware** | Python Runtime | Python 3.13.2 |
| **Network Topology** | Target Broker Endpoint | `nats://127.0.0.1:4222` |
| **Network Topology** | Topology Classification | Localhost / Loopback (collocated zero-hop IPC/TCP) |
| **NATS Broker** | Server Version | NATS v2.10.29 |
| **NATS Broker** | JetStream Engine | Enabled (Active) |
| **NATS Broker** | Maximum Frame / Payload | 1,048,576 bytes (1MB) |

## Current Release Baseline

### Core RPC Latency & Throughput

| Concurrency | Throughput (msgs/sec) | p50 Latency (ms) | p95 Latency (ms) | p99 Latency (ms) |
| :--- | :--- | :--- | :--- | :--- |
| 10 | 8,424.7 | 0.962 | 1.135 | 1.246 |
| 100 | 10,775.7 | 7.832 | 8.733 | 9.293 |
| 1000 | 11,733.0 | 65.774 | 71.917 | 72.668 |

### Serialization: JSON vs MessagePack

| Payload Size | JSON Ser (ms) | JSON Deser (ms) | MsgPack Ser (ms) | MsgPack Deser (ms) | MsgPack Speedup |
| :--- | :--- | :--- | :--- | :--- | :--- |
| 1KB | 0.0054 | 0.0035 | 0.0022 | 0.0010 | **2.7x** (ser) / **2.7x** (deser) |
| 100KB | 0.1473 | 0.0735 | 0.0058 | 0.0059 | **25.7x** (ser) / **12.4x** (deser) |
| 1MB | 1.5251 | 0.7541 | 0.0844 | 0.0699 | **18.1x** (ser) / **10.8x** (deser) |

### Extension Overheads & Reliability

| Component / Extension | Metric | Baseline Measurement | Guarantee / Invariant |
| :--- | :--- | :--- | :--- |
| Core JetStream Pull | Batch throughput | 53,302.9 msgs/sec | p50 batch fetch: 4.188 ms |
| `cliffracer-auth` | Token validation throughput | 67,993.5 ops/sec | PBKDF2 hash verif: 56.1 ops/sec |
| `cliffracer-kv` | Bulk Get/Put throughput | Put: 32,340.1 / Get: 29,354.7 ops/sec | Graceful failure & recovery: Verified |

## Historical Evolution Across Versions

Benchmark results for tagged releases:

| Version Tag | RPC 1000 Throughput | RPC p50 (ms) | JetStream (msgs/sec) | 1MB MsgPack Speedup | Extensions Introduced |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `v1.0.0` | 39,425.2 msgs/sec | 17.30 ms | 40,953.2 msgs/sec | 14.4x | auth, kv |

