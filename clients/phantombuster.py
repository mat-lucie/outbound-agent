"""PhantomBuster API v2 client (F-PR-5 typed contract).

`launch_agent` returns a typed `PBLaunch` envelope carrying the
container_id, agent_id, launched_at timestamp, and a stable hash of
the launch arguments. Downstream `wait_for_completion` and
`download_result_csv` consume the launch to key their work to THIS
run — preventing the prior "latest CSV" behavior where re-launches
silently fetched the most recent S3 file.

`wait_for_completion` raises `PBRunFailed` on `status="error"` and
`PBRunTimeout` on poll exhaustion. The prior contract returned the
output dict in both cases, leading to silent-error swallow in several
callers (see daily_check.py:696 for the lifted-up pattern).

See `clients/pb_envelope.py` for the SendOutcome envelope + advance
gate predicate consumed by send-phantom callers.
"""

import re
import time
from datetime import UTC, datetime

import httpx

from clients.pb_config import (
    li_session_cookie,
    li_user_agent_or_default,
    require_api_key,
)
from clients.pb_envelope import (
    PBCompletion,
    PBLaunch,
    PBRunFailed,
    PBRunTimeout,
    hash_arguments,
)


def get_phantombuster_credentials() -> tuple[str, str]:
    """Return (li_session_cookie, li_user_agent) from environment.

    Secrets are env-only (P3): the cookie defaults to ``""`` and the
    user-agent to the shipped :data:`clients.pb_config.DEFAULT_USER_AGENT`.
    """
    return li_session_cookie(), li_user_agent_or_default()


