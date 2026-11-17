"""Draw the ZeroDesk icon (store 256px + launcher 100px). Run: python3 tools/make_icons.py"""
import os
from PIL import Image, ImageDraw

S = 1024
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "packaging")


def draw():
    img = Image.new("RGB", (S, S), (13, 17, 23))
    d = ImageDraw.Draw(img)
    blue, green = (88, 166, 255), (63, 185, 80)
    d.rounded_rectangle((180, 230, 844, 650), radius=48, outline=blue, width=64)   # screen
    d.rounded_rectangle((300, 760, 724, 820), radius=30, fill=blue)                # stand
    d.line((330, 360, 430, 440, 330, 520), fill=green, width=56, joint="curve")    # prompt >
    d.line((480, 530, 640, 530), fill=green, width=56)                             # _
    return img


if __name__ == "__main__":
    big = draw()
    big.resize((256, 256), Image.LANCZOS).save(os.path.join(OUT, "icon-256.png"))
    big.resize((100, 100), Image.LANCZOS).save(os.path.join(OUT, "zerodesk.png"))
    print("icons written")
