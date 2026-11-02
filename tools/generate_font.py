"""Rasterize the separately licensed Noto font; the raster data stays OFL-1.1."""
from pathlib import Path
import argparse
import hashlib
import json
from PIL import Image, ImageDraw, ImageFont


def generate(font_path, characters_path, output):
    chars = characters_path.read_text(encoding='utf8').rstrip('\r\n')
    font = ImageFont.truetype(str(font_path), 30)
    replacement = font.getmask('\U0010ffff')
    absent_signature = (replacement.size, bytes(replacement))
    packed = bytearray()
    missing = []
    empty = []
    for char in chars:
        mask = font.getmask(char)
        if (mask.size, bytes(mask)) == absent_signature:
            missing.append(char)
        canvas = Image.new('L', (32, 32), 0)
        draw = ImageDraw.Draw(canvas)
        box = draw.textbbox((0, 0), char, font=font)
        x = (32 - (box[2] - box[0])) // 2 - box[0]
        y = (32 - (box[3] - box[1])) // 2 - box[1]
        draw.text((x, y), char, font=font, fill=255)
        values = list(canvas.getdata())
        if not any(value >= 128 for value in values) and not char.isspace():
            empty.append(char)
        for row in range(32):
            word = 0
            for col in range(32):
                if values[row * 32 + col] >= 128:
                    word |= 1 << (31 - col)
            packed.extend(word.to_bytes(4, 'big'))
    if missing or empty:
        raise ValueError('Missing characters: %r; empty characters: %r' % (missing, empty))
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise ValueError('Choose a fresh output file')
    output.write_bytes(bytes(packed))
    return dict(characters=len(chars), unique_characters=len(set(chars)),
        bytes=len(packed), font_sha256=hashlib.sha256(font_path.read_bytes()).hexdigest(),
        data_sha256=hashlib.sha256(packed).hexdigest(), pixel_size=30,
        cell=[32, 32], threshold=128, bit_order='row-major, MSB first',
        missing_characters=[], empty_nonspace_characters=[], license='OFL-1.1')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--font', required=True)
    parser.add_argument('--characters', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    print(json.dumps(generate(Path(args.font), Path(args.characters), Path(args.output)), ensure_ascii=False, indent=2))
