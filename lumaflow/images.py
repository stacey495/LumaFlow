import io
import warnings

from PIL import Image, ImageChops, ImageFilter, ImageOps

from .config import FORMATS, MAX_BYTES, MAX_PIXELS, MAX_RESULT_BYTES, MAX_SIDE, PROFILES

Image.MAX_IMAGE_PIXELS = MAX_PIXELS


def image_info(data):
    if not data or len(data) > MAX_BYTES:
        raise ValueError("Файл має важити від 1 байта до 10 МБ.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data), formats=tuple(FORMATS)) as image:
                width, height = image.size
                if max(width, height) > MAX_SIDE or width * height > MAX_PIXELS:
                    raise ValueError("Ліміт зображення — 6 Мп і 5000 пікселів на сторону.")
                if getattr(image, "n_frames", 1) != 1:
                    raise ValueError("Вибери статичне зображення без анімації.")
                if image.getexif().get(274) in (5, 6, 7, 8):
                    width, height = height, width
                kind = image.format
                image.verify()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise ValueError("Ліміт зображення — 6 Мп і 5000 пікселів на сторону.") from error
    except (OSError, SyntaxError, TypeError, OverflowError) as error:
        raise ValueError("Потрібен цілий статичний файл JPG, PNG або WebP.") from error
    return width, height, FORMATS[kind]


def restore(source, target, profile, report):
    report(3, "Декодування зображення")
    with Image.open(source, formats=tuple(FORMATS)) as opened:
        rotated = ImageOps.exif_transpose(opened)
        rgba = rotated.convert("RGBA")
        image = Image.new("RGB", rgba.size, "white")
        image.paste(rgba, mask=rgba.getchannel("A"))
    del rgba, rotated
    width, height = image.size
    spec = PROFILES[profile]
    kernel = spec["kernel"]
    passes = spec["passes"]
    halo = (kernel // 2) * passes
    strip = max(1, min(96, 180_000 // width))
    cleaned = Image.new("RGB", image.size)
    report(9, "Медіанне шумозаглушення")
    median = ImageFilter.MedianFilter(kernel)
    for top in range(0, height, strip):
        bottom = min(height, top + strip)
        region_top = max(0, top - halo)
        region_bottom = min(height, bottom + halo)
        region = image.crop((0, region_top, width, region_bottom))
        for _ in range(passes):
            region = region.filter(median)
        center = region.crop((0, top - region_top, width, bottom - region_top))
        cleaned.paste(center, (0, top))
        report(9 + int(77 * bottom / height), "Медіанне шумозаглушення")
    report(89, "Підсилення контурів")
    output = cleaned.filter(ImageFilter.UnsharpMask(radius=1.3, percent=55, threshold=3))
    report(95, "Експорт результату в JPG")
    for quality in (94, 85, 72):
        output.save(target, "JPEG", quality=quality, subsampling=0)
        if target.stat().st_size <= MAX_RESULT_BYTES:
            return
    raise ValueError("Результат перевищив дозволений обсяг.")
