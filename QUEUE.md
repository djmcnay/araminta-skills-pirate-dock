# Queue API

`GET /queue` returns `{"jobs": [...], "total": N}`. POST and other write methods are not supported.

Each row contains `job_id`, `rpc_port`, `started_at`, `name`, `status`, `progress` (`downloaded`, `total`, `percent`), `download_speed`, `upload_speed`, `connections`, `error`, `error_code`, and `telemetry_available`. Live rows additionally have `gid` and `aria2_status`. Sizes are bytes and rates are bytes/second. Status is downloading, completed, seeding, or stopped; aria2 waiting is included under downloading, paused/error/removed under stopped.

The persisted tracking file contains launch metadata only, keyed by reusable RPC port; it is not a complete permanent download history. The endpoint reads each recorded launch and queries only its matching live aria2 process. It does not start aria2, resume downloads, change configuration, or write the tracking file. Magnet metadata rows with followedBy are excluded in favour of payload rows.

When a launch no longer runs or RPC is unavailable, its row remains visible as stopped, with null telemetry and an explanatory error. **Stopped does not prove the payload never completed.** Historical final progress/completion was not persisted and is not reconstructed from file allocation. Names fall back to a bounded read of the launch's log, then job ID. A completed count of zero means no completion currently evidenced by RPC, not that nothing has ever completed.

The Gradio dashboard reads http://localhost:9876/queue for its table and statistics, refreshes every 15 seconds, and displays unknown values as an em dash. API errors are visible rather than shown as an empty queue. VPN and health sections are unchanged.

Regression tests: `python3 tests/test_queue.py` (stdlib only). Live verification: `curl -f http://localhost:9876/queue` and open the dashboard on port 7860.

Deployment: source is scripts/server.py; running container uses /app/server.py from its writable layer. Copy source into the existing container and restart only pirate-dock. Source changes also enter the next normal image build; do not recreate from an old image or the running patch will be lost.
