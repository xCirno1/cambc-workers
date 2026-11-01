# cambc-workers

Public GitHub Actions workers for running bot-vs-bot matches. The repo holds only generic worker code.

- Bot sources, maps, build output, game logs and results are packed into a bundle and sealed with
  AES-256-GCM on the owner's machine (`client.py`). The key exists only on the owner's machine and as the
  `BOX_KEY` Actions secret (not exposed to forks or PRs; workflows run only on `workflow_dispatch` from `main`).
- The sealed bundle is a release asset; workers download and decrypt it, run their shard, and upload
  sealed results as short-lived artifacts. All workload output is redirected away from the public Actions log.
- The game subprocess gets an allowlisted environment without the key or the repo token.
- Public: job tag (random), shard count, timing, ciphertext sizes. Nothing else.

Usage: `client.py setup`, then `client.py run bot starter-cpp --shards 4`.
