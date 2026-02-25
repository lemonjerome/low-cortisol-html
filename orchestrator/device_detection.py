from __future__ import annotations

import platform
import shutil
import subprocess


def detect_compute_backend(preferred: str = "auto") -> dict[str, str]:
    value = preferred.strip().lower()
    if value in {"cpu", "cuda", "mps"}:
        return {
            "device": value,
            "source": "explicit",
            "reason": "Selected by --device option",
        }

    system = platform.system().lower()
    machine = platform.machine().lower()

    if system == "darwin" and machine in {"arm64", "x86_64"}:
        return {
            "device": "mps",
            "source": "auto",
            "reason": "Detected macOS host; using MPS preference",
        }

    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi is not None:
        try:
            subprocess.run(
                [nvidia_smi, "-L"],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
            return {
                "device": "cuda",
                "source": "auto",
                "reason": "Detected NVIDIA GPU via nvidia-smi",
            }
        except Exception:  # noqa: BLE001
            pass

    return {
        "device": "cpu",
        "source": "auto",
        "reason": "No supported GPU backend detected",
    }
