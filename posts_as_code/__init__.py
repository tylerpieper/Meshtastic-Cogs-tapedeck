from .posts_as_code import PostsAsCode

__red_end_user_data_statement__ = (
    "This cog does not persistently store data or metadata about users. "
    "It stores per-guild Git repository settings, channel/thread mappings, "
    "and IDs and content hashes of messages posted by the bot."
)


async def setup(bot):
    await bot.add_cog(PostsAsCode(bot))
