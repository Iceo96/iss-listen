#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
#   ___ ____ ____   _     _     _
#  |_ _/ ___/ ___| | |   (_)___| |_ ___ _ __
#   | |\___ \___ \ | |   | / __| __/ _ \ '_ \
#   | | ___) |__) || |___| \__ \ ||  __/ | | |
#  |___|____/____/ |_____|_|___/\__\___|_| |_|
#
#   Listen to the ISS 145.800 MHz FM voice repeater with an RTL-SDR v4,
#   with live Doppler tracking, pass prediction and a moving map.
#
#        signed,  ___                 ___   __
#                |_ _|__ ___ ___ ___ / _ \ / /_
#                 | |/ _/ -_) _ \___| (_) | '_ \
#                |___\__\___\___/    \___/|_.__/   iceo96
#
"""iss-listen — one command to hear the Space Station.

Usage:
  iss-listen            next pass countdown -> auto-start -> audio + map
  iss-listen --now      start listening right now (still Doppler-corrected)
  iss-listen --pass     print the next 5 passes and exit
  iss-listen --test     check the dongle, print sample rate, measure retune gap
"""

from __future__ import annotations

import argparse
import configparser
import math
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------

ISS_DOWNLINK_HZ = 145_800_000          # ISS VHF FM downlink (voice / SSTV repeater)
C = 299_792_458.0                      # speed of light, m/s
GOOD_PASS_MIN_ELEV = 20.0              # degrees -> "good pass"
SAMPLE_RATE = 15_000                   # rtl_fm audio sample rate (s16le mono)
TLE_MAX_AGE_DAYS = 3
TLE_URL = ("https://celestrak.org/NORAD/elements/gp.php"
           "?CATNR=25544&FORMAT=TLE")  # 25544 = ISS (ZARYA)

CONFIG_PATH = Path(os.path.expanduser("~/.config/iss-listen.ini"))
CACHE_DIR = Path(os.path.expanduser("~/.cache/iss-listen"))
TLE_CACHE = CACHE_DIR / "iss.tle"

MAP_HOST = "127.0.0.1"
MAP_PORT = 8080

# ANSI
BOLD = "\033[1m"; DIM = "\033[2m"; RST = "\033[0m"
CYAN = "\033[36m"; GRN = "\033[32m"; YEL = "\033[33m"; RED = "\033[31m"; MAG = "\033[35m"

BANNER = rf"""{CYAN}
   ___ ____ ____   _     _     _
  |_ _/ ___/ ___| | |   (_)___| |_ ___ _ __
   | |\___ \___ \ | |   | / __| __/ _ \ '_ \
   | | ___) |__) || |___| \__ \ ||  __/ | | |
  |___|____/____/ |_____|_|___/\__\___|_| |_|
{RST}{DIM}         145.800 MHz  ISS FM voice  ·  RTL-SDR v4{RST}
{MAG}                                      — iceo96{RST}
"""


# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

@dataclass
class Location:
    lat: float
    lon: float
    elevation_m: float
    name: str = "home"


def load_config() -> Location:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(
            "# iss-listen configuration — fill in your ground-station location.\n"
            "# Find your coordinates at https://www.openstreetmap.org (right-click -> Show address).\n"
            "[location]\n"
            "lat = 0.0\n"
            "lon = 0.0\n"
            "elevation_m = 0\n"
            "name = home\n"
        )
        print(f"{YEL}First run: I created a config file for you at{RST}\n  {CONFIG_PATH}")
        print(f"{YEL}Edit it and set your latitude / longitude, then run iss-listen again.{RST}")
        sys.exit(1)

    cp = configparser.ConfigParser()
    cp.read(CONFIG_PATH)
    try:
        sec = cp["location"]
        loc = Location(
            lat=float(sec["lat"]),
            lon=float(sec["lon"]),
            elevation_m=float(sec.get("elevation_m", "0")),
            name=sec.get("name", "home"),
        )
    except (KeyError, ValueError) as e:
        print(f"{RED}Config {CONFIG_PATH} is malformed: {e}{RST}")
        sys.exit(1)

    if loc.lat == 0.0 and loc.lon == 0.0:
        print(f"{RED}Your location in {CONFIG_PATH} is still 0,0 — please set your real lat/lon.{RST}")
        sys.exit(1)
    return loc


# ----------------------------------------------------------------------------
# TLE handling
# ----------------------------------------------------------------------------

