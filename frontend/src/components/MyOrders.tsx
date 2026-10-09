import { useCallback, useEffect, useRef, useState } from "react";
import ConfirmationCards from "./ConfirmationCards";
import type { ProductCard, PurchaseRecord, TradeConfirmation } from "../types";
import { readProducts } from "../lib/productCards";
import { cjPurchaseUrl } from "../lib/cjProductLink";
import { money, ProductImage } from "./ProductCards";
import "./buyerWorkspace.css";
import "./myOrders.css";

type Order = {
  order_id: string; status: string; currency: string; total_amount_major: number;
  shipping_address: string; created_at: string; cancel_reason: string | null;
  lines: { sku_id: string; title: string; quantity: number; unit_price_major: number }[];
};
type Props = {
  request: (path: string, method?: string, body?: Record<string, unknown>) => Promise<any>;
  confirmations: TradeConfirmation[]; busy: boolean; error: string | null;
  onPrepare: (id: string, reason: string) => Promise<boolean>;
  onResolve: (c: TradeConfirmation, approved: boolean) => Promise<boolean>;
  onRefresh: () => Promise<void>;
  onViewProduct?: (product: ProductCard) => void;
  onBrowse?: () => void;
  purchaseRevision?: number;
};
const labels: Record<string, string> = { CONFIRMED: "已确认", CANCELLED: "已取消", DRAFT: "草稿" };
const filters = [["", "全部"], ["PENDING_PURCHASE", "待购买"], ["CONFIRMED", "已确认"], ["CANCELLED", "已取消"]];

function readRecords(value: unknown): PurchaseRecord[] {
  if (!Array.isArray(value)) throw new Error("待购记录格式无效");
  return value.map(record => {
    const product = readProducts([record?.product])[0];
    if (!product || typeof record.record_id !== "string" || record.status !== "PENDING_PURCHASE" ||
        typeof record.sku_id !== "string" || typeof record.created_at !== "string" || record.quantity !== 1)
      throw new Error("待购记录格式无效");
    return { ...record, product } as PurchaseRecord;
  });
}

