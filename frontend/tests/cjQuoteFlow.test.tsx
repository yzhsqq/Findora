// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import ProductDetail from "../src/components/ProductDetail";
import type { ProductCard } from "../src/types";

const listed: ProductCard = {
  product_id: "CJ-100",
  title: "Ceramic cup",
  brand: "",
  category: "Home",
  origin_country: "",
  price_major: 19.55,
  currency: "USD",
  highlights: [],
  skus: [],
  score: 1,
  source_platform: "CJdropshipping",
  price_text: "US$19.55",
  detail_available: false,
};

const variant = (id: string): ProductCard["skus"][number] => ({
  sku_id: id, spec: id, price_major: 20, currency: "USD", stock: 10, stock_known: true,
  cj_stock: 0, factory_stock: 10,
});

let host: HTMLDivElement;
let root: Root;
beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
});

async function mount(request: (path: string, method?: string, body?: Record<string, unknown>) => Promise<Record<string, unknown>>) {
  await act(async () => root.render(<ProductDetail product={listed} busy={false} request={request}
    onClose={() => {}} onCompare={() => {}} onAsk={() => {}} onPrepare={() => {}} />));
}

async function click(text: string) {
  const button = [...host.querySelectorAll("button")].find(item => item.textContent?.includes(text));
  expect(button).toBeTruthy();
  await act(async () => button!.click());
}

async function selectDestination(country: string) {
  await act(async () => {
    const select = host.querySelector<HTMLSelectElement>(".cj-quote-controls select")!;
    select.value = country;
    select.dispatchEvent(new Event("change", { bubbles: true }));
  });
}

it("列表商品可点击试算，单规格自动查询详情、物流并显示新库存时间", async () => {
  const detail = { ...listed, detail_available: true, skus: [variant("SKU-1")], default_sku_id: "SKU-1",
    ship_from_warehouses: [], factory_inventory_countries: ["CN"] };
  const request = vi.fn(async (path: string) => path === "/catalog/detail"
    ? { product: { ...detail, inventory_checked_at: "2026-09-29T04:00:00Z" } }
    : { quote: { status: "quoted", cj_trial_total_usd: 25, product_subtotal_usd: 20,
      shipping_and_cj_fees_usd: 5, fee_status: "tax_or_clearance_unknown", quote_origin_country: "CN",
      origin_inventory_kind: "factory_inventory", origin_inventory_verified: false, route_scope: "same_country",
      ship_to: "CN", shipping_method: "CJ Packet", route_count: 1, quoted_at: "2026-09-29T04:00:00Z" } });
  await mount(request);
  expect(host.querySelector<HTMLButtonElement>(".cj-quote-controls button")!.disabled).toBe(false);
  await click("获取规格并查询 CJ 运费");
  expect(request).not.toHaveBeenCalled();
  expect(host.textContent).toContain("请先选择目的国");
  await selectDestination("CN");
  await click("获取规格并查询 CJ 运费");
  expect(request.mock.calls.map(call => call[0])).toEqual(["/catalog/detail", "/catalog/quote", "/catalog/detail"]);
  expect(request.mock.calls[1][2]).toMatchObject({ product_id: "CJ-100", sku_id: "SKU-1", ship_to: "CN", quantity: 1 });
  expect(host.textContent).toContain("US$25.00");
  expect(host.textContent).toContain("SKU-1");
  expect(host.textContent).toContain("工厂备货记录，非 CJ 仓现货");
  expect(host.textContent).toContain("同国物流试算");
});

