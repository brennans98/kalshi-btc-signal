"""Durable JSON state that survives being killed at the worst possible moment.

Three defects motivated this module, and they are worth naming because each one
fails in the direction that costs money.

1. WRITES WERE NOT ATOMIC. `path.write_text(json.dumps(...))` truncates the
   file and then writes. A process killed between those two steps -- a redeploy,
   an OOM, a host restart -- leaves a half-written file. The reader caught the
   parse error and returned blank state. So the failure mode was: open positions
   silently forgotten, and, in risk.py, a LATCHED HALT SILENTLY CLEARED. A halt
   that clears itself on a crash is not a safety mechanism.

2. WRITE FAILURES WERE SWALLOWED. `except Exception: pass` means a full disk or
   a read-only volume discards every subsequent state write while the bot keeps
   trading as though it had saved. It would notice at the next restart, having
   forgotten everything since the first failure.

3. EVERY READ HIT THE DISK. That was tolerable on a 2-second loop. On an
   event-driven sub-second loop it puts filesystem latency directly inside the
   exit path -- the one path where the whole point is to be fast.

The fixes: write to a temp file, fsync, then os.replace (atomic on POSIX, so a
reader sees either the whole old file or the whole new one, never a partial);
keep the previous good copy as a .bak to recover from; cache in memory and write
through, so reads never touch the disk; and record failures loudly instead of
swallowing them.
"""

import json
import os
import tempfile
import time

# path string -> the authoritative in-memory copy.
_cache = {}

# Deferred disk writes: path string -> Path. See write_lazy().
_pending = {}
_last_write = {}

# Failures worth showing on the dashboard rather than discovering at 3am.
errors = {}
_recoveries = []


def _note_error(path, message):
    errors[str(path)] = {"message": str(message)[:200], "at": int(time.time())}


def _clear_error(path):
    errors.pop(str(path), None)


def _note_recovery(message):
    _recoveries.append({"message": message[:300], "at": int(time.time())})
    del _recoveries[:-20]


def status():
    """Persistence health, for /api/state.

    An empty payload here is the expected state. Anything in `errors` means
    state is not being written, which means a restart will lose whatever has
    happened since -- including a halt that ought to have latched.
    """
    return {
        # "healthy" is about the present: can state be read and written right
        # now. A file that was corrupt and has since been repaired is healthy
        # again, which is why recoveries are reported separately and stickily --
        # a self-healed corruption still means the process was killed mid-write
        # and is worth seeing rather than erasing.
        "healthy": not errors,
        "errors": dict(errors),
        "recovered_from_corruption": bool(_recoveries),
        "recoveries": list(_recoveries),
        "cached_files": len(_cache),
        "pending_writes": len(_pending),
    }


def read(path, default_factory):
    """The current state, from memory when possible.

    Recovers from a damaged primary file using the .bak copy. When both are
    unreadable it returns blank state -- there is nothing else it can do -- but
    it records the failure rather than passing it off as an empty account.
    """
    key = str(path)
    if key in _cache:
        return _cache[key]

    primary_error = None
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError("state file is not a JSON object")
        _cache[key] = value
        _clear_error(path)
        return value
    except FileNotFoundError:
        # First run. Not an error: there is genuinely nothing to load.
        value = default_factory()
        _cache[key] = value
        return value
    except Exception as error:
        primary_error = error

    backup = path.with_suffix(path.suffix + ".bak")
    try:
        value = json.loads(backup.read_text())
        if not isinstance(value, dict):
            raise ValueError("backup is not a JSON object")
        _cache[key] = value
        message = (
            f"{path.name} was unreadable ({primary_error}); recovered the "
            f"previous good copy from {backup.name}"
        )
        _note_recovery(message)
        _note_error(path, message)
        # Re-establish a valid primary immediately, so the next restart does not
        # have to repeat this recovery.
        try:
            write(path, value)
        except Exception:
            pass
        return value
    except FileNotFoundError:
        pass
    except Exception as backup_error:
        _note_recovery(f"{backup.name} was also unreadable ({backup_error})")

    _note_error(
        path,
        f"{path.name} could not be read ({primary_error}) and no usable backup "
        f"exists; starting from blank state. Open positions must be recovered by "
        f"reconciling against the exchange.",
    )
    value = default_factory()
    _cache[key] = value
    return value


