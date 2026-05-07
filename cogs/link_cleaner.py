"""
Link Cleaner Cog — whitelist-first
===================================
Keeps only params known to be functional. Everything else is stripped.
Hard-deny list catches known tracking params even if they share names
with innocent ones. Domain-specific overrides handle edge cases.

Config commands (toggle, ignore, status, test) live in cogs/config.py.
"""

import re
import logging
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import discord
from discord.ext import commands

log = logging.getLogger("link_cleaner")

# ---------------------------------------------------------------------------
# Global whitelist — functional params kept on any domain
# ---------------------------------------------------------------------------

GLOBAL_WHITELIST: set[str] = {
    "id", "v", "p",
    "q", "query", "search", "keyword", "keywords",
    "filter", "filters", "facet",
    "sort", "order", "dir", "direction",
    "type", "category", "cat", "subcategory",
    "brand", "color", "size", "variant", "option",
    "rating", "min_price", "max_price", "price",
    "page", "pg", "offset", "limit", "per_page", "pagesize",
    "start", "end", "to",
    "lang", "locale", "language", "hl", "gl",
    "region", "country", "currency",
    "tab", "section", "view", "mode", "layout",
    "range", "interval", "period", "date",
    "token", "invite", "code", "key", "hash",
    "format", "output", "download", "raw",
    "context",
}

# ---------------------------------------------------------------------------
# Per-domain extra whitelists
# ---------------------------------------------------------------------------

DOMAIN_WHITELIST: dict[str, set[str]] = {
    "youtube.com":      {"v", "t", "list", "index", "start_radio", "ab_channel"},
    "youtu.be":         {"t"},
    "amazon.com":       {"dp", "th", "psc", "keywords"},
    "google.com":       {"q", "ll", "z", "layer", "t", "hl"},
    "docs.google.com":  {"usp"},
    "drive.google.com": {"usp"},
    "github.com":       {"q", "type", "l", "tab", "diff"},
    "reddit.com":       {"context"},
    "old.reddit.com":   {"context"},
    "twitter.com":      set(),
    "x.com":            set(),
    "ebay.com":         {"_nkw", "sacat", "LH_BIN", "LH_ItemCondition"},
    "etsy.com":         {"search_query", "explicit", "order", "ships_to", "min_price", "max_price"},
    "walmart.com":      {"facet"},
    "cvs.com":          {"skuid", "sku"},
    "open.spotify.com": set(),
    "aliexpress.com":   {"SearchText", "catId", "minPrice", "maxPrice", "shipCountry"},
    "wikipedia.org":    {"title", "action", "section", "oldid", "diff"},
}

# ---------------------------------------------------------------------------
# Hard deny — always stripped, even if somehow in a whitelist
# ---------------------------------------------------------------------------

HARD_DENY: set[str] = {
    "gclid", "gclsrc", "dclid", "gad_source", "gad_campaignid",
    "fbclid", "fb_action_ids", "fb_action_types", "mibextid",
    "msclkid", "ocid",
    "utm_source", "utm_medium", "utm_campaign", "utm_term",
    "utm_content", "utm_id", "utm_reader", "utm_name",
    "_ga", "mc_cid", "mc_eid", "yclid", "zanpid",
    "_r", "_t", "share_link_id", "share_app_id", "share_app_name",
    "tt_from", "tt_medium", "tt_campaign",
    "twclid", "si",
    "clickid", "click_id", "adid", "ad_id", "adsetid", "campaignid",
    "subid", "sub_id",
    "tag", "smid", "linkid", "sprefix",
    "pf_rd_r", "pf_rd_t", "pf_rd_p", "pf_rd_s", "pf_rd_i", "pf_rd_m",
    "pd_rd_r", "pd_rd_w", "pd_rd_wg", "pd_rd_i",
    "igshid", "icid", "spm", "wfr", "cgaa", "cid", "cmpid",
    "ref", "referer", "referrer",
}

HARD_DENY_PATTERNS: list[re.Pattern] = [
    re.compile(r"^utm_", re.I),
    re.compile(r"^gad_", re.I),
    re.compile(r"^fb_", re.I),
    re.compile(r"^pf_rd_", re.I),
    re.compile(r"^pd_rd_", re.I),
    re.compile(r"^mc_", re.I),
    re.compile(r"^aff_", re.I),
    re.compile(r"_tracking", re.I),
    re.compile(r"tracking_", re.I),
    re.compile(r"^trk", re.I),
]