it("多规格商品先让用户选择，再用所选 SKU 请求试算", async () => {
  const detail = { ...listed, detail_available: true, skus: [variant("SKU-1"), variant("SKU-2")], default_sku_id: "SKU-1" };
  const request = vi.fn(async (path: string) => path === "/catalog/detail"
    ? { product: detail }
    : { quote: { status: "quoted", cj_trial_total_usd: 27, product_subtotal_usd: 20,
      shipping_and_cj_fees_usd: 7, fee_status: "tax_or_clearance_unknown", quote_origin_country: "CN",
      origin_inventory_kind: "factory_inventory", origin_inventory_verified: false, route_scope: "cross_border",
      ship_to: "US", shipping_method: "CJ Packet", route_count: 1, quoted_at: "2026-09-29T04:00:00Z" } });
  await mount(request);
  await selectDestination("US");
  await click("获取规格并查询 CJ 运费");
  expect(request).toHaveBeenCalledTimes(1);
  expect(host.textContent).toContain("找到 2 种规格");
  const radio = host.querySelector<HTMLInputElement>('input[value="SKU-2"]')!;
  await act(async () => radio.click());
  await click("查询 CJ 物流试算");
  expect(request.mock.calls[1][2]).toMatchObject({ sku_id: "SKU-2", ship_to: "US" });
  expect(host.querySelector<HTMLInputElement>('input[value="SKU-2"]')!.checked).toBe(true);
  expect(host.textContent).toContain("US$27.00");
  expect(host.textContent).toContain("跨境物流试算");
});

it("保存待购记录保留所选规格，未知库存不阻止保存", async () => {
  const request = vi.fn(async () => ({record: {record_id: "pending-1"}, created: false}));
  const saved = vi.fn();
  await act(async () => root.render(<ProductDetail product={{...listed, detail_available: true,
    skus: [{...variant("SKU-1"), stock: 0, stock_known: false}, variant("SKU-2")]}}
    busy={false} request={request} onClose={() => {}} onCompare={() => {}} onAsk={() => {}}
    onPrepare={() => {}} onPurchaseSaved={saved} />));
  await act(async () => host.querySelector<HTMLInputElement>('input[value="SKU-1"]')!.click());
  await click("加入待购记录");
  expect(request).toHaveBeenCalledWith("/purchase-records", "PUT", {product_id: "CJ-100", sku_id: "SKU-1"});
  expect(saved).toHaveBeenCalledWith(false);
});

it("无购买链接也可保存待购记录，并允许失败后重试", async () => {
  const request = vi.fn().mockRejectedValueOnce(new Error("保存失败"))
    .mockResolvedValue({record: {record_id: "pending-1"}, created: true});
  const saved = vi.fn();
  await act(async () => root.render(<ProductDetail product={listed} busy={false} request={request}
    onClose={() => {}} onCompare={() => {}} onAsk={() => {}} onPrepare={() => {}} onPurchaseSaved={saved} />));
  await click("加入待购记录");
  expect(host.textContent).toContain("保存失败");
  expect(saved).not.toHaveBeenCalled();
  await click("加入待购记录");
  expect(request).toHaveBeenLastCalledWith("/purchase-records", "PUT", {product_id: "CJ-100", sku_id: ""});
  expect(saved).toHaveBeenCalledWith(true);
  expect(host.textContent).toContain("已保存到“我的订单”");
});

it("购买选项打开 CJ 页面，界面不显示裸链接，也不请求下单", async () => {
  const pid = "05B050F6-9DF5-4488-9218-B1D919650ADE";
  const url = `https://cjdropshipping.com/product/green-sandalwood-hair-comb-p-${pid}.html`;
  const request = vi.fn();
  const props = { busy: false, request, onClose: () => {}, onCompare: () => {}, onAsk: () => {}, onPrepare: () => {} };
  await act(async () => root.render(<ProductDetail {...props} product={{ ...listed, product_id: pid,
    source_url: url, source_url_status: "observed", source_description: "Full supplier details" }} />));
  const link = host.querySelector<HTMLAnchorElement>(".cj-purchase-button")!;
  expect(link.textContent).toContain("前往商品购买页面");
  expect(link.href).toBe(url);
  expect(link.target).toBe("_blank");
  expect(link.rel).toBe("noopener noreferrer");
  expect(host.textContent).not.toContain(url);
  expect(host.textContent).toContain("Full supplier details");
  expect(request).not.toHaveBeenCalled();
  await act(async () => root.render(<ProductDetail {...props} key="missing" product={listed} />));
  expect(host.querySelector(".cj-purchase-button")).toBeNull();
});
