#!/usr/bin/env python3
"""
Rock-Paper-Scissors Game — Arduino UNO Q

Uses the video_object_detection brick for Edge Impulse inference.
The brick manages the camera and runs detection in its Docker container.
Flask on port 5001 serves the custom web UI.
"""

import os
import sys
import time
import queue
import random
import logging
import threading
from flask import Flask, render_template, jsonify

# ─── Silence Flask HTTP logs ─────────────────────────────────────────────
logging.getLogger('werkzeug').setLevel(logging.ERROR)

# ─── Non-blocking logging ────────────────────────────────────────────────
# Every detection stall we have hit traces back to a worker thread blocking
# while writing to stdout when the App Lab log pipe fills: a blocked write holds
# shared sys.stdout lock, which freezes every other thread — including the
# detector callback, which then stops servicing its WebSocket and detection
# dies (low CPU, app appears hung, restart doesn't help). So route ALL app
# logging through a bounded queue drained by one writer thread. Producer
# threads never block: if the queue is full (stdout wedged) we DROP the line
# rather than stall real work. Losing a log line is fine; stalling detection is
# not.
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

_log_queue = queue.Queue(maxsize=2000)


def log(msg):
    try:
        _log_queue.put_nowait(msg)
    except queue.Full:
        pass


def _log_writer():
    while True:
        msg = _log_queue.get()
        try:
            sys.stdout.write(msg + "\n")
            sys.stdout.flush()
        except Exception:
            pass


threading.Thread(target=_log_writer, daemon=True).start()

# Two thresholds, deliberately separate:
#  - DETECT_FLOOR is the brick's own threshold. Kept LOW so the brick reports
#    weak detections too (it filters anything below this BEFORE calling us), so
#    we can log the confidence of every outcome the model emits, including ones
#    below the play threshold. Note: this is an object-detection model, so it
#    only reports objects it actually detects above the floor — not a full
#    probability over all three classes every frame.
#  - CONFIDENCE_THRESHOLD is the gameplay bar: only detections at/above it are
#    eligible to be locked in as the human's move.
DETECT_FLOOR = float(os.environ.get('DETECT_FLOOR', '0.1'))
CONFIDENCE_THRESHOLD = float(os.environ.get('CONFIDENCE', '0.4'))

# ─── Video Object Detection Brick ────────────────────────────────────────
_detector = None
try:
    from arduino.app_bricks.video_objectdetection import VideoObjectDetection
    _detector = VideoObjectDetection(confidence=DETECT_FLOOR, debounce_sec=0.0)
    log(f"[BRICK] VideoObjectDetection initialized (floor={DETECT_FLOOR}, "
        f"play threshold={CONFIDENCE_THRESHOLD})")
except ImportError:
    log("[WARN] VideoObjectDetection brick not available — detection disabled")

# ─── LLM Brick (live play-by-play commentator) ───────────────────────────
_llm = None
LLM_PERSONA = (
    "You are a live sports commentator for a Rock Paper Scissors duel between a "
    "Human and an Arduino robot. Reply with ONE short sentence of at most 12 "
    "words. No emojis, no quotation marks, no explanations."
)
try:
    from arduino.app_bricks.llm import LargeLanguageModel
    _llm = LargeLanguageModel(system_prompt=LLM_PERSONA, max_tokens=24, temperature=0.9)
    log("[BRICK] LargeLanguageModel initialized")
except ImportError:
    log("[WARN] LLM brick not available — commentary disabled")

# ─── App Runner ──────────────────────────────────────────────────────────
_App = None
try:
    from arduino.app_utils import App as _App
except ImportError:
    try:
        from arduino.app import App as _App
    except ImportError:
        try:
            from arduino import App as _App
        except ImportError:
            pass

