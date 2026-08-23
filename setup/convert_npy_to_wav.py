"""
Dev utility: convert .npy embedding files in data/memoria_voz/ back to .wav.
Usage:  python setup/convert_npy_to_wav.py
"""
import os
import numpy as np
from scipy.io.wavfile import write

FOLDER = "data/memoria_voz"
SAMPLE_RATE = 16000

for f in os.listdir(FOLDER):
    if f.endswith(".npy"):
        data = np.load(os.path.join(FOLDER, f))
        wav_name = f.replace(".npy", ".wav")
        write(os.path.join(FOLDER, wav_name), SAMPLE_RATE, data)
        print(f"Converted {f} -> {wav_name}")
