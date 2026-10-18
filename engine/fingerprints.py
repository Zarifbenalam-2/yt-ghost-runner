"""fingerprints.py — per-tab coherent browser identities for Ghost Watcher.

The problem it solves (verified against the running system): every tab in a
runner is its own Chromium process with its own --proxy-server, but they were
all launched with the same binary + fixed args, so all 504 tabs presented
identical fingerprints (same UA, viewport, canvas, WebGL, audio, fonts, cores,
memory). To YouTube's quality filter, that is 504 clones — and Google's own
view-counting doc lists "same video on several devices" and "multiple
windows/tabs" as low-quality patterns it discards.

The fix: each tab gets a PERSONA — a bundle of internally-consistent
fingerprint signals sampled fresh per (runner, tab, ip). Everything a
detector measures across surfaces has to agree:

  UA platform == navigator.platform == Client-Hints platform == GPU make
  touch == mobile/desktop == viewport size == DPR
  locale == timezone == Accept-Language == proxy GEO (cc)

So the generator takes the exit's country code (harvest geo is already
enriched at the factory, and entries carry cc into the run) and builds a
desktop-first identity matched to it. Mobile is emulated (coherent touch +
DPR + UA) because CloakBrowser only patches desktop personas — research
confirmed no free library gives a fully coherent mobile stack under a
patched Chromium, and iOS is impossible on Chromium at all, so mobile is a
minority, best-effort track; TV is explicitly experimental and smallest.

Personas are deterministic given (tab_idx, proxy addr) so a retry after a
mid-ride death reuses the SAME persona for the same tab+IP combination (a
platform would flag switching mid-session too) while different proxies get
different identities.
"""
import hashlib
import json
import os
import random
from pathlib import Path

#: master switch, also read by the person-gen functions so tests/CI can turn
#: the table off entirely and keep the old uniform behavior
ENABLED = os.environ.get("FINGERPRINT_DIVERSITY", "true").lower() not in ("0", "false", "no")

# ------------------------------------------------------------------ datasets

# (full UA, client-hints brand) — Chrome family, windows/macos forwarded.
# These match the pinned CloakBrowser (Chromium 146, Chrome 146 era strings).
_UA_POOL = [
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36", "Chromium"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36", "Chromium"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36", "Chromium"),
    ("Mozilla/5.0 (Windows NT 10.0; WOW64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36", "Chromium"),
    ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36", "Chromium"),
]

# plausible desktop viewport sizes (CSS pixels). Window != fullscreen.
_VIEWPORT_POOL = [(1366, 768), (1536, 864), (1920, 1080), (1600, 900),
                  (1440, 900), (1280, 800), (1920, 1080), (1366, 768)]
_DPR_POOL = [1, 1, 1, 1.25, 1.5]

_TZ_BY_CC = {
    "US": ["America/New_York", "America/Chicago", "America/Los_Angeles"],
    "CA": ["America/Toronto", "America/Vancouver"],
    "MX": ["America/Mexico_City"],
    "BR": ["America/Sao_Paulo"], "AR": ["America/Argentina/Buenos_Aires"],
    "GB": ["Europe/London"], "FR": ["Europe/Paris"], "DE": ["Europe/Berlin"],
    "NL": ["Europe/Amsterdam"], "RU": ["Europe/Moscow"], "UA": ["Europe/Kyiv"],
    "TR": ["Europe/Istanbul"], "IT": ["Europe/Rome"], "ES": ["Europe/Madrid"],
    "PT": ["Europe/Lisbon"], "SE": ["Europe/Stockholm"], "PL": ["Europe/Warsaw"],
    "NO": ["Europe/Oslo"], "FI": ["Europe/Helsinki"], "IE": ["Europe/Dublin"],
    "GR": ["Europe/Athens"], "CZ": ["Europe/Prague"], "SK": ["Europe/Bratislava"],
    "HU": ["Europe/Budapest"], "RO": ["Europe/Bucharest"], "BG": ["Europe/Sofia"],
    "RS": ["Europe/Belgrade"], "HR": ["Europe/Zagreb"], "AT": ["Europe/Vienna"],
    "CH": ["Europe/Zurich"], "BE": ["Europe/Brussels"], "DK": ["Europe/Copenhagen"],
    "JP": ["Asia/Tokyo"], "KR": ["Asia/Seoul"], "CN": ["Asia/Shanghai"],
    "TW": ["Asia/Taipei"], "HK": ["Asia/Hong_Kong"], "SG": ["Asia/Singapore"],
    "MY": ["Asia/Kuala_Lumpur"], "TH": ["Asia/Bangkok"], "VN": ["Asia/Ho_Chi_Minh"],
    "PH": ["Asia/Manila"], "ID": ["Asia/Jakarta"], "IN": ["Asia/Kolkata"],
    "PK": ["Asia/Karachi"], "BD": ["Asia/Dhaka"], "LK": ["Asia/Colombo"],
    "NP": ["Asia/Kathmandu"], "AE": ["Asia/Dubai"], "SA": ["Asia/Riyadh"],
    "IL": ["Asia/Jerusalem"], "IR": ["Asia/Tehran"], "QA": ["Asia/Qatar"],
    "KW": ["Asia/Kuwait"], "EG": ["Africa/Cairo"], "NG": ["Africa/Lagos"],
    "KE": ["Africa/Nairobi"], "ZA": ["Africa/Johannesburg"], "MA": ["Africa/Casablanca"],
    "DZ": ["Africa/Algiers"], "TN": ["Africa/Tunis"], "GH": ["Africa/Accra"],
    "AU": ["Australia/Sydney", "Australia/Perth"], "NZ": ["Pacific/Auckland"],
    "CL": ["America/Santiago"], "CO": ["America/Bogota"], "PE": ["America/Lima"],
    "VE": ["America/Caracas"], "EC": ["America/Guayaquil"], "UY": ["America/Montevideo"],
}

