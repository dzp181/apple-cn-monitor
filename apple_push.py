#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
apple-cn-monitor —— Apple 中国大陆「官翻降价 + 直营店自提有货」→ ntfy 只提醒

零第三方依赖，只用 Python 标准库。

接口思路参考（2026-11-13 实测可用，代码全部重写，未复制任何源码）：
  - XGFan/apple-store-watcher      : /shop/sba/availability-message（2026 新版自提接口）、
                                     /api-www/graphql 门店列表、购买页 productSelectionData
  - hh-io/refurb-sentry            : 官翻列表页 REFURB_GRID_BOOTSTRAP、事件模型、
                                     抓取失败不当下架、货币护栏
  - moonhorsemmy/apple-pickup-bash : 三态库存（有货/无货/未知）、541 冷却自律、
                                     只在「无货→有货」跃迁时提醒

用法：
  python3 apple_push.py --init              # 建基线（抓全量、写 state，不推送）
  python3 apple_push.py --dry-run           # 跑一轮，只打印将要推送的消息
  python3 apple_push.py                     # 正式跑一轮
  python3 apple_push.py --test              # 发一条自检消息
  python3 apple_push.py --list-stores       # 打印中国大陆直营店代码
  python3 apple_push.py --list-products     # 打印当前监控的 SKU
  python3 apple_push.py --refresh-catalog   # 重建门店 / SKU 快照
  python3 apple_push.py --max 5             # 覆盖单轮推送上限