export default function MyOrders({ request, confirmations, busy, error, onPrepare, onResolve,
  onRefresh, onViewProduct, onBrowse, purchaseRevision = 0 }: Props) {
  const [orders, setOrders] = useState<Order[]>([]), [records, setRecords] = useState<PurchaseRecord[]>([]);
  const [total, setTotal] = useState(0), [offset, setOffset] = useState(0), [status, setStatus] = useState("");
  const [loading, setLoading] = useState(true), [failure, setFailure] = useState("");
  const [detail, setDetail] = useState<string | null>(null), [cancel, setCancel] = useState<string | null>(null);
  const [reason, setReason] = useState(""), [removing, setRemoving] = useState<string | null>(null);
  const revision = useRef(0);
  const refresh = useCallback(async () => {
    const id = ++revision.current;
    setLoading(true); setFailure("");
    try {
      const [orderData, pendingData] = await Promise.all([
        status === "PENDING_PURCHASE" ? Promise.resolve({ orders: [], total: 0 })
          : request(`/orders?offset=${offset}&limit=10${status ? `&status=${status}` : ""}`),
        !status || status === "PENDING_PURCHASE" ? request("/purchase-records") : Promise.resolve({ records: [] }),
      ]);
      if (id !== revision.current) return;
      if (!Array.isArray(orderData.orders) || typeof orderData.total !== "number") throw new Error("订单数据格式无效");
      const pending = readRecords(pendingData.records);
      setOrders(orderData.orders); setTotal(orderData.total); setRecords(pending);
    } catch (e) {
      if (id === revision.current) setFailure(e instanceof Error ? e.message : "选购记录暂时无法读取");
    } finally {
      if (id === revision.current) setLoading(false);
    }
  }, [request, status, offset, purchaseRevision]);
  useEffect(() => { void refresh(); return () => { ++revision.current; }; }, [refresh]);

  const removeRecord = async (recordId: string) => {
    if (removing) return;
    setRemoving(recordId); setFailure("");
    try {
      await request(`/purchase-records/${encodeURIComponent(recordId)}`, "DELETE");
      await refresh();
    } catch (e) { setFailure(e instanceof Error ? e.message : "移除失败，请重试。"); }
    finally { setRemoving(null); }
  };

  return <section className="buyer-workspace" aria-label="我的订单">
    <header className="workspace-heading">
      <div className="eyebrow">EVERY CHOICE, KEPT IN ORDER</div>
      <h1>每一次选择，<em>都有记录。</em></h1>
      <p>保存想买的好物，准备好后前往 CJ 购买。待购记录保存在当前用户下，不代表已经下单或付款。</p>
    </header>
    <div className="orders-toolbar">
      <div role="group" aria-label="订单状态筛选">{filters.map(([value, label]) =>
        <button key={value} type="button" aria-pressed={status === value}
          onClick={() => { setStatus(value); setOffset(0); setDetail(null); }}>{label}</button>)}</div>
      <button type="button" onClick={() => void refresh()} disabled={loading}>刷新订单</button>
    </div>
    {failure && <div className="workspace-error" role="alert">{failure}<button onClick={() => void refresh()}>重试</button></div>}
    {loading ? <p role="status">正在读取选购记录…</p> : <>
      {!failure && !orders.length && !records.length && <div className="workspace-empty">
        <h2>{status ? status === "PENDING_PURCHASE" ? "还没有待购记录" : "暂时没有这类订单" : "还没有选购记录"}</h2>
        <p>打开商品详情，点击“加入待购记录”，就能把想买的商品保存在这里。</p>
        {onBrowse && <button type="button" onClick={onBrowse}>去挑选商品</button>}
      </div>}
      {!!records.length && <section className="pending-purchases" aria-label="待购记录">
        <div className="pending-heading"><div><span>YOUR NEXT FINDS</span><h2>待购记录</h2></div><small>{records.length} 件待购买</small></div>
        <div className="order-list">{records.map(record => {
          const product = record.product, sku = product.skus.find(item => item.sku_id === record.sku_id);
          const purchaseUrl = cjPurchaseUrl(product);
          return <article className="order-card pending-card" key={record.record_id}>
            <header><small>保存于 {new Date(record.created_at).toLocaleString("zh-CN")}</small><span className="order-status PENDING_PURCHASE">待购买</span></header>
            <div className="pending-product"><div className="pending-image"><ProductImage product={product} /></div>
              <div><small>CJdropshipping · {product.category}</small><h2>{product.title}</h2>
                <p>{sku ? `规格：${sku.spec}` : record.sku_id ? `规格编号：${record.sku_id}` : "规格待选择，可在 CJ 购买时确认"}</p>
                <strong>{sku ? money(sku.price_major, sku.currency) : product.price_text || "价格待确认"}</strong>
                <small>CJ 商品参考价 · 最终价格以购买页面为准</small>
              </div>
            </div>
            <footer><small>待购记录 · 尚未在 CJ 下单</small><div>
              {onViewProduct && <button type="button" onClick={() => onViewProduct(product)}>查看商品</button>}
              {purchaseUrl && <a className="pending-purchase-link" href={purchaseUrl} target="_blank" rel="noopener noreferrer">前往商品购买页面 ↗</a>}
              <button type="button" disabled={removing !== null} onClick={() => void removeRecord(record.record_id)}>
                {removing === record.record_id ? "正在移除…" : "移除记录"}</button>
            </div></footer>
            {!purchaseUrl && <p className="pending-link-note">购买链接待补充，你可以先查看商品详情。</p>}
          </article>;
        })}</div>
      </section>}
      {!!orders.length && <div className="order-list">{orders.map(o => <article className="order-card" key={o.order_id}>
        <header><div><small>{new Date(o.created_at).toLocaleString("zh-CN")}</small><h2>{o.order_id}</h2></div><span className={`order-status ${o.status}`}>{labels[o.status] ?? o.status}</span></header>
        <div className="order-lines">{o.lines.map(line => <div key={line.sku_id}><div><strong>{line.title}</strong><small>{line.sku_id} · 数量 {line.quantity}</small></div><span>{money(line.unit_price_major, o.currency)} / 件</span></div>)}</div>
        <footer><span>商品合计 <strong>{money(o.total_amount_major, o.currency)}</strong></span><div><button type="button" aria-expanded={detail === o.order_id} onClick={() => setDetail(detail === o.order_id ? null : o.order_id)}>订单详情</button>{o.status === "CONFIRMED" && <button type="button" disabled={busy} onClick={() => { setCancel(o.order_id); setReason(""); }}>取消订单</button>}</div></footer>
        {detail === o.order_id && <div className="order-detail"><p>收货信息：{o.shipping_address}</p>{o.cancel_reason && <p>取消原因：{o.cancel_reason}</p>}<p>当前为本地演示订单，尚未接入支付和物流；金额不代表实际支付。</p></div>}
        {cancel === o.order_id && o.status === "CONFIRMED" && <form className="order-detail" onSubmit={async e => { e.preventDefault(); if (await onPrepare(o.order_id, reason.trim())) setCancel(null); }}>
          <label>取消原因<input aria-label="取消原因" required maxLength={500} value={reason} onChange={e => setReason(e.target.value)} placeholder="例如：调整了购买计划" /></label>
          <button type="submit" disabled={busy || !reason.trim()}>查看取消确认单</button><button type="button" onClick={() => setCancel(null)}>保留订单</button>
        </form>}
      </article>)}</div>}
    </>}
    {total > 10 && <nav className="orders-pagination" aria-label="订单分页"><button disabled={loading || offset === 0} onClick={() => setOffset(Math.max(0, offset - 10))}>上一页</button><span>共 {total} 单 · 第 {Math.floor(offset / 10) + 1} 页</span><button disabled={loading || offset + 10 >= total} onClick={() => setOffset(offset + 10)}>下一页</button></nav>}
    <ConfirmationCards confirmations={confirmations.filter(c => c.action === "cancel")} busy={busy} error={error}
      onResolve={async (c, approved) => { if (await onResolve(c, approved)) await refresh(); }}
      onCancelOrder={(id, r) => void onPrepare(id, r)} onRefresh={() => void onRefresh()} />
  </section>;
}
