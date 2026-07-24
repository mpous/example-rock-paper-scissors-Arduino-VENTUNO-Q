# PROMPTS.md

A running log of the prompts, reasoning, and outcomes behind changes to this
project (Rock Paper Scissors — Arduino UNO Q). Newest entries appended at the
bottom.

---

## Session 1 — Add LLM trash-talk after each round

**Date:** 2026-07-22

### User prompt

> Learn about this project — it's the classical rock paper scissors. I would
> like to add an extra step using the LLM brick
> (https://github.com/arduino/app-bricks-py/tree/main/src/arduino/app_bricks/llm).
> After a single game, if the human wins, the Arduino should use the LLM to
> share excuses for why it lost. If it wins, I want to read the Arduino teasing
> the human in a funny way.

An example of the LLM brick usage was provided (chat-with-tools + streaming,
`LargeLanguageModel(max_tokens=512, tools=[...])`, `llm.chat_stream(prompt)`).

The user asked for a plan and clarifying questions before any coding.

### Thinking / investigation notes

- **Explored the project structure:**
  - `app.yaml` — declares the `arduino:video_object_detection` brick, port 5001.
  - `python/main.py` — Flask app. A thread-safe `GameState` runs the countdown,
    snapshots the detected gesture, computes a `winner`
    (`human` / `arduino` / `draw` / `no_detection`), records history, then
    returns to `idle`. The web UI polls `/api/state` every 500 ms.
  - `python/templates/index.html` — polls state; shows detection panel, Arduino
    move, a result banner, and a history table.
  - `python/requirements.txt` — only `flask` (bricks are provided by App Lab, not
    pip-installed — consistent with `video_object_detection` not being listed).

- **Confirmed the LLM brick API** by reading the brick source
  (`local_llm.py`) rather than trusting the example alone:
  ```python
  LargeLanguageModel(
      system_prompt="",      # persona / constraints
      temperature=0.7,
      max_tokens=512,
      timeout=None,
      tools=None,
      model=None,            # falls back to app/brick config
  )
  llm.chat(message) -> str            # blocking, strips <think>…</think>
  llm.chat_stream(message) -> Iterator # streaming
  ```
  Key finding: the persona is passed via `system_prompt`, and `chat()` blocks
  and returns the whole string — good enough for a polling UI.

- **Consulted the `arduino-unoq-applab` skill** for App Lab conventions:
  bricks are declared in `app.yaml`; use graceful `ImportError` fallback so the
  code still runs off-device.

### Clarifying questions asked (and answers)

| Question | Answer chosen |
|----------|---------------|
| How to deliver the excuse/taunt? | **Text on screen only** (no TTS) |
| Which outcomes trigger a comment? | **All outcomes** (win, loss, draw, no-detection) |
| How should the text appear? | **Appear all at once** (blocking `chat()` + "thinking…" indicator) |

### Plan presented

1. `app.yaml` — add `arduino:llm: {}` to bricks.
2. `main.py` — init `LargeLanguageModel` with a cheeky persona (graceful
   fallback); add `llm_comment` / `llm_state` to `GameState`; after a round
   resolves, generate a per-outcome line in the existing round thread.
3. `index.html` — add an "Arduino says…" speech bubble with a thinking
   indicator, then the generated line.

### Changes made

**`app.yaml`**
- Added `arduino:llm: {}` alongside the existing object-detection brick.

**`python/main.py`**
- Initialized `LargeLanguageModel` with a "cheeky, competitive Arduino" persona
  (`system_prompt`, `max_tokens=80`, `temperature=0.9`), wrapped in try/except
  `ImportError` so it no-ops off-device (mirrors the detector pattern).
- `GameState` gained `llm_comment` and `llm_state` fields — reset on each new
  round and in `reset()`, exposed in `to_dict()` as `llmComment` / `llmState`.
- Added `_generate_comment(winner, human_move, arduino_move)`, called after the
  result is recorded (inside the existing round thread):
  - `human` wins → dramatic **excuse** for losing
  - `arduino` wins → playful **taunt**
  - `draw` → witty tie remark
  - `no_detection` → "show your hand properly" nudge
  - Sets `llm_state='thinking'`, calls blocking `chat()`, stores line with
    `llm_state='ready'`; on failure logs and reverts to `idle`.
- Added an `[MODE] LLM: yes/no` line to the startup banner.

**`python/templates/index.html`**
- Added `.llm-bubble` speech-bubble styling (with a CSS tail and animated
  blinking dots for the thinking state).
- Added an "Arduino says" bubble element under the result banner.
- Added `updateLlm(state, comment)` to the poll loop: dots while `thinking`,
  full line when `ready`, hidden otherwise.
- Hidden the bubble immediately on new round (`playRound`) and on `resetGame`.

### Outcome / status

- Code complete and internally consistent.
- **Not runnable on this machine:** no Python installed locally, and the
  `llm` / `video_object_detection` bricks only exist on the UNO Q. Must be
  deployed to the board to test live (watch `[LLM]` lines in the app logs).
- **On-device caveat:** the local LLM takes a few seconds per line, so the
  result screen holds longer while it "thinks" — expected with the blocking
  approach the user chose. The LLM brick also needs a model downloaded locally
  on the board.

---

## Session 2 — Push to a personal fork on a new branch

**Date:** 2026-07-24

### User prompt

> I want to create a branch called `ventuno` and push this code to this repo
> https://github.com/mpous/example-rock-paper-scissors-Arduino-UNO-Q

### Thinking / notes

- `origin` pointed at `edgeimpulse/…`, not the user's `mpous/…` repo, so a new
  remote was needed rather than pushing to `origin`.
- `.claude/settings.local.json` is local Claude Code config — deliberately left
  uncommitted. No `.gitignore` existed.

### Outcome

- Created branch `ventuno`; committed the Session 1 changes + `PROMPTS.md`.
- Added remote `mpous` → the personal repo (left `origin` untouched).
- Pushed `ventuno` → `mpous/ventuno` with upstream tracking.
- Note: commit authored as `Marc Pous <mpous@qti.qualcomm.com>` (git's
  auto-detected identity).

---

## Session 3 — Turn the LLM into a live play-by-play commentator

**Date:** 2026-07-24

### User prompt

> This worked well. The only thing I want to change now is that the LLM from the
> brick becomes a *commenter*: commenting on every change of the rock-paper-
> scissors detector, then when clicking the button, commenting the result,
> commenting when the Arduino chooses and the chances to win. I want it to be a
> real-time commenter, like a football game.

### Thinking / design notes

- **Core tension:** the local LLM is slow (seconds per line), but the desired UX
  is a continuous, real-time sports-commentary stream driven by frequent events
  (every detector change + several per-round milestones). Naively calling the
  LLM on every event would back up badly.
- **Chosen architecture:** a single background **commentator worker** thread
  that pulls from two sources with different drop policies:
  - **Milestones** (`round_start`, `arduino_choice`, `result`) → a real
    `queue.Queue`, never dropped, always narrated in order.
  - **Detection changes** → a single-slot "latest wins" pending value
    (coalesced), so rapid flicker doesn't create a backlog — only the most
    recent gesture gets narrated when the worker is free.
  - `_next_event()` drains milestones first, then the latest detection, then
    blocks on a `threading.Event` until woken.
- This naturally rate-limits to the model's speed while keeping the feed current
  and guaranteeing key game moments are always covered.

### Changes made

**`python/main.py`**
- Rewrote the LLM persona from a cheeky trash-talker into an "energetic live
  sports commentator" (one punchy play-by-play line, no emojis); `max_tokens`
  lowered to 60.
- Replaced `GameState.llm_comment` / `llm_state` with a `commentary` feed (list,
  newest first, capped at 10) and a `commentating` flag; added `add_commentary`
  and `set_commentating`; exposed `commentary` / `commentating` in `to_dict`;
  cleared them in `reset`.
- `update_detection` now fires `enqueue_detection(...)` on every label change.
- `play_round` emits `round_start` and `arduino_choice` milestones right after
  locking the gesture / picking the Arduino move, and a `result` milestone after
  the winner is computed. Removed the old one-shot `_generate_comment`.
- Added the commentator infrastructure (`_milestones` queue, coalesced
  `_pending_detection`, `_wake` event, `enqueue_*`, `_next_event`, `_prompt_for`
  per event kind, and `commentator_worker`), started as a daemon thread when the
  LLM brick is present.

**`python/templates/index.html`**
- Replaced the single "Arduino says" bubble with a **Live Commentary** feed: a
  panel with an "On Air" pulsing indicator (driven by `commentating`) and a list
  of recent lines (newest highlighted, HTML-escaped, slide-in animation).
- Swapped `updateLlm` for `updateCommentary(lines, commentating)`; cleared the
  feed on reset.

### Outcome / status

- Code complete and internally consistent; no lingering references to the old
  single-comment fields.
- **Still not runnable locally** (no Python; bricks are board-only). Deploy to
  the UNO Q to test; watch `[LLM]` / `[DETECT]` lines in the app logs.
- **Known tradeoff (flagged to user):** because milestones are never dropped,
  playing rounds faster than the LLM can generate will make commentary lag
  behind live action, and the "odds" line for a pick may land after the result
  is already on screen. Acceptable for a ticker-style feed; could be tuned later
  (e.g. drop stale milestones, or lengthen countdown/hold).

---

## Session 4 — Commit the commentator work to the dev branch

**Date:** 2026-07-24

### User prompt

> Append the new prompts and outcomes in the PROMPTS.md file and add, commit and
> push into the dev branch.

### Notes

- "Dev branch" interpreted as `ventuno` — the only non-`main` branch and the one
  all this work has been developed on.
- Staged the Session 3 code changes (`python/main.py`,
  `python/templates/index.html`) plus this `PROMPTS.md` update; `.claude/`
  remains uncommitted (local Claude config).

### Outcome

- Committed the live-commentator changes and pushed to `mpous/ventuno`.
- The first push was rejected: the remote `ventuno` had a new `Update README.md`
  commit. Fetched, **rebased** the local commit on top of it (clean, different
  files — no shared-history rewrite), then pushed (`db0c6f1..87ec9d3`).
- Noticed the remote reports the repo was **renamed** to
  `example-rock-paper-scissors-Arduino-VENTUNO-Q` (the `...-UNO-Q` URL still
  redirects, so the push worked). Offered to update the remote URL.

---

## Session 5 — Move commentary to a sidebar and kill the flicker

**Date:** 2026-07-24

### User prompt

> It works great, however let's do the live commentary on the side so it doesn't
> move history and the play buttons below. There's a flickering effect with the
> comments — they show for half a second and come back all the time, which is
> very unpleasant. The text needs to stay there and not flicker. Improve this.

### Thinking / diagnosis

- **Flicker root cause:** `updateCommentary` rebuilt `commentaryFeed.innerHTML`
  on *every* 500 ms poll, and the slide-in animation was on the base
  `.commentary-feed li` rule. So every poll recreated the DOM nodes and
  restarted the fade-in (opacity 0→1) on all lines — read as constant flicker.
- **Layout cause:** the feed was in the vertical document flow (between the
  result banner and the controls), so as lines were added/removed its height
  changed and pushed the controls and history up/down.

### Changes made (`python/templates/index.html` only)

- **Diff-based rendering:** cache the last rendered lines in
  `renderedCommentary`; `updateCommentary` returns early when the feed is
  unchanged, so stable text is never re-written. On a real change, only lines
  not present before get a `new` class.
- **Animation scoped to new items:** moved `animation: slidein` off the base
  `li` onto `.commentary-feed li.new`, so only a freshly-arrived comment
  animates once; existing lines stay still.
- **Two-column layout:** wrapped the game in `.layout` (flex row) → `.main`
  column (scoreboard, panels, banner, controls, history, reset) on the left and
  a `.commentary` **sidebar** (fixed 320px, `position: sticky`, `max-height`
  with internal `overflow-y: auto`) on the right. The sidebar's growth no longer
  affects the main column, so history and buttons never move.
- **Responsive:** at ≤900px the layout stacks (commentary full-width below the
  game, capped height); the existing ≤640px rules are unchanged.
- Reset also clears `renderedCommentary` to keep the diff state in sync.

### Outcome / status

- Flicker fix is deterministic in the render logic; commentary now lives in a
  right-hand sidebar that scrolls internally.
- **Not runnable locally** (no Python; bricks are board-only) — needs on-device
  verification for feel.
- Open question offered to the user: sidebar is on the right; could move to the
  left if preferred.

---

## Session 6 — Log Session 5, commit and push

**Date:** 2026-07-24

### User prompt

> Log everything on the PROMPTS.md, commit and push to the ventuno branch.

### Outcome

- Appended Sessions 5 and 6 to `PROMPTS.md`; committed the sidebar/flicker fix
  plus this log and pushed to `mpous/ventuno`.

---

## Session 7 — Fix video object detection stall (stdout backpressure)

**Date:** 2026-07-24

### User prompt

> The inference of the video object detection brick doesn't work really well. I
> need that video object detection inference to happen all the time and it's not
> running well. I was not showing scissors in camera anytime and the inference
> was not running after 2 plays. [on-device logs attached]

### Thinking / diagnosis

- **Symptom from logs:** detection worked for the first round or two, then froze
  — rounds 2–3 kept reusing a stale `scissors (99%)` reading even though the
  user's hand was not showing scissors. The camera/brick was clearly still
  running, but our callback stopped receiving fresh results.
- **Root cause — stdout pipe backpressure:**
  - `handle_detections` was printing `[BRICK-RAW] {...}` on *every* frame. With
    `debounce_sec=0.0` the brick fires dozens of callbacks per second, so this
    was a print firehose.
  - Independently, once the local gemma LLM warmed up (~round 2) the commentator
    worker began emitting long `[LLM] …` lines back-to-back.
  - Python serializes all `print()` calls through a single `sys.stdout` lock.
    App Lab consumes stdout over a pipe; when the pipe buffer fills, a write
    blocks *while holding that lock*. The blocked LLM write then stalled the
    detection callback thread inside its own `print()`, so it stopped reading
    the brick's WebSocket — and inference results stopped arriving. The last
    value (`scissors`) stayed frozen in `GameState`.
- This also explains why it always looked like "scissors": that was simply the
  last value latched before the stall, not a live reading.

### Changes made (`python/main.py` only)

- Added `DEBUG_DETECTIONS = os.environ.get('DEBUG_DETECTIONS') == '1'` to the
  config block (default **off**).
- Gated the per-frame `[BRICK-RAW]` print behind `DEBUG_DETECTIONS`, removing the
  firehose from the normal path.
- Wrapped the detection-parsing body in `try/except Exception` so a malformed
  frame logs once instead of killing the callback thread.

### Flagged (not code-fixable here)

- **Model accuracy:** the `.eim` model itself appears to over-report `scissors`;
  that's a training/data issue, not something the app code can fix.
- **Intentional lock:** detection is deliberately frozen for the duration of a
  round (~6 s countdown + hold) to snapshot the human's move — so "no updates
  during a round" is by design, distinct from the stall bug above.

### Outcome / status

- Backpressure fix is deterministic (the firehose is gone by default).
- **Still not runnable locally** (no Python; bricks are board-only) — needs
  on-device verification that inference now stays live across many rounds.

---

## Session 8 — Log Session 7, commit and push

**Date:** 2026-07-24

### User prompt

> Log everything in PROMPTS.md and commit and push in the ventuno branch.

### Outcome

- Appended Sessions 7 and 8; committed the detection-stall fix
  (`python/main.py`) plus this log and pushed to `mpous/ventuno`.

---

## Session 9 — Add inference logging + timing to the detection callback

**Date:** 2026-07-24

### User prompt

> In Python I want to see the logs of the object detection inference when it
> happens in the application. I don't think the object detection inference is
> running at all in the application. Also give me the inference time in the
> application.

### Thinking / investigation

- Checked the `video_object_detection` brick source on GitHub to confirm what is
  actually available to the app, rather than guessing. Two decisive findings:
  1. **`on_detect_all` only fires when there is at least one detection**
     (`if len(detections) > 0`). Empty frames trigger *no* callback. So if the
     model finds nothing above its confidence threshold, the app sees zero
     logs — which is exactly why it *looked* like "inference isn't running at
     all." Silence ≠ dead loop; it can just mean nothing scored ≥ confidence.
  2. **The brick exposes no inference-time / timestamp / latency / fps field.**
     `time.time()` is used internally only for debounce bookkeeping. The only
     timing observable from the app is the *interval between callbacks*.
- Also relevant: the current brick is initialized with `confidence=0.6`, so weak
  detections are filtered out *before* our callback — reinforcing the silence.
- **Constraint carried over from Session 7:** any new logging must not re-create
  the per-frame `print()` firehose that filled the App Lab stdout pipe and (via
  Python's shared `sys.stdout` lock) stalled the detection callback thread.

### Changes made (`python/main.py` only)

- Added module-level timing state (`_infer_last_ts`, `_infer_last_log`,
  `_infer_count`, `_infer_intervals`, `_infer_lock`).
- In `handle_detections`, on every callback: increment the count, record the
  interval since the previous callback (rolling window of the last 100), and
  emit a **throttled `[INFER]` heartbeat at most once per second**:
  ```
  [INFER] result #142  interval=118ms  avg=121ms  ~8.3/s
  ```
  - `result #N` — running callback count, proves inference is delivering results.
  - `interval` — ms since the previous callback (latest cadence).
  - `avg` — rolling average over the last 100 callbacks.
  - `~X/s` — effective detection rate.
- The 1/sec throttle keeps the heartbeat always-on (so inference activity is
  visible) without reintroducing the stdout backpressure stall. Per-frame raw
  output stays gated behind `DEBUG_DETECTIONS=1` (`[BRICK-RAW]`).

### Flagged to user (not code-fixable here)

- **The interval is a cadence proxy, not true model latency.** It reflects
  camera + inference + delivery time between results, because the brick does not
  surface a raw forward-pass duration.
- **No `[INFER]` line can still mean inference is running** but nothing scored ≥
  the brick's `confidence=0.6`. To surface weaker frames, lower the brick
  `confidence` (e.g. `0.3`) — offered to the user, not yet applied.

### Outcome / status

- Logging + timing added; deterministic and throttle-safe.
- **Not runnable locally** (no Python; bricks are board-only) — deploy to the
  UNO Q and watch for `[INFER]` lines in the app logs to confirm live inference.

---

## Session 10 — Log Session 9, commit and push

**Date:** 2026-07-24

### User prompt

> Commit push to the ventuno branch. Append all the information into the
> PROMPTS.md file.

### Outcome

- Appended Sessions 9 and 10; committed the inference-logging change
  (`python/main.py`) plus this log and pushed to `mpous/ventuno`.

---

## Session 11 — Restore detection speed (throttle LLM) + real-time confidence

**Date:** 2026-07-24

### User prompt

> The object detection inference is not working as expected. It's really slow.
> Check the initial Python version to learn how the video object detection was
> working and get back to that initial speed and inferencing results. [pasted the
> original LLM-free main.py + on-device logs] … I want to see the confidence
> field there to change it in real-time. I changed in the code to 0.4 but I
> didn't see any confidence change in the logs.

### Thinking / diagnosis

- **Root cause of the slowdown — the LLM starves detection of CPU.** The logs
  are decisive: detection ran at `~5.4/s` (interval ~185 ms) until
  `[Thread-1 (commentator_worker)] LLM: Initializing model
  llamacpp:gemma-3-1b-it-Q4_0...` fired, after which intervals jumped to
  `2202ms`, then `27404ms`, collapsing to `~0.3/s`. The original version the
  user pasted has no LLM at all — which is why it was fast. The gemma model runs
  locally on the UNO Q CPU and competes with the Edge Impulse detector for the
  same cores. The old commentator made this worse by firing an LLM generation on
  **every detection change** and on **each countdown milestone** — i.e. exactly
  during the window when detection needs the CPU.
- **Why lowering `CONFIDENCE_THRESHOLD` to 0.4 did nothing visible:** there are
  two thresholds. The brick was constructed as `VideoObjectDetection(confidence=0.6)`
  and *overrides the model threshold to 0.6* (`Overriding detection threshold.
  New confidence: 0.6` in the logs). It filters below 0.6 **before** ever calling
  our handler, so our separate 0.4 filter could never see anything lower.
- **Why confidence never appeared to change:** `[DETECT]` only prints when the
  *label* changes; while it stayed `scissors`, confidence updates were silent.

### Decision (asked the user)

Offered three options; user chose **"Keep LLM, throttle it."**

### Changes made (`python/main.py` only)

- **One confidence knob:** `CONFIDENCE_THRESHOLD = float(os.environ.get('CONFIDENCE', '0.4'))`,
  moved above the brick init and passed into `VideoObjectDetection(confidence=…)`
  so the brick and our filter always agree. Now overridable at runtime via the
  `CONFIDENCE` env var; default lowered to 0.4 as the user wanted.
- **Throttled the LLM to result-only:** removed the per-detection commentary
  (`enqueue_detection` in `update_detection`) and the `round_start` /
  `arduino_choice` countdown milestones. Only the `result` milestone is narrated
  now — one LLM call per round, during the result hold, when detection accuracy
  no longer matters. Deleted the now-dead coalescing plumbing
  (`_pending_detection`, `_pending_lock`, `_wake`, `_next_event`) and the
  detection/round_start/arduino_choice branches of `_prompt_for`; the worker now
  blocks on `_milestones.get()`.
- **Real-time confidence in the logs:** the throttled `[INFER]` heartbeat now
  includes the current best label + confidence, e.g.
  `[INFER] result #142  scissors 87%  interval=118ms  avg=121ms  ~8.3/s`, so
  confidence is visible every second even when the label doesn't change.

### Outcome / status

- Detection should return to the original ~5/s cadence between rounds; the only
  LLM contention is a single result line per round (brief, by design).
- Confidence is now both lower-threshold (0.4) and logged live.
- **Not runnable locally** (no Python; bricks are board-only) — deploy to the
  UNO Q and confirm `[INFER]` cadence stays high and `[LLM]` fires only at
  round end.

---

## Session 12 — Log Session 11, commit and push

**Date:** 2026-07-24

### User prompt

> Commit push to the ventuno branch. Append all the information into the
> PROMPTS.md file.

### Outcome

- Appended Sessions 11 and 12; committed the speed/throttle + confidence changes
  (`python/main.py`) plus this log and pushed to `mpous/ventuno`.

---

## Session 13 — Kill the recurring stdout hang with non-blocking logging

**Date:** 2026-07-24

### User prompt

> Let me share more logs. After the result 136 the application stopped. Having
> said that I never showed the inferenced result. [full on-device log] … Checked
> CPU, memory, etc: STORAGE 21.81/54.89GB, RAM 2.38/14.93GB, NPU 1%, CPU 11%. I
> stopped the app and clicked Run again and the application didn't improve.

### Thinking / diagnosis

- **Good news first:** the Session 11 confidence work is confirmed working — the
  logs now show live confidence (`scissors 46%`, `54%`, `66%`, `rock 94%`,
  `paper 85%`, …) and the brick threshold is 0.4 (`Overriding detection
  threshold. New confidence: 0.4`).
- **The hang is NOT resource starvation.** CPU 11%, RAM 2.4/15 GB, NPU 1% — the
  board is essentially idle while the app is frozen, and a restart doesn't help.
  An idle-but-stuck process means a **blocked thread**, not exhaustion.
- **The frozen-timing tell:** from `[INFER] result #101` through `#136` the
  timing fields are byte-identical (`interval=1617ms  avg=1334ms  ~0.7/s`) even
  though the count and label/confidence keep changing. The timing state stopped
  advancing — i.e. the process was winding down / threads stalling while the
  last buffered lines dribbled out.
- **Root cause — same disease as Sessions 7 and 11, now proven recurrent:** a
  worker thread blocks while writing to stdout when the App Lab log pipe fills.
  A blocked write holds Python's shared `sys.stdout` lock, which then freezes
  every other thread that logs — including the detector callback thread, which
  stops servicing its WebSocket, so detection dies. Low CPU because everyone is
  blocked on I/O, not spinning. Restart doesn't help if the log consumer stays
  wedged. Gating individual prints (Session 7) and reducing volume (Session 11)
  only *delayed* it; the fix has to remove blocking from the worker threads
  entirely.
- Aside: the first result still caused a one-time ~30 s detection stall
  (`interval=30020ms`) right when the LLM logged `Initializing model
  llamacpp...` — that's the model warm-up pegging the CPU on first use, a
  one-off, distinct from the stdout hang.

### Changes made (`python/main.py` only)

- Added a **non-blocking logging layer**: a bounded `queue.Queue(maxsize=2000)`
  plus a single daemon `_log_writer` thread that owns all `sys.stdout` writes.
  A new `log(msg)` helper does `put_nowait` and **drops the line on `queue.Full`**
  instead of blocking. So no producer thread (detector callback, game logic, LLM
  worker, Flask) can ever be stalled by a wedged stdout pipe — detection keeps
  running even if logs stop. Losing a log line is acceptable; stalling detection
  is not.
- Set `sys.stdout.reconfigure(line_buffering=True)` (best-effort) so lines flush
  promptly and appear live in the App Lab log view.
- Converted **every** `print(...)` in the app to `log(...)`.

### Outcome / status

- Removes the blocking path that has stalled detection three times. Detection
  liveness is now decoupled from log I/O.
- **Not runnable locally** (no Python; bricks are board-only) — verified by
  inspection (`log()` defined before first use; all call sites converted).
  Deploy to the UNO Q and confirm `[INFER]` keeps ticking indefinitely across
  many rounds without freezing.

---

## Session 14 — Log Session 13, commit and push

**Date:** 2026-07-24

### User prompt

> Log everything and commit + push to the ventuno branch.

### Outcome

- Appended Sessions 13 and 14; committed the non-blocking logging change
  (`python/main.py`) plus this log and pushed to `mpous/ventuno`.

---

## Session 15 — Log every inference + all-outcome confidences + LLM tokens/time

**Date:** 2026-07-24

### User prompt

> show me all the inferences, even if it doesn't reach the 0,4 confidence. I
> would like to see the confidences of the other possible outcomes of the
> model. Show me the tokens and time spent for the LLM brick as well in the
> logs.

Followed by: log everything to PROMPTS.md, commit and push to the ventuno
branch.

### Thinking / diagnosis

- Two separate thresholds were needed. The brick filters detections below its
  own `confidence` BEFORE calling us, so to see weak/sub-play-threshold
  outcomes we had to lower the brick's floor. Split into:
  - `DETECT_FLOOR` (0.1) — the brick's threshold, kept low so it reports weak
    detections we can log.
  - `CONFIDENCE_THRESHOLD` (0.4) — the gameplay bar; only detections at/above
    this are eligible to be locked as the human's move.
- Caveat noted: it's an object-detection model, not a classifier, so it only
  reports classes it actually detects above the floor — not a full softmax
  over all three classes every frame.
- The LLM brick's `chat()` returns only the final string with no usage info.
  To count tokens and time generation we switched the commentator to
  `chat_stream()` and count the yielded chunks (≈tokens) while timing with
  `perf_counter`. The startup warmup call is timed too.

### Changes made (`python/main.py`)

- Split `DETECT_FLOOR` / `CONFIDENCE_THRESHOLD`; brick initialized with
  `confidence=DETECT_FLOOR` and the init log reports both values.
- Rewrote `handle_detections` to collect ALL rock/paper/scissors confidences
  (`all_confs`), pick the best that qualifies (`>= CONFIDENCE_THRESHOLD`) for
  gameplay, and log every inference:
  `[INFER] #N  label=X%  label=Y%  lock=... (timing)` — where `lock=none
  (all <40%)` when nothing reached the play bar, and `timing` distinguishes
  back-to-back frames from idle gaps (`GAP_MS`) and the first frame.
- Rewrote `commentator_worker` to stream and log
  `[LLM] N tokens in X.Xs (Y.Y tok/s)` after each round's commentary, plus a
  timed warmup line `[LLM] model ready (X.Xs)`.
- Simplified `update_detection` (removed the old per-change `[DETECT]` line).

### Outcome

- Verified the edits are internally consistent (no stray references; all
  symbols defined before use). Committed `python/main.py` plus this log and
  pushed to `mpous/ventuno`.
