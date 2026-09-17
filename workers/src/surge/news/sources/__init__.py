"""Per-source adapters. One generic feed adapter covers the RSS and Atom sources."""

from surge.news.sources.feed_source import ASSUMED_TZ, FEED_SPECS, FeedSource, feed_source

__all__ = ["ASSUMED_TZ", "FEED_SPECS", "FeedSource", "feed_source"]
