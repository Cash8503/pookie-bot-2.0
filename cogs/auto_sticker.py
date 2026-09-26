"""Create Discord stickers and custom emoji from the subject of an image.

Both commands use the same local image pipeline:

1. Prefer an existing transparency mask.
2. Use a local rembg model to find people, animals, and ordinary objects.
3. Fall back to OpenCV foreground detection when the model is unavailable.
4. Fall back to a detail-weighted or centered crop for difficult images.

The finished asset keeps the subject's aspect ratio, adds a small safety margin,
and is encoded as a Discord-compatible PNG. The original image is never
modified on disk and is not sent to a third-party segmentation service.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import re
import threading
from dataclasses import dataclass

import aiohttp
import discord
from discord.ext import commands

from cogs._help import documented_hybrid_command

try:
    from PIL import Image as _Image
    from PIL import ImageChops as _ImageChops
    from PIL import ImageOps as _ImageOps

    _PIL = True
    _Image.MAX_IMAGE_PIXELS = 40_000_000
except ImportError:
    _PIL = False

try:
    import cv2 as _cv2
    import numpy as _np

    _CV2 = True
except Exception:
    _CV2 = False

try:
    from rembg import new_session as _new_rembg_session
    from rembg import remove as _remove_background

    _REMBG = True
except Exception:
    _REMBG = False
    _new_rembg_session = None
    _remove_background = None

try:
    from anthropic import AsyncAnthropic as _Anthropic
except ImportError:
    _Anthropic = None


log = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5-20251001"
SEGMENTATION_MODEL = os.getenv("STICKER_SEGMENTATION_MODEL", "isnet-general-use")

STICKER_SIZE = 320
STICKER_MAX_BYTES = 510 * 1024
EMOJI_SIZE = 128
EMOJI_MAX_BYTES = 255 * 1024
MAX_DOWNLOAD_BYTES = 15 * 1024 * 1024

_STICKER_NAME_RE = re.compile(r"[^a-zA-Z0-9 _-]")
_EMOJI_NAME_RE = re.compile(r"[^a-zA-Z0-9_]")
_IMAGE_EXTENSIONS = (".avif", ".gif", ".jpeg", ".jpg", ".png", ".webp")

_rembg_session = None
_rembg_session_failed = False
_rembg_session_lock = threading.Lock()
_rembg_inference_lock = threading.Lock()


@dataclass(frozen=True)
class SubjectSelection:
    """A detected subject mask and its bounding box in source coordinates."""

    bbox: tuple[int, int, int, int]
    method: str
    mask: "_Image.Image | None" = None
    apply_mask: bool = False


@dataclass(frozen=True)
class ProcessedAsset:
    """A Discord-ready PNG plus a short description of the crop method."""

    data: bytes
    method: str
    size: int


def _sanitize_sticker_name(name: str) -> str:
    cleaned = _STICKER_NAME_RE.sub("", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" _-")[:30]
    return cleaned if len(cleaned) >= 2 else "sticker"


def _sanitize_emoji_name(name: str) -> str:
    cleaned = re.sub(r"[\s-]+", "_", name.strip())
    cleaned = _EMOJI_NAME_RE.sub("", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")[:32]
    return cleaned if len(cleaned) >= 2 else "emoji"


def _sanitize_name(name: str) -> str:
    """Backward-compatible sticker-name sanitiser."""

    return _sanitize_sticker_name(name)


def _open_image(raw: bytes) -> "_Image.Image":
    if not _PIL:
        raise RuntimeError("Pillow is not installed")
    if not raw:
        raise ValueError("The image file is empty")

    try:
        with _Image.open(io.BytesIO(raw)) as source:
            if source.width * source.height > _Image.MAX_IMAGE_PIXELS:
                raise ValueError("Image dimensions exceed the 40 megapixel safety limit")
            source.seek(0)
            image = _ImageOps.exif_transpose(source)
            image.load()
            return image.convert("RGBA")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("The downloaded file is not a readable image") from exc


def _make_thumb(raw: bytes, size: int = 512) -> bytes:
    """Shrink an image to a small JPEG for the optional Claude naming call."""

    image = _open_image(raw)
    white = _Image.new("RGBA", image.size, (255, 255, 255, 255))
    white.alpha_composite(image)
    thumb = white.convert("RGB")
    thumb.thumbnail((size, size), _Image.Resampling.LANCZOS)
    out = io.BytesIO()
    thumb.save(out, format="JPEG", quality=85, optimize=True)
    return out.getvalue()


def _mask_coverage(mask: "_Image.Image", threshold: int = 24) -> float:
    histogram = mask.convert("L").histogram()
    foreground = sum(histogram[threshold:])
    total = mask.width * mask.height
    return foreground / total if total else 0.0


def _clean_mask(mask: "_Image.Image") -> "_Image.Image | None":
    """Remove tiny disconnected mask fragments and reject unusable masks."""

    mask = mask.convert("L")
    if not _CV2:
        coverage = _mask_coverage(mask)
        return mask if 0.003 <= coverage <= 0.96 and mask.getbbox() else None

    values = _np.asarray(mask, dtype=_np.uint8)
    binary = (values >= 24).astype(_np.uint8)
    height, width = binary.shape
    kernel_size = max(3, int(round(min(width, height) / 180)))
    if kernel_size % 2 == 0:
        kernel_size += 1
    kernel = _np.ones((kernel_size, kernel_size), dtype=_np.uint8)
    binary = _cv2.morphologyEx(binary, _cv2.MORPH_OPEN, kernel)
    binary = _cv2.morphologyEx(binary, _cv2.MORPH_CLOSE, kernel)

    count, labels, stats, centers = _cv2.connectedComponentsWithStats(binary, 8)
    if count <= 1:
        return None

    areas = stats[1:, _cv2.CC_STAT_AREA]
    primary = int(_np.argmax(areas)) + 1
    primary_area = int(stats[primary, _cv2.CC_STAT_AREA])
    minimum_area = max(8, int(width * height * 0.0008))
    px, py = centers[primary]
    pw = int(stats[primary, _cv2.CC_STAT_WIDTH])
    ph = int(stats[primary, _cv2.CC_STAT_HEIGHT])
    proximity = max(pw, ph) * 1.5

    kept = _np.zeros_like(binary)
    for label in range(1, count):
        area = int(stats[label, _cv2.CC_STAT_AREA])
        if area < minimum_area:
            continue
        cx, cy = centers[label]
        close_to_primary = abs(cx - px) <= proximity and abs(cy - py) <= proximity
        if label == primary or area >= primary_area * 0.12 or close_to_primary:
            kept[labels == label] = 1

    coverage = float(kept.mean())
    if not 0.003 <= coverage <= 0.96:
        return None

    cleaned = _np.where(kept != 0, values, 0).astype(_np.uint8)
    cleaned = _cv2.GaussianBlur(cleaned, (0, 0), sigmaX=0.7)
    return _Image.fromarray(cleaned, mode="L")


def _mask_bbox(mask: "_Image.Image") -> tuple[int, int, int, int] | None:
    binary = mask.point(lambda value: 255 if value >= 24 else 0, mode="L")
    return binary.getbbox()


def _rembg_mask(image: "_Image.Image") -> "_Image.Image | None":
    """Return a local general-purpose AI subject mask, if available."""

    global _rembg_session, _rembg_session_failed

    if not _REMBG or _rembg_session_failed:
        return None

    try:
        with _rembg_session_lock:
            if _rembg_session is None:
                assert _new_rembg_session is not None
                _rembg_session = _new_rembg_session(SEGMENTATION_MODEL)

        assert _remove_background is not None
        with _rembg_inference_lock:
            result = _remove_background(
                image.convert("RGB"),
                session=_rembg_session,
                only_mask=True,
                post_process_mask=True,
            )
        if not isinstance(result, _Image.Image):
            result = _Image.open(io.BytesIO(result))
        return _clean_mask(result.convert("L"))
    except Exception:
        _rembg_session_failed = True
        log.warning(
            "Local subject model %s failed; using OpenCV fallback.",
            SEGMENTATION_MODEL,
            exc_info=True,
        )
        return None


def _opencv_mask(image: "_Image.Image") -> "_Image.Image | None":
    """Estimate the foreground from border contrast and GrabCut."""

    if not _CV2:
        return None

    rgb = _np.asarray(image.convert("RGB"))
    height, width = rgb.shape[:2]
    scale = min(1.0, 720.0 / max(width, height))
    if scale < 1.0:
        small = _cv2.resize(
            rgb,
            (max(2, round(width * scale)), max(2, round(height * scale))),
            interpolation=_cv2.INTER_AREA,
        )
    else:
        small = rgb

    sh, sw = small.shape[:2]
    if min(sw, sh) < 8:
        return None

    grab_labels = _np.zeros((sh, sw), dtype=_np.uint8)
    inset = max(1, round(min(sw, sh) * 0.025))
    rect = (inset, inset, max(1, sw - 2 * inset), max(1, sh - 2 * inset))
    background_model = _np.zeros((1, 65), dtype=_np.float64)
    foreground_model = _np.zeros((1, 65), dtype=_np.float64)
    try:
        _cv2.grabCut(
            _cv2.cvtColor(small, _cv2.COLOR_RGB2BGR),
            grab_labels,
            rect,
            background_model,
            foreground_model,
            4,
            _cv2.GC_INIT_WITH_RECT,
        )
        grab = _np.isin(grab_labels, (_cv2.GC_FGD, _cv2.GC_PR_FGD)).astype(_np.uint8)
    except _cv2.error:
        grab = _np.zeros((sh, sw), dtype=_np.uint8)

    lab = _cv2.cvtColor(small, _cv2.COLOR_RGB2LAB).astype(_np.float32)
    border = max(1, round(min(sw, sh) * 0.035))
    samples = _np.concatenate(
        (
            lab[:border].reshape(-1, 3),
            lab[-border:].reshape(-1, 3),
            lab[:, :border].reshape(-1, 3),
            lab[:, -border:].reshape(-1, 3),
        ),
        axis=0,
    )
    background_color = _np.median(samples, axis=0)
    distance = _np.linalg.norm(lab - background_color, axis=2)
    upper = float(_np.percentile(distance, 98))
    if upper > 0:
        distance_u8 = _np.clip(distance * 255.0 / upper, 0, 255).astype(_np.uint8)
        _, contrast = _cv2.threshold(
            distance_u8, 0, 1, _cv2.THRESH_BINARY + _cv2.THRESH_OTSU
        )
    else:
        contrast = _np.zeros((sh, sw), dtype=_np.uint8)

    grab_coverage = float(grab.mean())
    contrast_coverage = float(contrast.mean())
    grab_valid = 0.005 <= grab_coverage <= 0.92
    contrast_valid = 0.005 <= contrast_coverage <= 0.92

    if grab_valid and contrast_valid:
        intersection = grab & contrast
        proposal = intersection if float(intersection.mean()) >= 0.004 else grab | contrast
    elif grab_valid:
        proposal = grab
    elif contrast_valid:
        proposal = contrast
    else:
        return None

    proposal = (proposal * 255).astype(_np.uint8)
    if (sw, sh) != (width, height):
        proposal = _cv2.resize(proposal, (width, height), interpolation=_cv2.INTER_LINEAR)
    return _clean_mask(_Image.fromarray(proposal, mode="L"))


def _detail_bbox(image: "_Image.Image") -> tuple[int, int, int, int] | None:
    """Locate the densest detailed region without pretending it is a clean mask."""

    if not _CV2:
        return None

    gray = _cv2.cvtColor(_np.asarray(image.convert("RGB")), _cv2.COLOR_RGB2GRAY)
    height, width = gray.shape
    edges = _cv2.Canny(gray, 35, 110).astype(_np.float32)
    sigma = max(width, height) * 0.04
    density = _cv2.GaussianBlur(edges, (0, 0), sigmaX=max(1.0, sigma))
    total = float(density.sum())
    if total <= 0:
        return None

    ys, xs = _np.mgrid[0:height, 0:width]
    cx = float((xs * density).sum() / total)
    cy = float((ys * density).sum() / total)
    x_std = float(_np.sqrt(((xs - cx) ** 2 * density).sum() / total))
    y_std = float(_np.sqrt(((ys - cy) ** 2 * density).sum() / total))
    half_width = max(width * 0.16, x_std * 1.8)
    half_height = max(height * 0.16, y_std * 1.8)
    return (
        max(0, round(cx - half_width)),
        max(0, round(cy - half_height)),
        min(width, round(cx + half_width)),
        min(height, round(cy + half_height)),
    )


def _select_subject(image: "_Image.Image") -> SubjectSelection:
    """Find one subject using the strongest available local signal."""

    width, height = image.size
    alpha = image.getchannel("A")
    alpha_coverage = _mask_coverage(alpha)
    if alpha.getextrema() != (255, 255) and 0.003 <= alpha_coverage <= 0.96:
        clean_alpha = _clean_mask(alpha)
        if clean_alpha is not None:
            bbox = _mask_bbox(clean_alpha)
            if bbox:
                return SubjectSelection(bbox, "existing transparency", alpha, False)

    model_mask = _rembg_mask(image)
    if model_mask is not None:
        bbox = _mask_bbox(model_mask)
        if bbox:
            return SubjectSelection(bbox, "AI subject detection", model_mask, True)

    foreground_mask = _opencv_mask(image)
    if foreground_mask is not None:
        bbox = _mask_bbox(foreground_mask)
        if bbox:
            return SubjectSelection(bbox, "foreground detection")

    detail_bbox = _detail_bbox(image)
    if detail_bbox:
        return SubjectSelection(detail_bbox, "detail-aware crop")

    return SubjectSelection((0, 0, width, height), "center crop")


def _padded_bbox(
    bbox: tuple[int, int, int, int],
    image_size: tuple[int, int],
    padding: float = 0.14,
) -> tuple[int, int, int, int]:
    width, height = image_size
    left, top, right, bottom = bbox
    subject_width = max(1, right - left)
    subject_height = max(1, bottom - top)
    pad = max(2, round(max(subject_width, subject_height) * padding))
    return (
        max(0, left - pad),
        max(0, top - pad),
        min(width, right + pad),
        min(height, bottom + pad),
    )


def _render_square(
    image: "_Image.Image",
    selection: SubjectSelection,
    size: int,
) -> "_Image.Image":
    working = image.copy()
    if selection.apply_mask and selection.mask is not None:
        combined_alpha = _ImageChops.multiply(working.getchannel("A"), selection.mask)
        working.putalpha(combined_alpha)

    crop = working.crop(_padded_bbox(selection.bbox, working.size))
    margin = max(2, round(size * 0.035))
    available = size - margin * 2
    scale = min(available / crop.width, available / crop.height)
    resized = (
        max(1, round(crop.width * scale)),
        max(1, round(crop.height * scale)),
    )
    crop = crop.resize(resized, _Image.Resampling.LANCZOS)

    canvas = _Image.new("RGBA", (size, size), (0, 0, 0, 0))
    offset = ((size - crop.width) // 2, (size - crop.height) // 2)
    canvas.alpha_composite(crop, offset)
    return canvas


def _encode_png(image: "_Image.Image", max_bytes: int) -> bytes:
    """Encode an exact-size PNG, reducing its palette only when necessary."""

    def save(candidate: "_Image.Image") -> bytes:
        out = io.BytesIO()
        candidate.save(out, format="PNG", optimize=True, compress_level=9)
        return out.getvalue()

    data = save(image)
    if len(data) <= max_bytes:
        return data

    for colors in (256, 192, 128, 96, 64, 32):
        palette = image.quantize(
            colors=colors,
            method=_Image.Quantize.FASTOCTREE,
            dither=_Image.Dither.FLOYDSTEINBERG,
        )
        data = save(palette)
        if len(data) <= max_bytes:
            return data

    raise ValueError(f"Could not compress the PNG below {max_bytes // 1024} KB")


def _process_asset(raw: bytes, kind: str) -> ProcessedAsset:
    if not _PIL:
        raise RuntimeError("Pillow is not installed — install the project requirements")
    if kind == "sticker":
        size, max_bytes = STICKER_SIZE, STICKER_MAX_BYTES
    elif kind == "emoji":
        size, max_bytes = EMOJI_SIZE, EMOJI_MAX_BYTES
    else:
        raise ValueError(f"Unknown expression type: {kind}")

    image = _open_image(raw)
    selection = _select_subject(image)
    rendered = _render_square(image, selection, size)
    return ProcessedAsset(_encode_png(rendered, max_bytes), selection.method, size)


def _subject_crop(raw: bytes) -> "_Image.Image":
    """Backward-compatible helper returning the padded subject crop."""

    image = _open_image(raw)
    selection = _select_subject(image)
    return image.crop(_padded_bbox(selection.bbox, image.size))


def _process_image(raw: bytes) -> bytes:
    """Backward-compatible helper returning a Discord-ready sticker PNG."""

    return _process_asset(raw, "sticker").data


class AutoStickerCog(commands.Cog, name="Expressions"):
    """Turns image subjects into server stickers and custom emoji using local cropping."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        api_key = os.getenv("ANTHROPIC_API_KEY")
        self.ai = _Anthropic(api_key=api_key) if _Anthropic and api_key else None

    def cog_load(self):
        log.info(
            "Expressions cog loaded. Pillow=%s rembg=%s OpenCV=%s model=%s",
            _PIL,
            _REMBG,
            _CV2,
            SEGMENTATION_MODEL,
        )
        if not _PIL:
            log.warning("Pillow is unavailable; sticker and emoji commands cannot process images.")
        if not _REMBG:
            log.warning("rembg is unavailable; subject detection will use OpenCV fallbacks.")

    def cog_unload(self):
        log.info("Expressions cog unloaded.")

    @staticmethod
    def _message_image_url(message: discord.Message) -> str | None:
        for attachment in message.attachments:
            content_type = (attachment.content_type or "").lower()
            filename = attachment.filename.lower()
            if content_type.startswith("image/") or filename.endswith(_IMAGE_EXTENSIONS):
                return attachment.url
        for embed in message.embeds:
            if embed.image and embed.image.url:
                return embed.image.url
            if embed.thumbnail and embed.thumbnail.url:
                return embed.thumbnail.url
        return None

    async def _find_image(self, ctx: commands.Context) -> tuple[str, str] | None:
        """Find an image in a reply, the command attachment, or recent history."""

        if ctx.message.reference:
            referenced = ctx.message.reference.resolved
            if not isinstance(referenced, discord.Message) and ctx.message.reference.message_id:
                try:
                    referenced = await ctx.channel.fetch_message(ctx.message.reference.message_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    referenced = None
            if isinstance(referenced, discord.Message):
                url = self._message_image_url(referenced)
                if url:
                    return url, referenced.author.display_name

        url = self._message_image_url(ctx.message)
        if url:
            return url, ctx.author.display_name

        async for message in ctx.channel.history(limit=20, before=ctx.message):
            url = self._message_image_url(message)
            if url:
                return url, message.author.display_name
        return None

    async def _download_image(self, url: str) -> bytes:
        timeout = aiohttp.ClientTimeout(total=30, connect=10)
        headers = {"User-Agent": "PookieBot/2.0 expression-maker"}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url, allow_redirects=True) as response:
                response.raise_for_status()
                if response.content_length and response.content_length > MAX_DOWNLOAD_BYTES:
                    raise ValueError("That image is larger than the 15 MB download limit")

                chunks: list[bytes] = []
                total = 0
                async for chunk in response.content.iter_chunked(64 * 1024):
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise ValueError("That image is larger than the 15 MB download limit")
                    chunks.append(chunk)
                return b"".join(chunks)

    async def _generate_name(self, sender: str, raw: bytes, kind: str) -> str:
        """Ask Claude for a short name, with deterministic local fallback names."""

        sanitize = _sanitize_emoji_name if kind == "emoji" else _sanitize_sticker_name
        if not self.ai:
            return sanitize(sender)

        try:
            loop = asyncio.get_running_loop()
            thumb = await loop.run_in_executor(None, _make_thumb, raw)
            separator = "underscores" if kind == "emoji" else "spaces"
            response = await asyncio.wait_for(
                self.ai.messages.create(
                    model=MODEL,
                    max_tokens=20,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": "image/jpeg",
                                        "data": base64.standard_b64encode(thumb).decode(),
                                    },
                                },
                                {
                                    "type": "text",
                                    "text": (
                                        f"Create a playful Discord {kind} name for this image. "
                                        f"The sender is called '{sender}'. Describe the subject or "
                                        f"expression in one or two words, optionally using the sender's "
                                        f"name. Use only letters, numbers, and {separator}; keep it under "
                                        "25 characters. Reply with only the name."
                                    ),
                                },
                            ],
                        }
                    ],
                ),
                timeout=15,
            )
            text = getattr(response.content[0], "text", "") or ""
            return sanitize(text.strip())
        except Exception as exc:
            log.warning("%s name generation failed: %s", kind.title(), exc)
            return sanitize(sender)

    async def _prepare_asset(
        self,
        ctx: commands.Context,
        kind: str,
        requested_name: str,
    ) -> tuple[ProcessedAsset, str] | None:
        found = await self._find_image(ctx)
        if not found:
            await ctx.send(
                "❌ No image found. Attach one, reply to one, or run the command after a recent image."
            )
            return None

        image_url, sender = found
        try:
            raw = await self._download_image(image_url)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            await ctx.send(f"❌ I couldn't download that image: {exc}")
            return None

        sanitize = _sanitize_emoji_name if kind == "emoji" else _sanitize_sticker_name
        name = (
            sanitize(requested_name)
            if requested_name.strip()
            else await self._generate_name(sender, raw, kind)
        )

        try:
            loop = asyncio.get_running_loop()
            processed = await loop.run_in_executor(None, _process_asset, raw, kind)
        except Exception as exc:
            log.warning("%s processing failed", kind.title(), exc_info=True)
            await ctx.send(f"❌ I couldn't process that image: {exc}")
            return None
        return processed, name

    @documented_hybrid_command(name="sticker")
    @commands.guild_only()
    @commands.has_guild_permissions(create_expressions=True)
    @commands.bot_has_guild_permissions(create_expressions=True)
    async def sticker(self, ctx: commands.Context, *, name: str = ""):
        """Create a subject-focused server sticker

        Finds the main person, animal, or object, isolates it when possible,
        and creates a 320x320 PNG sticker in this server.

        Usage:
            {prefix}sticker [name]

        Arguments:
            name: Optional sticker name; an image-based name is generated when omitted.

        Examples:
            {prefix}sticker
            {prefix}sticker judgement cat

        Notes:
            Attach an image, reply to one, or place the command after a recent image.
            The local subject model downloads once on its first use.
        """

        async with ctx.typing():
            prepared = await self._prepare_asset(ctx, "sticker", name)
            if prepared is None:
                return
            processed, sticker_name = prepared

            assert ctx.guild is not None
            try:
                sticker = await ctx.guild.create_sticker(
                    name=sticker_name,
                    description=f"Made by {ctx.author.display_name}"[:100],
                    emoji="🎭",
                    file=discord.File(io.BytesIO(processed.data), filename="sticker.png"),
                    reason=f"Sticker command by {ctx.author} ({ctx.author.id})",
                )
                await ctx.send(
                    f"✅ Created **{sticker.name}** · {processed.method}",
                    stickers=[sticker],
                )
            except discord.Forbidden:
                await ctx.send("❌ I need permission to create expressions in this server.")
            except discord.HTTPException as exc:
                if "maximum" in str(exc).lower() or "limit" in str(exc).lower():
                    await ctx.send("❌ This server has reached its sticker limit.")
                else:
                    await ctx.send(f"❌ Discord rejected the sticker: {exc}")

    @documented_hybrid_command(name="emoji")
    @commands.guild_only()
    @commands.has_guild_permissions(create_expressions=True)
    @commands.bot_has_guild_permissions(create_expressions=True)
    async def emoji(self, ctx: commands.Context, *, name: str = ""):
        """Create a subject-focused custom emoji

        Finds the main person, animal, or object, isolates it when possible,
        and creates a 128x128 PNG custom emoji in this server.

        Usage:
            {prefix}emoji [name]

        Arguments:
            name: Optional emoji name using letters, numbers, or underscores.

        Examples:
            {prefix}emoji
            {prefix}emoji judgement_cat

        Notes:
            Attach an image, reply to one, or place the command after a recent image.
            Spaces and hyphens in a supplied name become underscores.
        """

        async with ctx.typing():
            prepared = await self._prepare_asset(ctx, "emoji", name)
            if prepared is None:
                return
            processed, emoji_name = prepared

            assert ctx.guild is not None
            try:
                emoji = await ctx.guild.create_custom_emoji(
                    name=emoji_name,
                    image=processed.data,
                    reason=f"Emoji command by {ctx.author} ({ctx.author.id})",
                )
                await ctx.send(
                    f"✅ Created {emoji} `:{emoji.name}:` · {processed.method}"
                )
            except discord.Forbidden:
                await ctx.send("❌ I need permission to create expressions in this server.")
            except discord.HTTPException as exc:
                if "maximum" in str(exc).lower() or "limit" in str(exc).lower():
                    await ctx.send("❌ This server has reached its custom emoji limit.")
                else:
                    await ctx.send(f"❌ Discord rejected the emoji: {exc}")

    async def _command_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ You need **Create Expressions** permission to use this.")
        elif isinstance(error, commands.BotMissingPermissions):
            await ctx.send("❌ I need **Create Expressions** permission in this server.")
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send("❌ Stickers and custom emoji can only be created in a server.")

    @sticker.error
    async def sticker_error(self, ctx: commands.Context, error: commands.CommandError):
        await self._command_error(ctx, error)

    @emoji.error
    async def emoji_error(self, ctx: commands.Context, error: commands.CommandError):
        await self._command_error(ctx, error)


async def setup(bot: commands.Bot):
    await bot.add_cog(AutoStickerCog(bot))
