# Spec: MLX Server Reliability

Status: Partially implemented. Provider-side failover safeguards are implemented;
process-management and Kubernetes assets are present but unverified deployment
artifacts. Multi-node availability is future work.

## Scope

This record tracks reliability at three boundaries:

1. The router detects an unavailable inference endpoint and tries an eligible
   higher tier before output begins.
2. A single inference host can be supervised and restarted by an external
   process manager.
3. Multiple inference hosts can continue serving after a host failure.

Only the first boundary is currently implemented and verified in application
code. The latter boundaries require deployment-specific validation and must not
be inferred from checked-in assets alone.

## Implemented

| Capability | Status | Evidence |
| --- | --- | --- |
| Health preflight | Implemented | [OpenAI-compatible provider](../../src/glassrail/providers/openai_compat.py) probes an endpoint health route with a short timeout. [Tier router](../../src/glassrail/providers/router.py) skips providers that expose and fail that check before opening a generation stream. Providers without that route remain eligible. |
| Recoverable HTTP fallthrough | Implemented | [OpenAI-compatible provider](../../src/glassrail/providers/openai_compat.py) maps `401`, `403`, `404`, `429`, and server errors to `ProviderUnavailableError`, allowing router fallthrough before a chunk is emitted. It keeps `400` and `422` as `ProviderError`, because the request is malformed rather than the tier unavailable. |
| Per-request generation ceiling | Implemented | [Settings](../../src/glassrail/config/settings.py) defines `max_generation_tokens`; [provider factory](../../src/glassrail/providers/factory.py) passes it to the [tier router](../../src/glassrail/providers/router.py), which clamps larger requests before sending them to a provider. |

The router still commits once a provider emits output. A failure after the first
chunk propagates rather than replaying the request on another tier; this avoids
duplicated or divergent streamed output.

## Operational Assets

| Asset group | Status | Constraints |
| --- | --- | --- |
| Service-manager and memory-watchdog assets | Present, not deployment-ready | [Server service definition](../../ops/mlx/com.glassrail.mlx-server.plist), [subagent service definition](../../ops/mlx/com.glassrail.mlx-subagent.plist), [watchdog service definition](../../ops/mlx/com.glassrail.mlx-watchdog.plist), and [memory watchdog](../../ops/mlx/memory_watchdog.sh) encode fixed host paths, runtime parameters, and process assumptions. They require parameterization and deployment validation before being treated as a supported operational path. |
| Kubernetes manifests | Present, not production-ready | [Namespace](../../deploy/k8s/00-namespace.yaml), [MLX DaemonSet](../../deploy/k8s/mlx-daemonset.yaml), [MLX Service](../../deploy/k8s/mlx-service.yaml), and [gateway Deployment](../../deploy/k8s/glassrail-deployment.yaml) describe a single-node arrangement with health probes. The DaemonSet references `deploy/k8s/Dockerfile.mlx`, which is absent, and depends on host-coupled process access. No repository evidence establishes image build, cluster validation, rollout behavior, or recovery behavior. |

The existing watchdog's intended behavior is a controlled termination when a
memory watermark is crossed, after which the service manager restarts the
process. This is a single-host recovery mechanism, not high availability.

## Future Multi-Node Reliability

Multi-node reliability begins only when at least two independently recoverable
inference hosts can serve the same compatible model and the routing layer can
exclude unhealthy or saturated hosts. Required design and validation work:

- Define portable deployment inputs for the server command, model location,
  resource limits, logs, and restart policy; remove fixed host assumptions from
  operational assets.
- Establish image build and deployment validation for the Kubernetes path,
  including readiness during model load, restart behavior, and service reachability.
- Enforce one inference server per accelerator host and bound concurrent
  generation per server; multiple replicas on one host do not provide useful
  redundancy and can compete for memory.
- Add capacity-aware request routing or admission control. Service-level
  round-robin alone cannot account for long-running streams or per-host memory
  pressure.
- Add health, saturation, restart, and failover telemetry, then exercise a host
  loss while an eligible alternate host is available.

Until those conditions are met, cloud-tier fallthrough is the only documented
availability path beyond recovery of a single local inference process.
