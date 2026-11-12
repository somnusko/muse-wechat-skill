#!/usr/bin/env python3
"""Text-to-speech helper for the voice-out fallback.

Why this exists: the iLink channel silently drops outbound voice
bubbles (send-voice / proactive-voice return success but the client
never shows them -- verified live, see _media_item in ilink.py).
So "I want to speak" is delivered as an audio *file* instead:
synthesize here, then send with the existing file path
(`ilink.py send-file <to> <ctx> <mp3> --key <id>` for replies,
`ilink.py proactive-file <to> <mp3>` for proactive pushes).
The recipient gets a playable MP3 file message, not a voice bubble.

Backends (both need network; neither needs a key):
  * gtts (default): plain HTTPS to Google TTS. Works through the
    sandbox egress proxy. Voice is serviceable, not great; Chinese
    is zh-CN. Install: pip install gtts.
  * edge (edge-tts, Microsoft): much more natural Chinese voices
    (default zh-CN-XiaoxiaoNeural). Needs a WebSocket to
    speech.platform.bing.com. Two gotchas measured 2026-11-12:
      1. edge-tts pins certifi's CA bundle, which does not contain
         the sandbox egress CA, so TLS fails until you replace
         edge_tts.communicate._SSL_CTX with
         ssl.create_default_context() (done below).
      2. Even then, this sandbox's egress proxy breaks the WS
         upgrade ("101 Invalid connection header"), so edge-tts
         does NOT work from this VM. It is expected to work on a
         normal network; gtts is the fallback that works here.
    Install: pip install edge-tts.

No heavy dependencies: gtts/edge-tts are small pure-Python
packages. No espeak/pyttsx3: no system TTS is installed here and
their Chinese quality is poor anyway.

Usage:
  python3 tts_send.py "你好，今天天气很好" --out reply.mp3
  python3 tts_send.py --backend edge --voice zh-CN-YunyangNeural \
      --out reply.mp3 "今天的会改到三点"
  echo "text" | python3 tts_send.py --out reply.mp3 -
Prints one line: TTS_OK:<path>:<bytes>:<duration_ms> (duration -1
when ffprobe is unavailable). Exit 0 ok, 1 failure, 2 usage error.
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile

DEFAULT_VOICE_EDGE = "zh-CN-XiaoxiaoNeural"
# gTTS starts failing on very long single requests; keep chunks short.
_GTTS_CHUNK = 90
_SENTENCE_END = re.compile(r"([。！？；…\n])")


def _log(msg):
    print(f"tts_send: {msg}", file=sys.stderr, flush=True)


def _probe_duration_ms(path):
    """ffprobe duration in ms, or None."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            return int(float(r.stdout.strip()) * 1000)
    except Exception:
        pass
    return None


def _split_sentences(text):
    """Split on Chinese sentence ends, then pack into <= _GTTS_CHUNK chars."""
    parts = [p for p in _SENTENCE_END.split(text) if p]
    sentences, buf = [], ""
    for p in parts:
        buf += p
        if _SENTENCE_END.fullmatch(p):
            sentences.append(buf)
            buf = ""
    if buf.strip():
        sentences.append(buf)
    chunks, cur = [], ""
    for s in sentences:
        if len(cur) + len(s) > _GTTS_CHUNK and cur:
            chunks.append(cur)
            cur = ""
        cur += s
    if cur:
        chunks.append(cur)
    return [c for c in chunks if c.strip()]


def _concat_mp3(parts, out_path):
    """Join MP3 chunks with ffmpeg; fall back to byte concat."""
    if len(parts) == 1:
        import shutil
        shutil.copyfile(parts[0], out_path)
        return True
    try:
        with tempfile.NamedTemporaryFile(
                "w", suffix=".txt", delete=False) as lf:
            for p in parts:
                lf.write(f"file '{p}'\n")
            list_path = lf.name
        r = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
             "-i", list_path, "-c", "copy", out_path],
            capture_output=True, text=True, timeout=60)
        os.unlink(list_path)
        if r.returncode == 0 and os.path.getsize(out_path) > 0:
            return True
        _log("ffmpeg concat failed; falling back to byte concat")
    except Exception as e:
        _log(f"ffmpeg concat unavailable ({e}); byte concat fallback")
    try:
        with open(out_path, "wb") as out:
            for p in parts:
                with open(p, "rb") as f:
                    out.write(f.read())
        return os.path.getsize(out_path) > 0
    except OSError as e:
        _log(f"concat failed: {e}")
        return False


