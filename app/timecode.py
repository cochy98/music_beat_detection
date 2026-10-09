"""Frame arithmetic for Resolve: real frame rate and timecode labels are distinct."""

import math
import re
from dataclasses import dataclass
from fractions import Fraction


@dataclass(frozen=True)
class FrameRate:
    rate: Fraction
    nominal: int
    drop_frame: bool = False

    @classmethod
    def parse(cls, value, drop_frame=False):
        value = str(value)
        try:
            rate = Fraction(value)
        except (ValueError, ZeroDivisionError) as exc:
            raise ValueError(f"Invalid FPS: {value}") from exc
        if not 1 <= rate <= 120:
            raise ValueError("FPS must be between 1 and 120.")
        for nominal in (24, 30, 60):
            ntsc = Fraction(nominal * 1000, 1001)
            if abs(float(rate - ntsc)) < 0.0005:
                rate = ntsc
                break
        if rate.denominator == 1:
            nominal = int(rate)
        elif rate in (Fraction(24000, 1001), Fraction(30000, 1001), Fraction(60000, 1001)):
            nominal = round(float(rate))
        else:
            raise ValueError("Use integer FPS or 23.976, 29.97, 59.94 (or their exact fractions).")
        if drop_frame and rate not in (Fraction(30000, 1001), Fraction(60000, 1001)):
            raise ValueError("Drop-frame is supported only at 29.97 or 59.94 FPS.")
        return cls(rate, nominal, drop_frame)

    @property
    def dropped(self):
        return self.nominal // 15 if self.drop_frame else 0

    def seconds_to_frames(self, seconds):
        if not math.isfinite(float(seconds)):
            raise ValueError("Seconds must be finite.")
        frames = Fraction(str(seconds)) * self.rate
        # Nearest frame; ties away from zero, independent of Python's banker rounding.
        sign = -1 if frames < 0 else 1
        frames = abs(frames)
        return sign * ((2 * frames.numerator + frames.denominator) // (2 * frames.denominator))

    def parse_timecode(self, timecode):
        match = re.fullmatch(r"(\d{2}):(\d{2}):(\d{2})([:;])(\d{2,3})", timecode.strip())
        if not match:
            raise ValueError(f"Invalid timecode '{timecode}'; expected HH:MM:SS:FF.")
        hours, minutes, seconds = map(int, match.group(1, 2, 3))
        frames = int(match.group(5))
        if hours > 23 or minutes > 59 or seconds > 59 or frames >= self.nominal:
            raise ValueError(f"Timecode '{timecode}' is out of range for {self.nominal} FPS.")
        if match.group(4) == ";" and not self.drop_frame:
            raise ValueError("A ';' timecode requires --drop-frame.")
        if self.drop_frame and minutes % 10 and seconds == 0 and frames < self.dropped:
            raise ValueError(f"'{timecode}' is a skipped drop-frame label.")
        total_minutes = hours * 60 + minutes
        labels = (hours * 3600 + minutes * 60 + seconds) * self.nominal + frames
        return labels - self.dropped * (total_minutes - total_minutes // 10)

    def format_timecode(self, frames):
        if not isinstance(frames, int) or frames < 0:
            raise ValueError("Timecode frame count must be a non-negative integer.")
        labels = frames
        if self.drop_frame:
            per_minute = self.nominal * 60 - self.dropped
            per_ten_minutes = self.nominal * 600 - self.dropped * 9
            blocks, remainder = divmod(frames, per_ten_minutes)
            labels += self.dropped * 9 * blocks
            if remainder >= self.dropped:
                labels += self.dropped * ((remainder - self.dropped) // per_minute)
        seconds, frame = divmod(labels, self.nominal)
        minutes, second = divmod(seconds, 60)
        hour, minute = divmod(minutes, 60)
        if hour > 23:
            raise ValueError("EDL timecode exceeds 23:59:59; split the timeline before exporting.")
        separator = ";" if self.drop_frame else ":"
        return f"{hour:02d}:{minute:02d}:{second:02d}{separator}{frame:02d}"


# Compatibility helpers. Timeline placement must add frame counts, not wall-clock seconds.
def parse_timecode(timecode, fps):
    rate = FrameRate.parse(fps)
    return rate.parse_timecode(timecode) / float(rate.rate)


def seconds_to_timecode(seconds, fps):
    rate = FrameRate.parse(fps)
    return rate.format_timecode(rate.seconds_to_frames(seconds))


def add_frames_to_timecode(timecode, frames_to_add, fps):
    rate = FrameRate.parse(fps)
    return rate.format_timecode(rate.parse_timecode(timecode) + frames_to_add)
