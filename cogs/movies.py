import logging
import os

import aiohttp
import discord
from discord.ext import commands

from cogs._help import helped_command, helped_group, helped_hybrid_command, helped_hybrid_group

log = logging.getLogger(__name__)

TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMG  = "https://image.tmdb.org/t/p/w300"
OMDB_URL  = "http://www.omdbapi.com/"


# ------------------------------------------------------------------ #
#  API helpers
# ------------------------------------------------------------------ #

async def _search_movies(session: aiohttp.ClientSession, key: str, query: str) -> list[dict]:
    async with session.get(
        f"{TMDB_BASE}/search/movie",
        params={"query": query, "api_key": key, "include_adult": "false"},
    ) as resp:
        data = await resp.json(content_type=None)
    return data.get("results", [])


async def _get_movie(session: aiohttp.ClientSession, key: str, movie_id: int) -> dict:
    async with session.get(
        f"{TMDB_BASE}/movie/{movie_id}",
        params={"api_key": key, "append_to_response": "watch/providers,release_dates"},
    ) as resp:
        return await resp.json(content_type=None)


async def _get_omdb_ratings(
    session: aiohttp.ClientSession, omdb_key: str, imdb_id: str
) -> list[dict]:
    """Fetch IMDb / RT / Metacritic ratings from OMDB via IMDB ID."""
    try:
        async with session.get(
            OMDB_URL, params={"i": imdb_id, "apikey": omdb_key}
        ) as resp:
            data = await resp.json(content_type=None)
        return data.get("Ratings", [])
    except Exception:
        return []


async def _get_genre_map(session: aiohttp.ClientSession, key: str) -> dict[str, int]:
    async with session.get(
        f"{TMDB_BASE}/genre/movie/list", params={"api_key": key}
    ) as resp:
        data = await resp.json(content_type=None)
    return {g["name"].lower(): g["id"] for g in data.get("genres", [])}


async def _discover_by_genre(
    session: aiohttp.ClientSession, key: str, genre_id: int
) -> list[dict]:
    async with session.get(
        f"{TMDB_BASE}/discover/movie",
        params={
            "api_key": key,
            "with_genres": genre_id,
            "sort_by": "popularity.desc",
            "include_adult": "false",
        },
    ) as resp:
        data = await resp.json(content_type=None)
    return data.get("results", [])


# ------------------------------------------------------------------ #
#  Embed builder
# ------------------------------------------------------------------ #

_SOURCE_LABELS = {
    "Internet Movie Database": "IMDb",
    "Rotten Tomatoes": "Rotten Tomatoes",
    "Metacritic": "Metacritic",
}


def _build_embed(movie: dict, omdb_ratings: list[dict] | None = None) -> discord.Embed:
    title    = movie.get("title") or "Unknown"
    year     = (movie.get("release_date") or "")[:4] or "?"
    overview = movie.get("overview") or "No overview available."
    rating   = movie.get("vote_average") or 0
    votes    = movie.get("vote_count") or 0
    runtime  = movie.get("runtime")
    genres   = [g["name"] for g in (movie.get("genres") or [])]

    # US content rating (G / PG / PG-13 / R …)
    cert = ""
    for rc in (movie.get("release_dates") or {}).get("results", []):
        if rc.get("iso_3166_1") == "US":
            for rd in rc.get("release_dates", []):
                if rd.get("certification"):
                    cert = rd["certification"]
                    break
            break

    # Streaming availability (US)
    us     = (movie.get("watch/providers") or {}).get("results", {}).get("US", {})
    stream = [p["provider_name"] for p in us.get("flatrate", [])]
    rent   = [p["provider_name"] for p in us.get("rent", [])]
    buy    = [p["provider_name"] for p in us.get("buy", [])]

    embed = discord.Embed(
        title=f"{title} ({year})",
        description=overview,
        color=discord.Color.dark_blue(),
    )

    if movie.get("poster_path"):
        embed.set_thumbnail(url=f"{TMDB_IMG}{movie['poster_path']}")

    # Genre / runtime / cert row
    meta = []
    if genres:
        meta.append(" · ".join(genres[:3]))
    if runtime:
        meta.append(f"{runtime} min")
    if cert:
        meta.append(cert)
    if meta:
        embed.add_field(name="\u200b", value="  |  ".join(meta), inline=False)

    # Ratings — OMDB sources first, then TMDB
    rating_lines = []
    for r in (omdb_ratings or []):
        label = _SOURCE_LABELS.get(r["Source"], r["Source"])
        rating_lines.append(f"**{label}:** {r['Value']}")
    if votes > 0:
        rating_lines.append(f"**TMDB:** {rating:.1f}/10 ({votes:,} votes)")
    if rating_lines:
        embed.add_field(name="⭐ Ratings", value="\n".join(rating_lines), inline=False)

    # Where to watch
    watch_lines = []
    if stream:
        watch_lines.append(f"**Stream:** {', '.join(stream[:6])}")
    if rent:
        watch_lines.append(f"**Rent:** {', '.join(rent[:6])}")
    if buy:
        watch_lines.append(f"**Buy:** {', '.join(buy[:6])}")

    embed.add_field(
        name="📺 Where to Watch (US)",
        value="\n".join(watch_lines) if watch_lines else "Not available for streaming in the US",
        inline=False,
    )

    embed.set_footer(text="Powered by TMDB · Ratings via OMDB")
    return embed


# ------------------------------------------------------------------ #
#  Views
# ------------------------------------------------------------------ #