URL_REGEX = re.compile(r"https?://[^\s<>\"'`\]\[)]+", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Media URL detection — skip these entirely, their params are functional
# ---------------------------------------------------------------------------

MEDIA_HOSTNAMES: set[str] = {
    "cdn.discordapp.com",
    "media.discordapp.net",
    "images-ext-1.discordapp.net",
    "images-ext-2.discordapp.net",
    "tenor.com",
    "c.tenor.com",
    "media.tenor.com",
    "giphy.com",
    "media.giphy.com",
    "i.giphy.com",
    "i.imgur.com",
    "i.redd.it",
    "preview.redd.it",
    "external-preview.redd.it",
    "pbs.twimg.com",
    "video.twimg.com",
    "i.ibb.co",
    "images.unsplash.com",
    "cloudfront.net",
}

MEDIA_EXTENSIONS: frozenset[str] = frozenset({
    ".webp", ".gif", ".png", ".jpg", ".jpeg", ".avif", ".svg",
    ".mp4", ".webm", ".mov", ".avi", ".mkv",
    ".mp3", ".ogg", ".wav", ".flac",
})


def _is_media_url(parsed) -> bool:
    hostname = (parsed.hostname or "").lower()
    for media_host in MEDIA_HOSTNAMES:
        if hostname == media_host or hostname.endswith("." + media_host):
            return True
    last_segment = parsed.path.lower().split("/")[-1]
    ext = last_segment.rsplit(".", 1)[-1] if "." in last_segment else ""
    return f".{ext}" in MEDIA_EXTENSIONS


def _registered_domain(hostname: str) -> str:
    parts = hostname.lower().lstrip("www.").split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else hostname.lower()


def _is_hard_denied(param: str) -> bool:
    lower = param.lower()
    if lower in HARD_DENY:
        return True
    return any(p.search(lower) for p in HARD_DENY_PATTERNS)


def _is_allowed(param: str, hostname: str) -> bool:
    if _is_hard_denied(param):
        return False
    lower = param.lower()
    if lower in GLOBAL_WHITELIST:
        return True
    reg = _registered_domain(hostname)
    domain_extra = {d.lower() for d in DOMAIN_WHITELIST.get(reg, set())}
    if lower in domain_extra:
        return True
    full_extra = {d.lower() for d in DOMAIN_WHITELIST.get(hostname.lower(), set())}
    return lower in full_extra


def clean_url(raw_url: str) -> str:
    try:
        parsed = urlparse(raw_url.rstrip(".,;:!?)>\"'`"))
    except Exception:
        return raw_url
    if not parsed.query:
        return raw_url
    if _is_media_url(parsed):
        return raw_url
    params = parse_qs(parsed.query, keep_blank_values=True)
    clean_params = {k: v for k, v in params.items() if _is_allowed(k, parsed.hostname or "")}
    new_query = urlencode(clean_params, doseq=True)
    cleaned = urlunparse((
        parsed.scheme, parsed.netloc, parsed.path,
        parsed.params, new_query, parsed.fragment,
    ))
    return cleaned.rstrip(".,;:!?)>")


def extract_and_clean_urls(text: str) -> list[tuple[str, str]]:
    found = URL_REGEX.findall(text)
    results, seen = [], set()
    for url in found:
        url = url.rstrip(".,;:!?)>\"'`")
        if url in seen:
            continue
        seen.add(url)
        cleaned = clean_url(url)
        if cleaned != url:
            results.append((url, cleaned))
    return results


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class LinkCleaner(commands.Cog, name="Link Cleaner"):
    """Automatically strips tracking & referral junk from URLs posted in the server."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def cog_load(self):
        log.info("Loaded.")

    def cog_unload(self):
        log.info("Unloaded.")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return
        s = self.bot.settings
        if not s.get(message.guild.id, "link_cleaner", "enabled", True):
            return
        if message.channel.id in s.get(message.guild.id, "link_cleaner", "ignored_channels", []):
            return
        dirty_pairs = extract_and_clean_urls(message.content)
        if not dirty_pairs:
            return
        embed = discord.Embed(
            title="🧹 Cleaned Link(s)",
            color=0x5865F2,
            description="Removed tracking/ad junk. Use the clean link(s) below:",
        )
        for original, cleaned in dirty_pairs:
            original_params = set(parse_qs(urlparse(original).query).keys())
            clean_params    = set(parse_qs(urlparse(cleaned).query).keys())
            stripped = original_params - clean_params
            label = f"Stripped: `{'`, `'.join(sorted(stripped))}`" if stripped else "Cleaned"
            embed.add_field(name=label, value=cleaned, inline=False)
        embed.set_footer(text=f"Original message by {message.author.display_name}")
        await message.reply(embed=embed, mention_author=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(LinkCleaner(bot))
