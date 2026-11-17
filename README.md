<div align="center">

```
   ___ ____ ____   _     _     _
  |_ _/ ___/ ___| | |   (_)___| |_ ___ _ __
   | |\___ \___ \ | |   | / __| __/ _ \ '_ \
   | | ___) |__) || |___| \__ \ ||  __/ | | |
  |___|____/____/ |_____|_|___/\__\___|_| |_|
```

# 🛰️ iss-listen

**One command to hear the International Space Station fly over your house.**

Point an RTL-SDR at the sky, run one command, and listen to the ISS's
`145.800 MHz` FM downlink — with automatic Doppler correction, pass prediction,
desktop alerts, and a live satellite map of the station crossing overhead.

![Python](https://img.shields.io/badge/python-3.11%2B-blue?logo=python&logoColor=white)
![Platform](https://img.shields.io/badge/platform-Linux-informational?logo=linux&logoColor=white)
![SDR](https://img.shields.io/badge/radio-RTL--SDR%20v4-orange)
![Audio](https://img.shields.io/badge/audio-PipeWire%20%2F%20PulseAudio-green)
![License](https://img.shields.io/badge/license-MIT-lightgrey)
![Made by iceo96](https://img.shields.io/badge/made%20by-iceo96-ff69b4)

</div>

---

## ✨ What it does

```bash
$ iss-listen
```

> ```
> ► Next chance to listen: Tue 23:54  (in 5h 51m)
>   peak 69° elevation, NW→SE, ~11 min overhead
> ```

…then it counts down, pings you 10 and 2 minutes before the pass, and the
moment the station rises above your horizon it **starts the audio, corrects the
Doppler shift in real time, and opens a live map** with the ISS tracking across
a satellite view of the Earth — your house marked, the line-of-sight drawn, and
a HUD showing elevation, range, altitude and the live Doppler in Hz.

<div align="center">

| | |
|---|---|
| 🔮 **Pass prediction** | Celestrak TLE + `skyfield`/`sgp4`, cached 3 days |
| 📡 **Gapless Doppler** | hardware tuned once, shift removed in software — **no audio dropouts** |
| 🔊 **Plug-and-play audio** | straight to your speakers via `aplay`/`sox` |
| 🗺️ **Live map** | Leaflet + FastAPI, satellite tiles, trail & line-of-sight |
| 🔔 **Alerts** | `notify-send` at T-10 min, T-2 min, and lift-off |
| ⚙️ **Zero fuss** | one config line (your lat/lon), one command |

</div>

---

## 🚀 Quick start

```bash
# 1. clone
git clone https://github.com/Iceo96/iss-listen.git
cd iss-listen

# 2. set up the Python environment
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt

# 3. run it (first run writes a config; add your lat/lon and run again)
./iss-listen
```

Want it on your `PATH`?

```bash
ln -s "$PWD/iss-listen" ~/.local/bin/iss-listen
```

### Set your location

First run creates `~/.config/iss-listen.ini`. Drop in **your** coordinates
(grab them from [openstreetmap.org](https://www.openstreetmap.org) →
right-click → *Show address*):

```ini
[location]
lat = 51.5074      # <- your latitude
lon = -0.1278      # <- your longitude
elevation_m = 20
name = home
```

---

## 🎛️ Commands

| Command | What it does |
|---|---|
| `iss-listen` | Next-pass countdown → auto-start → audio + map |
| `iss-listen --now` | Start listening **right now** (still Doppler-corrected) |
| `iss-listen --pass` | Print the next 5 passes and exit |
| `iss-listen --test` | Check the dongle, print the sample rate, measure the retune gap |

**Handy flags:** `--gain 44` (RF gain dB), `--audio-gain 3`, `--ppm 0`
(dongle frequency correction), `--highpass` (300 Hz hum filter, needs `sox`),
`--simple` (classic `rtl_fm` engine), `--refresh-tle`.

---

## 🧠 How it works

### Gapless Doppler — the fun part

As the ISS screams past at 7.66 km/s, its `145.800 MHz` signal gets smeared by
up to **±3.5 kHz** of Doppler shift. The naive fix is to keep re-tuning the
dongle — but on the RTL-SDR **v4** a full `rtl_fm` restart costs **~950 ms** of
dead air, measured right in `--test`.

So `iss-listen` doesn't re-tune the hardware at all. It parks `rtl_tcp` at a
fixed `145.800 MHz` and **removes the Doppler shift in software** with a numpy
DSP chain — a numerically-controlled oscillator spins the carrier back to zero,
then an FIR low-pass + decimator + FM discriminator turn it into audio:

```
 rtl_tcp ──IQ 240kS/s──▶  NCO shift (−Δf)  ──▶  LPF + ÷16  ──▶  FM discriminator  ──▶  🔊 15 kHz audio
 (fixed 145.800 MHz)      software Doppler       anti-alias       d/dt of phase        aplay / sox
```

No hardware re-tune ⇒ **zero gaps**. The Doppler shift (recomputed every
250 ms from the SGP4 range-rate) is shown live on the terminal and the map.
Prefer the old way? `--simple` brings back the `rtl_fm` restart engine, with a
persistent speaker sink that masks each gap with silence.

### Pass prediction

The ISS TLE is pulled from Celestrak (CATNR 25544), cached in
`~/.cache/iss-listen`, and refreshed only when it's older than 3 days.
`skyfield` turns it into rise / culmination / set times, max elevation and
travel direction for your location. A **good pass** peaks above 20°.

### The map

A tiny FastAPI server on `http://127.0.0.1:8080` serves a Leaflet page that
polls `/api/pos` once a second and draws the ISS (with a fading trail), your
home marker, the line-of-sight, and a live readout of elevation, range,
altitude and Doppler. Satellite basemap, no API key required.

---

## 🩺 Will I actually hear voices?

`145.800 MHz` is receivable whenever the ISS is above your horizon — but what's
*on* it depends on the crew:

- 🎙️ **Live voice** → ARISS school contacts, scheduled at
  [ariss.org/upcoming-contacts.html](https://www.ariss.org/upcoming-contacts.html)
- 🖼️ **SSTV** → image events, announced at [amsat.org](https://www.amsat.org)
- 📶 **Most of the time** → APRS packet bursts (data chirps, not voice)

No orbital math can predict when an astronaut keys the mic — check the ARISS
schedule and line it up with a ★ pass. Even on a silent pass you'll hear packet
activity and watch the Doppler swing through zero at closest approach: proof
your whole chain works.

> 📻 **Antenna matters more than gain.** The stock whip will only catch high
> passes. A simple 2 m ¼-wave ground-plane or a tape-measure Yagi is a night-
> and-day upgrade.

---

## 📦 Requirements

- RTL-SDR dongle (built & tested on an **RTL-SDR Blog V4** / R828D) with the
  rtl-sdr-blog driver (`librtlsdr ≥ 2.0`).
- `rtl_tcp`, `rtl_fm`, `rtl_test` on `PATH` (the `rtl-sdr` package).
- `aplay` (alsa-utils) **or** `sox`; PipeWire or PulseAudio.
- `notify-send` for desktop alerts (optional).
- Python 3.11+ and the packages in [`requirements.txt`](requirements.txt).

---

## 🛠️ Troubleshooting

| Symptom | Fix |
|---|---|
| `RTL-SDR is busy` | Another SDR app (gqrx, SDR++, stray `rtl_tcp`) holds the dongle — the tool names it. Close it. |
| Kernel DVB grabbed it | `sudo modprobe -r dvb_usb_rtl28xxu` (the tool suggests this). |
| Only hiss | Normal when the ISS is below the horizon or not transmitting. Check `--pass`. |
| Too quiet | Raise `--gain` (try 44–49) and `--audio-gain`; add `--highpass` (needs `sox`). |
| Map tiles blank | You're offline — the satellite basemap needs internet; the HUD still works. |

---

## 📄 License

MIT — see [LICENSE](LICENSE). Do whatever you like, no warranty.

<div align="center">

**Built with ☕ and a love of space by [iceo96](https://github.com/Iceo96).**
*Look up. It's right there. 73!* 🛰️

</div>
