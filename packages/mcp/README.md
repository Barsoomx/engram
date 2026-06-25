# MCP

Owns the future thin MCP bridge that calls Engram server APIs for agent-native
memory and context workflows.

This directory is inactive in the skeleton checkpoint. It must not introduce a
local authoritative memory store, independent retrieval implementation, or
provider secret handling.

Activation gate: MCP contract slice after the first parity loop proves the
required API surface.
