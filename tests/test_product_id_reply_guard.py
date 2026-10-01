from app.application.agents.product_id_reply_guard import attach_visible_product_ids


def test_adds_id_for_visible_cj_title_prefix_only():
    title = ("Creative Portable Outdoor Pet Stainless Steel Water Cup Small Dog Water Bottle "
             "Convenient Dog Drink Dispenser Puppy Travel Portable Water Bowl Pet Products")
    result = {"recall_strategy": "cj_snapshot_keyword", "hits": [
        {"product_id": "2602090303381624300", "title": title},
        {"product_id": "other-id", "title": "Unmentioned Camping Lantern With Solar Panel And Bright Light"},
    ]}
    reply = "| 1 | Creative Portable Outdoor Pet Stainless Steel Water Cup Small Dog Water Bottle | 推荐 |"
    fixed = attach_visible_product_ids(reply, result)
    assert "2602090303381624300" in fixed
    assert "other-id" not in fixed
    assert attach_visible_product_ids(fixed, result) == fixed


def test_does_not_attach_ambiguous_or_non_cj_hits():
    hits = [
        {"product_id": "one", "title": "Portable Dog Water Bottle Outdoor Travel Pet Cup Blue"},
        {"product_id": "two", "title": "Portable Dog Water Bottle Outdoor Travel Pet Cup Green"},
    ]
    reply = "Portable Dog Water Bottle Outdoor Travel Pet Cup is available."
    assert attach_visible_product_ids(reply, {"recall_strategy": "cj_snapshot_keyword", "hits": hits}) == reply
    assert attach_visible_product_ids(reply, {"recall_strategy": "vector", "hits": hits}) == reply


def test_reply_guard_supports_cj_hybrid_results():
    result = {"recall_strategy": "cj_hybrid_rrf", "hits": [{
        "product_id": "2602090303381624300",
        "title": "Portable Outdoor Stainless Steel Water Cup for Small Dog",
    }]}
    reply = "推荐 Portable Outdoor Stainless Steel Water Cup for Small Dog。"
    guarded = attach_visible_product_ids(reply, result)
    assert "2602090303381624300" in guarded
