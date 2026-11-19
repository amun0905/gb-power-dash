# gb-power-dash

Data pipeline for a GB power-market dashboard built on public data. This first layer
collects, cleans and stores the data. The website reads from the same store later.

```
 GitHub Actions (cron, every 30 min)          Object storage (Cloudflare R2 / B2 / S3)
 ┌───────────────────────────────┐            ┌──────────────────────────────────────┐
 │ gbdash update                 │  Parquet   │ catalog.json                         │
 │  fetch → validate → upsert ───┼──────────► │ generation_fuelinst/2026-11.parquet  │
 │ gbdash status --fail-on-stale │            │ system_prices/_manifest.json  ...    │
 └───────────────────────────────┘            └──────────────────┬───────────────────┘
        ▲ Elexon Insights API (free, no key)                     │ read directly
                                                                 ▼
                                                     Website / dashboard (next step)
```

No server of your own needs to be running. GitHub runs the job on its machines, and the data lives in a bucket.

## Datasets (v1)

| name | what | resolution |
|---|---|---|
| `generation_fuelinst` | generation by fuel type, including interconnectors (FUELINST) | 5 min |
| `demand_indo` | initial national demand outturn (INDO) | 30 min |
| `market_index_mid` | Market Index Data price and volume (MID) | 30 min |
| `system_prices` | system (imbalance) price and net imbalance volume | 30 min |

To add a dataset, add a block to `config/datasets.yaml`. For a new *source* (NESO, ENTSO-E,
Carbon Intensity), add a client in `src/gbdash/sources/` that implements `fetch(spec, start, end)`.

## How robustness is handled

- **Idempotent upserts.** Rows are de-duplicated on `keys`. When a row is published more than
  once, the latest `version_column` (e.g. `publishTime`) wins, so re-runs never create duplicates.
- **Revisions and late data.** Every update re-fetches `lookback_days` before the newest stored row.
- **Schema guard.** Only the declared columns are kept and their types are enforced. If a key or
  time column disappears upstream, the run fails loudly and the error lists the columns the API actually returned.
- **Retries.** 429 and 5xx responses are retried with exponential back-off, and `Retry-After` is honoured.
- **Atomic writes and no overlapping runs.** Files are replaced whole, and the workflow `concurrency` group serialises writers.
- **Resumable backfill.** History loads month by month. Months that are already complete are skipped, so you can just re-run after a failure.
- **Monitoring.** `gbdash status --fail-on-stale` fails the job when data is older than
  `max_staleness_hours`, and GitHub then emails you. `_manifest.json` and `catalog.json` record rows, time range and last update.
- **Keep-alive.** GitHub disables cron jobs after 60 days with no repo activity. The workflow re-enables itself on every run.

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q                                   # offline tests (mocked API)
gbdash list
gbdash update                               # first run: last 7 days into ./data
gbdash backfill system_prices --start 2025-01-01
gbdash status
```

## Deploy (one-off, about 15 minutes)

1. **Bucket.** Create a Cloudflare R2 bucket, e.g. `gb-power-data`. The free tier gives 10 GB and free egress.
   Create an R2 API token with *Object Read & Write* on that bucket.
2. **GitHub repo.** Push this folder. Then go to *Settings → Secrets and variables → Actions*:
   - Variable `GBDASH_STORE` = `s3://gb-power-data`
   - Secret `GBDASH_S3_ENDPOINT` = `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`
   - Secrets `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` = the R2 token's keys
3. **History.** Go to *Actions → backfill → Run workflow*.
4. **Live updates.** The `update-data` workflow now runs at :07 and :37 every hour.

## Licence and attribution

Elexon data is used under the BMRS Data Licence. Show *"Contains BMRS data © Elexon Limited
copyright and database right"* wherever the data is displayed (the manifests carry this text).
Check each source's terms before any commercial use.
