The engine runs the consolidate mechanics (re-tier, death, edge hygiene, budget split → index
chaining) over the now-merged graph automatically, inside `promote_apply` — driven by the
plan's `consolidate: {run, approved_deaths}` field.