"""Why "behind me" is switched off on this server. Read-only; changes nothing.

Run from artifacts/subtitle-burner:

    python scripts/why_behind_me_is_off.py

For most purposes you do not need this -- open /can/behind-me in a browser
and the running app tells you the same reason. This is for when the app will
not start, or when you want the installed versions alongside the verdict.

The feature needs mediapipe with solutions.selfie_segmentation, which was
removed upstream in 0.10.22; the app pins mediapipe<0.10.22. Historically it
has broken here twice: once when the pin was not applied on a redeploy, and
once when another package pulled numpy 2.x in under it.
"""
import importlib.metadata as md
import sys

print("python     :", sys.version.split()[0])
for name in ("mediapipe", "opencv-python-headless", "opencv-python", "numpy", "pillow"):
    try:
        print(f"{name:24}: {md.version(name)}")
    except md.PackageNotFoundError:
        print(f"{name:24}: NOT INSTALLED")

print()
try:
    import mediapipe
    print("mediapipe imports  : yes", getattr(mediapipe, "__version__", "?"))
    sols = getattr(mediapipe, "solutions", None)
    print("  .solutions       :", "present" if sols else "MISSING")
    if sols:
        for a in ("selfie_segmentation", "face_detection"):
            print(f"  .{a:17}:", "present" if hasattr(sols, a) else "MISSING")
except Exception as e:
    print("mediapipe imports  : NO —", type(e).__name__, e)

try:
    import cv2
    print("cv2 imports        : yes", cv2.__version__)
except Exception as e:
    print("cv2 imports        : NO —", type(e).__name__, e)

print()
sys.path.insert(0, ".")
try:
    import app
    ok, why = app._segment_deps_available()
    print("VERDICT: behind-me available =", ok)
    if not ok:
        print("REASON :", why)
except Exception as e:
    print("could not load app.py:", type(e).__name__, e)