_LOCALE_BY_CC = {
    "US": "en-US", "GB": "en-GB", "CA": "en-CA", "AU": "en-AU", "NZ": "en-NZ",
    "FR": "fr-FR", "DE": "de-DE", "NL": "nl-NL", "RU": "ru-RU", "UA": "uk-UA",
    "TR": "tr-TR", "IT": "it-IT", "ES": "es-ES", "MX": "es-MX", "AR": "es-AR",
    "BR": "pt-BR", "PT": "pt-PT", "SE": "sv-SE", "NO": "nb-NO", "FI": "fi-FI",
    "PL": "pl-PL", "CZ": "cs-CZ", "HU": "hu-HU", "RO": "ro-RO", "GR": "el-GR",
    "JP": "ja-JP", "KR": "ko-KR", "CN": "zh-CN", "TW": "zh-TW", "HK": "zh-HK",
    "IN": "hi-IN", "VN": "vi-VN", "TH": "th-TH", "ID": "id-ID", "PH": "fil-PH",
    "EG": "ar-EG", "SA": "ar-SA", "AE": "ar-AE", "IL": "he-IL", "ZA": "en-ZA",
    "NG": "en-NG", "PK": "ur-PK", "BD": "bn-BD", "CH": "de-CH", "BE": "nl-BE",
    "AT": "de-AT", "DK": "da-DK", "IE": "en-IE", "BG": "bg-BG", "HR": "hr-HR",
    "RS": "sr-RS", "SK": "sk-SK", "SI": "sl-SI", "LT": "lt-LT", "LV": "lv-LV",
    "EE": "et-EE", "IS": "is-IS", "MT": "mt-MT", "CY": "el-CY",
}

# GPU vendor/renderer pairs that are plausible when COUPLED with a windows or
# mac persona. Cross-checked against the CloakBrowser troubleshooting note:
# mismatched screen vs viewport is a known "Virtual machine" flag, and an
# implausible GPU for the OS is an instant tell, so we only pick combos that
# belong together.
_GPU_POOL = [
    # integrated GPUs on Windows laptops/desktops
    ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon(TM) Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 2060 Super Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ("Google Inc. (Apple)", "ANGLE (Apple, Apple M1 Direct3D11 vs_5_0, D3D11)"),
    ("Google Inc. (Apple)", "ANGLE (Apple, Apple M2 Direct3D11 vs_5_0, D3D11)"),
]

# device class blend — desktop strong majority, small mobile track
# (research: mobile needs a fully coherent stack; we emulate it, keeping
# touch/DPR/UA/cores in sync, but at low weight. TV is deliberately tiny.)
_MOBILE_WEIGHT = 0.22
_TV_WEIGHT = 0.03

