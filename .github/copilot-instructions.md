# claudex-teams development notes

This repository is moving from an inherited local-worker memory tool toward a
server-only enterprise memory product for Claude Code and Codex teams.

When suggesting changes:

- keep hooks thin and server-directed;
- do not add developer-machine local worker requirements;
- keep secrets server-side;
- reuse the shared organization/team/project/user/API-key scope model;
- prefer exact, auditable behavior over speculative abstraction;
- update architecture docs before major implementation changes.