class PhantomBusterClient:
    """Client for the PhantomBuster API v2."""

    BASE_URL = "https://api.phantombuster.com/api/v2"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or require_api_key()
        self._client = httpx.Client(
            base_url=self.BASE_URL,
            headers={
                "X-Phantombuster-Key": self.api_key,
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

    def _request(self, method: str, path: str, **kwargs) -> dict:
        resp = self._client.request(method, path, **kwargs)
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    def list_agents(self) -> list[dict]:
        """List all configured phantoms (agents)."""
        data = self._request("GET", "/agents/fetch-all")
        return data if isinstance(data, list) else data.get("data", [])

    def get_agent(self, agent_id: str) -> dict:
        """Get details for a specific phantom."""
        return self._request("GET", "/agents/fetch", params={"id": agent_id})

    # Retry schedule for PB workspace-level concurrency cap (HTTP 429).
    # PB free/starter plans cap the number of phantoms running in parallel;
    # when exceeded, launch returns 429 with "Maximum number of parallel
    # executions reached". Back off and retry — another phantom will finish.
    _LAUNCH_BACKOFF_SCHEDULE = (30, 60, 120, 240, 480)  # seconds, ~15 min total

    def launch_agent(
        self, agent_id: str, arguments: dict | None = None
    ) -> PBLaunch:
        """Launch a phantom and return a typed `PBLaunch` envelope.

        Auto-retries on HTTP 429 (PB workspace parallel-execution cap) with
        exponential backoff. Other errors propagate immediately.

        The returned `PBLaunch.container_id` is the field name PB
        uses on the launch response (`containerId`). Downstream
        polling and CSV fetch key off this value, not the agent id —
        re-launches of the same agent get fresh container ids.
        """
        body: dict = {"id": agent_id}
        if arguments:
            body["arguments"] = arguments

        launched_at = datetime.now(UTC)
        for attempt, wait_s in enumerate((0, *self._LAUNCH_BACKOFF_SCHEDULE)):
            if wait_s:
                print(
                    f"  PhantomBuster parallel-execution cap reached; "
                    f"waiting {wait_s}s for a slot "
                    f"(attempt {attempt}/{len(self._LAUNCH_BACKOFF_SCHEDULE)})..."
                )
                time.sleep(wait_s)
            try:
                resp = self._request("POST", "/agents/launch", json=body)
                container_id = (
                    resp.get("containerId")
                    or resp.get("container_id")
                    or ""
                )
                if not container_id:
                    raise PBRunFailed(
                        container_id="",
                        agent_id=agent_id,
                        log_tail=(
                            f"PB launch returned no containerId; "
                            f"response={resp!r}"
                        ),
                    )
                return PBLaunch(
                    container_id=str(container_id),
                    agent_id=agent_id,
                    launched_at=launched_at,
                    arguments_sha256=hash_arguments(arguments),
                    # PB's launch response uses `requestId` only on some
                    # plans; we don't fall back to `id` (which is the
                    # AGENT id, not the request id — using it would be
                    # misleading).
                    request_id=resp.get("requestId"),
                )
            except httpx.HTTPStatusError as e:
                if e.response.status_code != 429:
                    raise
                if attempt == len(self._LAUNCH_BACKOFF_SCHEDULE):
                    raise
        raise RuntimeError("unreachable")

    def get_output(self, agent_id_or_container: str) -> dict:
        """Get the output/results of an agent run.

        PB's `/agents/fetch-output` endpoint accepts EITHER `id`
        (= agent id, returns the latest run) OR `containerId` (returns
        the specific run). F-PR-5 callers should always pass the
        container id via `wait_for_completion(launch)` — `agent_id`
        access stays for backward-compat with utility callers.
        """
        return self._request(
            "GET", "/agents/fetch-output",
            params={"id": agent_id_or_container},
        )

    def get_container_output(self, container_id: str, agent_id: str) -> dict:
        """Fetch a run's output dict (status, isAgentRunning, output).

        PB's `/agents/fetch-output` REQUIRES the agent `id`; a request with
        only `containerId` returns HTTP 400 (verified live 2026-05-28). We
        send BOTH: `id` satisfies the endpoint, `containerId` scopes intent.

        NOTE: PB returns the agent's LATEST run's output regardless of the
        `containerId` filter, so the response's own `containerId` field is
        the source of truth for WHICH run this is. Callers that need
        container precision (e.g. `wait_for_completion`) must compare
        `output["containerId"]` against their launch's container_id before
        trusting a terminal status — otherwise a prior run that is still
        "latest" (ours not yet registered) would be mistaken for ours.
        """
        return self._request(
            "GET", "/agents/fetch-output",
            params={"id": agent_id, "containerId": container_id},
        )

    def wait_for_completion(
        self,
        launch: PBLaunch,
        *,
        poll_interval: int = 10,
        max_wait: int = 600,
    ) -> PBCompletion:
        """Poll until the agent run finishes; return typed completion.

        Raises:
            PBRunFailed: PB reports `status="error"` for this
                container.
            PBRunTimeout: polling exhausts `max_wait` without seeing
                a terminal state. Carries the LAST observed status +
                output so the caller can decide whether to salvage
                partial state OR drop the batch. Callers MUST NOT
                write confirmed counts derived from `last_observed_output`
                to Attio without simultaneously escalating the
                uncertainty — partial counts must be either escalated
                or dropped, never silently committed.

        Returns:
            `PBCompletion` carrying the container id, log output, and
            raw PB response dict (kept for debugging — callers should
            prefer named fields).

        Mid-poll transient HTTP/network errors from
        `get_container_output` are caught and retried (consecutive
        failures fall through to the normal max_wait timeout, which
        then raises PBRunTimeout with whatever last_observed_status
        was successfully captured before the failures began). This
        preserves the salvage contract — a caller catching
        PBRunTimeout always gets a typed exception with the partial
        state, never a bare httpx error.
        """
        elapsed = 0
        last_status: str | None = None
        last_output: dict | None = None
        while elapsed < max_wait:
            try:
                output = self.get_container_output(
                    launch.container_id, launch.agent_id
                )
            except httpx.HTTPError as _exc:
                # Transient — let the next poll iteration retry. Do
                # NOT raise the bare HTTP error past the typed
                # PBRunTimeout boundary the docstring promises.
                time.sleep(poll_interval)
                elapsed += poll_interval
                continue
            last_output = output
            last_status = output.get("status", "") or None
            is_running = output.get("isAgentRunning", False)
            # PB's fetch-output returns the agent's LATEST run; only accept a
            # terminal status when it's OUR container. A response without a
            # containerId (e.g. test stubs) defaults to a match for
            # back-compat. This prevents returning a prior run's result while
            # ours is still queued (the F-PR-5 "latest CSV" hazard).
            is_our_container = (
                output.get("containerId", launch.container_id)
                == launch.container_id
            )
            if is_our_container and not is_running and last_status in ("finished", "error"):
                log_output = output.get("output", "") or ""
                if last_status == "error":
                    raise PBRunFailed(
                        container_id=launch.container_id,
                        agent_id=launch.agent_id,
                        log_tail=log_output,
                    )
                return PBCompletion(
                    container_id=launch.container_id,
                    status="finished",
                    log_output=log_output,
                    raw_output=output,
                )
            time.sleep(poll_interval)
            elapsed += poll_interval
        raise PBRunTimeout(
            container_id=launch.container_id,
            agent_id=launch.agent_id,
            elapsed_seconds=elapsed,
            last_observed_status=last_status,
            last_observed_output=last_output,
        )

    def get_result_csv_url(
        self, launch: PBLaunch, *, csv_name: str = "result"
    ) -> str | None:
        """Resolve the result CSV URL for a specific launch.

        Tries the container's log output first (Phantom prints `CSV
        saved at <url>` on success). Falls back to the agent's
        `s3Folder` + `orgS3Folder` — but in that path the URL is
        agent-scoped, NOT container-scoped, so the returned URL
        reflects whichever run last wrote `<csv_name>.csv`. Callers should
        treat the fallback as "best effort" and verify the CSV
        contents against `launch.container_id` if precision matters.

        Callers that launched with a custom `csvName` argument MUST pass
        the same name here, or the agent-scoped fallback fetches a stale file.
        """
        output = self.get_container_output(launch.container_id, launch.agent_id)
        log = output.get("output", "") or ""
        csv_match = re.search(r"CSV saved at (https://\S+\.csv)", log)
        if csv_match:
            return csv_match.group(1)

        # Fallback: agent-scoped S3 path. Less precise but matches
        # the prior client behavior for phantoms that don't print the
        # "CSV saved at" log line.
        agent = self.get_agent(launch.agent_id)
        s3_folder = agent.get("s3Folder", "")
        org_folder = agent.get("orgS3Folder", "")
        if org_folder and s3_folder:
            return (
                f"https://phantombuster.s3.amazonaws.com/"
                f"{org_folder}/{s3_folder}/{csv_name}.csv"
            )
        return None

    def download_result_csv(
        self, launch: PBLaunch, *, csv_name: str = "result"
    ) -> str | None:
        """Download the CSV result for a launch.

        Container-keyed on the happy path via the `CSV saved at <url>`
        log line in the container's fetch-output. Falls back to the
        agent-scoped S3 path (`get_result_csv_url`) — in that fallback
        the CSV reflects whichever run last wrote `<csv_name>.csv`, NOT
        necessarily this launch. Production send-phantom callers
        verify the resulting CSV against `launch.container_id` via
        the advance gate (`should_advance_batch` checks
        `outcome.container_id == launch.container_id` — though by
        construction `parse_send_outcome` copies the container_id
        from the launch, so the second layer of defense is the
        per-URL match against `outcome.sent_urls`).

        Callers that launched with a custom `csvName` argument MUST pass
        the same name here, or the agent-scoped fallback fetches a stale file.
        """
        url = self.get_result_csv_url(launch, csv_name=csv_name)
        if not url:
            return None
        try:
            resp = httpx.get(url, timeout=30.0)
            resp.raise_for_status()
        except httpx.HTTPError:
            # With per-launch csvName the agent-scoped fallback URL may not
            # exist (e.g. PB ignored the argument and wrote result.csv, or
            # the log line was truncated). A missing/erroring CSV must read
            # as "no CSV" — callers route that into their no-data degrade
            # paths — not as an unhandled crash that kills the daily run.
            return None
        return resp.text

    def download_latest_csv_for_agent(self, agent_id: str) -> str | None:
        """Legacy shim: download whichever CSV is currently at the
        agent's `result.csv` path.

        Pre-F-PR-5 behavior. Use only from debug/inspection scripts
        that don't have a `PBLaunch` in scope. Production callers
        MUST use `download_result_csv(launch)` so the CSV is keyed to
        a specific run.
        """
        agent = self.get_agent(agent_id)
        s3_folder = agent.get("s3Folder", "")
        org_folder = agent.get("orgS3Folder", "")
        if not org_folder or not s3_folder:
            return None
        url = (
            f"https://phantombuster.s3.amazonaws.com/"
            f"{org_folder}/{s3_folder}/result.csv"
        )
        resp = httpx.get(url, timeout=30.0)
        resp.raise_for_status()
        return resp.text

    def upload_csv(
        self, agent_id: str, csv_content: str, filename: str = "input.csv"
    ) -> str:
        """Upload a CSV to a phantom's container and return the
        container path.

        PhantomBuster phantoms can read files from their container
        storage. This uploads a CSV file that can be referenced as a
        spreadsheet input.

        Returns the container file path to use as spreadsheetUrl
        argument.
        """
        resp = self._client.post(
            "/agents/save",
            json={
                "id": agent_id,
                "argument": {
                    "spreadsheetUrl": f"file://{filename}",
                },
            },
        )
        resp.raise_for_status()

        upload_resp = self._client.post(
            f"/agents/{agent_id}/store",
            content=csv_content.encode(),
            headers={
                "X-Phantombuster-Key": self.api_key,
                "Content-Type": "text/csv",
                "X-Phantombuster-Container-Filename": filename,
            },
        )
        upload_resp.raise_for_status()
        return f"file://{filename}"

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
