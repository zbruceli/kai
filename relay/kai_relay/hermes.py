"""Client for Hermes Agent's API server (the slow-path brain; see relay/deploy/hermes/README.md).

Only the parts Kai uses: start a background run, poll it, and check health. Hermes runs on the same host,
bound to localhost, authenticated with API_SERVER_KEY.
"""

import httpx

# Statuses that mean "still working"; anything else is final.
RUNNING = {"queued", "pending", "running", "in_progress", "started"}


class HermesError(Exception):
    pass


class HermesClient:
    def __init__(self, base_url: str, api_key: str, http: httpx.AsyncClient, session_key: str = "kai:owner"):
        self.base_url = base_url.rstrip("/")
        self.http = http
        # X-Hermes-Session-Key scopes Hermes's long-term memory to Kai's owner across runs.
        self.headers = {"Authorization": f"Bearer {api_key}", "X-Hermes-Session-Key": session_key}

    async def health(self) -> bool:
        try:
            r = await self.http.get(f"{self.base_url}/health", timeout=5)
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    async def start_run(self, prompt: str, idempotency_key: str | None = None) -> str:
        headers = dict(self.headers)
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            r = await self.http.post(f"{self.base_url}/v1/runs", json={"input": prompt}, headers=headers, timeout=30)
        except httpx.HTTPError as e:
            raise HermesError(f"Hermes unreachable: {e}") from e
        if r.status_code == 429:
            raise HermesError("Hermes is busy with other tasks")
        if r.status_code >= 400:
            raise HermesError(f"Hermes refused the task ({r.status_code})")
        run_id = r.json().get("run_id")
        if not run_id:
            raise HermesError("Hermes returned no run id")
        return run_id

    async def get_run(self, run_id: str) -> dict:
        try:
            r = await self.http.get(f"{self.base_url}/v1/runs/{run_id}", headers=self.headers, timeout=30)
        except httpx.HTTPError as e:
            raise HermesError(f"Hermes unreachable: {e}") from e
        if r.status_code == 404:
            return {"status": "lost", "error": "Hermes no longer knows this run"}
        r.raise_for_status()
        return r.json()
