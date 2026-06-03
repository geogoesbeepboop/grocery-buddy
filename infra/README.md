# Infrastructure

Shared infrastructure used by all agents. Deploy once; agents connect to it.

## Components

| Component | Local | Production |
|---|---|---|
| **Temporal** | `docker compose -f infra/temporal/docker-compose.yml up -d` | `infra/temporal/fly.toml` → Fly.io `temporal-cluster` |

## Adding a new agent

1. Create the agent project (its own repo)
2. Set `TEMPORAL_HOST=temporal-cluster.internal:7233` (Fly) or `localhost:7233` (local)
3. Pick a unique `TEMPORAL_TASK_QUEUE` (e.g. `my-new-agent`)
4. Deploy its workers to Fly.io — they automatically connect to the shared Temporal cluster

No changes to this infra directory are needed.

## Namespaces

Each agent uses the `default` namespace for now. If you need hard isolation between agents (separate retry policies, quotas, or security boundaries), create per-agent namespaces:

```bash
# Using tctl (Temporal CLI)
tctl --namespace grocery-buddy namespace register
tctl --namespace dj-agent namespace register
```

Then set `TEMPORAL_NAMESPACE=grocery-buddy` in each agent's config.
