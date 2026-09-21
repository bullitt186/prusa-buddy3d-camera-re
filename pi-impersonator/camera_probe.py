"""Camera probe and sensor hand-off policy (WP-3a, AC-16, AC-18).

Host-only, stdlib-only and import-safe: importing this module performs no
subprocess call, no file access and no network I/O. Every external command is
executed through an injectable ``runner`` callable so the automated tests never
touch a camera or a Raspberry Pi.

Documented invocation
---------------------
The v1 sensor-detection command is the one in the distribution plan §4.2::

    rpicam-hello --list-cameras

The number of CSI sensors is the number of indexed camera entries in its
``Available cameras`` listing (lines shaped ``<index> : <model> [<modes>]``).

Probe contract (AC-16)
----------------------
:func:`probe` requires exactly one CSI sensor for v1, then exercises short
captures at 640x480, 1280x720 and 1920x1080, the H.264 pipeline, and one JPEG
capture. A failure keeps the appliance in setup/recovery: **this module never
retries or restarts anything.** The caller (the provisioning wizard) shows the
exact ``reason`` and a retry action, and the systemd unit must not be
``Restart=always`` on a probe failure (no restart loop).

Hand-off ordering contract (AC-18)
----------------------------------
:func:`sensor_handoff_allowed` gates the provisioning/QR capture on the
persisted provisioning state and on the live libcamera owner:

* probing is allowed only in the pre-runtime states
  ``factory``, ``storage_ready``, ``camera_validated``, ``unclaimed`` and
  ``claimed``;
* probing is denied while ``rpicam-source``/``prusa-cam`` are running
  (``camera_running``), because libcamera has a single consumer and a second
  ``rpicam-vid`` owner would corrupt the stream.

The setup service must stop and release libcamera *before*
``prusa-camera.target`` starts ``rpicam-source.service``. No feature may create
a second ``rpicam-vid`` process while the source service is active.

Secret hygiene
--------------
Failure reasons contain only tool names, exit status and the resolution under
test. They are stripped of control characters and bounded in length, so a
caller cannot smuggle a secret into a log through a probe reason.
"""
import dataclasses
import logging
import re
import subprocess

log = logging.getLogger('prusa-cam.camera_probe')

#: Bounded wall-clock timeouts for every command this module runs.
LIST_TIMEOUT_SECONDS = 10.0
CAPTURE_TIMEOUT_SECONDS = 20.0

#: The documented v1 sensor-detection command.
LIST_COMMAND = ('rpicam-hello', '--list-cameras')

#: The three capture resolutions the wizard must exercise (source §4.2 step 3).
CAPTURE_RESOLUTIONS = ((640, 480), (1280, 720), (1920, 1080))

#: Pre-runtime states in which the sensor may be probed/captured (AC-18).
PRE_RUNTIME_STATES = frozenset({
    'factory',
    'storage_ready',
    'camera_validated',
    'unclaimed',
    'claimed',
})

#: Indexed camera entries look like ``0 : ov5647 [2592x1944] (...)``.
_SENSOR_ENTRY_RE = re.compile(r'^\s*\d+\s*:\s*\S', re.MULTILINE)
_REASON_MAX = 200
_CONTROL_RE = re.compile(r'[\x00-\x1f\x7f]+')


# --------------------------------------------------------------------------- #
# Result objects
# --------------------------------------------------------------------------- #

@dataclasses.dataclass
class ProbeStep:
    """One named step of a probe with its own ok/reason."""

    name: str
    ok: bool
    reason: str = ''


@dataclasses.dataclass
class ProbeResult:
    """Outcome of a sensor listing or full probe.

    ``ok`` is the overall result, ``reason`` an exact non-secret failure,
    ``sensors`` the number of CSI sensors observed, ``steps`` the per-step
    outcomes (each a :class:`ProbeStep`) and ``retryable`` whether retrying the
    probe can plausibly succeed (a timeout can; a missing tool cannot).
    """

    ok: bool
    reason: str = ''
    sensors: int = 0
    steps: list = dataclasses.field(default_factory=list)
    retryable: bool = False


