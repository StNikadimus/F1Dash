#!/usr/bin/env python3
"""OPTIONAL: AI transcripts for the TEAM RADIO panel, made on a PC with a GPU (faster-whisper).

The dashboard never needs this - without it the panel just has no transcripts. Run it next to (or on)
the dashboard server while a session is shown; it asks the server which clips exist, downloads each MP3
through the server (the same /api/radio/audio/<id> the dashboard plays), transcribes it locally and
sends the text back. The dashboard marks it "AI" (machine transcription, may be wrong) - never official.

    pip install faster-whisper                          # + NVIDIA CUDA 12 / cuDNN 9 for the GPU
    python tools/transcribe_radio.py --server http://192.168.1.10:8080 --token <[remote] token>
    python tools/transcribe_radio.py --server http://127.0.0.1:8080 --loop 20     # keep following a live session

Models (RTX 3070 Ti, 8 GB): "large-v3" with --compute-type float16 fits (~4.5 GB) and copes best with
radio noise; "distil-large-v3" / "medium.en" are faster; "small.en" also runs on the CPU.
Authentication: the server's [remote] token (header X-Remote-Token); a server without a token accepts
only requests from its own machine. Nothing but the clip id, the text, the model name, the language and
a confidence estimate is sent.
"""
from __future__ import annotations

import argparse
import io
import math
import os
import sys
import time
from typing import Callable, Optional

import httpx

Transcriber = Callable[[bytes], tuple[str, Optional[str], Optional[float]]]


def load_whisper(model: str, device: str, compute_type: str, language: Optional[str]) -> Transcriber:
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        sys.exit("faster-whisper is not installed: pip install faster-whisper (optional; only this tool needs it)")
    wm = WhisperModel(model, device=device, compute_type=compute_type)

    def run(audio: bytes) -> tuple[str, Optional[str], Optional[float]]:
        segments, info = wm.transcribe(io.BytesIO(audio), language=language, beam_size=5, vad_filter=True,
                                       condition_on_previous_text=False)
        segs = list(segments)
        text = " ".join(s.text.strip() for s in segs).strip()
        conf = math.exp(sum(s.avg_logprob for s in segs) / len(segs)) if segs else None
        return text, getattr(info, "language", None), conf
    return run


def transcribe_pending(http: httpx.Client, transcribe: Transcriber, model: str, redo: bool = False,
                       log: Callable[[str], None] = print) -> int:
    """One pass: every playable clip of the session shown now without a transcript -> transcript. Returns
    how many were stored."""
    r = http.get("/api/radio/clips")
    if r.status_code == 401:
        raise SystemExit("the server refused the token (use the [remote] token; without one run this on the server)")
    if r.status_code == 404:
        raise SystemExit("team radio transcripts are off on the server ([team_radio] transcripts = false)")
    r.raise_for_status()
    done = 0
    for clip in r.json().get("clips") or []:
        cid = str(clip.get("id") or "")
        if not cid or (clip.get("has_transcript") and not redo):
            continue
        a = http.get(f"/api/radio/audio/{cid}")
        if a.status_code != 200:
            log(f"  {cid}: no audio ({a.status_code}: {a.text[:120]})")
            continue
        text, lang, conf = transcribe(a.content)
        if not text:
            log(f"  {cid}: no speech recognised")
            continue
        p = http.post("/api/radio/transcript", json={"id": cid, "text": text[:2000], "model": model,
                                                    "lang": lang, "confidence": conf})
        if p.status_code != 200:
            log(f"  {cid}: not stored ({p.status_code}: {p.text[:120]})")
            continue
        done += 1
        log(f"  #{clip.get('driver') or '?'} {clip.get('utc') or ''}  {text}")
    return done


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default="http://127.0.0.1:8080")
    ap.add_argument("--token", default=os.environ.get("F1DASH_REMOTE_TOKEN", ""),
                    help="the server's [remote] token (default: $F1DASH_REMOTE_TOKEN)")
    ap.add_argument("--model", default="large-v3")
    ap.add_argument("--device", default="auto", help="cuda / cpu / auto")
    ap.add_argument("--compute-type", default="default", help="float16 (GPU), int8_float16, int8 (CPU) ...")
    ap.add_argument("--language", default="en", help="radio language (\"\" = detect)")
    ap.add_argument("--loop", type=float, default=0, help="repeat every N seconds (live sessions); 0 = once")
    ap.add_argument("--redo", action="store_true", help="transcribe clips that already have a transcript again")
    a = ap.parse_args()
    transcribe = load_whisper(a.model, a.device, a.compute_type, a.language or None)
    headers = {"X-Remote-Token": a.token} if a.token else {}
    with httpx.Client(base_url=a.server.rstrip("/"), headers=headers, timeout=60) as http:
        while True:
            n = transcribe_pending(http, transcribe, a.model, a.redo)
            print(f"{n} new transcript(s)")
            if a.loop <= 0:
                break
            time.sleep(a.loop)


if __name__ == "__main__":
    main()
