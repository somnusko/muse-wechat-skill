#!/usr/bin/env python3
"""Local transcription for inbound WeChat voice messages.

Contract (see AGENTS.md, "Voice messages" section):
  * WeChat's own transcription (voice_item.text) wins whenever poll
    already filled it in. This module is only the fallback, run on the
    worker side when a queued voice entry has an empty text and a
    downloaded media_path.
  * Input is whatever download_media_item saved: a SILK_V3 file
    (extension .silk) or, rarely, a plain WAV. SILK is decoded to PCM
    with silk-wasm (Node), then ffmpeg normalises to 16 kHz mono
    16-bit WAV, then faster-whisper (tiny by default) transcribes it.
  * Every failure mode returns None (library) / TRANSCRIBE_FAILED
    (CLI) instead of raising: a missing model, a broken file, or a
    timeout must never kill the worker or corrupt poll's JSON stdout.
    Diagnostics go to stderr only.

Layout (production):
  * This file lives next to ilink.py. It shells out to two helpers
    that must exist on the host, both installed once, outside the
    data dir:
      - faster-whisper: a venv at $WHISPER_VENV_PYTHON, default
        ~/.local/share/wechat-muse/whisper-venv/bin/python, with the
        model pre-downloaded under ~/.cache/huggingface
        (HF_HUB_OFFLINE=0 python -c "from faster_whisper import
        WhisperModel; WhisperModel('tiny', device='cpu',
        compute_type='int8')").
      - SILK decoding: node + the silk-wasm npm package, resolved by
        scripts/silk_decode.mjs (see $SILK_WASM_DIR).
  * ffmpeg/ffprobe must be on PATH (already required by the bridge
    docs for video handling).

Why a subprocess for whisper instead of importing it here: the
bridge runs under the system python, which is PEP-668 managed and
does not have faster-whisper; importing ~470 MB of model runtime
into every ilink.py invocation would also slow down poll. The
worker subprocess keeps the bridge process light and lets the
model load (~3 s cold) happen only when a voice actually needs it.

Known quirk (measured 2026-11-12): faster-whisper 1.2.1 with
av 19.0.2 raises TypeError if you pass a file *path* to
model.transcribe (av.open() rejects the metadata_errors kwarg).
The worker below therefore reads the WAV with the stdlib `wave`
module, converts to a float32 numpy array, and passes the array
in -- never a path.
"""

import os
import shutil
import subprocess
import sys
import tempfile

DEFAULT_MODEL = "tiny"
DEFAULT_TIMEOUT_SECS = 120
# Per-step budgets inside the overall timeout. Whisper gets the
# remainder; a 60 s voice needs ~3 s model load + ~8 s (tiny) /
# ~14 s (base) inference on this host, so 120 s is generous.
_FFMPEG_TIMEOUT = 30
_SILK_TIMEOUT = 30
_SILK_MAGIC = b"#!SILK"


def _log(msg):
    print(f"voice_transcribe: {msg}", file=sys.stderr, flush=True)


def _is_silk(path):
    """True when the file looks like SILK. WeChat voice files carry
    a one-byte 0x02 prefix before the "#!SILK_V3" header, so this
    is a substring check over the first bytes, not startswith --
    a strict startswith misses every real WeChat voice file."""
    try:
        with open(path, "rb") as f:
            return _SILK_MAGIC in f.read(16)
    except OSError:
        return False


def _wav_info(path):
    """(sample_rate, channels, sample_width) or None if not a WAV."""
    import wave
    try:
        with wave.open(path, "rb") as wf:
            return wf.getframerate(), wf.getnchannels(), wf.getsampwidth()
    except Exception:
        return None


