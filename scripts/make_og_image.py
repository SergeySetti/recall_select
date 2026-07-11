"""One-off: build the social-share (Open Graph) image, 1200x630, with the brand
dark background baked in so it renders correctly on Facebook / Telegram / Threads
(which composite on their own backgrounds, where a transparent PNG would look
broken). Composes the keyed dog mark, the "recall.select" wordmark, and the
slogan graphic onto the canvas.

Run from repo root: python scripts/make_og_image.py
"""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

IMG = Path("app/static/images")
BG = (13, 11, 9)          # --rs-bg
TEXT = (232, 220, 192)    # --rs-text
ACCENT = (255, 122, 26)   # --rs-accent

W, H = 1200, 630
canvas = Image.new("RGB", (W, H), BG)
draw = ImageDraw.Draw(canvas)

font = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 96)

# Dog mark, scaled to a comfortable height, top-centered.
dog = Image.open(IMG / "logo-dog.png").convert("RGBA")
dog_h = 250
dog = dog.resize((round(dog.width * dog_h / dog.height), dog_h), Image.LANCZOS)
dog_x = (W - dog.width) // 2
dog_y = 70
canvas.paste(dog, (dog_x, dog_y), dog)

# Wordmark "recall.select": "recall" in beige, ".select" in accent.
part1, part2 = "recall", ".select"
w1 = draw.textlength(part1, font=font)
w2 = draw.textlength(part2, font=font)
total = w1 + w2
tx = (W - total) / 2
ty = dog_y + dog_h + 20
draw.text((tx, ty), part1, font=font, fill=TEXT)
draw.text((tx + w1, ty), part2, font=font, fill=ACCENT)

# Slogan graphic, centered under the wordmark.
slogan = Image.open(IMG / "logo-slogan.png").convert("RGBA")
sl_h = 60
slogan = slogan.resize((round(slogan.width * sl_h / slogan.height), sl_h), Image.LANCZOS)
sx = (W - slogan.width) // 2
sy = ty + 130
canvas.paste(slogan, (sx, sy), slogan)

canvas.save(IMG / "og-cover.png", optimize=True)
print("og-cover ->", canvas.size)
