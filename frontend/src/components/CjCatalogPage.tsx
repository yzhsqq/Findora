import { useEffect, useState, type FormEvent } from "react";
import { readProducts } from "../lib/commerceClient";
import type { ProductCard } from "../types";
import ProductCards from "./ProductCards";
import "./cjCatalogPage.css";

type Request = (path: string, method?: string, body?: Record<string, unknown>) => Promise<Record<string, unknown>>;
type Snapshot = { total: number; all_count: number; detail_count: number; inventory_count: number; page: number; products: ProductCard[]; categories: string[]; source_counts: Record<string, number> };
const CATEGORIES = ["", "Bags & Shoes", "Sports & Outdoors", "Consumer Electronics", "Phones & Accessories", "Home, Garden & Furniture", "Health, Beauty & Hair", "Pet Supplies", "Computer & Office", "Toys, Kids & Babies"];
const LABELS = ["全部", "箱包鞋履", "户外运动", "消费电子", "手机配件", "家居园艺", "美妆个护", "宠物用品", "电脑办公", "玩具母婴"];

export default function CjCatalogPage({ request, multiPlatform = false, favoriteIds, comparedIds, onFavorite, onCompare, onDetail }: {
  request: Request;
  multiPlatform?: boolean;
  favoriteIds: Set<string>; comparedIds: Set<string>;
  onFavorite: (product: ProductCard) => void;
  onCompare: (product: ProductCard) => void;
  onDetail: (product: ProductCard) => void;
}) {
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState("");
  const [platform, setPlatform] = useState("");
  const [page, setPage] = useState(1);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    const params = new URLSearchParams({ query, category, platform, page: String(page), page_size: "24" });
    setBusy(true); setError("");
    void request(`/catalog?${params.toString()}`).then(data => {
      if (!active) return;
      setSnapshot({
        total: Number(data.total) || 0, all_count: Number(data.all_count) || 0,
        detail_count: Number(data.detail_count) || 0, inventory_count: Number(data.inventory_count) || 0,
        page: Number(data.page) || page, products: readProducts(data.products),
        categories: Array.isArray(data.categories) ? data.categories.filter((c): c is string => typeof c === "string") : [],
        source_counts: data.source_counts && typeof data.source_counts === "object" ? data.source_counts as Record<string, number> : {},
      });
    }).catch(e => { if (active) setError(e instanceof Error ? e.message : "商品目录读取失败"); })
      .finally(() => { if (active) setBusy(false); });
    return () => { active = false; };
  }, [request, query, category, page, platform]);
  const categories = multiPlatform ? ["", ...(snapshot?.categories || [])] : CATEGORIES;
  const categoryLabel = (value: string) => LABELS[CATEGORIES.indexOf(value)] || ({ "Home & Kitchen": "家居厨房", "Tools & Home Improvement": "工具与家装", "Electronics": "电子产品", "Patio, Lawn & Garden": "庭院园艺", "Industrial & Scientific": "工业与科研", "Arts, Crafts & Sewing": "艺术手工与缝纫", "Pet Supplies": "宠物用品", "Health & Household": "健康家居", "Beauty & Personal Care": "美妆个护", "Baby": "母婴用品", "Baby Products": "母婴用品", "Office Products": "办公用品", "Clothing, Shoes & Jewelry": "服饰鞋包",
  "Grocery & Gourmet Food": "食品与杂货", "Toys & Games": "玩具与游戏", "Collectibles & Fine Art": "收藏品与艺术品",
  "Toys & Hobbies": "玩具与爱好", "Collectibles & Art": "收藏品与艺术", "Home & Garden": "家居园艺",
  "Health & Beauty": "健康与美容", "Fashion": "服饰", "Sporting Goods": "运动用品",
  "Musical Instruments": "乐器", "Software": "软件", "Automotive": "汽车用品", "Video Games": "电子游戏",
  "Cell Phones & Accessories": "手机与配件", "Kindle Store": "Kindle 电子书", "Books": "图书",
  "Sports & Outdoors": "运动与户外", "Movies & TV": "影视", "CDs & Vinyl": "音乐唱片", "Appliances": "家电",
  "Small Appliance Parts & Accessories": "小家电配件", "Kitchen & Dining": "厨房与餐桌",
  "Shoe, Jewelry & Watch Accessories": "鞋履珠宝手表配件", "Pantry Staples": "食品储藏" } as Record<string, string>)[value] || value;
  const search = (event: FormEvent) => {
    event.preventDefault(); setPage(1);
    const value = draft.trim();
    if (/^(?:\d{16,24}|CJ[A-Z0-9_-]{6,96}|(?:amazon:us:)?[A-Z0-9]{10}|(?:ebay:us:)?\d{9,15})$/i.test(value)) setCategory("");
    setQuery(value);
  };
  return <section className="cj-catalog">
    <div className="cj-hero">
      <div className="cj-hero-copy">
        <span className="cj-eyebrow"><span className="cj-eyebrow-dot" /> {multiPlatform ? "CJ + AMAZON + EBAY 美国站商品快照" : "CJ DROPSHIPPING 商品快照"}</span>
        <h1>从全球选，<br /><em>挑你喜欢的。</em></h1>
        <p>{multiPlatform ? "跨平台发现同类商品，对照规格与美元报价。尚未确认同款，跨境物流与最终到手价待核实。" : "在真实商品目录中发现好物。价格按 CJ 原始美元报价呈现，未核实的物流与库存会明确标出。"}</p>
        <a className="cj-hero-link" href="#catalog-results">开始逛商品 <span aria-hidden="true">↗</span></a>
      </div>
      <div className="cj-hero-panel" aria-label="商品快照统计">
        <span className="cj-panel-kicker">CATALOG / LOCAL SNAPSHOT</span>
        <div className="cj-stat-primary"><strong>{snapshot?.all_count.toLocaleString("zh-CN") ?? "—"}</strong><span>件商品列表</span></div>
        <div className="cj-stat-secondary">
          <div><strong>{snapshot?.detail_count.toLocaleString("zh-CN") ?? "—"}</strong><span>已取得规格详情</span></div>
          <div><strong>{snapshot?.inventory_count.toLocaleString("zh-CN") ?? "—"}</strong><span>已取得库存快照</span></div>
        </div>
      </div>
    </div>
    <div className="cj-facts"><span><b>01</b> {multiPlatform && platform !== "cj" ? `CJ ${snapshot?.source_counts.cj ?? "—"} · Amazon ${snapshot?.source_counts.amazon ?? "—"} · eBay ${snapshot?.source_counts.ebay ?? "—"} 件` : "CJ 来源商品"}</span><span><b>02</b> USD 原始报价</span><span><b>03</b> 未核实信息明确标注</span></div>
    <div className="cj-controls">
      <form onSubmit={search} className="cj-search">
        <label htmlFor="cj-search-input">找点感兴趣的</label>
        <div><input id="cj-search-input" value={draft} onChange={e => setDraft(e.target.value)} maxLength={120} placeholder={multiPlatform ? "搜索商品名、属性、商品 ID、CJ SKU、ASIN 或 eBay 商品编号" : "搜索中文商品名、属性、商品 ID 或 CJ SKU"} /><button type="submit">搜索 <span aria-hidden="true">↗</span></button></div>
      </form>
      <div className="cj-quick-search" aria-label="热门搜索">
        <span>快速发现</span>
        {(multiPlatform ? ["狗玩具", "家居装饰", "灯泡"] : ["帽子", "手机壳", "婴儿睡袋"]).map(term => <button key={term} type="button" onClick={() => { setDraft(term); setQuery(term); setPage(1); }}>{term}</button>)}
      </div>
      {multiPlatform && <div className="cj-categories" aria-label="商品平台">{[["", "全部平台"], ["cj", "CJ"], ["amazon", "Amazon 美国站"], ["ebay", "eBay 美国站"]].map(([value, label]) => <button key={value} type="button" className={platform === value ? "active" : ""} onClick={() => { setPlatform(value); setCategory(""); setPage(1); }}>{label}</button>)}</div>}
      <div className="cj-categories" aria-label="商品品类">
        {categories.map(item => <button key={item} type="button" className={category === item ? "active" : ""} onClick={() => { setCategory(item); setPage(1); }}>{categoryLabel(item)}</button>)}
      </div>
    </div>
    <div className="cj-list-heading" id="catalog-results"><div><span>EXPLORE / 商品目录</span><h2>{query ? `“${query}”的搜索结果` : category ? categoryLabel(category) : "逛逛全部商品"}</h2></div><span>共 {snapshot?.total.toLocaleString("zh-CN") ?? "—"} 件 · 商品名称、属性或编号匹配</span></div>
    {error && <p className="cj-error" role="alert">{error}</p>}
    {busy && <p className="cj-loading" role="status">正在读取{multiPlatform ? "多平台" : " CJ "}商品快照…</p>}
    {!busy && !error && snapshot && (snapshot.products.length ? <>
      <ProductCards products={snapshot.products} favoriteIds={favoriteIds} comparedIds={comparedIds} onFavorite={onFavorite} onCompare={onCompare} onDetail={onDetail} />
      <div className="cj-pages"><button disabled={page === 1} onClick={() => setPage(page - 1)}>上一页</button><span>第 {page} / {Math.max(1, Math.ceil(snapshot.total / 24))} 页</span><button disabled={page * 24 >= snapshot.total} onClick={() => setPage(page + 1)}>下一页</button></div>
    </> : <p className="cj-loading">这组条件下没有商品，试试其他关键词或品类。</p>)}
    <p className="cj-disclaimer">{multiPlatform ? "数据来源：CJdropshipping、Amazon 与 eBay 美国站本地快照。Amazon 报价对应采集时美国邮编，eBay 报价对应采集时页面报价与页面成色，促销、运费与会员条件需核对；都不代表可寄往中国。跨平台候选未确认同款，报价不含完整跨境运费与税费。" : "数据来源：CJdropshipping 商品接口的本地快照。列表报价以 USD 展示，可能为区间；当前不保证实时库存、配送范围或最终到手价。"}</p>
  </section>;
}
