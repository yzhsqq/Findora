import { useState } from "react";
import type { CJFreightQuote, ProductCard } from "../types";
import { readProducts } from "../lib/commerceClient";
import Icon from "./Icon";
import Modal from "./Modal";
import { money, ProductImage } from "./ProductCards";
export default function ProductDetail({
  product: initialProduct,
  busy,
  request,
  onClose,
  onCompare,
  onAsk,
  onPrepare,
}: {
  product: ProductCard;
  busy: boolean;
  request: (path: string, method?: string, body?: Record<string, unknown>) => Promise<Record<string, unknown>>;
  onClose: () => void;
  onCompare: (product: ProductCard) => void;
  onAsk: (query: string) => void;
  onPrepare: (product: ProductCard, skuId: string) => void;
}) {
  const [product, setProduct] = useState(initialProduct);
  const [skuId, setSkuId] = useState(
    initialProduct.source_platform === "CJdropshipping" && initialProduct.skus.length !== 1
      ? ""
      : initialProduct.default_sku_id || initialProduct.skus[0]?.sku_id || "",
  );
  const [shipTo, setShipTo] = useState("CN");
  const [detailBusy, setDetailBusy] = useState(false);
  const [quoteBusy, setQuoteBusy] = useState(false);
  const [quoteError, setQuoteError] = useState("");
  const [quoteNotice, setQuoteNotice] = useState("");
  const [quote, setQuote] = useState<CJFreightQuote | null>(null);
  const sku = product.skus.find((item) => item.sku_id === skuId),
    landed = product.landed_price;
  const canShowLanded =
    skuId === product.default_sku_id &&
    landed &&
    !landed.unavailable_reason &&
    Number.isFinite(landed.landed_total_major);
  const fetchCJDetail = async (): Promise<ProductCard> => {
    const data = await request("/catalog/detail", "POST", { product_id: product.product_id });
    const card = readProducts([data.product])[0];
    if (!card) throw new Error("CJ 商品详情格式无效");
    setProduct(card);
    setSkuId(card.skus.length === 1 ? card.skus[0].sku_id : "");
    setQuote(null);
    return card;
  };
  const loadCJDetail = async () => {
    setDetailBusy(true); setQuoteError(""); setQuoteNotice("");
    try {
      const card = await fetchCJDetail();
      if (card.skus.length > 1) setQuoteNotice(`找到 ${card.skus.length} 种规格，请选定后再查询物流。`);
      if (card.skus.length === 0) setQuoteError("CJ 未提供可报价的规格与价格。");
    } catch (error) {
      setQuoteError(error instanceof Error ? error.message : "详情获取失败");
    } finally { setDetailBusy(false); }
  };
  const loadCJQuote = async () => {
    setQuoteBusy(true); setQuoteError(""); setQuoteNotice(""); setQuote(null);
    try {
      let selected = sku;
      if (!selected && !product.detail_available) {
        const card = await fetchCJDetail();
        if (card.skus.length === 0) throw new Error("CJ 未提供可报价的规格与价格。");
        if (card.skus.length > 1) {
          setQuoteNotice(`找到 ${card.skus.length} 种规格，请选定后再点击查询。`);
          return;
        }
        selected = card.skus[0];
      }
      if (!selected) {
        setQuoteNotice("请先选择一个规格，再查询物流试算。");
        return;
      }
      const data = await request("/catalog/quote", "POST", {
        product_id: product.product_id, sku_id: selected.sku_id, ship_to: shipTo, quantity: 1,
      });
      const value = data.quote as Partial<CJFreightQuote> | undefined;
      if (value?.status !== "quoted" || typeof value.cj_trial_total_usd !== "number")
        throw new Error("CJ 报价格式无效");
      setQuote(value as CJFreightQuote);
      // The quote may have refreshed stock. Read the local snapshot without resetting the chosen SKU.
      try {
        const refreshed = await request("/catalog/detail", "POST", { product_id: product.product_id });
        const card = readProducts([refreshed.product])[0];
        if (card) setProduct(card);
      } catch { /* The valid quote is still usable. */ }
    } catch (error) {
      setQuoteError(error instanceof Error ? error.message : "物流试算失败");
    } finally { setQuoteBusy(false); }
  };
  return (
    <Modal title={`${product.title} 商品详情`} drawer onClose={onClose}>
      <div className="drawer-visual">
        <ProductImage product={product} />
        <span className="visual-caption">
          {product.image_kind === "illustration"
            ? "商品示意图 · 非实物照片"
            : product.image_kind === "source" ? "CJ 商品图片" : "暂无商品实拍"}
        </span>
      </div>
      <div className="drawer-kicker">
        {product.brand || product.source_platform || "商品目录"} · {product.product_id}
      </div>
      <h2>{product.title}</h2>
      <p className="drawer-description">
        {product.description || product.highlights.join("；")}
      </p>
      <div className="price">
        {product.source_platform === "CJdropshipping" && !sku ? product.price_text || "报价待核实" : money(
          sku?.price_major ?? product.price_major,
          sku?.currency ?? product.currency,
        )}
      </div>
      <span className="detail-price-kind">{product.source_platform === "CJdropshipping" ? sku ? "CJ 规格参考价" : "CJ 列表参考价" : "当前规格商品价"}</span>
      {product.skus.length > 0 && (
        <fieldset className="sku-picker">
          <legend>选择规格</legend>
          {product.skus.map((item) => (
            <label
              key={item.sku_id}
              className={skuId === item.sku_id ? "chosen" : ""}
            >
              <input
                type="radio"
                name="product-sku"
                value={item.sku_id}
                checked={skuId === item.sku_id}
                onChange={() => { setSkuId(item.sku_id); setQuote(null); setQuoteError(""); setQuoteNotice(""); }}
              />
              <span>{item.spec}</span>
              <small>
                {product.source_platform === "CJdropshipping" && !item.stock_known ? "库存未核验" : item.stock > 0 ? `快照库存 ${item.stock}` : "目录暂无库存"}
              </small>
            </label>
          ))}
        </fieldset>
      )}
      <div className="detail-specs">
        {sku && (
          <div>
            <span>规格编号</span>
            <span>{sku.sku_id}</span>
          </div>
        )}
        <div>
          <span>商品分类</span>
          <span>{product.category}</span>
        </div>
        <div>
          <span>原产地</span>
          <span>{product.origin_country || "未提供"}</span>
        </div>
        {product.source_platform === "CJdropshipping" && <>
          <div><span>品牌</span><span>{product.brand || "CJ 未提供"}</span></div>
          <div><span>供应商</span><span>{product.supplier_name || "未提供"}</span></div>
          <div><span>可选发货仓（库存快照）</span><span>{product.ship_from_warehouses?.join(" / ") ||
            (product.inventory_checked_at ? "未查到可用仓库" : "库存待核验")}</span></div>
          <div><span>仓库库存查询于</span><span>{product.inventory_checked_at
            ? new Date(product.inventory_checked_at).toLocaleString("zh-CN") : "未查询"}</span></div>
          <div><span>材质</span><span>{product.material_tags?.join(" / ") || "未提供"}</span></div>
          <div><span>商品重量</span><span>{product.weight_kg ? `${product.weight_kg} kg` : "未提供"}</span></div>
          <div><span>数据更新时间</span><span>{product.updated_at ? new Date(product.updated_at).toLocaleString("zh-CN") : "未提供"}</span></div>
        </>}
        {(product.ships_to?.length ?? 0) > 0 && (
          <div>
            <span>配送地区</span>
            <span>{product.ships_to?.join(" / ")}</span>
          </div>
        )}
        {product.rating_summary && (
          <div>
            <span>评分样例</span>
            <span>
              ★ {product.rating_summary.average} ·{" "}
              {product.rating_summary.review_count} 条
            </span>
          </div>
        )}
      </div>
      {product.source_platform === "CJdropshipping" ? <div className="cj-quote-panel">
        {!product.detail_available && <button type="button" className="drawer-compare" disabled={detailBusy || quoteBusy}
          onClick={() => void loadCJDetail()}>{detailBusy ? "正在获取 CJ 详情…" : "获取这件商品的规格详情"}</button>}
        <div className="cj-quote-controls">
          <label>目的国<select value={shipTo} onChange={event => { setShipTo(event.target.value); setQuote(null); }}>
            <option value="CN">中国 CN</option><option value="US">美国 US</option><option value="GB">英国 GB</option>
            <option value="JP">日本 JP</option><option value="SG">新加坡 SG</option>
          </select></label>
          <button type="button" disabled={quoteBusy || detailBusy} onClick={() => void loadCJQuote()}>
            {quoteBusy ? "正在向 CJ 查询…" : !product.detail_available ? "获取规格并查询 CJ 运费" : "查询 CJ 物流试算"}
          </button>
        </div>
        {quoteNotice && <p className="cj-quote-hint" role="status">{quoteNotice}</p>}
        {quoteError && <p className="cj-quote-error" role="alert">{quoteError}</p>}
        {quote && <div className="cj-quote-result">
          <span>{quote.fee_status === "cj_reported" ? "CJ 试算合计" : "已知费用合计 · 税费待核"}</span>
          <strong>{money(quote.cj_trial_total_usd, "USD")}</strong>
          <p>规格商品价 {money(quote.product_subtotal_usd, "USD")} + CJ 物流及已列费用 {money(quote.shipping_and_cj_fees_usd, "USD")}</p>
          <small>{quote.ship_from_warehouse} 仓 → {quote.ship_to} · {quote.shipping_method} · {quote.route_count} 条可选路线</small>
          <small>查询于 {new Date(quote.quoted_at).toLocaleString("zh-CN")} · 仅为 CJ 试算，非最终支付价</small>
        </div>}
      </div> : canShowLanded ? (
        <div className="landed-detail-panel">
          <strong>
            到手价 {money(landed.landed_total_major, landed.currency)}
          </strong>
          <span>
            小计 {money(landed.subtotal_major, landed.currency)} + 运费{" "}
            {money(landed.freight_major, landed.currency)} + 关税{" "}
            {money(landed.tariff_major, landed.currency)}
          </span>
          <small>配送至 {landed.ship_to} · 对应当前默认规格报价</small>
        </div>
      ) : (
        <p className="drawer-note">
          {skuId !== product.default_sku_id
            ? "已更换规格，到手价需重新查询。"
            : landed?.unavailable_reason || "到手价待目的地与规格确认。"}
        </p>
      )}
      <p className="drawer-note">{product.source_platform === "CJdropshipping"
        ? "商品来自 CJ 快照；发货仓与目的国配送是不同信息。试算前不会假定可配送；试算后仍需在下单前复核价格、库存、地址与税费，当前不支持直接下单。"
        : "价格、库存为目录查询结果，购买前需要再次核对。图片与评分如标注为示意或样例，不代表实时平台信息。"}</p>
      <button
        className="primary-button"
        disabled={busy}
        onClick={() => {
          onAsk(
            product.source_platform === "CJdropshipping"
              ? `请介绍 CJ 商品「${product.title}」（product_id=${product.product_id}${sku ? `，sku_id=${sku.sku_id}，规格=${sku.spec}` : ""}）。若我询问物流或到手费用，请调用 CJ 物流试算工具；区分试算与最终支付价，未知税费不要当作零。`
              : `请进一步核对「${product.title}」（product_id=${product.product_id}${sku ? `，sku_id=${sku.sku_id}，规格=${sku.spec}` : ""}）的当前库存与到手价。`,
          );
          onClose();
        }}
      >
        <Icon name="chat" />
        {busy ? "正在处理上一条需求" : product.source_platform === "CJdropshipping" ? "继续了解这款" : "帮我进一步确认这款"}
      </button>
      {product.source_platform !== "CJdropshipping" && <button
        className="drawer-compare"
        disabled={busy || !sku || sku.stock <= 0}
        onClick={() => sku && onPrepare(product, sku.sku_id)}
      >
        准备下单意向
      </button>}
      <button
        className="drawer-compare"
        onClick={() =>
          onCompare(
            sku
              ? {
                  ...product,
                  default_sku_id: sku.sku_id,
                  price_major: sku.price_major,
                  currency: sku.currency,
                  landed_price: canShowLanded ? landed : undefined,
                }
              : product,
          )
        }
      >
        <Icon name="compare" />
        用当前规格比较
      </button>
    </Modal>
  );
}