# ─── Configuration ───────────────────────────────────────────────────────
VALID_LABELS = {'rock', 'paper', 'scissors'}
PORT = int(os.environ.get('FLASK_PORT', '5001'))
COUNTDOWN_SECS = 3
RESULT_HOLD_SECS = 3
# Per-frame detection logging floods stdout; the App Lab log pipe then applies
# backpressure that can stall the detection callback thread. Off by default.
DEBUG_DETECTIONS = os.environ.get('DEBUG_DETECTIONS') == '1'
# The brick only calls us when it detects something, so a long pause between
# callbacks usually means "nothing was in view" — not slow inference. Treat any
# interval above this as an idle gap and keep it out of the cadence average, so
# the reported rate reflects real inference speed rather than empty-frame gaps.
GAP_MS = float(os.environ.get('GAP_MS', '1500'))

WINS = {'Rock': 'Scissors', 'Scissors': 'Paper', 'Paper': 'Rock'}


# ─── Game State ──────────────────────────────────────────────────────────
class GameState:
    """Thread-safe game state with scoring and round history."""

    def __init__(self):
        self._lock = threading.Lock()
        self.human_wins = 0
        self.arduino_wins = 0
        self.draws = 0
        self.round_number = 0
        self.state = 'idle'
        self.countdown = None
        self.arduino_move = None
        self.human_move = None
        self.winner = None
        self.detection = None
        self.confidence = 0.0
        self._detection_locked = False
        self.commentary = []
        self.commentating = False
        self.history = []

    def update_detection(self, label, confidence):
        with self._lock:
            if self._detection_locked:
                return
            self.detection = label
            self.confidence = confidence

    def play_round(self):
        arduino_move = random.choice(['Rock', 'Paper', 'Scissors'])

        # Lock detection: snapshot the current gesture immediately
        with self._lock:
            self._detection_locked = True
            detected = self.detection
            conf = self.confidence
            self.state = 'countdown'
            self.arduino_move = arduino_move
            self.human_move = None
            self.winner = None

        log(f"[GAME] Locked detection: {detected} ({conf:.0%})" if detected else
              "[GAME] Locked detection: none")

        for tick in [3, 2, 1]:
            with self._lock:
                self.countdown = tick
            time.sleep(1)

        with self._lock:
            self.state = 'evaluating'
            self.countdown = None

        human_move = detected.capitalize() if detected and detected in VALID_LABELS else None

        if human_move and WINS.get(human_move):
            if human_move == arduino_move:
                winner = 'draw'
            elif WINS[human_move] == arduino_move:
                winner = 'human'
            else:
                winner = 'arduino'
        else:
            winner = 'no_detection'

        with self._lock:
            self.human_move = human_move
            self.winner = winner
            self.round_number += 1

            if winner == 'human':
                self.human_wins += 1
            elif winner == 'arduino':
                self.arduino_wins += 1
            elif winner == 'draw':
                self.draws += 1

            round_record = {
                'round': self.round_number,
                'humanMove': human_move,
                'arduinoMove': arduino_move,
                'winner': winner,
                'confidence': conf,
            }
            self.history.insert(0, round_record)
            self.state = 'result'

        log(f"[GAME] Round {round_record['round']}: "
              f"Human={human_move or '?'} vs Arduino={arduino_move} -> {winner}")

        enqueue_milestone('result', winner=winner, human_move=human_move,
                          arduino_move=arduino_move)

        time.sleep(RESULT_HOLD_SECS)

        with self._lock:
            self.state = 'idle'
            self._detection_locked = False

        return round_record

    def add_commentary(self, text):
        with self._lock:
            self.commentary.insert(0, text)
            del self.commentary[10:]

    def set_commentating(self, value):
        with self._lock:
            self.commentating = value

    def reset(self):
        with self._lock:
            self.human_wins = 0
            self.arduino_wins = 0
            self.draws = 0
            self.round_number = 0
            self.state = 'idle'
            self.countdown = None
            self.arduino_move = None
            self.human_move = None
            self.winner = None
            self._detection_locked = False
            self.commentary.clear()
            self.commentating = False
            self.history.clear()
        log("[GAME] Scores reset")

    def to_dict(self):
        with self._lock:
            return {
                'humanWins': self.human_wins,
                'arduinoWins': self.arduino_wins,
                'draws': self.draws,
                'round': self.round_number,
                'state': self.state,
                'countdown': self.countdown,
                'arduinoMove': self.arduino_move,
                'humanMove': self.human_move,
                'winner': self.winner,
                'detection': self.detection,
                'confidence': self.confidence,
                'commentary': list(self.commentary),
                'commentating': self.commentating,
                'history': list(self.history),
            }


