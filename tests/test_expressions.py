import io

import pytest

from cogs import auto_sticker


pytestmark = pytest.mark.skipif(not auto_sticker._PIL, reason="Pillow is required")


def _png(image) -> bytes:
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_expression_names_follow_discord_rules():
    assert auto_sticker._sanitize_sticker_name("  cat!!! judging  ") == "cat judging"
    assert auto_sticker._sanitize_emoji_name(" Cat judging-now! ") == "Cat_judging_now"
    assert auto_sticker._sanitize_emoji_name("!") == "emoji"


def test_transparent_subject_is_detected_off_center():
    image = auto_sticker._Image.new("RGBA", (600, 300), (0, 0, 0, 0))
    subject = auto_sticker._Image.new("RGBA", (90, 180), (255, 40, 40, 255))
    image.alpha_composite(subject, (450, 60))

    selection = auto_sticker._select_subject(image)
    left, top, right, bottom = selection.bbox

    assert selection.method == "existing transparency"
    assert left >= 440
    assert right <= 550
    assert top <= 65
    assert bottom >= 235


@pytest.mark.skipif(not auto_sticker._CV2, reason="OpenCV is required")
def test_foreground_fallback_finds_plain_object(monkeypatch):
    monkeypatch.setattr(auto_sticker, "_REMBG", False)
    image = auto_sticker._Image.new("RGB", (640, 360), (245, 245, 245))
    subject = auto_sticker._Image.new("RGB", (130, 210), (20, 90, 210))
    image.paste(subject, (440, 80))

    selection = auto_sticker._select_subject(image.convert("RGBA"))
    left, _top, right, _bottom = selection.bbox

    assert selection.method in {"foreground detection", "detail-aware crop"}
    assert left > 300
    assert right > 500


@pytest.mark.parametrize(
    ("kind", "expected_size", "max_bytes"),
    [
        ("sticker", auto_sticker.STICKER_SIZE, auto_sticker.STICKER_MAX_BYTES),
        ("emoji", auto_sticker.EMOJI_SIZE, auto_sticker.EMOJI_MAX_BYTES),
    ],
)
def test_processed_assets_are_exact_discord_png_sizes(monkeypatch, kind, expected_size, max_bytes):
    monkeypatch.setattr(auto_sticker, "_REMBG", False)
    image = auto_sticker._Image.new("RGBA", (480, 300), (0, 0, 0, 0))
    subject = auto_sticker._Image.new("RGBA", (150, 240), (120, 30, 220, 255))
    image.alpha_composite(subject, (280, 30))

    result = auto_sticker._process_asset(_png(image), kind)

    assert result.data.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(result.data) <= max_bytes
    with auto_sticker._Image.open(io.BytesIO(result.data)) as finished:
        assert finished.size == (expected_size, expected_size)
        assert finished.format == "PNG"
        alpha_box = finished.convert("RGBA").getchannel("A").getbbox()
        assert alpha_box is not None
        width = alpha_box[2] - alpha_box[0]
        height = alpha_box[3] - alpha_box[1]
        # Subject fills most of the canvas while retaining a safety margin.
        assert max(width, height) >= expected_size * 0.75
