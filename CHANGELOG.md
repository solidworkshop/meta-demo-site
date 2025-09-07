# Changelog

## 2.1.1
- Added **Shared Event-ID mode** (optional): Pixel Test buttons can also send the same event to CAPI via `/ingest` with a shared `event_id` to verify dedup quickly.
- Added optional **fbp/fbc** pass-through from browser cookies to CAPI `user_data` when present.
- Captured **latency (ms)** and **HTTP status code** for CAPI requests; exposed in Event Console and Replay export.
- Introduced a basic **Payload Inspector** modal for each console row (raw sent payload + quick validation hints).
- Bounded dedup memory to limit growth during long runs.
- Extended **currency mismatch** options with `PIXEL_NULL` and `CAPI_NULL` (non-breaking; existing options preserved).
- Stabilized GA4 sink with a per-process **client_id** for better multi-event coherence.
- Improved proxy awareness for CAPI `client_ip_address` via `X-Forwarded-For` header (when present).
- `/healthz` now reports `pixel_ready`, `capi_ready`, `ga4_ready` and `ok` reflects actual readiness.
- Allow clearing **Shipping Options** to an empty list via the UI.
- Added **Self-Test** button (smoke tests common events and returns a quick pass/fail summary).
