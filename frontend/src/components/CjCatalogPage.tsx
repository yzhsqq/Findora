import { useEffect, useState, type FormEvent } from "react";
import { readProducts } from "../lib/commerceClient";
import type { ProductCard } from "../types";
import ProductCards from "./ProductCards";
import "./cjCatalogPage.css";

type Request = (path: string, method?: string, body?: Record<string, unknown>) => Promise<Record<string, unknown>>;
type Snapshot = { total: number; all_count: number; detail_count: number; inventory_count: number; page: number; products: ProductCard[] };
const CATEGORIES = ["", "Bags & Shoes", "Sports & Outdoors", "Consumer Electronics", "Phones & Accessories", "Home, Garden & Furniture", "Health, Beauty & Hair", "Pet Supplies", "Computer & Office", "Toys, Kids & Babies"];
const LABELS = ["全部", "箱包鞋履", "户外运动", "消费电子", "手机配件", "家居园艺", "美妆个护", "宠物用品", "电脑办公", "玩具母婴"];

export default function CjCatalogPage({ request, favoriteIds, comparedIds, onFavorite, onCompare, onDetail }: {
  request: Request;
  favoriteIds: Set<string>; comparedIds: Set<string>;
  onFavorite: (product: ProductCard) => void;
  onCompare: (product: ProductCard) => void;
  onDetail: (product: ProductCard) => void;
}) {
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState("");
  const [page, setPage] = useState(1);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    const params = new URLSearchParams({ query, category, page: String(page), page_size: "24" });
    setBusy(true); setError("");
    void request(`/catalog?${params.toString()}`).then(data => {
      if (!active) return;
      setSnapshot({
        total: Number(data.total) || 0, all_count: Number(data.all_count) || 0,
        detail_count: Number(data.detail_count) || 0, inventory_count: Number(data.inventory_count) || 0,
        page: Number(data.page) || page, products: readProducts(data.products),
      });
    }).catch(e => { if (active) setError(e instanceof Error ? e.message : "商品目录读取失败"); })
      .finally(() => { if (active) setBusy(false); });
    return () => { active = false; };
  }, [request, query, category, page]);
  const search = (event: FormEvent) => { event.preventDefault(); setPage(1); setQuery(draft.trim()); };
  return <section className="cj-catalog">
    <div className="cj-hero">
      <span className="cj-eyebrow">CJ DROPSHIPPING / PRODUCT SNAPSHOT</span>
      <h1>真实商品，<em>清楚标注。</em></h1>
      <p>浏览 CJ 商品快照。列表报价来自采集时刻；规格、库存和目的地运费按已取得的信息分别展示。</p>
      <div className="cj-stats" aria-label="商品快照统计">
        <div><strong>{snapshot?.all_count.toLocaleString("zh-CN") ?? "—"}</strong><span>商品列表</span></div>
        <div><strong>{snapshot?.detail_count.toLocaleString("zh-CN") ?? "—"}</strong><span>规格详情</span></div>
        <div><strong>{snapshot?.inventory_count.toLocaleString("zh-CN") ?? "—"}</strong><span>库存快照</span></div>
      </div>
    </div>
    <div className="cj-controls">
      <form onSubmit={search} className="cj-search">
        <label htmlFor="cj-search-input">搜索商品</label>
        <div><input id="cj-search-input" value={draft} onChange={e => setDraft(e.target.value)} maxLength={120} placeholder="例如 backpack、耳机、pet" /><button type="submit">搜索 ↗</button></div>
      </form>
      <div className="cj-categories" aria-label="商品品类">
        {CATEGORIES.map((item, i) => <button key={item} type="button" className={category === item ? "active" : ""} onClick={() => { setCategory(item); setPage(1); }}>{LABELS[i]}</button>)}
      </div>
    </div>
    <div className="cj-list-heading"><div><span>当前目录</span><h2>{query ? `“${query}”的搜索结果` : LABELS[CATEGORIES.indexOf(category)]}</h2></div><span>{snapshot?.total.toLocaleString("zh-CN") ?? "—"} 件</span></div>
    {error && <p className="cj-error" role="alert">{error}</p>}
    {busy && <p className="cj-loading" role="status">正在读取 CJ 商品快照…</p>}
    {!busy && !error && snapshot && (snapshot.products.length ? <>
      <ProductCards products={snapshot.products} favoriteIds={favoriteIds} comparedIds={comparedIds} onFavorite={onFavorite} onCompare={onCompare} onDetail={onDetail} />
      <div className="cj-pages"><button disabled={page === 1} onClick={() => setPage(page - 1)}>上一页</button><span>第 {page} / {Math.max(1, Math.ceil(snapshot.total / 24))} 页</span><button disabled={page * 24 >= snapshot.total} onClick={() => setPage(page + 1)}>下一页</button></div>
    </> : <p className="cj-loading">这组条件下没有商品，试试其他关键词或品类。</p>)}
    <p className="cj-disclaimer">数据来源：CJdropshipping 商品接口的本地快照。列表报价以 USD 展示，可能为区间；当前不保证实时库存、配送范围或最终到手价。</p>
  </section>;
}