只提醒，不下单、不加购、不自动抢购。请保持克制频率。
"""
from __future__ import annotations

import argparse
import datetime as dt
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CST = dt.timezone(dt.timedelta(hours=8))
BASE = "https://www.apple.com.cn"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

# StoreSearchByLocale 的 persisted query；Apple 轮换 hash 时会自动退回 retail 页面解析
GRAPHQL_HASH = "d95caa379372fe166f35bb89fe8e018e21781c66ee815ee3f68105f36ff4b1d8"

CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state.json"
CATALOG_PATH = ROOT / "data" / "catalog.json"
ARCHIVE_DIR = ROOT / "archive"

CATEGORY_CN = {"mac": "Mac", "ipad": "iPad", "watch": "Watch",
               "airpods": "AirPods", "homepod": "HomePod",
               "accessories": "配件", "iphone": "iPhone"}

# 购买页里这些 slug 不是商品族，抓了也没有 productSelectionData
IGNORED_SLUGS = {"carrier-offers", "compare", "trade-in", "applecare", "carrier",
                 "shop-help", "special-deals", "refurbished", "education-store",
                 "business", "government", "veterans-affairs"}


def log(msg):
    print(f"[{dt.datetime.now(CST):%m-%d %H:%M:%S}] {msg}", file=sys.stderr, flush=True)


def now_ts():
    return dt.datetime.now(CST).strftime("%Y-%m-%d %H:%M CST")


def today():
    return dt.datetime.now(CST).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- 基础 IO

def load_json(path, default):
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        log(f"读取 {p.name} 失败，用默认值：{e}")
        return default


def save_json(path, obj):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    tmp.replace(p)


def load_config():
    cfg = load_json(CONFIG_PATH, {})
    if not cfg:
        raise SystemExit("缺少 config.json")
    topic = cfg.get("ntfy_topic", "")
    # ntfy 发布令牌只授权 x-* 前缀，别的主题会 403
    if not topic.startswith("x-"):
        raise SystemExit(f"ntfy 主题必须以 x- 开头：{topic!r}")
    cfg.setdefault("currency", "CNY")
    cfg.setdefault("max_per_run", 3)
    cfg.setdefault("timeout", 25)
    return cfg


def new_state():
    return {"seeded": False, "cooldown_until": 0,
            "refurb": {}, "pickup": {}, "last_run": ""}


def clean(text):
    # 购买页 displayValues 里夹着 <as-footnote> 与 form-label-small 营销文案，
    # 不剔掉会在商品名里留下「脚注 1」这种尾巴。
    s = str(text)
    s = re.sub(r"<as-footnote\b.*?</as-footnote>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<div[^>]*form-label-small[^>]*>.*?</div>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"&nbsp;?", " ", s)
    import html as _html
    s = _html.unescape(s)
    s = re.sub(r"\s*脚注\s*\d+\s*", " ", s)
    return " ".join(s.split())


def money(cents):
    return "¥" + f"{cents // 100:,}" + (f".{cents % 100:02d}" if cents % 100 else "")


def price_cents(raw):
    """'4699.00' -> 469900。用整数分比较，避免浮点误差造成假降价。"""
    s = str(raw or "").strip()
    if not s:
        return None
    m = re.match(r"^(\d+)(?:\.(\d+))?$", s)
    if not m:
        return None
    whole = int(m.group(1))
    frac = ((m.group(2) or "") + "00")[:2]
    return whole * 100 + int(frac)


def pretty(value):
    s = str(value).replace("_", ".")
    low = s.lower()
    if low.endswith("gb") or low.endswith("tb"):
        return s[:-2].upper() + s[-2:].upper()
    if low.endswith("inch"):
        return s[:-4] + " 英寸"
    return s


# ---------------------------------------------------------------- HTTP

class RateLimited(Exception):
    """Apple 返回 541 / 429，需要进入冷却。"""


class FetchError(Exception):
    """抓取或解析失败：此时绝不能把已知商品判成下架 / 无货。"""


def request(path, post=None, timeout=25, opener=None):
    url = path if path.startswith("http") else BASE + path
    data = json.dumps(post, ensure_ascii=False).encode("utf-8") if post is not None else None
    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*" if post else "text/html,application/xhtml+xml,*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": BASE + "/shop/buy-iphone",
    }
    if post is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if post is not None else "GET")
    try:
        open_fn = opener.open if opener is not None else urllib.request.urlopen
        with open_fn(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        raise FetchError(str(e)) from e


# ---------------------------------------------------------------- 官翻列表

def parse_grid(page, category, cfg):
    marker = "window.REFURB_GRID_BOOTSTRAP"
    i = page.find(marker)
    if i < 0:
        raise FetchError("页面里没有 REFURB_GRID_BOOTSTRAP（可能被重定向或改版）")
    j = page.find("{", i)
    if j < 0:
        raise FetchError("REFURB_GRID_BOOTSTRAP 后面没有 JSON 对象")
    try:
        obj, _ = json.JSONDecoder().raw_decode(page[j:])
    except ValueError as e:
        raise FetchError(f"bootstrap JSON 解析失败：{e}") from e

    out = {}
    mismatch = 0
    # 空分类（例如 CN 官翻 AirPods）会返回 tiles: null，不是错误、也不是改版。
    # 返回空字典，由 fetch_refurb 把该类排除出本轮 diff，避免误判下架。
    tiles = obj.get("tiles") or []
    if not isinstance(tiles, list):
        tiles = []
    for tile in tiles:
        if not isinstance(tile, dict):
            continue
        part = str(tile.get("partNumber") or "").strip()
        if not part:
            continue
        price = tile.get("price") or {}
        currency = str(price.get("priceCurrency") or "").strip()
        # 境外出口 IP 偶尔会被重定向到别国站点，货币不符时整类丢弃
        if currency and currency != cfg["currency"]:
            mismatch += 1
            continue
        cents = price_cents((price.get("currentPrice") or {}).get("raw_amount"))
        if cents is None:
            continue
        url = str(tile.get("productDetailsUrl") or "").split("?")[0].strip()
        if url.startswith("/"):
            url = BASE + url
        out[f"{category}/{part}"] = {
            "key": f"{category}/{part}",
            "category": category,
            "part": part,
            "title": clean(tile.get("title") or part),
            "price": cents,
            "url": url,
        }
    if mismatch:
        raise FetchError(f"{mismatch} 件商品货币不是 {cfg['currency']}，可能被重定向到其他地区站点")
    if tiles and not out:
        raise FetchError("bootstrap 里有商品但价格全部无法解析（字段可能已改版）")
    return out


def fetch_refurb(cfg):
    """返回 (当前商品字典, 成功抓取的分类集合)。失败的分类不参与 diff / 下架判定。"""
    current, ok_categories = {}, set()
    for cat in cfg.get("refurb", {}).get("categories", []):
        path = f"/shop/refurbished/{cat}"
        try:
            status, page = request(path, timeout=cfg["timeout"])
            if status == 404:
                log(f"官翻 {cat}：该地区不提供此分类（404），跳过")
                continue
            if status in (541, 429):
                log(f"官翻 {cat}：HTTP {status}，本轮跳过")
                continue
            if status != 200:
                raise FetchError(f"HTTP {status}")
            items = parse_grid(page, cat, cfg)
        except Exception as e:
            # 单个分类解析异常不能拖垮整轮；该分类状态保持不变
            log(f"官翻 {cat}：抓取失败，本轮不更新该类状态（{type(e).__name__}: {e}）")
            continue
        if not items:
            log(f"官翻 {cat}：当前没有商品（空分类），本轮跳过 diff")
            continue
        current.update(items)
        ok_categories.add(cat)
        log(f"官翻 {cat}：{len(items)} 件")
        time.sleep(0.6)
    return current, ok_categories


# ---------------------------------------------------------------- 门店 / SKU 目录

def fetch_stores():
    gql = {
        "operationName": "StoreSearchByLocale",
        "variables": {"localeId": "zh_CN"},
        "extensions": {"persistedQuery": {"version": 1, "sha256Hash": GRAPHQL_HASH}},
    }
    status, text = request("/api-www/graphql", post=gql)
    if status != 200:
        raise FetchError(f"门店 GraphQL HTTP {status}")
    try:
        payload = json.loads(text)
        rows = payload["data"]["rmdLocale"]["stores"]
    except (ValueError, KeyError, TypeError) as e:
        raise FetchError(f"门店列表解析失败：{e}") from e
    stores = {}
    for row in rows:
        code = str(row.get("storeNumber") or "").strip()
        name = clean(row.get("storeName") or "")
        addr = row.get("_address") or {}
        region = clean((addr.get("state") or {}).get("name") or addr.get("city") or "")
        if code and name:
            stores[code] = f"{region} · {name}" if region else name
    if not stores:
        raise FetchError("门店列表为空")
    return stores


def page_title(page):
    m = re.search(r"<title>(.*?)</title>", page, re.S | re.I)
    if not m:
        return ""
    t = clean(m.group(1))
    t = re.sub(r"^(购买|选购|Buy)\s*", "", t)
    return re.split(r"\s*-\s*Apple", t)[0].strip()


def display_value(dv, key, raw):
    entry = (dv.get(key) or {}).get(raw)
    if isinstance(entry, dict):
        for k in ("value", "text", "label", "name"):
            if isinstance(entry.get(k), str) and entry[k].strip():
                return clean(entry[k])
    elif isinstance(entry, str) and entry.strip():
        return clean(entry)
    return pretty(raw)


def fetch_products(families):
    products = {}
    for slug in families:
        if slug in IGNORED_SLUGS:
            continue
        path = f"/shop/buy-iphone/{slug}"
        status, page = request(path)
        if status != 200:
            log(f"产品族 {slug}：HTTP {status}，跳过")
            continue
        q = page.find("productSelectionData:")
        if q < 0:
            log(f"产品族 {slug}：页面无 productSelectionData（可能已下架或重定向），跳过")
            continue
        start = page.find("{", q)
        if start < 0:
            continue
        try:
            obj, _ = json.JSONDecoder().raw_decode(page[start:])
        except ValueError as e:
            log(f"产品族 {slug}：bootstrap 解析失败（{e}）")
            continue
        sections = [x.get("formFieldName") for x in obj.get("sections", [])
                    if isinstance(x, dict) and x.get("formFieldName")]
        dv = obj.get("displayValues") or {}
        title = page_title(page) or slug
        count = 0
        for row in obj.get("products", []):
            part = str(row.get("partNumber") or row.get("part") or "").strip().upper()
            if not part:
                continue
            dims = []
            nested = row.get("dimensions") if isinstance(row.get("dimensions"), dict) else {}
            for key in sections:
                raw = row.get(key) or nested.get(key)
                if raw:
                    dims.append(display_value(dv, key, raw))
            products[part] = {
                "name": title + (" · " + " · ".join(dims) if dims else ""),
                "family": slug,
                "buy_url": f"{BASE}{path}?product={part}",
            }
            count += 1
        log(f"产品族 {slug}：{count} 个 SKU")
        time.sleep(0.6)
    if not products:
        raise FetchError("没有拿到任何 SKU，产品族 slug 可能已失效")
    return products


def build_catalog(cfg):
    stores = fetch_stores()
    products = fetch_products(cfg.get("pickup", {}).get("families", []))
    return {"generated_at": now_ts(), "stores": stores, "products": products}


def load_catalog():
    return load_json(CATALOG_PATH, {"generated_at": "", "stores": {}, "products": {}})


# ---------------------------------------------------------------- 自提库存

def warm_opener(cfg):
    """先访问购买页建立带地区 cookie 的会话，行为更接近真实浏览器。"""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    try:
        code, _ = request("/shop/buy-iphone", timeout=cfg["timeout"], opener=opener)
        log(f"自提会话预热 HTTP {code}，会话 cookie {len(jar)} 个")
    except Exception as e:
        log(f"自提会话预热失败（忽略）：{e}")
    return opener


def fetch_by_sba(cfg, parts, whitelist, opener):
    """2026 新版接口：一次拿到多个 SKU 在全部直营店的有货门店列表。

    返回 (snapshot, has_context)。Apple 按出口 IP 定位门店，境外 runner 上
    响应里 storeId / eligibleStores / 配送地区全为空，此时 has_context=False，
    调用方应切换到显式指定门店的兜底接口，绝不能把空结果当成「全无货」。
    """
    snapshot = {}
    diag = None
    for offset in range(0, len(parts), 10):
        batch = parts[offset:offset + 10]
        query = {"fae": "true"}
        for i, part in enumerate(batch):
            query[f"parts.{i}"] = part
        url = "/shop/sba/availability-message?" + urllib.parse.urlencode(query)
        status, text = request(url, timeout=cfg["timeout"], opener=opener)
        if status in (541, 429):
            raise RateLimited(f"HTTP {status}")
        if status != 200:
            raise FetchError(f"新版自提接口 HTTP {status}")
        try:
            data = json.loads(text)
            content = (data.get("body") or {}).get("content") or []
            head_status = str((data.get("head") or {}).get("status") or "")
        except ValueError as e:
            raise FetchError(f"新版自提响应不是 JSON：{e}") from e
        if head_status and head_status != "200":
            raise FetchError(f"新版自提接口内部状态 {head_status}")
        if not content:
            raise FetchError("新版自提接口返回空 content（型号可能已停售）")

        if diag is None:
            sample = content[0]
            addr = ((sample.get("deliveryMessage") or {}).get("address") or {})
            diag = {"store_id": sample.get("storeId"),
                    "region": f"{addr.get('state') or '-'}/{addr.get('city') or '-'}",
                    "eligible": sample.get("eligibleStores")}
            context = any(
                str(c.get("storeId") or "") or str(c.get("eligibleStores") or "") or
                (((c.get("deliveryMessage") or {}).get("address") or {}))
                for c in content
            )
            if not context:
                log("新版自提接口未返回中国大陆门店上下文（境外出口 IP 常见）")
                return {}, False, diag

        by_part = {str(c.get("partNumber") or "").upper(): c for c in content}
        for part in batch:
            item = by_part.get(part)
            if item is None:
                snapshot[part] = {"status": "unknown", "stores": [], "any_count": 0}
                continue
            eligible = [x.strip() for x in str(item.get("eligibleStores") or "").split(",")
                        if x.strip()]
            if whitelist:
                eligible = [x for x in eligible if x in whitelist]
            snapshot[part] = {
                "status": "available" if eligible else "unavailable",
                "stores": eligible,
                "any_count": int(item.get("partAvailableStoresCount") or 0),
            }
        if offset + 10 < len(parts):
            time.sleep(1.0)

    if diag:
        log(f"自提诊断：接口门店 {diag['store_id']}，配送地区 {diag['region']}，"
            f"首条 eligibleStores={diag['eligible']!r}")
    return snapshot, True, diag


def pick_fallback_parts(catalog, limit):
    """按产品族均衡取样，避免兜底模式只覆盖一个系列。"""
    groups = {}
    for part, info in (catalog.get("products") or {}).items():
        groups.setdefault(info.get("family", ""), []).append(part)
    for values in groups.values():
        values.sort()
    out = []
    while len(out) < limit:
        added = False
        for fam in sorted(groups):
            if groups[fam] and len(out) < limit:
                out.append(groups[fam].pop(0))
                added = True
        if not added:
            break
    return out


def fetch_by_retail(cfg, catalog, parts, opener):
    """显式指定门店的兜底接口：不依赖 IP 定位，境外 runner 同样可用。

    每家门店一次请求可带 10 个 SKU，返回 partsAvailability[sku].pickupDisplay。
    为克制频率只查配置里的少数重点门店，未参与查询的 SKU 一律标 unknown。
    """
    pc = cfg["pickup"]
    stores = pc.get("fallback_stores") or []
    if not stores:
        raise FetchError("没有配置 fallback_stores，无法在境外环境查自提")
    use = pick_fallback_parts(catalog, int(pc.get("fallback_max_parts", 10)))
    snapshot = {p: {"status": "unknown", "stores": [], "any_count": 0} for p in parts}
    for part in use:
        snapshot[part]["status"] = "unavailable"
    seen = {part: False for part in use}
    ok_stores = 0

    for store in stores:
        query = {"pl": "true", "mts.0": "regular", "store": store}
        for i, part in enumerate(use):
            query[f"parts.{i}"] = part
        url = "/shop/retail/pickup-message?" + urllib.parse.urlencode(query)
        status, text = request(url, timeout=cfg["timeout"], opener=opener)
        if status in (541, 429):
            raise RateLimited(f"HTTP {status}")
        if status != 200:
            log(f"兜底门店 {store}：HTTP {status}，跳过")
            continue
        try:
            data = json.loads(text)
            rows = (data.get("body") or {}).get("stores") or []
        except ValueError:
            log(f"兜底门店 {store}：响应不是 JSON，跳过")
            continue
        if not rows:
            log(f"兜底门店 {store}：接口未返回门店，跳过")
            continue
        ok_stores += 1
        for row in rows:
            code = str(row.get("storeNumber") or store)
            availability = row.get("partsAvailability") or {}
            for part in use:
                info = availability.get(part)
                if not isinstance(info, dict):
                    continue
                display = info.get("pickupDisplay")
                if display in ("available", "unavailable", "ineligible"):
                    seen[part] = True
                if display == "available":
                    snapshot[part]["status"] = "available"
                    if code not in snapshot[part]["stores"]:
                        snapshot[part]["stores"].append(code)
        if store != stores[-1]:
            time.sleep(0.8)

    if ok_stores == 0:
        raise FetchError("显式门店接口未返回任何门店，本轮自提数据不可用")
    for part in use:
        # 三态：某 SKU 在所有门店都没返回明确状态时，绝不能判成无货
        if not seen[part] and snapshot[part]["status"] != "available":
            snapshot[part]["status"] = "unknown"
        snapshot[part]["any_count"] = len(snapshot[part]["stores"])
    available = sum(1 for v in snapshot.values() if v["status"] == "available")
    log(f"自提兜底：{ok_stores}/{len(stores)} 家门店，{len(use)} 个 SKU，{available} 个有货")
    return snapshot


def fetch_pickup(cfg, catalog):
    """返回 (snapshot, mode)。mode = sba | retail，用于检测跨环境数据源切换。"""
    parts = sorted((catalog.get("products") or {}).keys())
    parts = parts[:int(cfg.get("pickup", {}).get("max_parts", 20))]
    if not parts:
        raise FetchError("catalog 里没有 SKU，请先跑 --refresh-catalog")

    whitelist = set(cfg.get("pickup", {}).get("store_whitelist") or [])
    opener = warm_opener(cfg)
    snapshot, has_context, _ = fetch_by_sba(cfg, parts, whitelist, opener)
    if has_context:
        return snapshot, "sba"

    log("切换显式门店兜底接口（新版接口依赖出口 IP 定位，境外拿不到大陆门店）")
    return fetch_by_retail(cfg, catalog, parts, opener), "retail"


# ---------------------------------------------------------------- 事件判定

def match_target(product, rules):
    for rule in rules:
        kw = str(rule.get("match") or "").strip()
        cap = rule.get("max_price")
        if not kw or cap is None:
            continue
        if kw.lower() in product["title"].lower() and product["price"] <= int(float(cap) * 100):
            return rule
    return None


def diff_refurb(state, current, ok_categories, cfg):
    """只在成功抓取的分类里做 diff。抓取失败的分类一律保持旧状态。"""
    rc = cfg.get("refurb", {})
    rules = rc.get("target_rules", [])
    events = []
    old = state.get("refurb", {})

    for key, product in current.items():
        prev = old.get(key)
        rule = match_target(product, rules)
        rule_tag = f"{rule.get('match')}|{rule.get('max_price')}" if rule else ""

        if prev is None or prev.get("gone"):
            if rc.get("notify_new_listing", False):
                events.append({"source": "refurb", "kind": "new_listing",
                               "key": key, "p": product, "rule_tag": rule_tag,
                               "priority": 2})
            continue

        old_price = int(prev.get("price") or 0)
        if old_price and product["price"] < old_price:
            drop = old_price - product["price"]
            events.append({"source": "refurb", "kind": "price_drop",
                           "key": key, "p": product, "old_price": old_price,
                           "drop": drop, "drop_pct": drop * 100.0 / old_price,
                           "rule_tag": rule_tag, "priority": 0})
        elif rule and prev.get("target_tag") != rule_tag:
            events.append({"source": "refurb", "kind": "target_price",
                           "key": key, "p": product, "rule": rule,
                           "rule_tag": rule_tag, "priority": 1})

    # 下架：只标记成功抓取过的分类，不推送
    gone_keys = [k for k, v in old.items()
                 if not v.get("gone") and k.split("/")[0] in ok_categories and k not in current]
    for key in gone_keys:
        log(f"官翻下架（不推送）：{key}")
    return events, gone_keys


def diff_pickup(state, snapshot, cfg):
    pc = cfg.get("pickup", {})
    reminder = int(pc.get("reminder_hours", 6)) * 3600
    epoch = int(time.time())
    old = state.get("pickup", {})
    events = []
    for part, snap in snapshot.items():
        if snap["status"] == "unknown":
            continue  # 未知态不写状态、不提醒
        prev = old.get(part) or {}
        prev_status = prev.get("status", "unknown")
        last = int(prev.get("last_notified") or 0)
        if snap["status"] == "available":
            if prev_status != "available":
                events.append({"source": "pickup", "kind": "restock",
                               "part": part, "snap": snap, "priority": 3})
            elif pc.get("remind_while_in_stock", False) and epoch - last >= reminder:
                events.append({"source": "pickup", "kind": "still_in_stock",
                               "part": part, "snap": snap, "priority": 4})
    return events


# ---------------------------------------------------------------- 推送

def store_label(catalog, code):
    return (catalog.get("stores") or {}).get(code, code)


def build_payload(event, cfg, catalog):
    topic = cfg["ntfy_topic"]
    if event["source"] == "refurb":
        p = event["p"]
        kind = event["kind"]
        if kind == "price_drop":
            title = f"官翻降价 · {money(p['price'])}"
            body = (f"{p['title']}\n\n"
                    f"{money(event['old_price'])} → {money(p['price'])}　"
                    f"降 {money(event['drop'])}（{event['drop_pct']:.1f}%）\n"
                    f"分类：{CATEGORY_CN.get(p['category'], p['category'])}　货号：{p['part']}")
            tags = ["arrow_downward", "moneybag"]
        elif kind == "target_price":
            rule = event["rule"]
            title = f"官翻到目标价 · {money(p['price'])}"
            body = (f"{p['title']}\n\n"
                    f"现价 {money(p['price'])}，已低于你设的上限 "
                    f"{money(int(float(rule['max_price']) * 100))}\n"
                    f"匹配条件：{rule['match']}　货号：{p['part']}")
            tags = ["moneybag", "white_check_mark"]
        else:
            title = f"官翻新上架 · {money(p['price'])}"
            body = (f"{p['title']}\n\n上架价 {money(p['price'])}\n"
                    f"分类：{CATEGORY_CN.get(p['category'], p['category'])}　货号：{p['part']}")
            tags = ["new", "shopping_cart"]
        click = p["url"] or f"{BASE}/shop/refurbished/{p['category']}"
        priority = 3
    else:
        part = event["part"]
        snap = event["snap"]
        info = (catalog.get("products") or {}).get(part, {})
        name = info.get("name", part)
        codes = snap["stores"]
        shown = [store_label(catalog, c) for c in codes[:6]]
        more = f"，另有 {len(codes) - 6} 家" if len(codes) > 6 else ""
        if event["kind"] == "restock":
            title = f"到店取货有货 · {name.split(' · ')[0]}"
            lead = "现在可到店取货"
        else:
            title = f"到店取货持续有货 · {name.split(' · ')[0]}"
            lead = "仍有门店可到店取货"
        body = (f"{name}\n\n{lead}：\n" + "\n".join(f"· {x}" for x in shown) +
                f"{more}\n\n货号：{part}　有货门店 {len(codes)} 家")
        click = info.get("buy_url") or f"{BASE}/shop/buy-iphone"
        tags = ["shopping_cart", "bell"]
        priority = 4

    return {
        "topic": topic,
        "title": title,
        "message": f"{body}\n\n{now_ts()}",
        "click": click,
        "tags": tags,
        "priority": priority,
    }


def publish(cfg, payload, dry_run=False):
    if dry_run:
        log("--- DRY RUN 将推送 ---")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return True
    token = os.environ.get("APPLE_NTFY_TOKEN") or ""
    url = cfg["ntfy_url"].rstrip("/") + "/"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
            return 200 <= r.status < 300
    except urllib.error.HTTPError as e:
        log(f"推送失败 HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}")
        return False
    except Exception as e:
        log(f"推送失败：{e}")
        return False


# ---------------------------------------------------------------- 状态与归档

def apply_refurb_state(state, current, ok_categories, gone_keys, events_by_key, pushed_keys):
    """推送成功的条目写新值；未推送成功的保留旧值，下轮会重试。"""
    refurb = dict(state.get("refurb", {}))
    # 下架标记
    for key in gone_keys:
        row = dict(refurb.get(key, {}))
        row["gone"] = True
        refurb[key] = row
    # 无事件的条目正常刷新（价格没变也要更新标题 / 链接）
    for key, product in current.items():
        if key in events_by_key and key not in pushed_keys:
            continue
        row = dict(refurb.get(key, {}))
        row.update({"category": product["category"], "part": product["part"],
                    "title": product["title"], "price": product["price"],
                    "url": product["url"], "gone": False})
        if key in pushed_keys:
            row["target_tag"] = events_by_key[key].get("rule_tag", "")
        refurb[key] = row
    state["refurb"] = refurb


def apply_pickup_state(state, snapshot, events_by_part, pushed_parts):
    pickup = dict(state.get("pickup", {}))
    epoch = int(time.time())
    for part, snap in snapshot.items():
        if snap["status"] == "unknown":
            continue
        if part in events_by_part and part not in pushed_parts:
            continue
        row = dict(pickup.get(part, {}))
        row.update({"status": snap["status"], "stores": snap["stores"]})
        if part in pushed_parts:
            row["last_notified"] = epoch
        pickup[part] = row
    state["pickup"] = pickup


def archive_events(events, pushed):
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    path = ARCHIVE_DIR / f"events-{today()[:7]}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        for ev in events:
            flat = {k: v for k, v in ev.items() if k not in ("p", "snap", "rule")}
            if "p" in ev:
                flat["title"] = ev["p"]["title"]
                flat["price"] = ev["p"]["price"]
            if "rule" in ev:
                flat["rule"] = ev["rule"]
            flat["ts"] = now_ts()
            flat["pushed"] = ev["key"] in pushed if ev["source"] == "refurb" else ev["part"] in pushed
            f.write(json.dumps(flat, ensure_ascii=False, sort_keys=True) + "\n")


# ---------------------------------------------------------------- 命令

def cmd_init(cfg):
    log("建基线：刷新门店 / SKU 目录")
    catalog = build_catalog(cfg)
    save_json(CATALOG_PATH, catalog)
    log(f"目录：{len(catalog['stores'])} 家门店，{len(catalog['products'])} 个 SKU")

    current, ok_categories = fetch_refurb(cfg)
    if not current:
        raise SystemExit("官翻一个分类都没抓到，基线未建立（下一轮会重试）")

    state = new_state()
    for key, product in current.items():
        state["refurb"][key] = {"category": product["category"], "part": product["part"],
                                "title": product["title"], "price": product["price"],
                                "url": product["url"], "gone": False, "target_tag": ""}
    try:
        snapshot, mode = fetch_pickup(cfg, catalog)
        for part, snap in snapshot.items():
            if snap["status"] != "unknown":
                state["pickup"][part] = {"status": snap["status"],
                                         "stores": snap["stores"],
                                         "last_notified": 0}
        state["pickup_mode"] = mode
        in_stock = sum(1 for v in snapshot.values() if v["status"] == "available")
        log(f"自提基线（{mode}）：{len(snapshot)} 个 SKU，当前 {in_stock} 个有货")
    except (RateLimited, FetchError) as e:
        log(f"自提基线未建立（{e}），下轮自动补齐，不影响官翻")

    state["seeded"] = True
    state["last_run"] = now_ts()
    save_json(STATE_PATH, state)
    log(f"基线完成：官翻 {len(state['refurb'])} 件。首轮不推送，避免刷屏。")


def cmd_refresh_catalog(cfg):
    catalog = build_catalog(cfg)
    save_json(CATALOG_PATH, catalog)
    log(f"目录已刷新：{len(catalog['stores'])} 家门店，{len(catalog['products'])} 个 SKU")


def run_once(cfg, dry_run=False, max_override=None):
    state = load_json(STATE_PATH, new_state())
    if not state.get("seeded"):
        log("state 未建基线，自动执行 --init（本轮不推送）")
        cmd_init(cfg)
        return
    catalog = load_catalog()
    if not catalog.get("products"):
        log("catalog 为空，本轮跳过自提部分（跑一次 --refresh-catalog 可修复）")

    epoch = int(time.time())
    cooldown = int(state.get("cooldown_until") or 0)
    events = []
    gone_keys = []
    snapshot = {}
    current = {}
    ok_categories = set()
    mode_changed = False

    # ---- 官翻：静态 CDN 页，不受 541 影响
    current, ok_categories = fetch_refurb(cfg)
    if ok_categories:
        refurb_events, gone_keys = diff_refurb(state, current, ok_categories, cfg)
        events.extend(refurb_events)
        log(f"官翻：{len(current)} 件，{len(refurb_events)} 个候选事件")
    else:
        log("官翻：本轮没有成功抓取的分类，状态保持不变")

    # ---- 自提：有冷却就整段跳过
    if epoch < cooldown:
        log(f"自提：处于 541 冷却（还剩 {(cooldown - epoch) // 60} 分钟），本轮跳过")
    elif not catalog.get("products"):
        pass
    else:
        try:
            snapshot, mode = fetch_pickup(cfg, catalog)
            prev_mode = state.get("pickup_mode") or ""
            in_stock = sum(1 for v in snapshot.values() if v["status"] == "available")
            if prev_mode and prev_mode != mode:
                # 两种数据源覆盖的门店范围不同（全大陆 vs 少数重点门店），
                # 切换时只重建基线，避免「全无货→有货」的跨环境误报。
                log(f"自提数据源 {prev_mode} → {mode}，本轮只重建基线不推送")
                rebuilt = {}
                for part, snap in snapshot.items():
                    if snap["status"] == "unknown":
                        continue
                    old_row = (state.get("pickup") or {}).get(part, {})
                    rebuilt[part] = {"status": snap["status"], "stores": snap["stores"],
                                     "last_notified": int(old_row.get("last_notified") or 0)}
                state["pickup"] = rebuilt
                snapshot = {}
                pickup_events = []
                mode_changed = True
            else:
                pickup_events = diff_pickup(state, snapshot, cfg)
                events.extend(pickup_events)
                if not prev_mode:
                    mode_changed = True
            state["pickup_mode"] = mode
            log(f"自提（{mode}）：{in_stock} 个有货，{len(pickup_events)} 个候选事件")
        except RateLimited as e:
            state["cooldown_until"] = epoch + 900
            log(f"自提被限频（{e}），进入 15 分钟冷却；状态不变，不当无货")
        except FetchError as e:
            log(f"自提抓取失败（{e}）；状态不变，不当无货")

    events.sort(key=lambda e: e["priority"])
    limit = max_override if max_override is not None else int(cfg.get("max_per_run", 3))
    selected, overflow = events[:limit], events[limit:]
    if overflow:
        log(f"单轮上限 {limit} 条，另有 {len(overflow)} 个事件留到下一轮")

    pushed_refurb, pushed_pickup = set(), set()
    for ev in selected:
        payload = build_payload(ev, cfg, catalog)
        ok = publish(cfg, payload, dry_run=dry_run)
        if not ok:
            log(f"推送失败，状态不更新，下轮重试：{payload['title']}")
            continue
        if ev["source"] == "refurb":
            pushed_refurb.add(ev["key"])
        else:
            pushed_pickup.add(ev["part"])
        log(f"已推送：{payload['title']}")

    # dry-run 不落盘
    if dry_run:
        log(f"DRY RUN 结束：共 {len(selected)} 条候选消息，未写 state / 归档")
        return

    events_by_key = {e["key"]: e for e in events if e["source"] == "refurb"}
    events_by_part = {e["part"]: e for e in events if e["source"] == "pickup"}
    apply_refurb_state(state, current, ok_categories, gone_keys, events_by_key, pushed_refurb)
    apply_pickup_state(state, snapshot, events_by_part, pushed_pickup)
    state["last_run"] = now_ts()

    # 只在有事件 / 冷却变化 / 数据源切换时回写，避免无意义的 CI commit
    if events or mode_changed or state.get("cooldown_until") != load_json(STATE_PATH, {}).get("cooldown_until"):
        save_json(STATE_PATH, state)
        if events:
            pushed = pushed_refurb | pushed_pickup
            archive_events(events, pushed)
        log(f"state 已更新并归档 {len(events)} 个事件")
    else:
        state["last_run"] = load_json(STATE_PATH, {}).get("last_run", "")
        log("本轮无事件、无冷却 / 数据源变化，state 保持不变（不产生 commit）")


def cmd_test(cfg, dry_run=False):
    payload = {
        "topic": cfg["ntfy_topic"],
        "title": "苹果监控 · 自检",
        "message": f"链路自检：{now_ts()}\n主题 {cfg['ntfy_topic']} 已就绪，只提醒不下单。",
        "click": BASE + "/shop/refurbished",
        "tags": ["wrench"],
    }
    ok = publish(cfg, payload, dry_run=dry_run)
    log("自检推送成功" if ok else "自检推送失败")
    return ok


def main():
    ap = argparse.ArgumentParser(description="Apple 中国大陆官翻降价 / 直营店自提 → ntfy")
    ap.add_argument("--init", action="store_true", help="建基线，不推送")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要推送的消息，不落盘")
    ap.add_argument("--test", action="store_true", help="发一条自检消息")
    ap.add_argument("--max", type=int, default=None, help="覆盖单轮推送上限")
    ap.add_argument("--list-stores", action="store_true", help="联网打印直营店代码")
    ap.add_argument("--list-products", action="store_true", help="打印当前监控的 SKU")
    ap.add_argument("--refresh-catalog", action="store_true", help="重建门店 / SKU 快照")
    args = ap.parse_args()
    cfg = load_config()

    if args.list_stores:
        try:
            stores = fetch_stores()
        except FetchError as e:
            raise SystemExit(f"门店列表获取失败：{e}")
        for code, name in sorted(stores.items()):
            print(f"{code}\t{name}")
        return
    if args.list_products:
        products = (load_catalog().get("products") or {})
        for part, info in sorted(products.items()):
            print(f"{part}\t{info['name']}")
        print(f"共 {len(products)} 个 SKU", file=sys.stderr)
        return
    if args.refresh_catalog:
        cmd_refresh_catalog(cfg)
        return
    if args.init:
        cmd_init(cfg)
        return
    if args.test:
        if not cmd_test(cfg, dry_run=args.dry_run):
            raise SystemExit(1)
        return
    run_once(cfg, dry_run=args.dry_run, max_override=args.max)


if __name__ == "__main__":
    main()
