---
id: RB-002
title: TLS certificate expiry and handshake failures
covers: [expired_tls_certificate]
services: [auth-gateway, checkout-api, notify-dispatch]
---

# TLS certificate expiry and handshake failures

## Signature

Client logs carry `certificate verify failed`, `CERTIFICATE_VERIFY_FAILED`, or
`x509: certificate has expired or is not yet valid`. Failure is total and
instantaneous for every caller that validates the chain - there is no ramp.
Error rate goes from near zero to near 100% within one scrape interval.

The metric shape is the giveaway: `tls_handshake_failures` steps from 0 to the
full request rate in a single interval, while `cpu_utilization` and
`db_active_connections` stay perfectly normal. Load-related causes ramp;
expiry is a step function.

## Confirming

Check the leaf certificate `notAfter` field against the current time, and check
it at the edge that is actually terminating TLS - an expired certificate on an
internal hop presents identically to callers but will not show up if you only
inspect the public endpoint.

## Remediation

Reissue and deploy the leaf certificate, then reload the terminating proxy.
Certificates issued by the internal CA have a 90-day lifetime and renewal is
automated, so an expiry almost always means the renewal job has been failing
silently. Check that job before closing the incident.
