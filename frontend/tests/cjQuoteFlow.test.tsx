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
  sku_id: id, spec: id, price_major: 20, currency: "USD", stock: 0, stock_known: false,
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

it("列表商品可点击试算，单规格自动查询详情、物流并显示新库存时间", async () => {
  const detail = { ...listed, detail_available: true, skus: [variant("SKU-1")], default_sku_id: "SKU-1" };
  const request = vi.fn(async (path: string) => path === "/catalog/detail"
    ? { product: { ...detail, inventory_checked_at: "2026-09-29T04:00:00Z" } }
    : { quote: { status: "quoted", cj_trial_total_usd: 25, product_subtotal_usd: 20,
      shipping_and_cj_fees_usd: 5, fee_status: "tax_or_clearance_unknown", ship_from_warehouse: "CN",
      ship_to: "CN", shipping_method: "CJ Packet", route_count: 1, quoted_at: "2026-09-29T04:00:00Z" } });
  await mount(request);
  expect(host.querySelector<HTMLButtonElement>(".cj-quote-controls button")!.disabled).toBe(false);
  await click("获取规格并查询 CJ 运费");
  expect(request.mock.calls.map(call => call[0])).toEqual(["/catalog/detail", "/catalog/quote", "/catalog/detail"]);
  expect(request.mock.calls[1][2]).toMatchObject({ product_id: "CJ-100", sku_id: "SKU-1", ship_to: "CN", quantity: 1 });
  expect(host.textContent).toContain("US$25.00");
  expect(host.textContent).toContain("SKU-1");
});

it("多规格商品先让用户选择，再用所选 SKU 请求试算", async () => {
  const detail = { ...listed, detail_available: true, skus: [variant("SKU-1"), variant("SKU-2")], default_sku_id: "SKU-1" };
  const request = vi.fn(async (path: string) => path === "/catalog/detail"
    ? { product: detail }
    : { quote: { status: "quoted", cj_trial_total_usd: 27, product_subtotal_usd: 20,
      shipping_and_cj_fees_usd: 7, fee_status: "tax_or_clearance_unknown", ship_from_warehouse: "CN",
      ship_to: "CN", shipping_method: "CJ Packet", route_count: 1, quoted_at: "2026-09-29T04:00:00Z" } });
  await mount(request);
  await click("获取规格并查询 CJ 运费");
  expect(request).toHaveBeenCalledTimes(1);
  expect(host.textContent).toContain("找到 2 种规格");
  const radio = host.querySelector<HTMLInputElement>('input[value="SKU-2"]')!;
  await act(async () => radio.click());
  await click("查询 CJ 物流试算");
  expect(request.mock.calls[1][2]).toMatchObject({ sku_id: "SKU-2" });
  expect(host.querySelector<HTMLInputElement>('input[value="SKU-2"]')!.checked).toBe(true);
  expect(host.textContent).toContain("US$27.00");
});
