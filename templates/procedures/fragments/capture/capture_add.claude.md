```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mnx_stage.py" add --json <<'JSON'
{ "type": "pattern",
  "summary": "Reconcile settlement before posting",
  "aliases": ["settle-recon"],
  "domain": ["settlement"],
  "trigger": "reviewing or curating a settlement spec",
  "score": "now", "urgent": true, "volatility": "default",
  "provenance": { "artifact": "tap-vic-settlement-spec", "reviews": ["r3","r7"],
                  "rejected": ["post-then-reconcile (causes orphaned legs)"],
                  "rationale": "human correction in review r7" },
  "body": "Always reconcile the settlement batch against [[iso8583-field124]] before posting legs, because …" }
JSON
```