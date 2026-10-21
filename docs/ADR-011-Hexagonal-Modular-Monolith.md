# ADR-011: Hexagonal Modular Monolith + Strangler for V11 Archive

- **Status:** Accepted
- **Date:** 2026-07-26
- **Context:** V12 needs upgrade-friendly decoupling without microservice cost.
- **Decision:** Adopt hexagonal modular monolith; confine archive to adapters/legacy_v11; complete ports before package splits.
- **Consequences:** import boundaries enforced; MarketData/Chronos/Broker/Notifier/Clock via ports; microservices deferred.
- **Detail SSOT:** docs/superpowers/specs/2026-07-26-architecture-decoupling-upgrade-design.md
- **Implementation plan (P0–P1):** docs/superpowers/plans/2026-07-26-architecture-decoupling-phase0-1.md