# --------------------------------------------------------------------------- #
# Runner plumbing
# --------------------------------------------------------------------------- #

def _default_runner(args, timeout):
    """Run ``args`` with a bounded timeout, capturing text stdout/stderr.

    The single place this module touches :mod:`subprocess`; tests replace it
    with a fake so no hardware or process is involved.
    """
    return subprocess.run(
        list(args),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _stdout(result):
    """Best-effort text stdout of a runner result (never raises)."""
    value = getattr(result, 'stdout', '') or ''
    if isinstance(value, bytes):
        return value.decode('utf-8', 'replace')
    return value


def _returncode(result):
    """Best-effort return code of a runner result (missing means failure)."""
    value = getattr(result, 'returncode', 1)
    return value if isinstance(value, int) else 1


def _sanitize_reason(reason):
    """Bound and strip a reason so it can never carry control characters."""
    if not isinstance(reason, str):
        return ''
    cleaned = _CONTROL_RE.sub(' ', reason).strip()
    if len(cleaned) > _REASON_MAX:
        cleaned = cleaned[:_REASON_MAX]
    return cleaned


def _run(runner, args, timeout):
    """Invoke ``runner`` and normalize the outcome.

    Returns ``(returncode, stdout, failure)`` where ``failure`` is a
    :class:`ProbeResult` on an exception and ``None`` on a completed command.
    """
    try:
        result = runner(list(args), timeout)
    except subprocess.TimeoutExpired:
        return None, '', ProbeResult(
            False, 'camera probe timed out', retryable=True
        )
    except OSError:
        return None, '', ProbeResult(
            False, 'camera probe tool unavailable', retryable=False
        )
    except Exception as e:  # noqa: BLE001 - a probe must never raise
        log.warning(f'camera_probe: runner failed: {e}')
        return None, '', ProbeResult(False, 'camera probe failed', retryable=False)
    return _returncode(result), _stdout(result), None


# --------------------------------------------------------------------------- #
# Sensor listing
# --------------------------------------------------------------------------- #

def parse_sensor_count(text):
    """Count indexed CSI camera entries in ``rpicam-hello --list-cameras``.

    Returns ``0`` for empty, ``None`` or unparsable output so a caller can
    treat "no cameras available" as zero sensors rather than a crash.
    """
    if not isinstance(text, str) or not text:
        return 0
    return len(_SENSOR_ENTRY_RE.findall(text))


def list_sensors(runner=None):
    """List CSI sensors via ``rpicam-hello --list-cameras`` (bounded timeout).

    ``ok`` means the command completed and its listing parsed; ``sensors`` is
    the parsed count and may legitimately be ``0``. A timeout is retryable; a
    missing tool is not. The command is documented in the module docstring.
    """
    runner = runner or _default_runner
    returncode, stdout, failure = _run(runner, LIST_COMMAND, LIST_TIMEOUT_SECONDS)
    if failure is not None:
        return failure
    if returncode != 0:
        return ProbeResult(
            False,
            _sanitize_reason(f'rpicam-hello --list-cameras failed (exit {returncode})'),
            sensors=0,
            retryable=False,
        )
    sensors = parse_sensor_count(stdout)
    return ProbeResult(True, '', sensors=sensors, retryable=False)


# --------------------------------------------------------------------------- #
# Capture backends
# --------------------------------------------------------------------------- #

def _capture_command(width, height, codec):
    """Build the bounded capture command for ``codec`` at ``width``x``height``.

    ``-o -`` streams the capture to stdout so the probe never needs a durable
    file. The exact tool flags are a hardware-only assumption (the probe is
    host-tested with a fake runner).
    """
    tool = {'still': 'rpicam-still', 'jpeg': 'rpicam-jpeg', 'h264': 'rpicam-vid'}[codec]
    command = [
        tool,
        '--width', str(width),
        '--height', str(height),
        '--timeout', '1000',
        '--nopreview',
    ]
    if codec == 'h264':
        command += ['--codec', 'h264']
    command += ['-o', '-']
    return command


def _default_capture(width, height, codec, runner):
    """Run one capture through ``runner``; return ``(ok, reason)``.

    Uses the same injectable runner as :func:`list_sensors`, so faking the
    runner is sufficient to drive every step of :func:`probe` in tests.
    """
    command = _capture_command(width, height, codec)
    returncode, stdout, failure = _run(runner, command, CAPTURE_TIMEOUT_SECONDS)
    if failure is not None:
        return False, _sanitize_reason(f'{codec} capture: {failure.reason}')
    if returncode != 0:
        return False, f'{codec} capture failed at {width}x{height}'
    if not stdout.strip():
        return False, f'{codec} capture produced no output at {width}x{height}'
    return True, ''


# --------------------------------------------------------------------------- #
# Full probe
# --------------------------------------------------------------------------- #

def _capture_steps():
    """The ordered capture checks: three resolutions, H.264, then one JPEG."""
    plan = [
        (f'capture_{width}x{height}', width, height, 'still')
        for width, height in CAPTURE_RESOLUTIONS
    ]
    plan.append(('h264_pipeline', 1920, 1080, 'h264'))
    plan.append(('jpeg_capture', 1920, 1080, 'jpeg'))
    return plan


def probe(runner=None, captures=None):
    """Probe one CSI sensor and exercise the capture pipeline (AC-16).

    Requires exactly one CSI sensor, then validates short captures at
    640x480/1280x720/1920x1080, the H.264 pipeline and one JPEG capture.
    ``runner`` performs the sensor listing (and the default captures); an
    optional ``captures(width, height, codec, runner) -> (ok, reason)`` callable
    overrides capture execution for tests.

    A failure is returned, never raised, and never triggers a retry or a
    service restart. The caller keeps the appliance in setup/recovery and
    offers an explicit retry.
    """
    runner = runner or _default_runner
    captures = captures or _default_capture
    steps = []

    listing = list_sensors(runner)
    steps.append(ProbeStep('list_sensors', listing.ok, listing.reason))
    if not listing.ok:
        return ProbeResult(
            False, listing.reason, sensors=listing.sensors,
            steps=steps, retryable=listing.retryable,
        )

    if listing.sensors == 0:
        reason = 'no CSI sensor detected'
        steps.append(ProbeStep('sensor_count', False, reason))
        return ProbeResult(False, reason, sensors=0, steps=steps, retryable=True)
    if listing.sensors != 1:
        reason = f'multiple CSI sensors detected ({listing.sensors}); v1 supports exactly one'
        steps.append(ProbeStep('sensor_count', False, reason))
        return ProbeResult(
            False, reason, sensors=listing.sensors, steps=steps, retryable=False
        )
    steps.append(ProbeStep('sensor_count', True, ''))

    for name, width, height, codec in _capture_steps():
        try:
            ok, reason = captures(width, height, codec, runner)
        except Exception as e:  # noqa: BLE001 - a probe must never raise
            log.warning(f'camera_probe: capture {name} failed: {e}')
            ok, reason = False, 'capture step failed'
        reason = _sanitize_reason(reason)
        steps.append(ProbeStep(name, bool(ok), reason))
        if not ok:
            return ProbeResult(
                False, reason or f'{name} failed', sensors=1,
                steps=steps, retryable=False,
            )

    return ProbeResult(True, '', sensors=1, steps=steps, retryable=False)


# --------------------------------------------------------------------------- #
# Sensor hand-off policy
# --------------------------------------------------------------------------- #

def sensor_handoff_allowed(state, camera_running):
    """Whether the provisioning capture may own the sensor right now (AC-18).

    Returns ``(allowed, reason)``. Probing is allowed only in a pre-runtime
    provisioning state *and* while no live libcamera owner is running. The
    setup service must stop before ``prusa-camera.target`` starts
    ``rpicam-source.service``; no second ``rpicam-vid`` may run concurrently.
    """
    if state not in PRE_RUNTIME_STATES:
        if state == 'recovery' or state == 'configured' or state == 'running':
            return False, f'probing is not allowed in state {state}'
        return False, 'unknown provisioning state'
    if camera_running:
        return False, 'camera source is running; release libcamera before probing'
    return True, 'sensor hand-off allowed'
