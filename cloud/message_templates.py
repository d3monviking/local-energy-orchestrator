"""cloud/message_templates.py — household message texts, English and Kannada.

One template per message kind (contracts/events.md, C-4). `render` fills
a template from plain fields; cloud/messaging.notify decides who receives
it. Times are shown in IST, and day words ("today", "tomorrow") are
relative to when the message is sent, because DR offers go out the night
before.

English texts stay within one SMS (160 GSM-7 characters, so "Rs", not
"₹"). Kannada is UCS-2: 70 characters per single SMS, 67 per segment of a
longer one, so those messages run to 2–3 segments; `sms_segments` counts
them.

The Kannada texts should be reviewed by a native speaker before they go
in front of households.

Fields by kind (all times ISO 8601 / datetime, timezone-aware):

    load_shedding_notice  start, end
    grid_loss             expected_end (optional), planned (bool, default False)
    backup_start          limit_a, limit_kw          (per premise)
    restore               —                          (this premise is back on the grid)
    grid_return           —
    overload_trip         —
    dr_auto_shift         slot_ist (decimal IST hour, e.g. 10.75)
    dr_auto_ac            start, end, payment_rs
    dr_offer              start, end, level_rs (0 = an appeal, no payment)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

IST = timezone(timedelta(hours=5, minutes=30))
LANGS = ("en", "kn")

_KN_MONTHS = ["ಜನವರಿ", "ಫೆಬ್ರವರಿ", "ಮಾರ್ಚ್", "ಏಪ್ರಿಲ್", "ಮೇ", "ಜೂನ್",
              "ಜುಲೈ", "ಆಗಸ್ಟ್", "ಸೆಪ್ಟೆಂಬರ್", "ಅಕ್ಟೋಬರ್", "ನವೆಂಬರ್", "ಡಿಸೆಂಬರ್"]


class TemplateError(ValueError):
    pass


def to_ist(value) -> datetime:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        raise TemplateError(f"time {value!r} has no timezone")
    return ts.to_pydatetime().astimezone(IST)


def fmt_time(value, lang: str) -> str:
    """'7:40 pm' / 'ಸಂಜೆ 7:40'."""
    t = to_ist(value)
    h12 = t.hour % 12 or 12
    hm = f"{h12}:{t.minute:02d}" if t.minute else f"{h12}"
    if lang == "en":
        return f"{hm} {'am' if t.hour < 12 else 'pm'}"
    if 4 <= t.hour < 12:
        period = "ಬೆಳಿಗ್ಗೆ"      # morning
    elif 12 <= t.hour < 16:
        period = "ಮಧ್ಯಾಹ್ನ"      # afternoon
    elif 16 <= t.hour < 20:
        period = "ಸಂಜೆ"          # evening
    else:
        period = "ರಾತ್ರಿ"         # night
    return f"{period} {hm}"


def fmt_hour(decimal_hour_ist: float, lang: str) -> str:
    """A decimal IST hour (10.75) as a clock time on an arbitrary day."""
    h = int(decimal_hour_ist)
    m = int(round((decimal_hour_ist - h) * 60))
    return fmt_time(datetime(2000, 1, 1, h, m, tzinfo=IST), lang)


def fmt_day(when, at, lang: str) -> str:
    """'today' / 'tomorrow' / 'on 27 Apr', relative to the sending time `at`."""
    d, today = to_ist(when).date(), to_ist(at).date()
    delta = (d - today).days
    if lang == "en":
        return {0: "today", 1: "tomorrow"}.get(delta, f"on {d.day} {d.strftime('%b')}")
    return {0: "ಇಂದು", 1: "ನಾಳೆ"}.get(delta, f"{_KN_MONTHS[d.month - 1]} {d.day} ರಂದು")


def _span(f: dict, lang: str) -> str:
    # English keeps to GSM-7: an en dash would make the whole SMS Unicode (70 chars, not 160).
    dash = "-" if lang == "en" else "–"
    return f"{fmt_time(f['start'], lang)}{dash}{fmt_time(f['end'], lang)}"


def _need(fields: dict, kind: str, *names: str) -> None:
    missing = [n for n in names if fields.get(n) is None]
    if missing:
        raise TemplateError(f"{kind}: missing field(s) {missing}")


def _load_shedding_notice(f, at, lang):
    _need(f, "load_shedding_notice", "start", "end")
    day = fmt_day(f["start"], at, lang)
    if lang == "en":
        return f"LEO: Scheduled power cut {day} {_span(f, lang)}. Please charge phones and home inverters now."
    return f"LEO: {day} {_span(f, lang)} ನಿಗದಿತ ವಿದ್ಯುತ್ ಕಡಿತ. ದಯವಿಟ್ಟು ಈಗಲೇ ಫೋನ್ ಮತ್ತು ಮನೆಯ ಇನ್ವರ್ಟರ್ ಚಾರ್ಜ್ ಮಾಡಿ."


def _grid_loss(f, at, lang):
    end = f.get("expected_end")
    if f.get("planned"):
        if lang == "en":
            return "LEO: The scheduled power cut has started." + (f" Power returns about {fmt_time(end, lang)}." if end else "")
        return "LEO: ನಿಗದಿತ ವಿದ್ಯುತ್ ಕಡಿತ ಆರಂಭವಾಗಿದೆ." + (f" ಸುಮಾರು {fmt_time(end, lang)} ಕ್ಕೆ ವಿದ್ಯುತ್ ಮರಳುತ್ತದೆ." if end else "")
    if lang == "en":
        return ("LEO: Power cut in your area." + (f" Expected back about {fmt_time(end, lang)}." if end else "")
                + " We will message you when it returns.")
    return ("LEO: ನಿಮ್ಮ ಪ್ರದೇಶದಲ್ಲಿ ವಿದ್ಯುತ್ ಕಡಿತ." + (f" ಸುಮಾರು {fmt_time(end, lang)} ರೊಳಗೆ ಮರಳುವ ನಿರೀಕ್ಷೆ." if end else "")
            + " ವಿದ್ಯುತ್ ಬಂದಾಗ ತಿಳಿಸುತ್ತೇವೆ.")


def _backup_start(f, at, lang):
    _need(f, "backup_start", "limit_a", "limit_kw")
    a, kw = f"{float(f['limit_a']):g}", f"{float(f['limit_kw']):.1f}"
    if lang == "en":
        return f"LEO: Grid power is off. You are on LEO backup power. Please keep use under {a} A (about {kw} kW)."
    return f"LEO: ಗ್ರಿಡ್ ವಿದ್ಯುತ್ ಇಲ್ಲ. ನೀವು LEO ಬ್ಯಾಕಪ್ ವಿದ್ಯುತ್‌ನಲ್ಲಿದ್ದೀರಿ. ದಯವಿಟ್ಟು ಬಳಕೆಯನ್ನು {a} A (ಸುಮಾರು {kw} kW) ಒಳಗೆ ಇರಿಸಿ."


def _restore(f, at, lang):
    if lang == "en":
        return "LEO: Your premises is back on grid power. Backup has ended."
    return "LEO: ನಿಮ್ಮ ಆವರಣಕ್ಕೆ ಗ್ರಿಡ್ ವಿದ್ಯುತ್ ಮರಳಿದೆ. ಬ್ಯಾಕಪ್ ಮುಗಿದಿದೆ."


def _grid_return(f, at, lang):
    if lang == "en":
        return "LEO: Power is back. Thank you for your patience."
    return "LEO: ವಿದ್ಯುತ್ ಮರಳಿದೆ. ನಿಮ್ಮ ತಾಳ್ಮೆಗೆ ಧನ್ಯವಾದಗಳು."


def _overload_trip(f, at, lang):
    if lang == "en":
        return "LEO: Power on your line went off because of overload. The repair team has been informed."
    return "LEO: ಅತಿಯಾದ ಹೊರೆಯಿಂದ ನಿಮ್ಮ ಲೈನ್‌ನಲ್ಲಿ ವಿದ್ಯುತ್ ಕಡಿತವಾಗಿದೆ. ದುರಸ್ತಿ ತಂಡಕ್ಕೆ ತಿಳಿಸಲಾಗಿದೆ."


def _dr_auto_shift(f, at, lang):
    _need(f, "dr_auto_shift", "slot_ist")
    slot = fmt_hour(float(f["slot_ist"]), lang)
    if lang == "en":
        return f"LEO: Your pump will run at {slot} today on neighbourhood solar, not in the evening. Reply STOP to run it as usual."
    return f"LEO: ಇಂದು ನಿಮ್ಮ ಪಂಪ್ ಸಂಜೆಯ ಬದಲು {slot} ಕ್ಕೆ ನೆರೆಹೊರೆಯ ಸೌರ ವಿದ್ಯುತ್‌ನಲ್ಲಿ ಓಡುತ್ತದೆ. ಎಂದಿನಂತೆ ಓಡಿಸಲು STOP ಎಂದು ಉತ್ತರಿಸಿ."


def _dr_auto_ac(f, at, lang):
    _need(f, "dr_auto_ac", "start", "end", "payment_rs")
    day, pay = fmt_day(f["start"], at, lang), f"{float(f['payment_rs']):g}"
    if lang == "en":
        return (f"LEO: Power-saving event {day} {_span(f, lang)}. Your AC pre-cools, then runs lighter. "
                f"You earn Rs {pay}. Reply STOP to opt out.")
    return (f"LEO: {day} {_span(f, lang)} ವಿದ್ಯುತ್ ಉಳಿತಾಯ ಕಾರ್ಯಕ್ರಮ. ನಿಮ್ಮ AC ಮೊದಲು ತಂಪಾಗಿಸಿ ನಂತರ ಕಡಿಮೆ ಓಡುತ್ತದೆ. "
            f"ನೀವು ₹{pay} ಗಳಿಸುವಿರಿ. ಬೇಡವಾದರೆ STOP ಎಂದು ಉತ್ತರಿಸಿ.")


def _dr_offer(f, at, lang):
    _need(f, "dr_offer", "start", "end", "level_rs")
    day, level = fmt_day(f["start"], at, lang), float(f["level_rs"])
    if level <= 0:  # an appeal: no payment offered
        if lang == "en":
            return f"LEO: Please help avoid a local power cut: use less power {day} {_span(f, lang)}. Reply YES if you can."
        return f"LEO: ಸ್ಥಳೀಯ ವಿದ್ಯುತ್ ಕಡಿತ ತಪ್ಪಿಸಲು ಸಹಾಯ ಮಾಡಿ: {day} {_span(f, lang)} ಕಡಿಮೆ ವಿದ್ಯುತ್ ಬಳಸಿ. ಸಾಧ್ಯವಾದರೆ YES ಎಂದು ಉತ್ತರಿಸಿ."
    if lang == "en":
        return (f"LEO: Earn Rs {level:g} by using less power {day} {_span(f, lang)} (cooler, pump, iron). "
                f"Reply YES to join.")
    return (f"LEO: {day} {_span(f, lang)} ಕಡಿಮೆ ವಿದ್ಯುತ್ ಬಳಸಿ (ಕೂಲರ್, ಪಂಪ್, ಇಸ್ತ್ರಿ) ಮತ್ತು ₹{level:g} ಗಳಿಸಿ. "
            f"ಭಾಗವಹಿಸಲು YES ಎಂದು ಉತ್ತರಿಸಿ.")


TEMPLATES = {
    "load_shedding_notice": _load_shedding_notice,
    "grid_loss": _grid_loss,
    "backup_start": _backup_start,
    "restore": _restore,
    "grid_return": _grid_return,
    "overload_trip": _overload_trip,
    "dr_auto_shift": _dr_auto_shift,
    "dr_auto_ac": _dr_auto_ac,
    "dr_offer": _dr_offer,
}


def render(kind: str, lang: str, fields: dict, at) -> str:
    """The message text for one household. `at` is the sending time."""
    if kind not in TEMPLATES:
        raise TemplateError(f"no template for kind {kind!r}; known: {sorted(TEMPLATES)}")
    if lang not in LANGS:
        raise TemplateError(f"unsupported language {lang!r}")
    return TEMPLATES[kind](fields, at, lang)


# GSM 03.38 basic character set (what fits a 160-character SMS).
_GSM7 = set("@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
            "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà")
_GSM7_EXT = set("^{}\\[~]|€")  # each costs two characters


def sms_segments(body: str) -> int:
    """How many SMS segments `body` costs."""
    if all(c in _GSM7 or c in _GSM7_EXT for c in body):
        n = sum(2 if c in _GSM7_EXT else 1 for c in body)
        return 1 if n <= 160 else -(-n // 153)
    n = len(body.encode("utf-16-le")) // 2
    return 1 if n <= 70 else -(-n // 67)