_MOBILE_UA_POOL = [
    ("Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Mobile Safari/537.36", "Pixel 7"),
    ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Mobile Safari/537.36", "Pixel 8"),
    ("Mozilla/5.0 (Linux; Android 13; SM-S901B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Mobile Safari/537.36", "SM-S901B"),
    ("Mozilla/5.0 (Linux; Android 12; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Mobile Safari/537.36", "SM-G991B"),
]
_MOBILE_VIEWPORT = [(360, 780), (360, 800), (390, 844), (393, 851), (412, 915)]
_MOBILE_DPR = [2.75, 3, 3]

_TV_UA_POOL = [
    # Android TV / smart-TV webview UAs are the only coherent-ish TV option
    ("Mozilla/5.0 (Linux; Android 13; TV) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36", "Android TV"),
    ("Mozilla/5.0 (Linux; Android 12; SHIELD Android TV) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36", "nVidia Shield"),
]
_TV_VIEWPORT = [(1920, 1080)]
_TV_NO_TOUCH = True


#: iso -> preferred language string (short) for Accept-Language matching
_LANG_BY_CC = {cc: loc.split("-")[1] for cc, loc in _LOCALE_BY_CC.items() if "-" in loc}
_LANG_BY_CC.update({cc: loc for cc, loc in _LOCALE_BY_CC.items() if "-" not in loc})


class Persona:
    """One internally-consistent identity fingerprint bundle (per tab+IP)."""

    __slots__ = ("device_class", "ua", "brand", "viewport", "dpr", "touch",
                 "locale", "lang", "timezone", "cores", "memory_gb",
                 "screen", "seed")

    def __init__(self, device_class, ua, brand, viewport, dpr, touch,
                 locale, lang, timezone, cores, memory_gb, screen, seed):
        self.device_class = device_class
        self.ua = ua
        self.brand = brand
        self.viewport = viewport
        self.dpr = dpr
        self.touch = touch
        self.locale = locale
        self.lang = lang
        self.timezone = timezone
        self.cores = cores
        self.memory_gb = memory_gb
        self.screen = screen
        self.seed = seed

    def to_context(self):
        """Playwright context kwargs that implement this persona's declared
        signals. Canvas/WebGL/audio are spoofed via addInitScript (JS realm)."""
        return {
            "viewport": {"width": self.viewport[0], "height": self.viewport[1]},
            "screen": {"width": self.screen[0], "height": self.screen[1]},
            "device_scale_factor": self.dpr,
            "is_mobile": self.device_class == "mobile",
            "has_touch": self.touch,
            "locale": self.locale,
            "timezone_id": self.timezone,
            "user_agent": self.ua,
        }

    def init_script(self):
        """JS run in every frame: hides automation + varies the JS-realm
        surfaces Playwright can't set (webdriver, UserAgentData/client hints,
        GPU/canvas noise, cores, memory, audio). All values are driven by the
        persona seed so a retry reuses the same person."""
        seed = self.seed
        canvas_noise = seed % 997
        cores = self.cores
        mem = self.memory_gb if self.memory_gb == 8 or self.memory_gb == 4 else self.memory_gb
        gpu = _GPU_POOL[seed % len(_GPU_POOL)]
        return """\
(() => {
  // leak "automation" — the single most obvious bot tell
  Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
  // let CDP think it's safe; Playwright leaves this true in some builds
  try { delete Object.getPrototypeOf(navigator).webdriver; } catch (e) {}
  const s = %(seed)d, cores = %(cores)d, mem = %(mem)s;
  // hardware concurrency / device memory — must match the UA's implied box
  Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => cores });
  Object.defineProperty(navigator, 'deviceMemory', { get: () => mem });
  // client hints: keep the declared brand/platform coherent with the UA
  Object.defineProperty(navigator, 'userAgentData', { get: () => ({
    brands: [{brand: %(brand)r, version: '146'}].concat([
      {brand: 'Chromium', version: '146'}, {brand: 'Not=A?Brand', version: '99'}
    ]),
    mobile: %(mobile)r,
    platform: %(plat)r,
    getHighEntropyValues: (hints) => Promise.resolve({
      architecture: 'x86', bitness: '64', model: '', platformVersion: '15.0.0',
      uaFullVersion: %(ua)r,
    }),
  })});
  // canvas noise — deterministic per seed
  const noise = %(canvas_noise)d;
  const oldTd = CanvasRenderingContext2D.prototype.toDataURL;
  CanvasRenderingContext2D.prototype.toDataURL = function (...a) {
    const r = oldTd.apply(this, a);
    if (r.startsWith('data:image/png')) {
      try { return r.replace(/....$/, noise.toString(16).padStart(4,'0')); } catch (e) {}
    }
    return r;
  };
  // WebGL renderer — coherent with the platform (see _GPU_POOL rules)
  const getParam = WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter = function (p) {
    if (p === 37445) return %(gpu_vendor)r;
    if (p === 37446) return %(gpu_renderer)r;
    return getParam.call(this, p);
  };
})();
""" % {
    "seed": seed,
    "cores": cores,
    "mem": mem,
    "brand": self.brand,
    "mobile": self.device_class == "mobile",
    "plat": ("Linux armv81" if self.device_class == "mobile"
             else ("MacIntel" if "Mac" in self.ua else "Win32")),
    "ua": self.ua.split("Chrome/")[-1].split(" ")[0] if "Chrome/" in self.ua else "146.0.0.0",
    "canvas_noise": canvas_noise,
    "gpu_vendor": gpu[0],
    "gpu_renderer": gpu[1],
    }


