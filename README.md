# RoboMaster forum monitor

## 2026-11-08 repair

The 2026-11-04 run https://github.com/1gerfengoole-source/rm-announce/actions/runs/41766695989 explicitly skipped 16 newly discovered articles because their displayed publication ages were 96–168 hours. It then stored their IDs as seen. The old 48-hour gate therefore prevented all future delivery of those articles.

The monitor now:
- Queues newly discovered IDs regardless of publication age.
- Decodes HTML entities in titles and parses article cards independent of attribute order.
- Keeps failed deliveries in a persistent pending queue, including when an article leaves the list.
- Tracks acknowledgements per webhook using SHA-256 identifiers; webhook URLs are never stored.
- Requires HTTP success and an explicit Feishu success code.
- Exits with failure for fetch/parse/delivery errors; saves pending state even after delivery failures.
- Does not send or save state in dry-run mode.
- Retains historical IDs without trimming.

Both notification workflows share a concurrency group to avoid simultaneous state pushes. State commits rebase and retry if the other workflow has advanced main.

## Recover the 16 confirmed omissions

The exact titles and IDs from the log are in `forum_backfill_20261104.json`.
In Actions → RM Forum Monitor → Run workflow:
1. Keep `dry_run=true` and set `backfill_confirmed=true` to preview.
2. To actually send the historical messages, set `dry_run=false`, `backfill_confirmed=true`.
3. Successful historical deliveries are recorded in `backfilled_ids`, so rerunning does not replay them again. Failed destinations remain pending.

No historical backfill is enabled by default. Existing `seen_ids` are retained because the old state cannot distinguish delivered articles from the initial baseline.

## Timing and coverage limits

The configured schedule is every ten minutes, offset from the hour. GitHub Actions schedules are best effort: observed runs on November 7 were several hours apart. A cron change cannot guarantee minute-level delivery. Use an independent scheduler or persistent service if that is required; do not run concurrent copies against separate state files.

The current public HTML endpoint exposes the latest 20 article cards. This repair does not invent an unverified pagination API. If more than 20 articles arrive between successful fetches, older unseen articles may still be missed. A future paginated API integration is needed to remove this coverage limit.

Webhook delivery is at least once: an accepted request whose response is lost, or a crash before state persistence, can cause a duplicate. Feishu webhooks do not provide an application-level idempotency key here.

## Local verification

```sh
pip install requests beautifulsoup4
python -m unittest -v test_rm_forum
python rm_forum_actions.py --dry-run
python rm_forum_actions.py --dry-run --backfill-confirmed
```