game = GameState()


# ─── Live Commentator ────────────────────────────────────────────────────
# One background worker turns game events into play-by-play lines. The local
# LLM is CPU-heavy and competes with the Edge Impulse detector for cores, which
# collapses detection throughput (seconds per frame). So we narrate ONLY the
# round result — one line per round, generated during the result hold when
# detection accuracy no longer matters — instead of every detection change or
# countdown milestone. This keeps live detection running at full speed.
_milestones = queue.Queue()


def enqueue_milestone(kind, **data):
    if not _llm:
        return
    _milestones.put({'kind': kind, **data})


def _prompt_for(event):
    if event['kind'] == 'result':
        verdict = {
            'human': 'the HUMAN wins the round',
            'arduino': 'the ARDUINO wins the round',
            'draw': "it's a DRAW",
            'no_detection': 'no valid move from the human — no contest',
        }.get(event['winner'], event['winner'])
        return (f"FINAL WHISTLE: {verdict}! Human played "
                f"{event.get('human_move') or '—'}, Arduino played {event['arduino_move']}. "
                f"Give the dramatic verdict.")
    return None


def commentator_worker():
    # Warm the model once at startup. The first call triggers a multi-second
    # model init that pegs the CPU and starves detection; paying it at boot
    # keeps the first real round from stalling mid-game.
    try:
        log("[LLM] warming up model...")
        t0 = time.perf_counter()
        _llm.chat("Say ready.")
        log(f"[LLM] model ready ({time.perf_counter() - t0:.1f}s)")
    except Exception as e:
        log(f"[LLM] warmup failed: {e}")
    while True:
        event = _milestones.get()
        prompt = _prompt_for(event)
        if not prompt:
            continue
        game.set_commentating(True)
        # Stream so we can count tokens (chunks) and time the generation. The
        # brick's chat() only returns the final string with no usage info.
        t0 = time.perf_counter()
        chunks = []
        try:
            for chunk in _llm.chat_stream(prompt):
                chunks.append(chunk)
        except Exception as e:
            log(f"[LLM] commentary failed: {e}")
            game.set_commentating(False)
            continue
        elapsed = time.perf_counter() - t0
        game.set_commentating(False)
        text = "".join(chunks).strip()
        n_tok = len(chunks)
        tps = n_tok / elapsed if elapsed else 0.0
        if text:
            log(f"[LLM] {text}")
            log(f"[LLM] {n_tok} tokens in {elapsed:.1f}s ({tps:.1f} tok/s)")
            game.add_commentary(text)


if _llm:
    threading.Thread(target=commentator_worker, daemon=True).start()


# ─── Brick Detection Callback ────────────────────────────────────────────
# The brick fires on_detect_all only when it has at least one detection — empty
# frames trigger no callback and the brick exposes no inference-time field. So
# we time the interval between callbacks ourselves as an effective inference
# cadence. We log every inference (all class confidences, including sub-play-
# threshold) via the non-blocking log() so a wedged stdout can never stall us.
_infer_last_ts = None
_infer_count = 0
_infer_intervals = []
_infer_lock = threading.Lock()


