## GitHub Pages portfolio

`site/index.html` is a standalone static project page. The Pages workflow deploys it from `main` after GitHub Pages is configured to use GitHub Actions. This provides a public resume URL without implying the voice backend is live. The backend remains separate and needs persistent HTTPS/WebSocket hosting.

# Deployment

The included Dockerfile and Compose file start one FastAPI service with persistent SQLite volume. To serve remote microphone users, put it behind public HTTPS with WebSocket proxying. Twilio requires a publicly reachable HTTPS endpoint and exact `VOCALIS_PUBLIC_BASE_URL`; the phone number must POST to `/twilio/voice`. Set `TWILIO_AUTH_TOKEN`, create a tenant configured with the matching `To` number, and configure provider keys. Check `/api/health`, run `pytest -q`, and place a real test call before exposing the number to customers.

This deployment is **not production hardened**. It lacks PostgreSQL, durable cross-process call locks, distributed rate limiting, migrations, health/readiness checks for providers, metrics export, alerting, secret rotation, backups, retention jobs, and separate real-time workers. Use one replica only. On a real deployment, configure strict origin/host controls, TLS, volume encryption, backup/restore drills, network egress policy, and provider retry/error monitoring. A static or serverless site host alone cannot maintain these WebSockets.
