# Design decisions

Why pinbench works the way it does, one decision per line. Part of [pinbench](../README.md).

- **Pins, not aliases.** A floating alias changes what you run on a date the vendor picked. A
  pin changes only when `bump` changes it, and only on a RECOMMEND.
- **Your own frozen inputs, not a leaderboard.** A public benchmark scores someone else's task.
  The suite is your inputs, frozen by a hash, run unchanged every time.
- **Blind, with the key outside the page.** Knowing which output is the familiar one makes the
  vote a habit. The page never shows a model id; the mapping lives in a separate file the page
  never reads.
- **The cost cap is checked before and during.** An estimate can be wrong (a real output can
  run past its stated ceiling), so the meter checks again before every call. A cap is a hard
  stop.
- **The decision rule is printed, not implied.** Every number in the rule is read from the
  suite's policy, so the next person can check the math.
- **A bump is a commit on a branch.** A clear before and after, revertable with one command.
- **A watch that calls no model.** Checking for something newer should never cost money or
  risk a call to an untested id. `watch` only lists models and diffs id strings.
- **The judge is one fixed model, named out loud.** `run.json` and the report header record
  its id and a hash of its rubric, so runs scored by different judges or rubrics are visibly
  different measurements. `judge.assert_comparable` is the library check for that; no command
  calls it yet.