def handle_detections(detections):
    """Called by the video_object_detection brick with detection results.

    The brick may pass either:
      - {label: {"confidence": float}} (dict values)
      - {label: float}                 (plain float values)
      - {label: [ {"confidence": float, ...}, ... ]} (list of detail dicts)
    """
    if not detections:
        return

    if DEBUG_DETECTIONS:
        log(f"[BRICK-RAW] {detections}")

    # Collect the confidence of EVERY rock/paper/scissors detection in this
    # frame, regardless of the play threshold, so we can log all outcomes.
    all_confs = {}
    try:
        for k, v in detections.items():
            label = k.lower()
            if label not in VALID_LABELS:
                continue
            if isinstance(v, dict):
                conf = v.get("confidence")
            elif isinstance(v, list):
                conf = v[0].get("confidence") if v and isinstance(v[0], dict) else None
            else:
                conf = v
            if conf is None:
                continue
            if label not in all_confs or conf > all_confs[label]:
                all_confs[label] = conf
    except Exception as e:
        log(f"[BRICK] detection handler error: {e}")
        return

    # Only detections at/above the play threshold can be locked as the move.
    best_label, best_conf = None, None
    qualified = {l: c for l, c in all_confs.items() if c >= CONFIDENCE_THRESHOLD}
    if qualified:
        best_label = max(qualified, key=qualified.get)
        best_conf = qualified[best_label]
        game.update_detection(best_label, best_conf)

    global _infer_last_ts, _infer_count
    now = time.perf_counter()
    with _infer_lock:
        _infer_count += 1
        last = None
        if _infer_last_ts is not None:
            last = (now - _infer_last_ts) * 1000.0
            # Only frames delivered back-to-back count toward the cadence; a long
            # pause is an idle gap (nothing detected), not a slow inference.
            if last <= GAP_MS:
                _infer_intervals.append(last)
                del _infer_intervals[100:]
        _infer_last_ts = now
        count = _infer_count
        active = list(_infer_intervals)

    if active:
        avg = sum(active) / len(active)
        rate = 1000.0 / avg if avg else 0.0
    else:
        avg = rate = 0.0

    confs = "  ".join(f"{l}={all_confs[l]:.0%}"
                      for l in sorted(all_confs, key=all_confs.get, reverse=True))
    lock = (f"lock={best_label} {best_conf:.0%}" if best_label
            else f"lock=none (all <{CONFIDENCE_THRESHOLD:.0%})")
    if last is None:
        timing = "first"
    elif last > GAP_MS:
        timing = f"gap={last:.0f}ms after idle, active ~{rate:.1f}/s"
    else:
        timing = f"interval={last:.0f}ms, active avg={avg:.0f}ms ~{rate:.1f}/s"

    log(f"[INFER] #{count}  {confs}  {lock}  ({timing})")


if _detector:
    _detector.on_detect_all(handle_detections)


# ─── Flask Application ───────────────────────────────────────────────────
app = Flask(__name__)


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/state')
def api_state():
    return jsonify(game.to_dict())


@app.route('/api/play', methods=['POST'])
def api_play():
    state = game.to_dict()
    if state['state'] != 'idle':
        return jsonify({'status': 'busy', 'message': 'Round in progress'}), 409
    threading.Thread(target=game.play_round, daemon=True).start()
    return jsonify({'status': 'ok', 'message': 'Round started'})


@app.route('/api/reset', methods=['POST'])
def api_reset():
    game.reset()
    return jsonify({'status': 'ok'})


# ─── Entry Point ─────────────────────────────────────────────────────────
if __name__ == '__main__':
    log('=' * 50)
    log('  Rock Paper Scissors — Arduino UNO Q')
    log('=' * 50)
    log(f'[MODE] Brick: {"yes" if _detector else "no"}')
    log(f'[MODE] LLM: {"yes" if _llm else "no"}')
    log(f'[MODE] App runner: {"yes" if _App else "no"}')

    threading.Thread(
        target=lambda: app.run(
            host='0.0.0.0', port=PORT, threaded=True, use_reloader=False
        ),
        daemon=True
    ).start()
    log(f'[WEB] http://0.0.0.0:{PORT}')

    if _App:
        _App.run()
    else:
        log('[INFO] Running standalone (no App runner)')
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            log('\n[EXIT] Shutting down')
