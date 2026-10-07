import type { ProductCard } from "../types";
import { platformLabel } from "../lib/productPlatform";
import Modal from "./Modal";
import { money, ProductImage } from "./ProductCards";
export default function ProductComparison({
  products,
  onClose,
}: {
  products: ProductCard[];
  onClose: () => void;
}) {
  return (
    <Modal title="商品比较" onClose={onClose}>
      <h2>放在一起，选择更清楚。</h2>
      <p className="modal-intro">同类候选对比，尚未确认同款。请先核对品牌、型号、尺寸和件数，再比较商品报价。</p>
      <div className="comparison-scroll">
        <table className="compare-table">
          <thead>
            <tr>
              <th>你的心选</th>
              {products.map((product) => (
                <th key={product.product_id}>
                  <ProductImage product={product} />
                  <strong>{product.title}</strong>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            <tr><td>来源平台</td>{products.map(p => <td key={p.product_id}>{platformLabel(p)}</td>)}</tr>
            <tr>
              <td>商品报价</td>
              {products.map((p) => (
                <td key={p.product_id}>
                  <span className="compare-price">
                    {p.price_text || money(p.price_major, p.currency)}
                  </span>
                </td>
              ))}
            </tr>
            <tr><td>采集 / 更新时间</td>{products.map(p => <td key={p.product_id}>{p.updated_at ? new Date(p.updated_at).toLocaleString("zh-CN") : "未提供"}</td>)}</tr>
            <tr><td>报价配送地区</td>{products.map(p => <td key={p.product_id}>{p.source_platform === "Amazon" ? `美国邮编 ${p.delivery_zipcode || "未提供"}；跨境配送待核实` : p.source_platform === "eBay" ? "美国页面报价；跨境配送待核实" : "目的地配送和费用需单独核实"}</td>)}</tr>
            <tr><td>报价条件</td>{products.map(p => <td key={p.product_id}>{p.price_conditions?.join("；") || "具体规格与结算价格需核对"}</td>)}</tr>
            <tr>
              <td>报价规格</td>
              {products.map((p) => (
                <td key={p.product_id}>
                  {p.source_platform === "CJdropshipping" && !p.quote_sku_id ? "列表报价；尚未绑定具体规格" :
                    p.skus.find((sku) => sku.sku_id === (p.quote_sku_id || p.default_sku_id))?.spec || "报价规格未提供"}
                </td>
              ))}
            </tr>
            <tr>
              <td>分类</td>
              {products.map((p) => (
                <td key={p.product_id}>{p.category}</td>
              ))}
            </tr>
            <tr>
              <td>商品特点</td>
              {products.map((p) => (
                <td key={p.product_id}>
                  {p.highlights.slice(0, 3).join("；") || "目录未提供"}
                </td>
              ))}
            </tr>
            <tr>
              <td>可选规格</td>
              {products.map((p) => (
                <td key={p.product_id}>
                  {p.skus.map((s) => s.spec).join(" / ") || "目录未提供"}
                </td>
              ))}
            </tr>
            <tr>
              <td>评分快照 / 样例</td>
              {products.map((p) => (
                <td key={p.product_id}>
                  {p.rating_summary
                    ? `★ ${p.rating_summary.average}（${p.rating_summary.review_count} 条）`
                    : "未提供"}
                </td>
              ))}
            </tr>
          </tbody>
        </table>
      </div>
      <p className="drawer-note comparison-note">
        基于当前目录快照比较商品报价。报价不代表含跨境运费、税费的到手价；不同规格、件数、配送地区、币种与促销资格可能影响最终价格。
      </p>
    </Modal>
  );
}
