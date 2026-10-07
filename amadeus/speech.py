"""Local VAD with original WAV offsets; Qwen receives only speech regions."""

import wave
from math import gcd

import numpy as np
import webrtcvad
from scipy.signal import resample_poly


def speech_regions(path):
    with wave.open(str(path), "rb") as wav:
        if wav.getsampwidth() != 2 or wav.getcomptype() != "NONE":
            raise ValueError("转写需要 PCM 16-bit WAV。")
        rate, channels = wav.getframerate(), wav.getnchannels()
        audio = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
    audio = audio.astype(np.float32).reshape(-1, channels).mean(axis=1) / 32768
    divisor = gcd(rate, 16000)
    if rate != 16000:
        audio = resample_poly(audio, 16000 // divisor, rate // divisor).astype(np.float32)
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2")
    vad = webrtcvad.Vad(2)
    frame = 480  # 30 ms at 16 kHz.
    regions = []
    for start in range(0, len(pcm) - frame + 1, frame):
        if vad.is_speech(pcm[start : start + frame].tobytes(), 16000):
            if regions and start - regions[-1][1] <= 11200:  # Merge gaps <= 700 ms.
                regions[-1][1] = start + frame
            else:
                regions.append([start, start + frame])
    # Reject isolated clicks and pad speech boundaries. Timestamps refer to these spans,
    # rather than word alignment; speaker identity comes from Discord, not the model.
    padded = []
    for start, end in regions:
        if end - start < 1920:
            continue
        start, end = max(0, start - 3200), min(len(audio), end + 3200)
        if padded and start <= padded[-1][1]:
            padded[-1][1] = end
        else:
            padded.append([start, end])
    return audio, padded