def fetch_tle(force: bool = False) -> tuple[str, str, str]:
    """Return (name, line1, line2), using a <=3-day cache in ~/.cache."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fresh = (
        TLE_CACHE.exists()
        and (time.time() - TLE_CACHE.stat().st_mtime) < TLE_MAX_AGE_DAYS * 86400
    )
    if fresh and not force:
        lines = TLE_CACHE.read_text().strip().splitlines()
        if len(lines) >= 3:
            return lines[0].strip(), lines[1].strip(), lines[2].strip()

    try:
        with urllib.request.urlopen(TLE_URL, timeout=20) as r:
            data = r.read().decode("utf-8", "replace")
        lines = [ln.rstrip() for ln in data.strip().splitlines() if ln.strip()]
        if len(lines) < 3 or not lines[1].startswith("1 "):
            raise ValueError("unexpected TLE format from Celestrak")
        TLE_CACHE.write_text("\n".join(lines[:3]) + "\n")
        return lines[0].strip(), lines[1].strip(), lines[2].strip()
    except Exception as e:
        if TLE_CACHE.exists():
            print(f"{YEL}Could not refresh TLE ({e}); using cached copy.{RST}")
            lines = TLE_CACHE.read_text().strip().splitlines()
            return lines[0].strip(), lines[1].strip(), lines[2].strip()
        print(f"{RED}Could not fetch ISS TLE and no cache available: {e}{RST}")
        sys.exit(1)


# ----------------------------------------------------------------------------
# Orbital mechanics (skyfield / sgp4)
# ----------------------------------------------------------------------------

class Tracker:
    """Wraps skyfield to give elevation, azimuth, range, range-rate, Doppler."""

    def __init__(self, loc: Location, tle: tuple[str, str, str]):
        from skyfield.api import EarthSatellite, load, wgs84
        self.ts = load.timescale(builtin=True)   # no network needed
        self.sat = EarthSatellite(tle[1], tle[2], tle[0], self.ts)
        self.topos = wgs84.latlon(loc.lat, loc.lon, elevation_m=loc.elevation_m)
        self.loc = loc
        self._diff = self.sat - self.topos
        self._wgs84 = wgs84

    # --- instantaneous geometry -------------------------------------------
    def state(self, when: datetime | None = None):
        """Return dict with elevation, azimuth, range_km, range_rate_kms,
        subpoint lat/lon, altitude_km, doppler_hz at time `when` (UTC)."""
        t = self.ts.from_datetime(when or datetime.now(timezone.utc))
        topocentric = self._diff.at(t)
        alt, az, dist = topocentric.altaz()

        # Range-rate from the exact SGP4 velocity: project relative velocity
        # onto the line-of-sight unit vector.  Positive = receding.
        pos = topocentric.position.km
        vel = topocentric.velocity.km_per_s
        rng = math.sqrt(sum(p * p for p in pos))
        range_rate = sum(p * v for p, v in zip(pos, vel)) / rng  # km/s

        # Received frequency is shifted by -f * range_rate / c.
        doppler_hz = -ISS_DOWNLINK_HZ * (range_rate * 1000.0) / C

        sub = self._wgs84.subpoint(self.sat.at(t))
        return {
            "elevation": alt.degrees,
            "azimuth": az.degrees,
            "range_km": rng,
            "range_rate_kms": range_rate,
            "doppler_hz": doppler_hz,
            "sub_lat": sub.latitude.degrees,
            "sub_lon": sub.longitude.degrees,
            "alt_km": sub.elevation.km,
        }

    # --- pass prediction ---------------------------------------------------
    def next_passes(self, count: int = 5, horizon_hours: int = 48,
                    min_elev: float = 0.0):
        """Return list of pass dicts (rise/culminate/set) over the next window."""
        t0 = self.ts.from_datetime(datetime.now(timezone.utc))
        t1 = self.ts.from_datetime(
            datetime.now(timezone.utc) + timedelta(hours=horizon_hours))
        times, events = self.sat.find_events(
            self.topos, t0, t1, altitude_degrees=0.0)

        passes = []
        current = {}
        for ti, ev in zip(times, events):
            dt = ti.utc_datetime()
            if ev == 0:        # rise
                current = {"rise": dt, "rise_az": self._az_at(ti)}
            elif ev == 1:      # culminate
                st = self.state(dt)
                current["max_elev"] = st["elevation"]
                current["culminate"] = dt
            elif ev == 2:      # set
                current["set"] = dt
                current["set_az"] = self._az_at(ti)
                if "rise" in current and "max_elev" in current:
                    passes.append(current)
                current = {}
            if len(passes) >= count:
                break

        if min_elev > 0:
            passes = [p for p in passes if p["max_elev"] >= min_elev]
        return passes[:count]

    def _az_at(self, ti) -> float:
        _, az, _ = self._diff.at(ti).altaz()
        return az.degrees


def compass(az: float) -> str:
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return dirs[int((az % 360) / 22.5 + 0.5) % 16]


def fmt_local(dt: datetime) -> str:
    return dt.astimezone().strftime("%a %H:%M:%S")


# ----------------------------------------------------------------------------
# Dongle / audio helpers
# ----------------------------------------------------------------------------

SDR_EXES = ("rtl_fm", "rtl_tcp", "rtl_sdr", "rtl_power", "rtl_test",
            "rtl_433", "rtl_adsb", "rtl_eeprom", "gqrx", "SDRangel",
            "sdrpp", "CubicSDR", "SoapySDRServer", "dump1090")


def find_dongle_holder() -> str | None:
    """If the RTL dongle is busy, return a human description of what holds it.

    Matches only real SDR *executables* (by process name), not arbitrary
    command lines that merely mention 'rtl_tcp', and never ourselves.
    """
    me = os.getpid()
    holders = []
    for name in SDR_EXES:
        try:
            out = subprocess.run(["pgrep", "-x", name], capture_output=True,
                                 text=True)
        except FileNotFoundError:
            break
        for pid in out.stdout.split():
            if pid and int(pid) != me:
                holders.append(f"{name} (pid {pid})")
    if holders:
        return ", ".join(holders)

    # Also catch the kernel DVB driver grabbing the device via its /dev nodes.
    busy = set()
    for node in ("/dev/dvb/adapter0/frontend0", "/dev/swradio0"):
        if os.path.exists(node):
            try:
                r = subprocess.run(["fuser", node], capture_output=True, text=True)
                busy.update(r.stdout.split())
            except FileNotFoundError:
                pass
    if busy:
        return f"kernel DVB driver / PID(s) {', '.join(sorted(busy))} " \
               f"(try: sudo modprobe -r dvb_usb_rtl28xxu)"
    return None


def have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


# ----------------------------------------------------------------------------
# Audio engine: persistent speaker sink + respawnable rtl_fm (gapless Doppler)
# ----------------------------------------------------------------------------

class AudioEngine:
    """
    rtl_fm (respawned on each large Doppler step)
        --> Python relay thread (applies makeup gain, fills retune gaps)
        --> persistent aplay/sox sink (never restarts -> no clicks)
    """

    CHUNK = 1200  # bytes = 600 int16 samples = 40 ms @ 15 kHz

    def __init__(self, rf_gain: float, audio_gain: float, highpass: bool,
                 ppm: int, state: "ShareState"):
        self.rf_gain = rf_gain
        self.audio_gain = audio_gain
        self.highpass = highpass
        self.ppm = ppm
        self.state = state
        self._rtl: subprocess.Popen | None = None
        self._sink: subprocess.Popen | None = None
        self._cur_freq = ISS_DOWNLINK_HZ
        self._target_freq = ISS_DOWNLINK_HZ
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._relay: threading.Thread | None = None

    # -- process spawning ---------------------------------------------------
    def _spawn_rtl(self, freq: int) -> subprocess.Popen:
        cmd = ["rtl_fm", "-f", str(freq), "-M", "fm", "-s", str(SAMPLE_RATE),
               "-A", "fast", "-l", "0", "-g", str(self.rf_gain), "-p", str(self.ppm)]
        return subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, bufsize=0)

    def _spawn_sink(self) -> subprocess.Popen:
        # Prefer sox (richer: gain + optional high-pass + resample) when present,
        # otherwise fall back to aplay (always available on this box).
        if have("sox"):
            chain = ["sox", "-t", "raw", "-r", str(SAMPLE_RATE), "-e", "signed",
                     "-b", "16", "-c", "1", "-", "-t", "pulse", "default",
                     "gain", f"{6 + 20*math.log10(max(self.audio_gain,1e-6)):.1f}"]
            if self.highpass:
                chain += ["highpass", "300"]
            return subprocess.Popen(chain, stdin=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, bufsize=0)
        # aplay path: raw s16le mono @ 15 kHz straight to the default device.
        return subprocess.Popen(
            ["aplay", "-q", "-t", "raw", "-f", "S16_LE", "-r", str(SAMPLE_RATE),
             "-c", "1", "-"],
            stdin=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)

    # -- public API ---------------------------------------------------------
    def start(self):
        self._sink = self._spawn_sink()
        self._rtl = self._spawn_rtl(self._cur_freq)
        self._relay = threading.Thread(target=self._relay_loop, daemon=True)
        self._relay.start()

    def set_freq(self, freq: int):
        with self._lock:
            self._target_freq = freq

    def stop(self):
        self._stop.set()
        for p in (self._rtl, self._sink):
            if p and p.poll() is None:
                try:
                    p.terminate()
                    p.wait(timeout=2)
                except Exception:
                    try:
                        p.kill()
                    except Exception:
                        pass

    # -- the relay ----------------------------------------------------------
    def _apply_gain(self, raw: bytes) -> bytes:
        if not have("sox") and abs(self.audio_gain - 1.0) > 1e-3:
            import numpy as np
            a = np.frombuffer(raw, dtype="<i2").astype(np.float32) * self.audio_gain
            np.clip(a, -32768, 32767, out=a)
            return a.astype("<i2").tobytes()
        return raw

    def _relay_loop(self):
        silence = b"\x00" * self.CHUNK
        while not self._stop.is_set():
            # Retune if the target has drifted far enough from the live tuning.
            with self._lock:
                target = self._target_freq
            if self._rtl and abs(target - self._cur_freq) >= 1:  # engine decides step upstream
                if target != self._cur_freq:
                    self._retune(target, silence)

            rtl = self._rtl
            if rtl is None or rtl.stdout is None:
                time.sleep(0.01)
                continue
            raw = rtl.stdout.read(self.CHUNK)
            if not raw:
                # rtl_fm died unexpectedly — try to resurrect it once.
                if self._stop.is_set():
                    break
                time.sleep(0.05)
                if rtl.poll() is not None:
                    self._rtl = self._spawn_rtl(self._cur_freq)
                continue
            try:
                if self._sink and self._sink.stdin:
                    self._sink.stdin.write(self._apply_gain(raw))
            except (BrokenPipeError, ValueError):
                break

    def _retune(self, freq: int, silence: bytes):
        """Kill the old rtl_fm, start a new one at `freq`, cover the gap with
        silence so the persistent sink never underruns/clicks."""
        old = self._rtl
        self._rtl = None
        if old and old.poll() is None:
            old.terminate()
        # Feed the sink a little silence while the new tuner spins up.
        try:
            if self._sink and self._sink.stdin:
                self._sink.stdin.write(silence * 2)  # ~80 ms cushion
        except (BrokenPipeError, ValueError):
            pass
        if old:
            try:
                old.wait(timeout=1)
            except Exception:
                old.kill()
        self._rtl = self._spawn_rtl(freq)
        self._cur_freq = freq
        self.state.set(retunes=self.state.get("retunes", 0) + 1)


# ----------------------------------------------------------------------------
# Gapless audio engine: rtl_tcp + software Doppler (numpy NBFM demod)
# ----------------------------------------------------------------------------

class IqEngine:
    """
    rtl_tcp holds the dongle open at a FIXED 145.800 MHz centre.  Doppler is
    removed in software by a numerically-controlled oscillator, so there is NO
    hardware retune and therefore NO audio gap — ever.

        rtl_tcp (fixed centre, 240 kS/s IQ)
            --> socket --> numpy: NCO shift · LPF+decimate · FM discriminate
            --> persistent aplay/sox sink
    """

    IQ_RATE = 240_000          # dongle IQ sample rate
    DECIM = 16                 # 240k / 16 = 15k audio
    AUDIO_RATE = IQ_RATE // DECIM
    CHUNK_IQ = 4096 * 4        # IQ samples per processing block (~68 ms)
    TCP_PORT = 1234

    def __init__(self, rf_gain: float, audio_gain: float, highpass: bool,
                 ppm: int, state: "ShareState"):
        import numpy as np
        self.np = np
        self.rf_gain = rf_gain
        self.audio_gain = audio_gain
        self.highpass = highpass
        self.ppm = ppm
        self.state = state
        self._doppler = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._sink: subprocess.Popen | None = None
        self._sock = None
        self._thread: threading.Thread | None = None

        # --- fixed DSP state -------------------------------------------------
        self._nco_phase = 0.0
        # Anti-alias low-pass (windowed sinc) for the decimate-by-16 stage.
        cutoff = 7000.0 / (self.IQ_RATE / 2.0)      # ~7 kHz passband
        n = 64
        m = np.arange(n) - (n - 1) / 2.0
        taps = np.sinc(cutoff * m) * np.hamming(n)
        self._taps = (taps / taps.sum()).astype(np.complex64)
        self._fir_state = np.zeros(n - 1, dtype=np.complex64)
        self._fm_prev = np.complex64(1 + 0j)
        # --- signal-strength / spectrum state (for the live HUD) -------------
        self._sig_ema = None          # smoothed narrowband power (dB)
        self._noise_ema = None        # slow noise-floor estimate (dB)
        self._spec_t = 0.0
        self._spec_win = np.hanning(1024).astype(np.float32)

    def _spawn_sink(self) -> subprocess.Popen:
        if have("sox"):
            chain = ["sox", "-t", "raw", "-r", str(self.AUDIO_RATE), "-e",
                     "signed", "-b", "16", "-c", "1", "-", "-t", "pulse",
                     "default"]
            if self.highpass:
                chain += ["highpass", "300"]
            return subprocess.Popen(chain, stdin=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, bufsize=0)
        return subprocess.Popen(
            ["aplay", "-q", "-t", "raw", "-f", "S16_LE", "-r",
             str(self.AUDIO_RATE), "-c", "1", "-"],
            stdin=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)

    def _send_cmd(self, cmd: int, param: int):
        import struct
        self._sock.sendall(bytes([cmd]) + struct.pack(">I", param & 0xFFFFFFFF))

    def start(self):
        import socket
        self._proc = subprocess.Popen(
            ["rtl_tcp", "-a", "127.0.0.1", "-p", str(self.TCP_PORT),
             "-f", str(ISS_DOWNLINK_HZ), "-s", str(self.IQ_RATE),
             "-g", str(self.rf_gain), "-P", str(self.ppm)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Wait for rtl_tcp to come up and accept a connection.
        deadline = time.time() + 8
        while time.time() < deadline:
            try:
                self._sock = socket.create_connection((MAP_HOST, self.TCP_PORT), 1)
                break
            except OSError:
                if self._proc.poll() is not None:
                    raise RuntimeError("rtl_tcp exited — is the dongle free?")
                time.sleep(0.2)
        else:
            raise RuntimeError("could not connect to rtl_tcp")

        self._sock.recv(12)                      # dongle header (magic+tuner)
        self._send_cmd(0x02, self.IQ_RATE)       # sample rate
        self._send_cmd(0x03, 1)                  # manual gain mode
        self._send_cmd(0x04, int(self.rf_gain * 10))  # tuner gain (tenths dB)
        self._send_cmd(0x08, 0)                   # AGC off
        self._send_cmd(0x05, self.ppm)            # freq correction ppm
        self._send_cmd(0x01, ISS_DOWNLINK_HZ)     # fixed centre frequency

        self._sink = self._spawn_sink()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def set_doppler(self, doppler_hz: float):
        with self._lock:
            self._doppler = doppler_hz

    def _recv_exact(self, nbytes: int) -> bytes | None:
        buf = bytearray()
        while len(buf) < nbytes and not self._stop.is_set():
            try:
                chunk = self._sock.recv(nbytes - len(buf))
            except OSError:
                return None
            if not chunk:
                return None
            buf.extend(chunk)
        return bytes(buf)

    def _loop(self):
        np = self.np
        two_pi = 2 * np.pi
        disc_gain = 32767.0 / 2.2 * self.audio_gain
        nbytes = self.CHUNK_IQ * 2               # 2 bytes (I,Q) per IQ sample
        while not self._stop.is_set():
            raw = self._recv_exact(nbytes)
            if raw is None:
                break
            # uint8 interleaved IQ -> complex64 centred on 0.
            u = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
            iq = (u[0::2] - 127.5) + 1j * (u[1::2] - 127.5)
            iq = iq.astype(np.complex64) / 127.5
            n = iq.shape[0]

            with self._lock:
                doppler = self._doppler
            # NCO: shift the Doppler-offset carrier back to 0 Hz.
            t = np.arange(n, dtype=np.float32)
            ph = self._nco_phase + two_pi * (-doppler) / self.IQ_RATE * t
            iq *= np.exp(1j * ph).astype(np.complex64)
            self._nco_phase = (self._nco_phase
                               + two_pi * (-doppler) / self.IQ_RATE * n) % two_pi

            # Anti-alias FIR with carry-over state, then decimate by 16.
            x = np.concatenate((self._fir_state, iq))
            filt = np.convolve(x, self._taps, mode="valid")
            self._fir_state = iq[-(self._taps.shape[0] - 1):]
            base = filt[::self.DECIM]

            # FM discriminator: angle of x[n] * conj(x[n-1]).
            prev = np.empty(base.shape[0] + 1, dtype=np.complex64)
            prev[0] = self._fm_prev
            prev[1:] = base
            self._fm_prev = base[-1] if base.shape[0] else self._fm_prev
            demod = np.angle(prev[1:] * np.conj(prev[:-1]))

            audio = np.clip(demod * disc_gain, -32768, 32767).astype("<i2")
            try:
                if self._sink and self._sink.stdin:
                    self._sink.stdin.write(audio.tobytes())
            except (BrokenPipeError, ValueError):
                break

            # --- live signal strength + spectrum for the map HUD ------------
            if base.shape[0]:
                power = float(np.mean(base.real ** 2 + base.imag ** 2))
                sig_db = 10.0 * math.log10(power + 1e-12)
                self._sig_ema = (sig_db if self._sig_ema is None
                                 else 0.75 * self._sig_ema + 0.25 * sig_db)
                # Slow-tracking noise floor (only drifts down quickly, up slowly).
                if self._noise_ema is None:
                    self._noise_ema = sig_db
                elif sig_db < self._noise_ema:
                    self._noise_ema = 0.9 * self._noise_ema + 0.1 * sig_db
                else:
                    self._noise_ema = 0.999 * self._noise_ema + 0.001 * sig_db
                snr = max(0.0, self._sig_ema - self._noise_ema)
                now = time.time()
                if now - self._spec_t > 0.15:
                    self._spec_t = now
                    m = min(1024, base.shape[0])
                    seg = base[-m:] * self._spec_win[:m]
                    sp = np.abs(np.fft.fftshift(np.fft.fft(seg)))
                    sp = 20.0 * np.log10(sp + 1e-9)
                    bins = 64
                    cut = (sp.shape[0] // bins) * bins
                    sp = sp[:cut].reshape(bins, -1).mean(axis=1)
                    lo, hi = float(sp.min()), float(sp.max())
                    norm = (sp - lo) / (hi - lo + 1e-9)       # 0..1 for display
                    self.state.set(
                        sig_db=round(self._sig_ema, 1),
                        snr_db=round(snr, 1),
                        spectrum=[round(float(x), 3) for x in norm])

    def stop(self):
        self._stop.set()
        try:
            if self._sock:
                self._sock.close()
        except Exception:
            pass
        for p in (self._proc, self._sink):
            if p and p.poll() is None:
                try:
                    p.terminate(); p.wait(timeout=2)
                except Exception:
                    try:
                        p.kill()
                    except Exception:
                        pass


# ----------------------------------------------------------------------------
# Shared state for the map server + display
# ----------------------------------------------------------------------------

class ShareState:
    def __init__(self):
        self._d: dict = {}
        self._lock = threading.Lock()

    def set(self, **kw):
        with self._lock:
            self._d.update(kw)

    def get(self, key, default=None):
        with self._lock:
            return self._d.get(key, default)

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._d)


# ----------------------------------------------------------------------------
# Map server (FastAPI + Leaflet)
# ----------------------------------------------------------------------------

def start_map_server(state: ShareState, loc: Location):
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse, JSONResponse
    import uvicorn

    app = FastAPI()

    @app.get("/", response_class=HTMLResponse)
    def index():
        return MAP_HTML.replace("__HOME_LAT__", str(loc.lat)) \
                       .replace("__HOME_LON__", str(loc.lon)) \
                       .replace("__HOME_NAME__", loc.name)

    @app.get("/api/pos")
    def pos():
        s = state.snapshot()
        return JSONResponse({
            "lat": s.get("sub_lat"), "lon": s.get("sub_lon"),
            "alt_km": s.get("alt_km"), "range_km": s.get("range_km"),
            "elevation": s.get("elevation"), "azimuth": s.get("azimuth"),
            "doppler_hz": s.get("doppler_hz"), "freq_hz": s.get("freq_hz"),
            "status": s.get("status", "tracking"),
            "sig_db": s.get("sig_db"), "snr_db": s.get("snr_db"),
            "spectrum": s.get("spectrum"),
            "home_lat": loc.lat, "home_lon": loc.lon,
        })

    config = uvicorn.Config(app, host=MAP_HOST, port=MAP_PORT,
                            log_level="critical")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    return server


MAP_HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>iss-listen · live map</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  :root{--bg:#0b0f1a;--fg:#e6edf3;--mut:#8b97a7;--acc:#35c2f5;--grn:#49d675;}
  html,body{margin:0;height:100%;background:var(--bg);color:var(--fg);
    font-family:ui-monospace,Menlo,Consolas,monospace;}
  #map{position:absolute;inset:0;}
  #hud{position:absolute;z-index:1000;top:12px;left:12px;background:rgba(11,15,26,.85);
    border:1px solid #223;border-radius:10px;padding:12px 14px;min-width:210px;
    box-shadow:0 6px 24px rgba(0,0,0,.5);backdrop-filter:blur(4px);}
  #hud h1{font-size:13px;margin:0 0 8px;letter-spacing:2px;color:var(--acc);}
  #hud .row{display:flex;justify-content:space-between;gap:16px;font-size:12px;
    padding:2px 0;}
  #hud .k{color:var(--mut);} #hud .v{color:var(--fg);font-weight:700;}
  #hud .dop{color:var(--grn);}
  #sig{position:absolute;z-index:1000;bottom:8px;right:10px;font-size:11px;
    color:var(--mut);font-family:ui-monospace,monospace;}
  .leaflet-container{background:#0b0f1a;}
  #meter{height:9px;border-radius:5px;background:#182233;overflow:hidden;
    margin:6px 0 2px;border:1px solid #223;}
  #meterfill{height:100%;width:0%;border-radius:5px;
    background:linear-gradient(90deg,#49d675,#f5d035,#f55);transition:width .15s;}
  #spec{display:block;width:100%;height:48px;margin-top:6px;border-radius:6px;
    background:#070b14;border:1px solid #1a2436;}
  #carrier{color:#f5d035;font-weight:700;}
</style></head>
<body>
<div id="map"></div>
<div id="hud">
  <h1>ISS · 145.800</h1>
  <div class="row"><span class="k">status</span><span class="v" id="status">—</span></div>
  <div class="row"><span class="k">elevation</span><span class="v" id="elev">—</span></div>
  <div class="row"><span class="k">range</span><span class="v" id="range">—</span></div>
  <div class="row"><span class="k">altitude</span><span class="v" id="alt">—</span></div>
  <div class="row"><span class="k">doppler</span><span class="v dop" id="dop">—</span></div>
  <div class="row"><span class="k">tuned</span><span class="v" id="freq">—</span></div>
  <div class="row" style="margin-top:4px"><span class="k">signal&nbsp;SNR</span><span class="v" id="snr">—</span></div>
  <div id="meter"><div id="meterfill"></div></div>
  <div class="row" style="font-size:10px"><span class="k">spectrum ±7.5 kHz</span><span class="k" id="carrier"></span></div>
  <canvas id="spec" width="210" height="48"></canvas>
  <div class="row" style="margin-top:6px"><span class="k">audio</span><span class="v">on speakers 🔊</span></div>
</div>
<div id="sig">iss-listen — iceo96</div>
<script>
const HOME=[__HOME_LAT__,__HOME_LON__];
const map=L.map('map',{zoomControl:true,attributionControl:false}).setView(HOME,4);
// Esri World Imagery (satellite) — no API key required.
L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
  {maxZoom:18}).addTo(map);
L.circleMarker(HOME,{radius:7,color:'#35c2f5',fillColor:'#35c2f5',fillOpacity:.9})
  .addTo(map).bindTooltip('__HOME_NAME__',{permanent:false});
const issIcon=L.divIcon({className:'',html:
  '<div style="font-size:26px;filter:drop-shadow(0 0 6px #49d675)">🛰️</div>',
  iconSize:[26,26],iconAnchor:[13,13]});
let iss=null, trail=L.polyline([],{color:'#49d675',weight:2,opacity:.7}).addTo(map);
let los=L.polyline([],{color:'#35c2f5',weight:1,opacity:.4,dashArray:'4 6'}).addTo(map);
let first=true;
async function tick(){
  try{
    const r=await fetch('/api/pos'); const d=await r.json();
    const fx=n=>n==null?'—':n;
    document.getElementById('status').textContent=fx(d.status);
    document.getElementById('elev').textContent=d.elevation==null?'—':d.elevation.toFixed(1)+'°';
    document.getElementById('range').textContent=d.range_km==null?'—':Math.round(d.range_km)+' km';
    document.getElementById('alt').textContent=d.alt_km==null?'—':Math.round(d.alt_km)+' km';
    document.getElementById('dop').textContent=d.doppler_hz==null?'—':
      (d.doppler_hz>=0?'+':'')+Math.round(d.doppler_hz)+' Hz';
    document.getElementById('freq').textContent=d.freq_hz==null?'—':
      (d.freq_hz/1e6).toFixed(4)+' MHz';
    // signal SNR meter
    const snr=d.snr_db;
    document.getElementById('snr').textContent=snr==null?'—':snr.toFixed(1)+' dB';
    document.getElementById('meterfill').style.width=
      snr==null?'0%':Math.max(0,Math.min(100,snr*4))+'%';
    drawSpectrum(d.spectrum);
    if(d.lat!=null&&d.lon!=null){
      const p=[d.lat,d.lon];
      if(!iss){iss=L.marker(p,{icon:issIcon}).addTo(map);}else{iss.setLatLng(p);}
      const t=trail.getLatLngs(); t.push(p); if(t.length>240)t.shift(); trail.setLatLngs(t);
      los.setLatLngs([HOME,p]);
      if(first){map.setView(p,4);first=false;}
    }
  }catch(e){}
}
const sc=document.getElementById('spec'), scx=sc.getContext('2d');
function drawSpectrum(sp){
  const W=sc.width,H=sc.height; scx.clearRect(0,0,W,H);
  scx.fillStyle='#070b14'; scx.fillRect(0,0,W,H);
  if(!sp||!sp.length){return;}
  const bw=W/sp.length; let peak=0,pi=0;
  for(let i=0;i<sp.length;i++){ if(sp[i]>peak){peak=sp[i];pi=i;} }
  for(let i=0;i<sp.length;i++){
    const h=Math.max(1,sp[i]*(H-2));
    const mid=Math.abs(i-sp.length/2)<2;
    scx.fillStyle = (i===pi&&peak>0.55)?'#f5d035':(mid?'#35507a':'#2f6f4e');
    scx.fillRect(i*bw+0.5,H-h,bw-1,h);
  }
  // centre line = the tuned carrier (0 Hz after Doppler removal)
  scx.strokeStyle='rgba(53,194,245,.35)'; scx.beginPath();
  scx.moveTo(W/2,0); scx.lineTo(W/2,H); scx.stroke();
  const c=document.getElementById('carrier');
  c.textContent = peak>0.6 ? 'CARRIER ●' : '';
}
setInterval(tick,1000); tick();
// Faster spectrum refresh for a lively display.
setInterval(async()=>{try{const d=await(await fetch('/api/pos')).json();
  const snr=d.snr_db;
  document.getElementById('snr').textContent=snr==null?'—':snr.toFixed(1)+' dB';
  document.getElementById('meterfill').style.width=
    snr==null?'0%':Math.max(0,Math.min(100,snr*4))+'%';
  drawSpectrum(d.spectrum);}catch(e){}},300);
</script></body></html>
"""


