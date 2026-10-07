import { useState } from "react";
import type { CJFreightQuote, ProductCard } from "../types";
import { readProducts } from "../lib/commerceClient";
import { productPurchaseUrl } from "../lib/productPurchaseUrl";
import { isMarketplaceSnapshot, shortPlatformLabel } from "../lib/productPlatform";
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
  onPurchaseSaved,
}: {
  product: ProductCard;
  busy: boolean;
  request: (path: string, method?: string, body?: Record<string, unknown>) => Promise<Record<string, unknown>>;
  onClose: () => void;
  onCompare: (product: ProductCard) => void;
  onAsk: (query: string) => void;
  onPrepare: (product: ProductCard, skuId: string) => void;
  onPurchaseSaved?: (created: boolean) => void;
}) {
  const [product, setProduct] = useState(initialProduct);
  const amazon = product.source_platform === "Amazon";
  const ebay = product.source_platform === "eBay";
  const marketplace = isMarketplaceSnapshot(product);
  const platformName = shortPlatformLabel(product);
  const observedPurchaseUrl = productPurchaseUrl(product);
  const [skuId, setSkuId] = useState(
    (initialProduct.source_platform === "CJdropshipping" && initialProduct.skus.length !== 1) || (marketplace && !initialProduct.default_sku_id)
      ? ""
      : initialProduct.default_sku_id || initialProduct.skus[0]?.sku_id || "",
  );
  const [shipTo, setShipTo] = useState("");
  const [detailBusy, setDetailBusy] = useState(false);
  const [quoteBusy, setQuoteBusy] = useState(false);
  const [quoteError, setQuoteError] = useState("");
  const [quoteNotice, setQuoteNotice] = useState("");
  const [quote, setQuote] = useState<CJFreightQuote | null>(null);
  const [savingPurchase, setSavingPurchase] = useState(false);
  const [purchaseError, setPurchaseError] = useState("");
  const [purchaseSaved, setPurchaseSaved] = useState(false);
  const sku = product.skus.find((item) => item.sku_id === skuId),
    landed = product.landed_price;
  // A selected variation opens the platform's own listing for that variation.
  const purchaseUrl = observedPurchaseUrl && amazon && sku?.variant_id && /^[A-Z0-9]{10}$/.test(sku.variant_id)
    ? `https://www.amazon.com/dp/${sku.variant_id}`
    : observedPurchaseUrl && ebay && sku?.variant_id && /^\d{6,20}$/.test(sku.variant_id) &&
      sku.variant_id !== product.external_product_id && product.external_product_id
      ? `https://www.ebay.com/itm/${product.external_product_id}?var=${sku.variant_id}`
      : observedPurchaseUrl;
  const originCountry = quote?.quote_origin_country ||
    (quote as (CJFreightQuote & { ship_from_warehouse?: string }) | null)?.ship_from_warehouse || "未提供";
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
  const savePurchase = async () => {
    if (savingPurchase) return;
    setSavingPurchase(true); setPurchaseError(""); setPurchaseSaved(false);
    try {
      const data = await request("/purchase-records", "PUT", {
        product_id: product.product_id, sku_id: sku?.sku_id || "",
      });
      if (!data.record || typeof data.created !== "boolean") throw new Error("待购记录保存结果无效，请刷新后重试。");
      setPurchaseSaved(true);
      onPurchaseSaved?.(data.created);
    } catch (error) {
      setPurchaseError(error instanceof Error ? error.message : "待购记录未保存，请重试。");
    } finally { setSavingPurchase(false); }
  };
  const loadCJQuote = async () => {
    setQuoteBusy(true); setQuoteError(""); setQuoteNotice(""); setQuote(null);
    try {
      if (!shipTo) {
        setQuoteNotice("请先选择目的国；中国到中国是同国配送，不属于跨境物流。");
        return;
      }
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
      if (value?.status !== "quoted" || typeof value.cj_trial_total_usd !== "number" ||
          typeof value.quote_origin_country !== "string" ||
          !["cj_warehouse", "factory_inventory", "unknown"].includes(value.origin_inventory_kind || "") ||
          !["same_country", "cross_border"].includes(value.route_scope || ""))
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
            : product.image_kind === "source" ? `${platformName} 商品图片` : "暂无商品实拍"}
        </span>
      </div>
      <div className="drawer-kicker">
        {product.brand || product.source_platform || "商品目录"} · {product.product_id}
      </div>
      <h2>{product.title}</h2>
      <p className="drawer-description">
        {product.description || product.highlights.join("；")}
      </p>
      {(product.source_title || (product.source_description && product.source_description !== product.description)) && (
        <details className="cj-source-description">
          <summary>{platformName} 商品原文</summary>
          {product.source_title && <p>原标题：{product.source_title}</p>}
          {product.source_description && <p>{product.source_description}</p>}
          {product.source_highlights?.length ? <ul>{product.source_highlights.map((text, i) => <li key={i}>{text}</li>)}</ul> : null}
          {sku?.source_spec && <p>所选规格原文：{sku.source_spec}</p>}
          {product.source_price_conditions?.length ? <p>报价条件原文：{product.source_price_conditions.join("；")}</p> : null}
        </details>
      )}
      <div className="price">
        {product.price_kind === "unknown" && !sku ? "报价待核实" : product.source_platform === "CJdropshipping" && !sku ? product.price_text || "报价待核实" : money(
          sku?.price_major ?? product.price_major,
          sku?.currency ?? product.currency,
        )}
      </div>
      <span className="detail-price-kind">{product.source_platform === "CJdropshipping" ? sku ? "CJ 规格参考价" : "CJ 列表参考价" : marketplace ? `${platformName} 规格快照报价 · USD` : "当前规格商品价"}</span>
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
                {product.source_platform === "CJdropshipping"
                  ? !item.stock_known ? "库存未核验"
                    : item.cj_stock !== undefined && item.factory_stock !== undefined
                      ? `CJ 仓 ${item.cj_stock} · 工厂备货 ${item.factory_stock}（快照）`
                      : `CJ 记录总库存 ${item.stock}（可能含工厂备货）`
                  : marketplace ? "实时库存未核实" : item.stock > 0 ? `快照库存 ${item.stock}` : "目录暂无库存"}
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
        {amazon && <>
          <div><span>来源平台</span><span>Amazon 美国站</span></div>
          <div><span>页面 ASIN</span><span>{product.external_product_id || "未提供"}</span></div>
          <div><span>卖家</span><span>{product.seller_name || "未提供"}（所采集页面）</span></div>
          <div><span>报价配送地区</span><span>美国邮编 {product.delivery_zipcode || "未提供"}</span></div>
          <div><span>页面状态</span><span>{product.availability_text || "未提供"}</span></div>
          <div><span>采集时间</span><span>{product.updated_at ? new Date(product.updated_at).toLocaleString("zh-CN") : "未提供"}</span></div>
          <div><span>报价条件</span><span>{product.price_conditions?.join("；") || "待核实"}</span></div>
        </>}
        {ebay && <>
          <div><span>来源平台</span><span>eBay 美国站</span></div>
          <div><span>页面商品编号</span><span>{product.external_product_id || "未提供"}</span></div>
          <div><span>卖家</span><span>{product.seller_name || "未提供"}（所采集页面）</span></div>
          <div><span>页面成色</span><span>{product.condition || "未提供"}</span></div>
          <div><span>页面状态</span><span>{product.availability_text || "未提供"}</span></div>
          <div><span>采集时间</span><span>{product.updated_at ? new Date(product.updated_at).toLocaleString("zh-CN") : "未提供"}</span></div>
          <div><span>报价条件</span><span>{product.price_conditions?.join("；") || "待核实"}</span></div>
        </>}
        {product.source_platform === "CJdropshipping" && <>
          <div><span>品牌</span><span>{product.brand || "CJ 未提供"}</span></div>
          <div><span>供应商</span><span>{product.supplier_name || "未提供"}</span></div>
          <div><span>CJ 仓有库存的国家</span><span>{product.factory_inventory_countries === undefined
            ? "旧快照未区分 CJ 仓与工厂备货"
            : product.ship_from_warehouses?.join(" / ") ||
              (product.inventory_checked_at ? "未见 CJ 仓现货" : "库存待查询")}</span></div>
          <div><span>工厂备货记录国家</span><span>{product.factory_inventory_countries?.join(" / ") ||
            (product.inventory_checked_at && product.factory_inventory_countries !== undefined ? "未见工厂备货" : "库存待查询")}</span></div>
          <div><span>库存记录查询于</span><span>{product.inventory_checked_at
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
            <span>{marketplace ? "评分快照" : "评分样例"}</span>
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
          <label>目的国<select value={shipTo} onChange={event => { setShipTo(event.target.value); setQuote(null); setQuoteNotice(""); }}>
            <option value="">请选择目的国</option>
            <option value="CN">中国 CN（同国或寄中国）</option><option value="US">美国 US</option><option value="GB">英国 GB</option>
            <option value="JP">日本 JP</option><option value="SG">新加坡 SG</option>
          </select></label>
          <button type="button" disabled={quoteBusy || detailBusy} onClick={() => void loadCJQuote()}>
            {quoteBusy ? "正在向 CJ 查询…" : !product.detail_available ? "获取规格并查询 CJ 运费" : "查询 CJ 物流试算"}
          </button>
        </div>
        {quoteNotice && <p className="cj-quote-hint" role="status">{quoteNotice}</p>}
        {quoteError && <p className="cj-quote-error" role="alert">{quoteError}</p>}
        {quote && <div className="cj-quote-result">
          <span>{(quote.route_scope || (originCountry === quote.ship_to ? "same_country" : "cross_border")) === "same_country"
            ? "CJ 同国物流试算" : "CJ 跨境物流试算"}</span>
          <strong>{money(quote.cj_trial_total_usd, "USD")}</strong>
          <p>规格商品价 {money(quote.product_subtotal_usd, "USD")} + CJ 物流及已列费用 {money(quote.shipping_and_cj_fees_usd, "USD")}</p>
          <small>试算起运国 {originCountry} → 目的国 {quote.ship_to} · {quote.shipping_method} · {quote.route_count} 条可选路线</small>
          <small>来源库存：{quote.origin_inventory_kind === "cj_warehouse" ? "CJ 仓库存快照"
            : quote.origin_inventory_kind === "factory_inventory" ? "工厂备货记录，非 CJ 仓现货" : "类型未确认"}
            {quote.origin_inventory_verified ? " · CJ 标记已核验" : " · CJ 未标记已核验"}。起运国是试算参数，最终发货地待下单核对。</small>
          {quote.fee_status !== "cj_reported" && <small>税费或清关费用未完整返回；以上仅为已知费用合计。</small>}
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
        ? "商品来自 CJ 快照；物流费用仅供试算。你可以保存待购记录，购买时前往 CJ 核对规格、价格、库存、地址与税费。"
        : amazon ? "来自 Amazon 美国站快照；美国配送报价不代表可寄往中国。商品报价不含完整跨境运费与税费；跨平台候选未经确认同款。所选变体的卖家及优惠条件需在商品页重新核对。"
        : ebay ? "来自 eBay 美国站快照；美国页面报价不代表可寄往中国，页面成色与卖家信息需在商品页复核。商品报价不含完整跨境运费与税费；跨平台候选未经确认同款。所选变体的成色、运费及优惠条件需在商品页重新核对。"
        : "价格、库存为目录查询结果，购买前需要再次核对。图片与评分如标注为示意或样例，不代表实时平台信息。"}</p>
      <button
        className="primary-button"
        disabled={busy}
        onClick={() => {
          onAsk(
            product.source_platform === "CJdropshipping"
              ? `请介绍 CJ 商品「${product.title}」（product_id=${product.product_id}${sku ? `，sku_id=${sku.sku_id}，规格=${sku.spec}` : ""}）。若我询问物流或到手费用，请调用 CJ 物流试算工具；区分试算与最终支付价，未知税费不要当作零。`
              : amazon ? `请介绍 Amazon 美国站商品「${product.title}」（product_id=${product.product_id}${sku ? `，sku_id=${sku.sku_id}，规格=${sku.spec}` : ""}），并找 CJ 同类候选比较。请注明采集时间、规格和报价条件；未确认同款，跨境配送和到手价待核实。`
              : ebay ? `请介绍 eBay 美国站商品「${product.title}」（product_id=${product.product_id}${sku ? `，sku_id=${sku.sku_id}，规格=${sku.spec}` : ""}），并找 CJ 同类候选比较。请注明采集时间、页面成色、规格和运费条件；未确认同款，跨境配送和到手价待核实。`
              : `请进一步核对「${product.title}」（product_id=${product.product_id}${sku ? `，sku_id=${sku.sku_id}，规格=${sku.spec}` : ""}）的当前库存与到手价。`,
          );
          onClose();
        }}
      >
        <Icon name="chat" />
        {busy ? "正在处理上一条需求" : product.source_platform === "CJdropshipping" ? "继续了解这款" : "帮我进一步确认这款"}
      </button>
      {product.source_platform !== "CJdropshipping" && !marketplace && <button
        className="drawer-compare"
        disabled={busy || !sku || sku.stock <= 0}
        onClick={() => sku && onPrepare(product, sku.sku_id)}
      >
        准备下单意向
      </button>}
      {product.source_platform === "CJdropshipping" && <>
        <button type="button" className="drawer-compare" disabled={savingPurchase || detailBusy || quoteBusy}
          onClick={() => void savePurchase()}>{savingPurchase ? "正在保存待购记录…" : "加入待购记录"}</button>
        <p className="drawer-note">保存在“我的订单”中，状态为待购买。保存记录不会在 CJ 下单。
          {!sku && "规格尚未选择，可在购买时确认。"}</p>
        {purchaseSaved && <p className="cj-quote-hint" role="status">已保存到“我的订单”的待购记录。</p>}
        {purchaseError && <p className="cj-quote-error" role="alert">{purchaseError}</p>}
      </>}
      {purchaseUrl && <a className="cj-purchase-button" href={purchaseUrl} target="_blank" rel="noopener noreferrer">
        {amazon ? "前往 Amazon 查看 / 购买" : ebay ? "前往 eBay 查看 / 购买" : "前往商品购买页面"} <Icon name="arrow" />
      </a>}
      {purchaseUrl && <p className="drawer-note">将在新窗口打开 {platformName} 商品页面，请在 {platformName} 确认规格、价格、库存和配送地址后购买。</p>}
      <button
        className="drawer-compare"
        onClick={() =>
          onCompare(
            sku
              ? {
                  ...product,
                  default_sku_id: sku.sku_id,
                  quote_sku_id: sku.sku_id,
                  price_major: sku.price_major,
                  currency: sku.currency,
                  price_text: money(sku.price_major, sku.currency),
                  price_kind: "listing",
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
