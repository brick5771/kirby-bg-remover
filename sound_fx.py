"""Procedural retro-cute sound effects synthesizer for Kirby BG Remover."""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path
from typing import Dict

from PySide6.QtCore import QUrl
from PySide6.QtMultimedia import QSoundEffect

SAMPLE_RATE = 44100


def _write_wav(path: Path, samples: list[float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        frames = bytearray()
        for s in samples:
            clamped = max(-0.95, min(0.95, s))
            frames.extend(struct.pack("<h", int(clamped * 32767)))
        wf.writeframes(bytes(frames))


def _env(t: float, dur: float, attack: float = 0.01, release: float = 0.04) -> float:
    if t < 0 or t > dur:
        return 0.0
    a = min(1.0, t / max(attack, 1e-4))
    r = min(1.0, (dur - t) / max(release, 1e-4))
    return min(a, r)


def _gen_inhale() -> list[float]:
    """Cute ascending breezy vacuum swirl + playful Kirby glide."""
    dur = 0.72
    n = int(SAMPLE_RATE * dur)
    out: list[float] = []
    phase = 0.0
    phase2 = 0.0
    # Deterministic lightweight pseudo-noise for soft breeze
    seed = 1234567
    for i in range(n):
        t = i / SAMPLE_RATE
        p = t / dur
        # Rising pitch with slight bubbly vibrato
        freq = 260.0 + 620.0 * (p ** 1.5) + 28.0 * math.sin(2 * math.pi * 16.0 * t)
        phase += 2 * math.pi * freq / SAMPLE_RATE
        phase2 += 2 * math.pi * (freq * 1.5) / SAMPLE_RATE
        tone = 0.55 * math.sin(phase) + 0.18 * math.sin(phase2)

        # Soft filtered-feel airy whoosh
        seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
        noise = ((seed / 0x7FFFFFFF) * 2.0 - 1.0) * 0.10 * (0.3 + 0.7 * p)
        e = _env(t, dur, attack=0.05, release=0.08)
        out.append((tone + noise) * e * 0.42)
    return out


def _gen_gulp() -> list[float]:
    """Cute bouncy 'poyo / gulp' double-bloop when swallowed."""
    dur = 0.24
    n = int(SAMPLE_RATE * dur)
    out: list[float] = []
    phase = 0.0
    for i in range(n):
        t = i / SAMPLE_RATE
        p = t / dur
        # Quick dip then cheerful bounce upward
        if p < 0.45:
            freq = 560.0 - 220.0 * (p / 0.45)
        else:
            q = (p - 0.45) / 0.55
            freq = 360.0 + 380.0 * math.sin(q * math.pi * 0.75)
        phase += 2 * math.pi * freq / SAMPLE_RATE
        e = _env(t, dur, attack=0.012, release=0.06)
        out.append(math.sin(phase) * e * 0.46)
    return out


def _gen_popout() -> list[float]:
    """Sparkly retro pop + cheerful major-9th arpeggio when cutout pops from back."""
    dur = 0.56
    n = int(SAMPLE_RATE * dur)
    out = [0.0] * n

    # 1) Soft cork-pop burst at t=0..0.09
    pop_phase = 0.0
    for i in range(int(SAMPLE_RATE * 0.10)):
        t = i / SAMPLE_RATE
        p = t / 0.10
        freq = 220.0 + 700.0 * (1.0 - (1.0 - p) ** 2)
        pop_phase += 2 * math.pi * freq / SAMPLE_RATE
        e = _env(t, 0.10, attack=0.004, release=0.045)
        out[i] += math.sin(pop_phase) * e * 0.45

    # 2) Sparkle chime notes: C6, E6, G6, D7
    notes = [
        (0.05, 1046.5, 0.22),
        (0.11, 1318.5, 0.22),
        (0.17, 1568.0, 0.24),
        (0.23, 2093.0, 0.30),
    ]
    for start, freq, ndur in notes:
        i0 = int(start * SAMPLE_RATE)
        i1 = min(n, int((start + ndur) * SAMPLE_RATE))
        for i in range(i0, i1):
            t = (i - i0) / SAMPLE_RATE
            e = _env(t, ndur, attack=0.008, release=ndur * 0.72)
            # Soft triangle-like chime (fundamental + gentle 2nd harmonic)
            val = 0.75 * math.sin(2 * math.pi * freq * t) + 0.20 * math.sin(
                2 * math.pi * freq * 2.0 * t
            )
            out[i] += val * e * 0.26

    return out


def _gen_save() -> list[float]:
    """Crisp happy two-note confirmation chime."""
    dur = 0.34
    n = int(SAMPLE_RATE * dur)
    out = [0.0] * n
    notes = [(0.0, 880.0, 0.14), (0.09, 1318.5, 0.24)]
    for start, freq, ndur in notes:
        i0 = int(start * SAMPLE_RATE)
        i1 = min(n, int((start + ndur) * SAMPLE_RATE))
        for i in range(i0, i1):
            t = (i - i0) / SAMPLE_RATE
            e = _env(t, ndur, attack=0.006, release=ndur * 0.7)
            val = 0.8 * math.sin(2 * math.pi * freq * t) + 0.2 * math.sin(
                2 * math.pi * freq * 2.0 * t
            )
            out[i] += val * e * 0.34
    return out


def _gen_click() -> list[float]:
    """Soft bubble UI click."""
    dur = 0.065
    n = int(SAMPLE_RATE * dur)
    out: list[float] = []
    phase = 0.0
    for i in range(n):
        t = i / SAMPLE_RATE
        p = t / dur
        freq = 680.0 + 260.0 * p
        phase += 2 * math.pi * freq / SAMPLE_RATE
        e = _env(t, dur, attack=0.004, release=0.035)
        out.append(math.sin(phase) * e * 0.28)
    return out


class SoundManager:
    """Generates and plays cute procedural Kirby-themed sound effects."""

    def __init__(self, sfx_dir: Path, parent=None) -> None:
        self._sfx_dir = sfx_dir
        self._muted = False
        self._effects: Dict[str, QSoundEffect] = {}

        generators = {
            "inhale": _gen_inhale,
            "gulp": _gen_gulp,
            "popout": _gen_popout,
            "save": _gen_save,
            "click": _gen_click,
        }
        for name, gen in generators.items():
            wav_path = self._sfx_dir / f"{name}.wav"
            if not wav_path.exists():
                _write_wav(wav_path, gen())
            eff = QSoundEffect(parent)
            eff.setSource(QUrl.fromLocalFile(str(wav_path)))
            eff.setVolume(0.70)
            self._effects[name] = eff

    @property
    def muted(self) -> bool:
        return self._muted

    def set_muted(self, muted: bool) -> None:
        self._muted = muted

    def toggle_mute(self) -> bool:
        self._muted = not self._muted
        return self._muted

    def play(self, name: str) -> None:
        if self._muted:
            return
        eff = self._effects.get(name)
        if eff is None:
            return
        if eff.isPlaying():
            eff.stop()
        eff.play()