def write(path, value):
    """Persist state atomically and durably. Raises on failure.

    os.replace is atomic on POSIX: a reader sees either the complete previous
    file or the complete new one, never a partial. fsync before the replace is
    what makes that hold across a host crash rather than just a process kill.

    The .bak copy is written AFTER the primary succeeds, not before. Writing it
    first (by renaming the old primary aside) leaves the very first write
    unprotected -- there is no previous version to fall back to -- and leaves the
    backup permanently one revision behind. Writing it after means .bak always
    holds the last state that was successfully committed.
    """
    key = str(path)
    _cache[key] = value
    _pending.pop(key, None)

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, indent=2)

    _atomic_write(path, payload, fsync=True)
    _last_write[key] = time.time()

    # Best effort, and deliberately not fsynced: the backup exists to survive a
    # torn primary, which is a process-level failure, and paying a second fsync
    # on the exit path to also survive a host-level one is not worth it.
    try:
        _atomic_write(path.with_suffix(path.suffix + ".bak"), payload, fsync=False)
    except Exception:
        pass

    _clear_error(path)


def write_lazy(path, value, max_delay=1.0):
    """Update state in memory now; commit to disk at most every `max_delay`.

    For fields that change on every tick and are reconstructible after a crash
    -- the current bid mark and the running peak of an open lot, specifically.
    Those were being fsynced to disk several times a second from inside the exit
    path, which put filesystem latency in front of every stop and trail
    evaluation. The in-memory copy is still updated immediately, so nothing the
    strategy reads is ever stale; only the disk write is coalesced.

    Anything that must survive a crash -- an entry, an exit, a halt -- uses
    write() instead, and flushes whatever was pending along with it.
    """
    key = str(path)
    _cache[key] = value

    if time.time() - float(_last_write.get(key) or 0) >= max_delay:
        try:
            write(path, value)
        except Exception:
            # Already recorded by write(); a deferred write is allowed to fail
            # without taking down the tick that triggered it.
            _pending[key] = path
    else:
        _pending[key] = path


def flush(max_delay=0.0):
    """Commit anything write_lazy deferred. Safe to call from the loop.

    Called once per tick outside the exit path, so a deferred write is never
    deferred indefinitely -- a quiet market must not leave the last mark
    unwritten forever.
    """
    for key, path in list(_pending.items()):
        if time.time() - float(_last_write.get(key) or 0) < max_delay:
            continue
        value = _cache.get(key)
        if value is None:
            _pending.pop(key, None)
            continue
        try:
            write(path, value)
        except Exception:
            pass


def _atomic_write(path, payload, fsync=True):
    handle = None
    tmp_name = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
        )
        handle = os.fdopen(fd, "w")
        handle.write(payload)
        handle.flush()
        if fsync:
            os.fsync(handle.fileno())
        handle.close()
        handle = None
        os.replace(tmp_name, path)
        tmp_name = None
    except Exception as error:
        _note_error(path, f"could not persist {path.name}: {error}")
        raise
    finally:
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass
        if tmp_name and os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except Exception:
                pass


def forget(path=None):
    """Drop the in-memory copy, forcing the next read to hit the disk.

    Only useful in tests and after an out-of-band edit to a state file.
    """
    if path is None:
        _cache.clear()
        _pending.clear()
        _last_write.clear()
    else:
        _cache.pop(str(path), None)
        _pending.pop(str(path), None)
        _last_write.pop(str(path), None)