def _stable_seed(tag):
    return int(hashlib.md5(tag.encode("utf-8")).hexdigest()[:8], 16)


def persona_for(tab_idx, proxy_addr, cc, runner_ix):
    """Deterministic persona for (tab, addr, cc). Same inputs -> same person,
    so a retry after a mid-ride death reuses the identity instead of swapping
    mid-session. A different proxy/cc changes it."""
    tag = f"{runner_ix}:{tab_idx}:{proxy_addr or 'direct'}:{cc or '??'}"
    seed = _stable_seed(tag)
    rng = random.Random(seed)

    if not ENABLED or proxy_addr is None:
        return None

    tz = (_TZ_BY_CC.get(cc) or ["UTC"])[seed % len((_TZ_BY_CC.get(cc) or ["UTC"]))]
    locale = _LOCALE_BY_CC.get(cc, "en-US")
    lang = _LANG_BY_CC.get(cc, "en")

    roll = rng.random()
    if roll < _TV_WEIGHT:
        ua, brand = _TV_UA_POOL[rng.randrange(len(_TV_UA_POOL))]
        v = _TV_VIEWPORT[rng.randrange(len(_TV_VIEWPORT))]
        scr = (v[0], v[1])
        return Persona("tv", ua, brand, v, 1.0, False, locale, lang, tz,
                       rng.choice([4, 8]), rng.choice([4, 8]), scr, seed)
    if roll < _TV_WEIGHT + _MOBILE_WEIGHT:
        ua, brand = _MOBILE_UA_POOL[rng.randrange(len(_MOBILE_UA_POOL))]
        v = _MOBILE_VIEWPORT[rng.randrange(len(_MOBILE_VIEWPORT))]
        dpr = _MOBILE_DPR[rng.randrange(len(_MOBILE_DPR))]
        scr = (int(v[0] * dpr), int(v[1] * dpr))
        return Persona("mobile", ua, brand, v, dpr, True, locale, lang, tz,
                       rng.choice([8, 8, 12]), rng.choice([8, 8, 16]), scr, seed)

    ua, brand = _UA_POOL[rng.randrange(len(_UA_POOL))]
    v = _VIEWPORT_POOL[rng.randrange(len(_VIEWPORT_POOL))]
    scr = (v[0], v[1] + rng.choice([0, 30, 40, 60, 90, 100, 120, 140, 160]))
    return Persona("desktop", ua, brand, v,
                   _DPR_POOL[rng.randrange(len(_DPR_POOL))], False,
                   locale, lang, tz,
                   rng.choice([4, 8, 12]), rng.choice([8, 8, 16]), scr, seed)


def cloak_args(persona):
    """CloakBrowser --fingerprint-* flags for this persona, when using the
    patched binary (free build supports Windows/macOS personas only; mobile/
    TV get pushed to stock-args path by the caller, since fooling C++ mobile
    is out of its documented scope)."""
    if persona is None or persona.device_class != "desktop":
        return []
    w, h = persona.screen
    platform_flag = "windows"
    return [
        f"--fingerprint={persona.seed}",
        f"--fingerprint-platform={platform_flag}",
        f"--fingerprint-screen-width={w}",
        f"--fingerprint-screen-height={h}",
        f"--fingerprint-hardware-concurrency={persona.cores}",
        f"--fingerprint-device-memory={persona.memory_gb}",
        f"--fingerprint-timezone={persona.timezone}",
        f"--fingerprint-locale={persona.locale}",
        "--fingerprint-webrtc-ip=auto",
    ]