def synthesize_gtts(text, out_path, lang="zh-CN"):
    """gTTS backend. Returns True on success."""
    try:
        from gtts import gTTS
    except ImportError:
        _log("gtts not installed (pip install gtts)")
        return False
    chunks = _split_sentences(text)
    if not chunks:
        _log("empty text")
        return False
    with tempfile.TemporaryDirectory(prefix="tts-gtts-") as td:
        parts = []
        for i, chunk in enumerate(chunks):
            part = os.path.join(td, f"part{i}.mp3")
            try:
                gTTS(chunk, lang=lang).save(part)
            except Exception as e:
                _log(f"gTTS chunk {i} failed: {type(e).__name__}: {e}")
                return False
            parts.append(part)
        return _concat_mp3(parts, out_path)


def synthesize_edge(text, out_path, voice=DEFAULT_VOICE_EDGE):
    """edge-tts backend. Returns True on success."""
    try:
        import edge_tts
        import edge_tts.communicate as ec
    except ImportError:
        _log("edge-tts not installed (pip install edge-tts)")
        return False
    # edge-tts pins certifi's CA bundle; on hosts behind a MITM
    # egress proxy that bundle lacks the proxy CA and TLS fails.
    # Use the system default context instead (honours SSL_CERT_FILE).
    import ssl
    ec._SSL_CTX = ssl.create_default_context()
    import asyncio
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")

    async def _run():
        comm = edge_tts.Communicate(text, voice, proxy=proxy)
        await comm.save(out_path)

    try:
        asyncio.run(_run())
    except Exception as e:
        _log(f"edge-tts failed: {type(e).__name__}: {str(e)[:200]}")
        return False
    return os.path.isfile(out_path) and os.path.getsize(out_path) > 0


def synthesize(text, out_path, backend="gtts", voice=None, lang="zh-CN"):
    """Synthesize text to an MP3 file. Returns (ok, duration_ms|None)."""
    text = (text or "").strip()
    if not text:
        _log("empty text")
        return False, None
    if backend == "edge":
        ok = synthesize_edge(text, out_path,
                             voice=voice or DEFAULT_VOICE_EDGE)
    elif backend == "gtts":
        ok = synthesize_gtts(text, out_path, lang=lang)
    else:
        _log(f"unknown backend: {backend} (gtts|edge)")
        return False, None
    if not ok:
        return False, None
    return True, _probe_duration_ms(out_path)


def main(argv):
    ap = argparse.ArgumentParser(description="Synthesize speech to MP3.")
    ap.add_argument("text", nargs="?",
                    help="text to speak ('-' reads stdin)")
    ap.add_argument("--out", required=True, help="output .mp3 path")
    ap.add_argument("--backend", default="gtts", choices=["gtts", "edge"],
                    help="gtts (default, works here) or edge (better voice)")
    ap.add_argument("--voice", default=None,
                    help="edge-tts voice, e.g. zh-CN-YunyangNeural")
    ap.add_argument("--lang", default="zh-CN", help="gTTS language")
    args = ap.parse_args(argv)
    text = args.text
    if text == "-":
        text = sys.stdin.read()
    if not text:
        ap.error("no text given")
    ok, dur = synthesize(text, args.out, backend=args.backend,
                         voice=args.voice, lang=args.lang)
    if not ok:
        return 1
    size = os.path.getsize(args.out)
    print(f"TTS_OK:{args.out}:{size}:{dur if dur is not None else -1}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
