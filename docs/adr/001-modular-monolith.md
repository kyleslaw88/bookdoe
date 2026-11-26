# ADR 001 — Modular monoliths

**Decision:** bookdoe and OpenReads remain independently deployable modular monoliths rather than decomposing into microservices.

**Reason:** The workload benefits from transactional local state and simple operations. Redis, brokers and distributed transactions would add failure modes without solving a current scale constraint.
