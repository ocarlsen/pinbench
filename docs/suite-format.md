# Suite format

The fields of a frozen suite file and the four example suites. Part of [pinbench](../README.md).

A suite is a frozen set of inputs in `suite.yaml` (or `.json` with the same keys):

- `task`: the system prompt for both arms
- `max_output_tokens`: the output ceiling for both arms
- `params` (optional): extra sampling params sent to both arms verbatim. Empty by default; set
  `params: {temperature: 0}` only if you want it and your models accept it.
- `scoring`: `labels: exact | contains | regex | none | judge`, a `normalize` list, and
  `votes: required | optional | none`
- `policy`: the decision rule's knobs, including `max_truncated` (truncated outputs tolerated
  per arm before the verdict refuses to call the run complete; default 0) and, for judge
  suites, `recall_tolerance_points` and `extra_tolerance` (see [Judge scoring](judge-scoring.md))
- `items`: each with an `id`, an `input`, and an optional `expect`

Either every item has `expect` or none do. The exception is `scoring.labels: judge`, where
`expect` is not used and every item instead needs a non-empty `rubric_points` list (`id` +
`description`) plus a suite-level `judge` block (`model`, `rubric`, optional
`max_output_tokens`).

Four example suites ship in `examples/suites/`:

| suite | items | scoring |
|---|---|---|
| `capitals.yaml` | 12 labeled, world capitals | exact match |
| `summaries.yaml` | 10 unlabeled, short synthetic paragraphs | vote-only |
| `summaries-facts.yaml` | the same 10 paragraphs, three key facts each | fixed judge model (recall of key facts, unsupported claims as extras) |
| `review.yaml` | 6 synthetic code/config snippets, one planted defect and a decoy each | fixed judge model |
