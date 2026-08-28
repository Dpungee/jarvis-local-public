---
name: capability-engineering
description: Research an official API and turn a bounded part of it into a reviewable declarative JARVIS connector.
version: 1.0.0
---
# Capability engineering

Use this playbook when the operator asks JARVIS to learn a new service, API, or account integration.

## Outcome

Produce a small `connector.json` in the workspace that exposes only the actions required for the operator's stated outcome. A connector is data, not executable code. It cannot contain a credential, shell command, hook, plugin import, arbitrary header, or local filesystem path.

## Method

1. Research current primary documentation for authentication, endpoints, request fields, rate limits, and irreversible effects.
2. Separate public/read-only actions from external mutations. Do not disguise a POST as a read action.
3. Choose a credential reference named `JARVIS_CONNECTOR_<SERVICE>_<PURPOSE>`; never request or write the secret value into a file or chat.
4. Declare only fixed HTTPS GET or POST paths. Use required `{parameter}` path segments and a closed JSON parameter schema with `additionalProperties: false`.
5. Give every string and number a sensible bound. Never accept parameter names such as token, password, cookie, private_key, authorization, or api_key.
6. Write the manifest in the workspace, call `connector_validate`, inspect the exact action summary, and correct every validation failure.
7. Call `connector_install` only after validation. Installation requires the operator's one-shot approval for the exact manifest digest and cannot replace an existing connector.
8. Before the first live call, use `connector_describe`, explain the destination and visible effect, and call only the exact action requested. Every call receives a separate one-shot approval.

## Minimal schema

```json
{
  "schema_version": 1,
  "id": "example-service",
  "name": "Example Service",
  "version": "1.0.0",
  "description": "One bounded service integration.",
  "base_url": "https://api.example.com",
  "credential": {
    "kind": "bearer_env",
    "environment": "JARVIS_CONNECTOR_EXAMPLE_ACCESS"
  },
  "actions": [
    {
      "name": "create-item",
      "description": "Create one item in the authenticated account.",
      "method": "POST",
      "path": "/v1/items",
      "risk": "external_mutation",
      "parameters": {
        "type": "object",
        "properties": {
          "text": {"type": "string", "minLength": 1, "maxLength": 280}
        },
        "required": ["text"],
        "additionalProperties": false
      }
    }
  ]
}
```

## Verification

- `connector_validate` returns `valid: true` and a SHA-256 digest.
- The manifest contains no secret-shaped value or credential parameter.
- The official documentation supports the method, path, fields, and authentication style.
- A mutation is never claimed successful without a successful `connector_call` result.
- If the API needs OAuth refresh, multipart upload, streaming, webhooks, a local SDK, or transaction signing, stop at a design brief: this declarative connector version does not pretend to support it.