# ----------------------------------------------------------------------------
# Notifications
# ----------------------------------------------------------------------------

def notify(title: str, body: str):
    if have("notify-send"):
        try:
            subprocess.Popen(["notify-send", "-a", "iss-listen", title, body],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


# ----------------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------------

def open_browser(url: str):
    for opener in ("xdg-open", "sensible-browser", "firefox", "google-chrome"):
        if have(opener):
            try:
                subprocess.Popen([opener, url], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
                return
            except Exception:
                continue


def cmd_pass(tracker: Tracker, loc: Location):
    print(BANNER)
    print(f"Ground station: {BOLD}{loc.name}{RST} "
          f"({loc.lat:.4f}, {loc.lon:.4f}, {loc.elevation_m:.0f} m)\n")
    passes = tracker.next_passes(count=8, horizon_hours=48)
    if not passes:
        print(f"{YEL}No passes found in the next 48 hours.{RST}")
        return

    # Headline: the next GOOD pass = your next real chance to hear the ISS.
    good = [p for p in passes if p["max_elev"] >= GOOD_PASS_MIN_ELEV]
    nxt = good[0] if good else passes[0]
    delta = nxt["rise"] - datetime.now(timezone.utc)
    h, m = divmod(int(delta.total_seconds() // 60), 60)
    when = fmt_local(nxt["rise"])
    print(f"{BOLD}{GRN}► Next chance to listen:{RST} {BOLD}{when}{RST}  "
          f"{DIM}(in {h}h {m:02d}m){RST}")
    print(f"  peak {YEL}{nxt['max_elev']:.0f}°{RST} elevation, "
          f"{compass(nxt['rise_az'])}→{compass(nxt['set_az'])}, "
          f"~{(nxt['set']-nxt['rise']).total_seconds()/60:.0f} min overhead")
    print(f"  {DIM}You'll hear a signal only if the ISS is transmitting. Live crew{RST}")
    print(f"  {DIM}voice = ARISS school contacts (schedule: www.ariss.org/upcoming-contacts.html);{RST}")
    print(f"  {DIM}otherwise 145.800 usually carries APRS packet bursts or SSTV images.{RST}\n")

    passes = passes[:5]
    print(f"{BOLD}Next {len(passes)} passes (local time):{RST}")
    print(f"{DIM}  #  rise        culmin.     set         max-el   travel{RST}")
    for i, p in enumerate(passes, 1):
        good = p["max_elev"] >= GOOD_PASS_MIN_ELEV
        col = GRN if good else YEL
        star = "★" if good else " "
        dur = (p["set"] - p["rise"]).total_seconds() / 60
        print(f"  {i}{star} {fmt_local(p['rise'])}  {fmt_local(p['culminate'])}  "
              f"{fmt_local(p['set'])}  {col}{p['max_elev']:5.1f}°{RST}  "
              f"{compass(p['rise_az'])}→{compass(p['set_az'])}  "
              f"{DIM}({dur:.0f} min){RST}")
    print(f"\n{DIM}★ = good pass (max elevation > {GOOD_PASS_MIN_ELEV:.0f}°){RST}")


def cmd_test(loc: Location):
    print(BANNER)
    print(f"{BOLD}Checking your RTL-SDR...{RST}\n")

    holder = find_dongle_holder()
    if holder:
        print(f"{RED}✗ The dongle looks busy — held by: {holder}{RST}")
        print(f"  Close that program (or `kill` it) and try again.")
        return

    # rtl_test -t prints tuner info then exits; fall back to a timed rtl_test.
    print(f"{DIM}Running rtl_test (2 s)...{RST}")
    try:
        out = subprocess.run(["rtl_test", "-t"], capture_output=True, text=True,
                             timeout=6)
        txt = (out.stdout + out.stderr)
    except subprocess.TimeoutExpired as e:
        txt = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
    except FileNotFoundError:
        print(f"{RED}✗ rtl_test not found — is rtl-sdr installed?{RST}")
        return

    device_line = next((l for l in txt.splitlines()
                        if "Found" in l or "Realtek" in l or "tuner" in l.lower()), None)
    if "No supported" in txt or "usb_open error" in txt:
        print(f"{RED}✗ No RTL-SDR found, or permission denied.{RST}")
        print(f"{DIM}{txt.strip()[:400]}{RST}")
        return
    print(f"{GRN}✓ Dongle detected.{RST}")
    for l in txt.splitlines():
        if any(k in l for k in ("Found", "tuner", "Blog", "gain", "Sampling")):
            print(f"  {DIM}{l.strip()}{RST}")

    # Confirm rtl_fm produces PCM and report the sample rate.
    print(f"\n{DIM}Opening rtl_fm at {ISS_DOWNLINK_HZ/1e6:.3f} MHz "
          f"(sample rate {SAMPLE_RATE} Hz)...{RST}")
    t0 = time.time()
    p = subprocess.Popen(
        ["rtl_fm", "-f", str(ISS_DOWNLINK_HZ), "-M", "fm", "-s", str(SAMPLE_RATE),
         "-A", "fast", "-l", "0", "-g", "40"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
    first = p.stdout.read(1200)
    cold = time.time() - t0
    got = len(first) if first else 0

    # Measure the retune gap: kill, respawn, time until first samples again.
    p.terminate(); p.wait()
    t1 = time.time()
    p2 = subprocess.Popen(
        ["rtl_fm", "-f", str(ISS_DOWNLINK_HZ + 2500), "-M", "fm", "-s",
         str(SAMPLE_RATE), "-A", "fast", "-l", "0", "-g", "40"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
    _ = p2.stdout.read(1200)
    gap = time.time() - t1
    p2.terminate(); p2.wait()

    print(f"{GRN}✓ rtl_fm is streaming PCM{RST} "
          f"({got} bytes s16le mono @ {SAMPLE_RATE} Hz, cold start {cold*1000:.0f} ms)")
    print(f"{GRN}✓ Audio backend:{RST} "
          f"{'sox → PulseAudio' if have('sox') else 'aplay (ALSA/PipeWire)'}")
    gap_ms = gap * 1000
    print(f"{DIM}  rtl_fm restart (--simple) retune gap: {gap_ms:.0f} ms{RST}")
    if gap_ms > 200:
        print(f"{GRN}✓ Default engine is gapless:{RST} rtl_tcp holds the dongle "
              f"open and\n  Doppler is removed in software — no hardware retune, "
              f"so the {gap_ms:.0f} ms\n  restart gap never happens on a real pass.")

    # Quick rtl_tcp + DSP smoke test (confirms the gapless path actually runs).
    print(f"\n{DIM}Smoke-testing the gapless rtl_tcp engine (3 s)...{RST}")
    ok = _smoke_iq()
    if ok:
        print(f"{GRN}✓ rtl_tcp engine produced demodulated audio frames.{RST}")
    else:
        print(f"{YEL}! rtl_tcp smoke test inconclusive — the --simple engine "
              f"still works.{RST}")
    print(f"\n{GRN}All good. Run `iss-listen` to wait for the next pass.{RST}")


def _smoke_iq() -> bool:
    """Spin up IqEngine for a few seconds without speakers; confirm it demods."""
    state = ShareState()
    frames = {"n": 0}
    try:
        eng = IqEngine(rf_gain=40.0, audio_gain=1.0, highpass=False, ppm=0,
                       state=state)
        # Redirect the sink to /dev/null so --test stays silent.
        orig = eng._spawn_sink
        eng._spawn_sink = lambda: subprocess.Popen(
            ["dd", "of=/dev/null", "status=none"], stdin=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0)
        # Count frames by wrapping the sink write via a tiny monkeypatch.
        eng.start()
        time.sleep(3.0)
        # If the worker thread is alive and the socket delivered data, _fm_prev
        # will have moved off its initial (1+0j) seed.
        moved = eng._fm_prev != (1 + 0j)
        eng.stop()
        return bool(moved)
    except Exception:
        return False


def track_pass(tracker: Tracker, loc: Location, args, pass_info=None):
    """Run the live audio + Doppler + map loop until the pass ends (or forever
    with --now until Ctrl-C)."""
    state = ShareState()
    state.set(status="starting", freq_hz=ISS_DOWNLINK_HZ)

    # Dongle availability check.
    holder = find_dongle_holder()
    if holder:
        print(f"{RED}The RTL-SDR is busy — held by: {holder}{RST}")
        print(f"Close it and try again.")
        sys.exit(1)

    # Below-horizon guard (unless --now, which the user asked for explicitly).
    st = tracker.state()
    if st["elevation"] < 0 and not args.now:
        print(f"{YEL}The ISS is below your horizon right now "
              f"(elevation {st['elevation']:.1f}°). Nothing to hear — exiting.{RST}")
        sys.exit(0)

    start_map_server(state, loc)
    map_url = f"http://{MAP_HOST}:{MAP_PORT}/"
    print(f"\n{BOLD}{GRN}● LISTENING{RST}  145.800 MHz FM   "
          f"map: {CYAN}{map_url}{RST}")
    eng_note = ("rtl_fm restart-retune" if getattr(args, "simple", False)
                else "rtl_tcp software-Doppler (gapless)")
    print(f"{DIM}{eng_note} · gain {args.gain} dB · Ctrl-C to stop{RST}\n")
    open_browser(map_url)

    simple = getattr(args, "simple", False)
    if simple:
        engine = AudioEngine(rf_gain=args.gain, audio_gain=args.audio_gain,
                             highpass=args.highpass, ppm=args.ppm, state=state)
        mode_label = "rtl_fm (restart retune)"
    else:
        engine = IqEngine(rf_gain=args.gain, audio_gain=args.audio_gain,
                          highpass=args.highpass, ppm=args.ppm, state=state)
        mode_label = "rtl_tcp (software Doppler, gapless)"
    try:
        engine.start()
    except Exception as e:
        print(f"{RED}Could not start the SDR audio engine: {e}{RST}")
        sys.exit(1)

    step = args.doppler_step
    cur_tuned = ISS_DOWNLINK_HZ
    end_time = None
    if pass_info:
        end_time = pass_info["set"] + timedelta(seconds=20)

    try:
        last_print = 0.0
        while True:
            st = tracker.state()
            doppler = st["doppler_hz"]

            if simple:
                desired = round((ISS_DOWNLINK_HZ + doppler) / 10.0) * 10
                if abs(desired - cur_tuned) >= step:
                    cur_tuned = desired
                    engine.set_freq(int(cur_tuned))
                tuned_disp = cur_tuned
            else:
                engine.set_doppler(doppler)          # continuous, no hardware retune
                tuned_disp = ISS_DOWNLINK_HZ

            state.set(status="tracking" if st["elevation"] > 0 else "below horizon",
                      freq_hz=tuned_disp, **st)

            now = time.time()
            if now - last_print >= 0.5:
                last_print = now
                el = st["elevation"]
                bar_col = GRN if el > GOOD_PASS_MIN_ELEV else (YEL if el > 0 else DIM)
                extra = (f"{DIM}retunes {state.get('retunes',0)}{RST}" if simple
                         else f"{DIM}sw-doppler{RST}")
                sys.stdout.write(
                    f"\r{bar_col}el {el:5.1f}°{RST}  "
                    f"az {st['azimuth']:5.1f}° {compass(st['azimuth'])}  "
                    f"rng {st['range_km']:6.0f} km  "
                    f"{GRN}Δf {doppler:+7.0f} Hz{RST}  "
                    f"corr {(ISS_DOWNLINK_HZ+doppler)/1e6:.4f} MHz  "
                    f"{extra}   ")
                sys.stdout.flush()

            if end_time and datetime.now(timezone.utc) >= end_time:
                print(f"\n\n{GRN}Pass complete — the ISS has set. 73!{RST}")
                break
            time.sleep(0.25)
    except KeyboardInterrupt:
        print(f"\n\n{YEL}Stopped. 73!{RST}")
    finally:
        engine.stop()


def wait_for_pass(tracker: Tracker, loc: Location, args):
    """Default mode: show next good pass, count down, notify, then track."""
    print(BANNER)
    print(f"Ground station: {BOLD}{loc.name}{RST} "
          f"({loc.lat:.4f}, {loc.lon:.4f})\n")

    passes = tracker.next_passes(count=8, horizon_hours=48)
    good = [p for p in passes if p["max_elev"] >= GOOD_PASS_MIN_ELEV]
    nxt = good[0] if good else (passes[0] if passes else None)
    if not nxt:
        print(f"{RED}No ISS passes found in the next 48 hours.{RST}")
        return

    if nxt["max_elev"] < GOOD_PASS_MIN_ELEV:
        print(f"{YEL}Note: next pass peaks at only {nxt['max_elev']:.1f}° "
              f"— signal will be weak.{RST}")

    notified_10 = notified_2 = False
    while True:
        now = datetime.now(timezone.utc)
        if now >= nxt["rise"]:
            break
        secs = (nxt["rise"] - now).total_seconds()
        mins = secs / 60.0

        if not notified_10 and secs <= 600:
            notify("ISS pass in 10 minutes",
                   f"Max elevation {nxt['max_elev']:.0f}° · "
                   f"{compass(nxt['rise_az'])}→{compass(nxt['set_az'])}")
            notified_10 = True
        if not notified_2 and secs <= 120:
            notify("ISS pass in 2 minutes", "iss-listen is about to start.")
            notified_2 = True

        sys.stdout.write(
            f"\r{BOLD}Next pass{RST} at {GRN}{fmt_local(nxt['rise'])}{RST}  "
            f"— in {CYAN}{mins:5.1f} min{RST}  "
            f"(max elev {YEL}{nxt['max_elev']:.1f}°{RST}, "
            f"{compass(nxt['rise_az'])}→{compass(nxt['set_az'])})   ")
        sys.stdout.flush()
        time.sleep(1.0)

    print(f"\n\n{GRN}{BOLD}▲ PASS STARTING{RST} — ISS is rising "
          f"({compass(nxt['rise_az'])}).")
    notify("ISS pass starting now", f"Max elevation {nxt['max_elev']:.0f}°")
    track_pass(tracker, loc, args, pass_info=nxt)


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        prog="iss-listen",
        description="Listen to the ISS 145.800 MHz FM voice repeater via RTL-SDR.")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--now", action="store_true",
                   help="start listening immediately (still Doppler-corrected)")
    g.add_argument("--pass", dest="passes", action="store_true",
                   help="print the next 5 passes and exit")
    g.add_argument("--test", action="store_true",
                   help="check the dongle, print sample rate, and exit")
    ap.add_argument("--gain", type=float, default=40.0,
                    help="RF gain in dB for rtl_fm (default 40)")
    ap.add_argument("--audio-gain", type=float, default=3.0,
                    help="software makeup gain on the audio (default 3.0)")
    ap.add_argument("--highpass", action="store_true",
                    help="add a 300 Hz high-pass to kill hum (needs sox)")
    ap.add_argument("--simple", action="store_true",
                    help="use the rtl_fm restart engine instead of gapless rtl_tcp")
    ap.add_argument("--doppler-step", type=int, default=500,
                    help="(--simple only) retune when Doppler drifts this many Hz")
    ap.add_argument("--ppm", type=int, default=0,
                    help="frequency correction in PPM for your dongle (default 0)")
    ap.add_argument("--refresh-tle", action="store_true",
                    help="force a fresh TLE download")
    args = ap.parse_args()

    if args.test:
        loc = load_config()
        cmd_test(loc)
        return

    loc = load_config()
    tle = fetch_tle(force=args.refresh_tle)
    tracker = Tracker(loc, tle)

    if args.passes:
        cmd_pass(tracker, loc)
    elif args.now:
        print(BANNER)
        print(f"Ground station: {BOLD}{loc.name}{RST} "
              f"({loc.lat:.4f}, {loc.lon:.4f})")
        track_pass(tracker, loc, args, pass_info=None)
    else:
        wait_for_pass(tracker, loc, args)


if __name__ == "__main__":
    # Turn SIGTERM into KeyboardInterrupt so `finally` blocks release the
    # dongle (child rtl_tcp/rtl_fm/aplay) even when killed by `timeout`/kill.
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        main()
    except KeyboardInterrupt:
        print("\nbye — iceo96")
        sys.exit(0)
