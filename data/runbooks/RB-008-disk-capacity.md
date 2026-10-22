---
id: RB-008
title: Disk and volume capacity exhaustion
covers: [disk_full]
services: [media-transcode, ledger-sync, report-builder, search-indexer]
---

# Disk and volume capacity exhaustion

## Signature

`disk_used_percent` climbs steadily to 100% and stays there. Writes begin
failing with `ENOSPC` / `No space left on device`. Reads keep working, so
health checks that only read will continue to pass while the service is
materially broken - do not trust a green health check here.

The climb is usually linear and predictable, which means the alert should have
fired on the trend rather than the threshold. If capacity went from healthy to
full in under an hour, suspect a single large write - a runaway log, a core
dump, or an unbounded temporary file - rather than organic growth.

## Confirming

Break the usage down by directory before deleting anything. The three usual
offenders are unrotated application logs, orphaned temporary files from failed
jobs, and retained build or media artifacts past their intended lifetime.

Check inode usage as well as bytes. A volume with free bytes but exhausted
inodes produces the same `ENOSPC` error and is routinely misdiagnosed.

## Remediation

Reclaim space, then fix the retention policy that allowed the growth. Growing
the volume without fixing retention reschedules the incident rather than
resolving it.
