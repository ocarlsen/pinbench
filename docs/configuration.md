# Configuration

The optional `pinbench.yaml`, its keys, and the precedence rule every setting follows. Part of [pinbench](../README.md).

An optional `pinbench.yaml` at your project root sets project-wide defaults, so you don't
repeat `--prices`/`--state`/`--cap` on every call or paste the same `policy:` block into every
suite. pinbench walks up from the current directory to find it, or takes `--config PATH`.
Without one, every setting falls back to the built-in default.

```yaml
# pinbench.yaml
cap_usd: 0.50
judge:
  model: claude-opus-5-5
  max_output_tokens: 4096
  # rubric has no sensible generic default; a judge suite must still state its own, or
  # loading refuses, exactly as if no pinbench.yaml existed.
paths:
  pins: examples/pins.yaml
  prices: examples/prices.yaml
  runs: .pinbench
policy:
  min_items: 10
  label_tolerance_points: 0.0
  cost_tolerance: 0.25
  require_votes_when_unlabeled: true
  max_truncated: 0
  recall_tolerance_points: 0.0
  extra_tolerance: 0.5
```

`incumbent`/`candidate` are deliberately not config keys: which two models are compared is a
per-run choice, never a file default. An unknown key, a wrong type, or an out-of-range value (a
cap at or below zero, a negative tolerance, `min_items` under 1) refuses with the key and value
named, never a silent fallback.

Precedence, applied the same way everywhere:

| precedence | example |
|---|---|
| 1. CLI flag | `--cap 0.75` |
| 2. the suite's own value | a suite's top-level `cap_usd:`, or its `judge:`/`policy:` block |
| 3. `pinbench.yaml` | this file's `cap_usd`, `judge`, `paths`, `policy` |
| 4. built-in default | `cap_usd: null` (a cap must come from somewhere), `judge.model: claude-opus-5-5`, `.pinbench` for `paths.runs`, etc. |

Paths:

- A `paths` value from `pinbench.yaml` resolves relative to that file's own directory, so the
  file behaves the same from any working directory.
- A `--prices`/`--state`/`--pins` flag or a built-in default is left as given, relative to
  your current directory.
- Wherever a config-resolved path or the config file's location is printed or persisted
  (`pinbench config`, `run.json`, the report header), it is written relative to your current
  directory, so a `report.md` that `bump` commits never carries an absolute path.

`pinbench config [--suite SUITE]` prints every effective setting and its tier; `--suite` also
shows how that suite's own `cap_usd`/`judge`/`policy` fold in:

```
$ pinbench config --suite examples/suites/review.yaml
config file: pinbench.yaml
config hash: sha256:...
cap_usd: 0.5  (source: config)
judge:
  max_output_tokens: 4096  (source: suite)
  model: 'claude-opus-5-5'  (source: suite)
  rubric: '...'  (source: suite)
paths:
  pins: examples/pins.yaml  (source: config)
  ...
policy:
  ...
```

Every `run` records its full effective settings in `run.json` (cap, judge/policy fallbacks,
paths, which config file if any and its content hash, each with its source). `pinbench report`
reloads the suite from that record, so a report built later reproduces the run even if
`pinbench.yaml` has since changed or been deleted. The report header names the config file and
hash whenever the run used one.
