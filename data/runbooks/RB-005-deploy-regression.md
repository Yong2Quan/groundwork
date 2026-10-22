---
id: RB-005
title: Post-deploy regressions and feature-flag config drift
covers: [bad_deploy_regression, config_drift_feature_flag]
services: [checkout-api, notify-dispatch, inventory-cache, session-store]
---

# Post-deploy regressions and feature-flag config drift

## Deploy regression

The signature is a step change in `error_rate` that lines up with a deploy
timestamp to within one scrape interval, with no corresponding change in
traffic, memory, or database metrics. The system is not under stress; it is
running new code that is wrong.

Confirm by checking the deploy log for the service and comparing the
regression onset against it. If the onset *precedes* the most recent deploy,
the deploy is not your cause - keep looking.

Remediation: roll back first, diagnose second.

## Feature-flag config drift

Harder, because there is no deploy to correlate against. The signature is a
step change in behaviour with no deploy, no infrastructure event, and no
traffic change. Flag evaluation changes take effect on the next request, so
the step is as sharp as a deploy.

Config drift is most often a flag enabled for a percentage rollout in one
environment and never reconciled, or a default that changed when a flag was
deleted from the registry while callers still referenced it. Check the flag
audit log for the window, not just the current flag state - the current state
may already have been corrected by someone else.
