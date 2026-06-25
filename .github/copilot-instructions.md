# Engram development notes

This repository is moving from an inherited local-worker memory tool toward a
server-only engineering memory product for AI coding agents.

When suggesting changes:

- keep hooks thin and server-directed;
- do not add developer-machine local worker requirements;
- keep secrets server-side;
- reuse the shared organization/team/project/user/API-key scope model;
- prefer exact, auditable behavior over speculative abstraction;
- update architecture docs before major implementation changes.
