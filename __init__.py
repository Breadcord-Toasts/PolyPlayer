import datetime
import re
from abc import ABC, abstractmethod
from logging import getLogger

import aiohttp
import discord
from discord.ext import commands, tasks
from yarl import URL

import breadcord


class BadResponseError(Exception):
    pass


class AIOLoadable(ABC):
    @abstractmethod
    async def load(self) -> None:
        pass

    @abstractmethod
    async def close(self) -> None:
        pass


class VideoInfo:
    def __init__(
        self,
        data: dict,
        audio_url: URL,
        input_url: str,
        yt_url: str,
    ) -> None:
        self.data = data
        self.audio_url = audio_url
        self.input_url = input_url
        self.yt_url = yt_url


class ChannelPlayer:
    def __init__(self, connection: discord.VoiceClient) -> None:
        self.connection: discord.VoiceClient = connection
        self.queue: list[VideoInfo] = []
        self.now_playing: VideoInfo | None = None
        self.loop: bool = False

    @property
    def volume(self) -> float:
        if isinstance(self.connection.source, discord.PCMVolumeTransformer):
            # One of us is wrong about this being defined, and I am betting on it being the type checker (it's me)
            return self.connection.source.volume  # type: ignore[attr-defined]
        else:
            raise ValueError("Audio source is not a volume transformer")

    @volume.setter
    def volume(self, value: float) -> None:
        if isinstance(self.connection.source, discord.PCMVolumeTransformer):
            self.connection.source.volume = value
        else:
            raise ValueError("Audio source is not a volume transformer")


ChannelID = int