def _decode_silk_to_pcm(silk_path, pcm_path, timeout=_SILK_TIMEOUT):
    """SILK -> raw s16le PCM @24 kHz via scripts/silk_decode.mjs.

    Returns True on success. The helper writes raw PCM (no WAV
    header); the caller wraps it with the stdlib wave module.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    helper = os.environ.get(
        "SILK_DECODE_JS", os.path.join(here, "scripts", "silk_decode.mjs"))
    node = shutil.which("node")
    if not node:
        _log("node not found on PATH; cannot decode SILK")
        return False
    if not os.path.isfile(helper):
        _log(f"silk decode helper missing: {helper}")
        return False
    try:
        r = subprocess.run(
            [node, helper, silk_path, pcm_path],
            capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        _log("silk decode timed out")
        return False
    except Exception as e:
        _log(f"silk decode failed to start: {type(e).__name__}: {e}")
        return False
    if r.returncode != 0:
        _log(f"silk decode failed: {(r.stderr or r.stdout or '').strip()[:200]}")
        return False
    return os.path.isfile(pcm_path) and os.path.getsize(pcm_path) > 0


def _pcm_to_wav(pcm_path, wav_path, sample_rate=24000):
    import wave
    try:
        with open(pcm_path, "rb") as f:
            raw = f.read()
        with wave.open(wav_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(raw)
        return True
    except Exception as e:
        _log(f"pcm->wav failed: {type(e).__name__}: {e}")
        return False


def _normalise_to_16k(src_wav, dst_wav, timeout=_FFMPEG_TIMEOUT):
    """ffmpeg: any WAV -> 16 kHz mono s16le WAV. Verifies the header.

    If src is already 16 kHz / mono / 16-bit, copies it unchanged.
    Returns True on success.
    """
    info = _wav_info(src_wav)
    if info == (16000, 1, 2):
        try:
            shutil.copyfile(src_wav, dst_wav)
            return True
        except OSError as e:
            _log(f"wav copy failed: {e}")
            return False
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        _log("ffmpeg not found on PATH; cannot normalise audio")
        return False
    try:
        r = subprocess.run(
            [ffmpeg, "-y", "-v", "error", "-i", src_wav,
             "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
             "-f", "wav", dst_wav],
            capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        _log("ffmpeg normalise timed out")
        return False
    except Exception as e:
        _log(f"ffmpeg failed to start: {type(e).__name__}: {e}")
        return False
    if r.returncode != 0:
        _log(f"ffmpeg failed: {(r.stderr or '').strip()[-200:]}")
        return False
    if _wav_info(dst_wav) != (16000, 1, 2):
        _log("ffmpeg produced unexpected format (want 16k/mono/16-bit)")
        return False
    return True


def _whisper_python():
    """Interpreter that has faster-whisper installed."""
    override = os.environ.get("WHISPER_VENV_PYTHON")
    if override:
        return override
    home = os.path.expanduser("~")
    return os.path.join(
        home, ".local", "share", "wechat-muse",
        "whisper-venv", "bin", "python")


def _run_whisper(wav_path, model, language, timeout):
    """Run this file in worker mode under the whisper venv.

    Returns the transcribed text (possibly ""), or None on failure.
    Stdout of the worker is the text; anything else is stderr.
    """
    exe = _whisper_python()
    if not os.path.isfile(exe):
        _log(f"whisper python not found: {exe} "
             f"(install: python3 -m venv + pip install faster-whisper numpy)")
        return None
    env = dict(os.environ)
    env["HF_HUB_OFFLINE"] = "1"  # never stall on a model download here
    cmd = [exe, os.path.abspath(__file__), "_worker", wav_path,
           "--model", model, "--language", language]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        _log(f"whisper timed out after {timeout}s")
        return None
    except Exception as e:
        _log(f"whisper failed to start: {type(e).__name__}: {e}")
        return None
    if r.returncode != 0:
        _log(f"whisper worker failed: {(r.stderr or '').strip()[-300:]}")
        return None
    return (r.stdout or "").strip()


def transcribe_voice(media_path, model=DEFAULT_MODEL,
                     timeout=DEFAULT_TIMEOUT_SECS, language="zh"):
    """Transcribe one downloaded voice file.

    Returns the text ("" when the audio holds no speech), or None
    when transcription was not possible for any reason. Never raises
    for bad input; only programming errors propagate.
    """
    if not media_path or not os.path.isfile(media_path):
        _log(f"no such media file: {media_path}")
        return None
    if os.path.getsize(media_path) == 0:
        _log("empty media file")
        return None
    model = os.environ.get("WHISPER_MODEL", model)
    try:
        timeout = int(os.environ.get("VOICE_TRANSCRIBE_TIMEOUT", timeout))
    except ValueError:
        pass
    with tempfile.TemporaryDirectory(prefix="voice-transcribe-") as td:
        wav_path = media_path
        if _is_silk(media_path):
            pcm_path = os.path.join(td, "decoded.pcm")
            if not _decode_silk_to_pcm(media_path, pcm_path):
                return None
            wav24 = os.path.join(td, "decoded.wav")
            if not _pcm_to_wav(pcm_path, wav24, sample_rate=24000):
                return None
            wav_path = wav24
        elif _wav_info(media_path) is None:
            _log("not SILK and not WAV; unsupported input format")
            return None
        norm_path = os.path.join(td, "norm16k.wav")
        if not _normalise_to_16k(wav_path, norm_path):
            return None
        # Whisper gets whatever is left of the budget after the
        # decode/normalise steps (both are normally < 2 s).
        return _run_whisper(norm_path, model, language,
                            timeout=max(10, timeout - 5))


def _worker_main(argv):
    """Hidden worker entry: runs inside the whisper venv.

    Usage: voice_transcribe.py _worker <wav16k> [--model tiny]
    [--language zh]. Prints the transcription (only) to stdout.
    Importing faster-whisper here -- not at module top -- keeps the
    bridge process free of the heavy dependency.
    """
    import argparse
    import wave
    import numpy as np
    from faster_whisper import WhisperModel

    ap = argparse.ArgumentParser()
    ap.add_argument("wav")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--language", default="zh")
    args = ap.parse_args(argv)
    with wave.open(args.wav, "rb") as wf:
        if wf.getnchannels() != 1 or wf.getsampwidth() != 2:
            print("worker: want 16k mono 16-bit WAV", file=sys.stderr)
            return 1
        raw = wf.readframes(wf.getnframes())
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if audio.size == 0:
        return 0
    # local_files_only: the model must already be downloaded; a
    # missing model is a deployment error, reported on stderr.
    model = WhisperModel(args.model, device="cpu",
                         compute_type="int8", local_files_only=True)
    segments, _info = model.transcribe(audio, language=args.language)
    text = "".join(s.text for s in segments).strip()
    if text:
        print(text)
    return 0


def _cli_main(argv):
    """`voice_transcribe.py <media_path>`: manual test entry."""
    import argparse
    ap = argparse.ArgumentParser(description="Transcribe a voice file.")
    ap.add_argument("media_path")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECS)
    ap.add_argument("--language", default="zh")
    args = ap.parse_args(argv)
    text = transcribe_voice(args.media_path, model=args.model,
                            timeout=args.timeout, language=args.language)
    if text is None:
        print("TRANSCRIBE_FAILED:see stderr", file=sys.stderr)
        return 1
    print(text)
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "_worker":
        sys.exit(_worker_main(sys.argv[2:]))
    sys.exit(_cli_main(sys.argv[1:]))
