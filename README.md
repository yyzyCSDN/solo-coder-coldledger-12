# ColdLedger Pharmaceutical Cold-Chain Operations Lab

Dependency-free Python reference implementation for temperature-sensitive pharmaceutical distribution. It models inventory expiry, chain-of-custody, telemetry correction, packaging qualification, dry-ice depletion, freezer allocation, customs holds, serialization, recalls, release decisions, vehicle compartment compatibility, returns, SLA calendars, cross-dock state and urgent express carrier allocation.

Urgent express allocation (`dispatch.express_allocation`) evaluates carrier qualification, booking cut-off, packaging/thermal insulation, driver duty, airport handoff and vehicle temperature control together as one hard gate before ranking survivors on timeliness vs risk; locked assignments are preserved through resource failures and surfaced instead of being silently moved.

Runtime third-party dependencies: none.