class PolyPlayer(breadcord.module.ModuleCog):
    def __init__(self, module_id: str) -> None:
        super().__init__(module_id)
        self.invidious = Invidious(host_url=self.settings.invidious_host_url.value)  # type: ignore[call-arg]
        self.translator = Translator(settings=self.settings, invidious=self.invidious)
        self.players: dict[ChannelID, ChannelPlayer] = {}

    async def cog_load(self) -> None:
        await super().cog_load()
        await self.invidious.load()
        await self.translator.load()

        self.play_queue.start()

    async def cog_unload(self) -> None:
        await super().cog_unload()
        await self.invidious.close()
        await self.translator.close()

    @tasks.loop(seconds=1)
    async def play_queue(self) -> None:
        for guild_id, player in dict(self.players).items():
            if player.connection.is_playing() or player.connection.is_paused():
                continue
            if not player.queue:
                await player.connection.disconnect()
                del self.players[guild_id]
                continue

            video_info = player.queue.pop(0)
            if player.loop:
                player.queue.append(video_info)
            player.now_playing = video_info

            player.connection.play(discord.PCMVolumeTransformer(discord.FFmpegPCMAudio(
                str(video_info.audio_url),
                before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 3",
            )))

    @commands.hybrid_command()
    async def play(self, ctx: commands.Context, url: str | None = None) -> None:
        if not (ctx.guild and isinstance(ctx.author, discord.Member) and ctx.author.voice and ctx.author.voice.channel):
            await ctx.reply("You must be in a voice channel to use this command", ephemeral=True)
            return

        player = self.players.get(ctx.author.voice.channel.id)
        if player and player.connection.is_paused() and not player.queue:
            player.connection.resume()
            if url is None:
                return
        if url is None:
            await ctx.reply("You must provide a URL", ephemeral=True)
            return

        try:
            video_id = await self.translator.to_invidious_id(url)
            if video_id is None:
                await ctx.reply("Invalid URL", ephemeral=True)
                return
            video = await self.invidious.get_video(video_id)
            audio_url = await self.invidious.get_audio_url(video)
        except BadResponseError as error:
            await ctx.reply(f"Error: {error}")
            return

        player = self.players.get(ctx.author.voice.channel.id)  # Race condition, this helps a bit
        player = player or ChannelPlayer(await ctx.author.voice.channel.connect())
        player.queue.append(VideoInfo(
            video,
            audio_url,
            input_url=url,
            yt_url=f"https://www.youtube.com/watch?v={video_id}",
        ))
        self.players[ctx.author.voice.channel.id] = player
        await ctx.reply(f"Added [{video['title']}](<{player.queue[-1].yt_url}>) to the queue")

    @commands.hybrid_command()
    async def queue(self, ctx: commands.Context, ephemeral: bool = False) -> None:
        if not (ctx.guild and isinstance(ctx.author, discord.Member) and ctx.author.voice and ctx.author.voice.channel):
            await ctx.reply("You must be in a voice channel to use this command", ephemeral=True)
            return
        player = self.players.get(ctx.author.voice.channel.id)
        if player is None or (not player.queue and not player.connection.is_playing()):
            await ctx.reply("Nothing is currently playing", ephemeral=True)
            return

        embed = discord.Embed(
            title="Queue",
            colour=discord.Colour.random(seed=hash(player.now_playing or 0)),
        ).set_footer(text="PolyPlayer")
        if player.now_playing:
            embed.add_field(
                name="Now playing",
                value=f"[{player.now_playing.data['title']}]({player.now_playing.yt_url})",
                inline=False,
            )
        if player.queue:
            formated_values = [
                f"{i}. [{video_info.data['title']}]({video_info.yt_url})\n"
                for i, video_info in enumerate(player.queue, start=1)
            ]
            description = ""
            while formated_values:
                if len(description) + len(formated_values[0]) > 2000:
                    break
                description += formated_values.pop(0)
            if formated_values:
                description += f"and {len(formated_values)} more..."

            embed.add_field(
                name="Up next",
                value=description,
                inline=False,
            )

        await ctx.reply(embed=embed, ephemeral=ephemeral)

    @commands.hybrid_command()
    async def volume(self, ctx: commands.Context, volume_percentage: int | None = None) -> None:
        if not (ctx.guild and isinstance(ctx.author, discord.Member) and ctx.author.voice and ctx.author.voice.channel):
            await ctx.reply("You must be in a voice channel to use this command", ephemeral=True)
            return
        player = self.players.get(ctx.author.voice.channel.id)
        if player is None:
            await ctx.reply("Nothing is currently playing", ephemeral=True)
            return
        if volume_percentage is None:
            await ctx.reply(f"Volume is currently at {player.volume * 100:.0f}%", ephemeral=True)
            return
        player.volume = max(0.5, min(2.0, volume_percentage / 100))
        await ctx.reply(f"Volume set to {player.volume * 100:.0f}%")

    @commands.hybrid_command()
    async def pause(self, ctx: commands.Context) -> None:
        if not (ctx.guild and isinstance(ctx.author, discord.Member) and ctx.author.voice and ctx.author.voice.channel):
            await ctx.reply("You must be in a voice channel to use this command", ephemeral=True)
            return
        player = self.players.get(ctx.author.voice.channel.id)
        if player is None:
            await ctx.reply("Nothing is currently playing")
            return
        if player.connection.is_paused():
            player.connection.resume()
        else:
            player.connection.pause()

    @commands.hybrid_command()
    async def resume(self, ctx: commands.Context) -> None:
        if not (ctx.guild and isinstance(ctx.author, discord.Member) and ctx.author.voice and ctx.author.voice.channel):
            await ctx.reply("You must be in a voice channel to use this command", ephemeral=True)
            return
        player = self.players.get(ctx.author.voice.channel.id)
        if player is None:
            await ctx.reply("Nothing is currently playing", ephemeral=True)
            return
        if player.connection.is_paused():
            player.connection.resume()
        else:
            await ctx.reply("Not currently paused")

    @commands.hybrid_command()
    async def loop(self, ctx: commands.Context, value: bool | None = None) -> None:
        if not (ctx.guild and isinstance(ctx.author, discord.Member) and ctx.author.voice and ctx.author.voice.channel):
            await ctx.reply("You must be in a voice channel to use this command", ephemeral=True)
            return
        player = self.players.get(ctx.author.voice.channel.id)
        if player is None:
            await ctx.reply("Nothing is currently playing", ephemeral=True)
            return

        if value is None:
            player.loop = not player.loop
        else:
            player.loop = value
        await ctx.reply(f"Looping is now {'enabled' if player.loop else 'disabled'}")

    @commands.hybrid_command()
    async def skip(self, ctx: commands.Context, steps: int = 1) -> None:
        if not (ctx.guild and isinstance(ctx.author, discord.Member) and ctx.author.voice and ctx.author.voice.channel):
            await ctx.reply("You must be in a voice channel to use this command", ephemeral=True)
            return
        player = self.players.get(ctx.author.voice.channel.id)
        if player is None:
            await ctx.reply("Nothing is currently playing", ephemeral=True)
            return
        if not player.queue:
            await ctx.reply("Nothing is currently playing", ephemeral=True)
            return
        player.queue = player.queue[steps:]
        if player.loop:
            player.queue.extend(player.queue[:steps])
        await ctx.reply(f"Skipped {steps} songs")


