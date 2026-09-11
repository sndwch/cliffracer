# Changelog

What an upgrader needs, newest first.

Every release also gets a note on the
[releases page](https://github.com/sndwch/cliffracer/releases).
The release job writes it there from the commit messages and `BREAKING CHANGE:`
footers in that release's range, so the full commit-level history lives there
rather than here.

<!-- version list -->

## 1.0.0

- Initial open-source release of Cliffracer.
- Introduces core NATS RPC and event routing with `CliffracerService`.
- Natively supports JetStream for distributed pub/sub and `@idempotent` deduplication.
- Extensible architecture using modular workspace extensions (HTTP, WebSockets, Auth, Metrics, Cron, Backdoor, Resilience, OTel).
- Built-in dynamic typed client generator and auto-description of schemas.
