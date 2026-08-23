"""
Run this script once from the project root to register the owner's voice.
Usage:  python setup/registrar_owner.py
Records 3 × 10-second samples and saves WAV + numpy embeddings to data/memoria_voz/.
"""
import os
import time

import sounddevice as sd
import soundfile as sf
import numpy as np
import torchaudio
from speechbrain.pretrained import SpeakerRecognition

MEMORIA_DIR = "data/memoria_voz"
MODEL_SAVEDIR = "data/models/spkrec-ecapa-voxceleb"
DURACION = 10  # seconds per sample
FS = 16000

os.makedirs(MEMORIA_DIR, exist_ok=True)

PROMPTS = {
    1: "Hola, esta es la primera muestra para registrar mi voz con Jarvis.",
    2: "Esta es la segunda muestra. Estoy entrenando a Jarvis para reconocerme mejor.",
    3: "Finalmente, esta tercera muestra ayudará a que Jarvis entienda mejor mi voz.",
}

print("Loading speaker recognition model...")
spkrec = SpeakerRecognition.from_hparams(
    source="speechbrain/spkrec-ecapa-voxceleb",
    savedir=MODEL_SAVEDIR,
)


def grabar_audio(nombre_archivo):
    print(f"Recording {DURACION}s — starting in 3 seconds...")
    time.sleep(3)
    print("Recording now...")
    audio = sd.rec(int(DURACION * FS), samplerate=FS, channels=1, dtype="int16")
    sd.wait()
    sf.write(nombre_archivo, audio, FS, subtype="PCM_16")
    print(f"Saved: {nombre_archivo}")


def crear_embedding(nombre_archivo):
    torchaudio.set_audio_backend("soundfile")
    signal, _ = torchaudio.load(nombre_archivo)
    embedding = spkrec.encode_batch(signal)
    return embedding.squeeze().detach().cpu().numpy()


for i in range(1, 4):
    print(f"\nSample {i}/3 — please read aloud:\n  \"{PROMPTS[i]}\"")
    wav_path = os.path.join(MEMORIA_DIR, f"owner{i}.wav")
    npy_path = os.path.join(MEMORIA_DIR, f"owner{i}.npy")

    grabar_audio(wav_path)
    embedding = crear_embedding(wav_path)
    np.save(npy_path, embedding)
    print(f"Embedding saved: {npy_path}")

print(f"\nRegistration complete. Voice samples are in {MEMORIA_DIR}/")