class Invidious(AIOLoadable):
    def __init__(self, *, host_url: str | None = None) -> None:
        self.host_url: str | None = host_url.rstrip("/") if host_url else None
        self.logger = getLogger("poy_player.Invidious")
        self.session: aiohttp.ClientSession = aiohttp.ClientSession()

    async def load(self) -> None:
        if not self.host_url:
            self.host_url = await self.find_best_host()
        self.logger.debug(f"Using invidious instance: {self.host_url}")

    async def close(self) -> None:
        await self.session.close()

    async def find_best_host(self) -> str:
        async with self.session.get("https://api.invidious.io/instances.json") as response:
            instances_list: list[dict] = [
                instance
                for _, instance in await response.json()
                if all((
                    instance.get("stats") is not None,
                    instance.get("api"),
                    instance.get("type") == "https",
                    instance.get("uri"),
                ))
            ]
        return max(
            instances_list,
            key=lambda instance: instance["stats"]["usage"]["users"]["activeHalfyear"],
        )["uri"]

    async def get_video(self, video_id: str) -> dict:
        async def inner():
            self.logger.debug(f"Fetching video with ID: {video_id}")
            async with self.session.get(f"{self.host_url}/api/v1/videos/{video_id}") as response:
                data = await response.json()
                if error := data.get("error"):
                    raise BadResponseError(f"Error fetching video: {error}")
                return data
        # The "The video returned by YouTube isn't the requested one" errors seems to be quite common
        # It seems like it can sometimes be fixed by just trying again?
        try:
            return await inner()
        except BadResponseError:
            self.logger.warning("Failed to fetch video, trying once more")
            return await inner()

    async def search_for(self, query: str) -> list[dict]:
        self.logger.debug(f"Searching for: {query}")
        async with self.session.get(
            URL(f"{self.host_url}/api/v1/search") % {
                "q": query,
                "sort_by": "relevance",
                "type": "video",
            },
        ) as response:
            return await response.json()

    async def get_audio_url(self, video: dict) -> URL:
        best = max(
            (frmt for frmt in video["adaptiveFormats"] if frmt["type"].startswith("audio/")),
            key=lambda frmt: frmt["bitrate"],
        )
        async with self.session.get(
            URL(f"{self.host_url}/latest_version") % {
                "id": video["videoId"],
                "itag": best["itag"],
                "local": "true",
            },
        ) as response:
            if not response.ok:
                raise BadResponseError(f"Error fetching audio: {response.reason}")
            return response.url


class Translator(AIOLoadable):
    def __init__(self, settings: breadcord.config.SettingsGroup, invidious: Invidious) -> None:
        self.settings = settings
        self.invidious = invidious
        self.session: aiohttp.ClientSession = aiohttp.ClientSession()

        self._spotify_token: str | None = None
        self._spotify_token_expires_at: datetime.datetime | None = None

    async def load(self) -> None:
        pass

    async def close(self) -> None:
        await self.session.close()

    INVIDIOUS_ID_RE = re.compile(r".+watch\?v=(?P<id>[a-zA-Z0-9_-]+)$")
    YOUTUBE_ID_RE = re.compile(
        r"""
        https?://(?:
        (?:www\.)?youtube\.[a-z]+/watch(?:\?v=|/)
        |
        youtu\.be(?:/watch/*\?v=|/)
        )
        (?P<id>[0-9a-zA-Z-_]+)
        (?:$|&)
        """,
        flags=re.VERBOSE,
    )
    SPOTIFY_ID_RE = re.compile(r"https?://open\.spotify\.com/track/(?P<id>\w+)")

    async def to_invidious_id(self, url: str) -> str | None:
        if match := self.INVIDIOUS_ID_RE.match(url):
            return match.group("id")
        elif match := self.YOUTUBE_ID_RE.match(url):
            return match.group("id")
        elif match := self.SPOTIFY_ID_RE.match(url):
            return await self.spotify_to_youtube_id(match.group("id"))
        # TODO: Apple music

        return None

    async def spotify_to_youtube_id(self, track_id: str) -> str:
        await self.update_spotify_token()
        async with self.session.get(
            f"https://api.spotify.com/v1/tracks/{track_id}",
            headers={"Authorization": f"Bearer {self._spotify_token}"},
        ) as response:
            if response.status == 401:
                raise BadResponseError("Invalid spotify token")
            elif response.status != 200:
                raise BadResponseError("Could not get track data")
            elif not response.ok:
                raise BadResponseError(f"{response.status} Could not get track data: {response.reason}")
            data = await response.json()

        query = data["name"] + " by " + " ".join(artist["name"] for artist in data["artists"])
        search = await self.invidious.search_for(query)
        return search[0]["videoId"]

    async def update_spotify_token(self) -> None:
        if (
            self._spotify_token_expires_at is not None
            and self._spotify_token_expires_at < datetime.datetime.now() + datetime.timedelta(minutes=15)
        ):
            return
        async with self.session.post(
            "https://accounts.spotify.com/api/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.settings.spotify.client_id.value,  # type: ignore[attr-defined]
                "client_secret": self.settings.spotify.client_secret.value,  # type: ignore[attr-defined]
            },
        ) as response:
            data = await response.json()
            if data.get("error") == "invalid_client":
                raise ValueError("Invalid spotify client id or secret")
            self._spotify_token = data["access_token"]
            self._spotify_token_expires_at = datetime.datetime.now() + datetime.timedelta(seconds=data["expires_in"])


async def setup(bot: breadcord.Bot):
    await bot.add_cog(PolyPlayer("polyplayer"))
