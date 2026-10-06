# Upkeep uptime

Checks the clinic's public endpoints every 5 minutes from outside its own server and reports to
Oracle Cloud Monitoring, whose alarms email the clinic. See `check.py`. Maintained from the clinic's
repository (`ops/uptime/`); changes made here are overwritten.
