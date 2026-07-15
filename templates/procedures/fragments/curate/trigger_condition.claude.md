If invoked with `--drop <provisional-id>` or `--discard-all`, this is the local **un-stage** path — the
cheap way to prune staging (and the escape valve when the hard cap is blocking new captures). It still
runs the locate preflight (to find the staging tier) but does **no** extraction, scoring, or staging: