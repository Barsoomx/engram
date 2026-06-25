# North Star

## Vision

Engram is an open platform for shared engineering memory used by AI development
agents.

Long term, memory stops being a set of notes and becomes an organization-level
context layer used by Claude Code, Codex, Gemini CLI, Cursor, OpenAI Agents, and
future agent runtimes.

Each next agent should not start from zero. It should start with the engineering
experience the team has already accumulated.

## Current Release Direction

V1 is not the whole platform.

V1 is a high-quality evolution of `claude-mem` focused on one problem:

> Reduce the time AI agents spend re-learning a project.

V1 includes:

- backend;
- frontend;
- API;
- CLI;
- multiple projects;
- memory storage;
- memory search;
- context bundle generation;
- Claude Code and Codex integration.

## Architectural Invariants

### LLM-Agnostic

Memory does not belong to a specific model or agent runtime.

Claude Code, Codex, Gemini CLI, Cursor, OpenAI Agents, and future agents should
use the same memory. Replacing a model or adding an agent must not require a
data-model rewrite.

### Memory-First

Memory is the product core.

UI, CLI, API, MCP, integrations, hooks, and agents are interfaces for reading
and writing memory. If any one interface is removed, the memory layer should
continue to exist and remain useful.

### Context, Not Search

The product goal is not to find similar notes.

The product goal is to assemble the smallest useful context for a concrete
task. The primary output is a ready-to-inject context bundle, not a list of
records.

### Local-First

Users own their data.

Self-hosted deployment is the primary scenario. A hosted cloud service is only
another deployment option. Memory must remain portable without vendor lock-in.

### Agent-Native

Every important capability should be available to agents through APIs as
naturally as it is available to humans through the UI.

CLI, API, and MCP are first-class interfaces alongside the web application.

## Core Domain Concepts

The architecture must be shaped around these concepts even when V1 uses some of
them minimally:

- Organization;
- Project;
- Team;
- Agent;
- Memory;
- Session;
- Context.

The memory model must not depend on a specific LLM provider. Current and future
agents should read from and write to the same memory layer.

## Roadmap

### V1

Build a high-quality evolution of `claude-mem`:

- backend;
- frontend;
- API;
- CLI;
- multiple projects;
- memory storage;
- search;
- context bundle generation.

The main goal is to reduce repeated project re-learning by AI agents.

### Next Iteration

Do not widen the platform first. Improve memory quality first:

- automatic proposals for new memories;
- links between memory, code, pull requests, and tasks;
- task-relevant context;
- deduplication;
- memory versions;
- collaboration across multiple developers.

At this point, memory becomes collective rather than personal.

### Public Release

By public release, Engram should not feel like another knowledge base or another
RAG product.

It should feel like the memory layer between codebases and AI agents.

Required public-release properties:

- support for multiple models and agent runtimes;
- shared team memory;
- automatic context assembly for a task;
- UI, API, CLI, and MCP access;
- local self-hosted deployment;
- portable data without vendor lock-in;
- open architecture for new agents and integrations.

## Main Principle

We are not building a Confluence replacement.

We are not building just another RAG product.

We are building the engineering memory layer between the codebase and AI
development agents.

Code answers: how does the system work?

Superpowers answer: what must not be broken?

Memory answers: what should the agent know before changing this code?

That is the core value of the project.
