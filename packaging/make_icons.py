"""Writes build/icons/packrat.ico (Windows) and packrat.png (Linux) from the app's own icon."""
import os

from packrat.launcher import icon_image

out = os.path.join("build", "icons")
os.makedirs(out, exist_ok=True)
big = icon_image(256)
big.save(os.path.join(out, "packrat.png"))
big.save(os.path.join(out, "packrat.ico"), sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("icons written to", out)
