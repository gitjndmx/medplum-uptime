#!/usr/bin/env python3
"""Medplum Health, watched from outside - run every 5 minutes by GitHub Actions (not on the clinic's
own server, which is what it watches).

Each check posts up=1 or up=0 to Oracle Cloud Monitoring (namespace medplum_uptime, dimension
`check`). Alarms there email the clinic through Oracle Notifications - so an outage is reported even
when the clinic's server, its email and its own monitoring are all down together:
  - any check at 0                     -> "Medplum Health: <check> is down"
  - no data for 20 minutes             -> "the outside checker has stopped" (GitHub delayed or
                                           disabled the schedule, or its key stopped working)
  - the box's own metrics absent       -> "the server has gone silent"

`canary` is a check that is meant to fail now and then: the clinic plants it (ops/medplum-canary)
to prove, every night, that a failing check really becomes an email. Nothing else here knows it.
No secrets are in this repository; the Oracle key is a GitHub Actions secret, and the Oracle user it
belongs to can do nothing but post metrics to this one namespace.
"""
import datetime, json, os, ssl, sys, urllib.error, urllib.request

API = "https://api.medplum.site"
TIMEOUT = 20


def fetch(url, method="GET", body=None, headers=None):
    r = urllib.request.Request(url, method=method, data=body, headers=headers or {})
    try:
        with urllib.request.urlopen(r, timeout=TIMEOUT, context=ssl.create_default_context()) as res:
            return res.status, res.read(200000).decode("utf-8", "replace"), dict(res.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(2000).decode("utf-8", "replace"), dict(e.headers or {})
    except Exception as e:  # noqa: BLE001
        return 0, str(e), {}


def checks():
    out = {}
    s, b, _ = fetch("https://portal.medplum.site/")
    out["portal"] = (s == 200 and "<div id=\"root\"" in b, "HTTP %s" % s)
    s, b, _ = fetch("https://medplum.site/")
    out["provider"] = (s == 200 and "<div id=\"root\"" in b, "HTTP %s" % s)
    s, b, _ = fetch(API + "/healthcheck")
    try:
        h = json.loads(b)
        ok = s == 200 and h.get("ok") and h.get("postgres") and h.get("redis")
    except ValueError:
        ok = False
    out["api"] = (bool(ok), "HTTP %s %s" % (s, b[:80]))
    # The sign-up route answers its browser preflight (a real sign-up would make a record).
    s, _, hd = fetch(API + "/signup", method="OPTIONS", headers={"Origin": "https://portal.medplum.site", "Access-Control-Request-Method": "POST"})
    out["signup"] = (s == 204 and hd.get("Access-Control-Allow-Origin") == "https://portal.medplum.site", "HTTP %s" % s)
    # Stripe's webhook receiver answers - and refuses an unsigned event, as it must.
    s, _, _ = fetch(API + "/stripe/webhook", method="POST", body=b"{}", headers={"Content-Type": "application/json"})
    out["stripe-webhook"] = (s in (400, 401, 403), "HTTP %s (an unsigned event must be refused, not crash or time out)" % s)
    # The box writes this every 5 minutes after signing in to its mail server: email can be sent.
    s, b, _ = fetch(API + "/public/heartbeat.json")
    try:
        hb = json.loads(b)
        age = (datetime.datetime.now(datetime.timezone.utc) - datetime.datetime.fromisoformat(hb["smtp_ok_at"])).total_seconds() / 60
        out["email"] = (s == 200 and age < 20, "mail server sign-in %.0f min ago" % age)
    except (ValueError, KeyError, TypeError):
        out["email"] = (False, "HTTP %s, no heartbeat" % s)
    s, b, _ = fetch(API + "/public/canary.json")
    try:
        out["canary"] = (s == 200 and json.loads(b).get("ok") is True, "HTTP %s %s" % (s, b[:60]))
    except ValueError:
        out["canary"] = (False, "HTTP %s" % s)
    return out


def post(results):
    import oci
    cfg = {"user": os.environ["OCI_USER"], "tenancy": os.environ["OCI_TENANCY"], "fingerprint": os.environ["OCI_FINGERPRINT"],
           "region": "us-chicago-1", "key_content": os.environ["OCI_KEY"]}
    client = oci.monitoring.MonitoringClient(cfg, service_endpoint="https://telemetry-ingestion.us-chicago-1.oraclecloud.com")
    now = datetime.datetime.now(datetime.timezone.utc)
    data = [oci.monitoring.models.MetricDataDetails(
        namespace="medplum_uptime", compartment_id=os.environ["OCI_TENANCY"], name="up",
        dimensions={"check": k}, datapoints=[oci.monitoring.models.Datapoint(timestamp=now, value=1.0 if ok else 0.0)])
        for k, (ok, _) in results.items()]
    r = client.post_metric_data(oci.monitoring.models.PostMetricDataDetails(metric_data=data))
    if r.data.failed_metrics_count:
        sys.exit("Oracle refused %d metrics: %s" % (r.data.failed_metrics_count, r.data.failed_metrics))


if __name__ == "__main__":
    res = checks()
    for k, (ok, why) in res.items():
        print("%-15s %s  %s" % (k, "UP  " if ok else "DOWN", why))
    if "--dry" not in sys.argv:
        post(res)
