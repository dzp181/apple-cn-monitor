#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线自检：不联网、不推送、不改 state，只验证事件判定与消息构造。

    python3 selftest.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import apple_push as m  # noqa: E402

FAILS = []


def ok(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), name, detail)
    if not cond:
        FAILS.append(name)


def main():
    cfg = m.load_config()
    catalog = m.load_catalog()

    # --- 文本 / 价格 ---
    ok("clean 去掉脚注", m.clean("256 GB<as-footnote>脚注 1</as-footnote>") == "256 GB")
    ok("price_cents 带小数", m.price_cents("4699.00") == 469900)
    ok("price_cents 整数", m.price_cents("4299") == 429900)
    ok("price_cents 空值", m.price_cents("") is None)
    ok("money 千位分隔", m.money(469900) == "¥4,699")
    ok("主题前缀", cfg["ntfy_topic"].startswith("x-"))

    product = {"key": "mac/FHFA4CH/A", "category": "mac", "part": "FHFA4CH/A",
               "title": "翻新 MacBook Neo - 银色", "price": 429900,
               "url": "https://www.apple.com.cn/shop/product/fhfa4ch/a"}
    current = {product["key"]: product}

    # --- 官翻降价 ---
    state = {"refurb": {"mac/FHFA4CH/A": {"price": 469900, "title": product["title"],
                                          "gone": False}}, "pickup": {}}
    events, _ = m.diff_refurb(state, current, {"mac"}, cfg)
    ok("降价事件触发", len(events) == 1 and events[0]["kind"] == "price_drop"
       and events[0]["drop"] == 40000)
    payload = m.build_payload(events[0], cfg, catalog)
    ok("降价消息含原价现价", payload["topic"] == "x-apple-refurb"
       and "¥4,699 → ¥4,299" in payload["message"] and payload["click"] == product["url"])

    # --- 新上架 ---
    events_new, _ = m.diff_refurb({"refurb": {}, "pickup": {}}, current, {"mac"}, cfg)
    ok("新上架事件触发", len(events_new) == 1 and events_new[0]["kind"] == "new_listing")

    # --- 目标价规则 ---
    cfg_t = dict(cfg)
    cfg_t["refurb"] = dict(cfg["refurb"])
    cfg_t["refurb"]["target_rules"] = [{"match": "MacBook Neo", "max_price": 4500}]
    ok("目标价规则命中", m.match_target(product, cfg_t["refurb"]["target_rules"]) is not None)
    events_t, _ = m.diff_refurb(state, current, {"mac"}, cfg_t)
    ok("降价优先于目标价", len(events_t) == 1 and events_t[0]["kind"] == "price_drop")

    # --- 抓取失败的分类不得被当成下架 ---
    state_fail = {"refurb": {"mac/OLD": {"price": 100000, "gone": False}}, "pickup": {}}
    current_other = {"ipad/NEW": {"key": "ipad/NEW", "category": "ipad", "part": "NEW",
                                  "title": "翻新 iPad", "price": 200000, "url": ""}}
    _, gone = m.diff_refurb(state_fail, current_other, {"ipad"}, cfg)
    ok("失败分类不清理", gone == [])

    # --- 自提跃迁 / 三态 ---
    snapshot = {"MG364CH/A": {"status": "available", "stores": ["R389", "R320"],
                               "any_count": 2},
                "MK2Q4CH/A": {"status": "unknown", "stores": []}}
    st_pickup = {"pickup": {"MG364CH/A": {"status": "unavailable", "stores": [],
                                          "last_notified": 0}}}
    ev_pickup = m.diff_pickup(st_pickup, snapshot, cfg)
    ok("无货→有货触发", len(ev_pickup) == 1 and ev_pickup[0]["kind"] == "restock")
    ok("unknown 不触发", all(e["part"] != "MK2Q4CH/A" for e in ev_pickup))
    pl_pickup = m.build_payload(ev_pickup[0], cfg, catalog)
    ok("有货消息含门店名", "浦东" in pl_pickup["message"]
       and pl_pickup["click"].startswith("https://www.apple.com.cn/shop/buy-iphone"))

    # --- 空分类 ---
    page = ('<html><script>window.REFURB_GRID_BOOTSTRAP = '
            '{"dimensions":[],"tiles":null,"products":[]};</script></html>')
    ok("tiles:null 返回空字典", m.parse_grid(page, "airpods", cfg) == {})

    # --- 推不动时不更新状态（重试语义）---
    st2 = {"refurb": {}, "pickup": {}, "seeded": True}
    m.apply_refurb_state(st2, current, {"mac"}, [], {product["key"]: events[0]}, set())
    ok("推送失败不写价格", product["key"] not in st2["refurb"])
    st3 = {"refurb": {}, "pickup": {}, "seeded": True}
    m.apply_refurb_state(st3, current, {"mac"}, [], {product["key"]: events[0]},
                         {product["key"]})
    ok("推送成功写入价格", st3["refurb"][product["key"]]["price"] == 429900)

    print()
    if FAILS:
        print(f"{len(FAILS)} 项失败：{', '.join(FAILS)}")
        sys.exit(1)
    print("全部离线自检通过")


if __name__ == "__main__":
    main()
