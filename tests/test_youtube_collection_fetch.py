from datetime import UTC, datetime

from autoresearch.youtube_collection.fetch import (
    collect_trending,
    fetch_category_map,
    fetch_channel_map,
    fetch_trending_video_items,
)
from autoresearch.youtube_collection.schema import TrendingVideo


def test_fetch_trending_video_items_paginates_until_exhausted():
    responses = [
        {"items": [{"id": "v1"}, {"id": "v2"}], "nextPageToken": "tok2"},
        {"items": [{"id": "v3"}, {"id": "v4"}], "nextPageToken": "tok3"},
        {"items": [{"id": "v5"}]},  # no nextPageToken -> stop
    ]
    calls = []

    def fake_list(**params):
        calls.append(params)
        return responses[len(calls) - 1]

    items = fetch_trending_video_items(fake_list, region_code="KR", max_results=200)

    assert [i["id"] for i in items] == ["v1", "v2", "v3", "v4", "v5"]
    assert len(calls) == 3
    assert "pageToken" not in calls[0]
    assert calls[1]["pageToken"] == "tok2"
    assert calls[2]["pageToken"] == "tok3"
    assert all(c["regionCode"] == "KR" for c in calls)
    assert all(c["chart"] == "mostPopular" for c in calls)


def test_fetch_channel_map_batches_in_groups_of_fifty():
    ids = [f"c{i}" for i in range(120)]
    calls = []

    def fake_list(**params):
        requested = params["id"].split(",")
        calls.append(requested)
        return {
            "items": [{"id": cid, "snippet": {"title": cid}} for cid in requested]
        }

    mapping = fetch_channel_map(fake_list, ids)

    assert len(mapping) == 120
    assert mapping["c0"]["id"] == "c0"
    assert len(calls) == 3
    assert len(calls[0]) == 50
    assert len(calls[1]) == 50
    assert len(calls[2]) == 20


def test_fetch_category_map_returns_id_to_name():
    def fake_list(**params):
        return {
            "items": [
                {"id": "24", "snippet": {"title": "Entertainment"}},
                {"id": "10", "snippet": {"title": "Music"}},
            ]
        }

    mapping = fetch_category_map(fake_list, region_code="KR")

    assert mapping == {"24": "Entertainment", "10": "Music"}


def _video_item(vid, channel_id, category_id="24"):
    return {
        "id": vid,
        "snippet": {
            "channelId": channel_id,
            "categoryId": category_id,
            "publishedAt": "2026-06-20T10:00:00Z",
            "title": f"title-{vid}",
            "description": "desc",
        },
        "contentDetails": {"duration": "PT1M", "dimension": "2d", "definition": "hd", "licensedContent": False},
        "statistics": {"viewCount": "100"},
    }


def test_collect_trending_assembles_videos_with_categories_and_channels():
    def fake_list_videos(**params):
        return {"items": [_video_item("v1", "c1"), _video_item("v2", "c2", "10")]}

    def fake_list_channels(**params):
        ids = params["id"].split(",")
        return {
            "items": [
                {
                    "id": i,
                    "snippet": {"title": i},
                    "statistics": {"hiddenSubscriberCount": False, "viewCount": "9"},
                }
                for i in ids
            ]
        }

    def fake_list_categories(**params):
        return {
            "items": [
                {"id": "24", "snippet": {"title": "Entertainment"}},
                {"id": "10", "snippet": {"title": "Music"}},
            ]
        }

    collected_at = datetime(2026, 6, 25, 15, 30, tzinfo=UTC)
    videos = collect_trending(
        fake_list_videos,
        fake_list_channels,
        fake_list_categories,
        collected_at=collected_at,
    )

    assert len(videos) == 2
    assert all(isinstance(v, TrendingVideo) for v in videos)
    by_id = {v.video_id: v for v in videos}
    assert by_id["v1"].video_category == "Entertainment"
    assert by_id["v2"].video_category == "Music"
    assert by_id["v1"].channel_title == "c1"
    assert by_id["v1"].video_view_count == 100


def test_collect_trending_normalizes_video_even_when_channel_missing():
    def fake_list_videos(**params):
        return {"items": [_video_item("v1", "c_gone")]}

    def fake_list_channels(**params):
        return {"items": []}  # channel not returned

    def fake_list_categories(**params):
        return {"items": [{"id": "24", "snippet": {"title": "Entertainment"}}]}

    collected_at = datetime(2026, 6, 25, 15, 30, tzinfo=UTC)
    videos = collect_trending(
        fake_list_videos,
        fake_list_channels,
        fake_list_categories,
        collected_at=collected_at,
    )

    assert len(videos) == 1
    assert videos[0].video_id == "v1"
    assert videos[0].channel_title == ""  # missing channel -> empty defaults


def test_fetch_channel_map_skips_items_without_id():
    def fake_list(**params):
        return {
            "items": [
                {"id": "c1", "snippet": {"title": "c1"}},
                {"snippet": {"title": "no_id"}},  # id 누락 -> skip (KeyError X)
            ]
        }

    mapping = fetch_channel_map(fake_list, ["c1"])
    assert mapping == {"c1": {"id": "c1", "snippet": {"title": "c1"}}}


def test_fetch_category_map_skips_items_without_title():
    def fake_list(**params):
        return {
            "items": [
                {"id": "24", "snippet": {"title": "Music"}},
                {"id": "99"},  # snippet/title 누락 -> skip
                {"snippet": {"title": "x"}},  # id 누락 -> skip
            ]
        }

    mapping = fetch_category_map(fake_list)
    assert mapping == {"24": "Music"}
