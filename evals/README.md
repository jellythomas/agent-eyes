# agent-eyes Skill Evaluation History

Tracks skill evolution through structured evals. Each iteration tests the Safety Protocol
for destructive actions (close_tab, navigate, quit app).

## Structure

```
evals/
├── evals.json              # Test case definitions + assertions
├── README.md               # This file
└── iteration-1/            # v0.3.4 — Safety Protocol added
    ├── eval-1-close-youtube/
    │   ├── eval_metadata.json
    │   ├── with_skill/     # outputs/, grading.json, timing.json
    │   └── without_skill/  # outputs/, grading.json, timing.json
    ├── eval-2-navigate-github/
    │   └── ...
    └── eval-3-close-all-except/
        └── ...
```

## Iteration History

### Iteration 1 (v0.3.4) — Safety Protocol for Destructive Actions
- **Trigger:** Bug where close_tab executed without checking current app/tabs
- **Fix:** Added Safety Protocol section, title-matching in close_tab, force-refresh for destructive ops
- **Result:** With skill 8/8 (100%), Without skill 7/8 (87.5%)
- **Key improvement:** Title matching over index-based targeting, mandatory orient gate, re-list between chained closes