class MovieSelectView(discord.ui.View):
    def __init__(
        self,
        results: list[dict],
        session: aiohttp.ClientSession,
        api_key: str,
        omdb_key: str | None = None,
    ):
        super().__init__(timeout=30)
        self._session  = session
        self._api_key  = api_key
        self._omdb_key = omdb_key

        options = []
        for movie in results[:25]:
            year  = (movie.get("release_date") or "")[:4]
            label = (movie.get("title") or "Unknown")[:100]
            blurb = (movie.get("overview") or "")
            desc  = f"{year} — {blurb}"[:100] if blurb else year
            options.append(discord.SelectOption(
                label=label,
                value=str(movie["id"]),
                description=desc or None,
            ))

        sel = discord.ui.Select(placeholder="Choose a movie...", options=options)
        sel.callback = self._on_select
        self.add_item(sel)

    async def _on_select(self, interaction: discord.Interaction):
        await interaction.response.defer()
        movie_id = int(interaction.data["values"][0])
        movie = await _get_movie(self._session, self._api_key, movie_id)
        omdb_ratings = []
        if self._omdb_key and movie.get("imdb_id"):
            omdb_ratings = await _get_omdb_ratings(self._session, self._omdb_key, movie["imdb_id"])
        await interaction.edit_original_response(
            content=None, embed=_build_embed(movie, omdb_ratings), view=None
        )

    async def on_timeout(self):
        pass


class WrongMovieView(discord.ui.View):
    """Shown with the auto-selected top result. Opens the full picker on click."""

    def __init__(
        self,
        results: list[dict],
        session: aiohttp.ClientSession,
        api_key: str,
        omdb_key: str | None = None,
    ):
        super().__init__(timeout=60)
        self._results  = results
        self._session  = session
        self._api_key  = api_key
        self._omdb_key = omdb_key

    @discord.ui.button(label="Wrong Movie?", style=discord.ButtonStyle.secondary, emoji="🔽")
    async def wrong_movie(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = MovieSelectView(self._results, self._session, self._api_key, self._omdb_key)
        await interaction.response.edit_message(
            content=f"Pick from **{len(self._results)}** results:",
            embed=None,
            view=view,
        )

    async def on_timeout(self):
        pass


# ------------------------------------------------------------------ #
#  Cog
# ------------------------------------------------------------------ #

class MoviesCog(commands.Cog, name="Movies"):
    """Movie lookup, ratings, and streaming availability via TMDB."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._session: aiohttp.ClientSession | None = None

    def cog_load(self):
        log.info("Cog Loaded.")

    async def cog_unload(self):
        if self._session and not self._session.closed:
            await self._session.close()
        log.info("Cog Unloaded.")

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    # ------------------------------------------------------------------ #
    #  Commands
    # ------------------------------------------------------------------ #

    @helped_hybrid_group("movie",
        name="movie",
        invoke_without_command=True,
        case_insensitive=True,
    )
    async def movie(self, ctx: commands.Context, *, query: str = None):
        if not query:
            await ctx.send(
                "**Movies**\n"
                "`!movie <title>` — Search by title\n"
                "`!movie genre <name>` — Browse by genre\n\n"
                "Run `!help movie` for more details."
            )
            return

        tmdb_key = os.getenv("TMDB_API_KEY")
        if not tmdb_key:
            await ctx.send(
                "❌ `TMDB_API_KEY` is not set. Get a free key at "
                "https://www.themoviedb.org/settings/api and add it to your `.env`.",
                ephemeral=True,
            )
            return

        omdb_key = os.getenv("OMDB_API_KEY")

        await ctx.defer()
        session = await self._get_session()
        results = await _search_movies(session, tmdb_key, query)

        if not results:
            await ctx.send(f"❌ No results found for **{query}**.")
            return

        # Always auto-show the top result
        movie = await _get_movie(session, tmdb_key, results[0]["id"])
        omdb_ratings = []
        if omdb_key and movie.get("imdb_id"):
            omdb_ratings = await _get_omdb_ratings(session, omdb_key, movie["imdb_id"])

        embed = _build_embed(movie, omdb_ratings)

        if len(results) == 1:
            await ctx.send(embed=embed)
        else:
            view = WrongMovieView(results, session, tmdb_key, omdb_key)
            await ctx.send(embed=embed, view=view)

    @helped_command(movie, "movie genre",
        name="genre",
    )
    async def movie_genre(self, ctx: commands.Context, *, genre: str):
        tmdb_key = os.getenv("TMDB_API_KEY")
        if not tmdb_key:
            await ctx.send("❌ `TMDB_API_KEY` is not set.", ephemeral=True)
            return

        omdb_key = os.getenv("OMDB_API_KEY")

        await ctx.defer()
        session = await self._get_session()
        genre_map = await _get_genre_map(session, tmdb_key)
        genre_lower = genre.lower()

        genre_id = genre_map.get(genre_lower)
        matched_name = genre_lower
        if genre_id is None:
            matches = [name for name in genre_map if genre_lower in name]
            if not matches:
                available = ", ".join(sorted(g.title() for g in genre_map))
                await ctx.send(
                    f"❌ Unknown genre **{genre}**.\n\nAvailable genres: {available}",
                    ephemeral=True,
                )
                return
            matched_name = matches[0]
            genre_id = genre_map[matched_name]

        results = await _discover_by_genre(session, tmdb_key, genre_id)
        if not results:
            await ctx.send(f"❌ No results found for **{genre}**.")
            return

        view = MovieSelectView(results, session, tmdb_key, omdb_key)
        await ctx.send(
            f"Top **{matched_name.title()}** movies — pick one to see details:",
            view=view,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(MoviesCog(bot))
