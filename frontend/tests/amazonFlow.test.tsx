// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { readProducts } from "../src/lib/commerceClient";
import ProductDetail from "../src/components/ProductDetail";
import ProductComparison from "../src/components/ProductComparison";
import CjCatalogPage from "../src/components/CjCatalogPage";
import type { ProductCard } from "../src/types";

const card: ProductCard = {
  product_id: "amazon:us:B000MD58UM", external_product_id: "B000MD58UM", source_platform: "Amazon",
  title: "KONG Dog toy", brand: "KONG", category: "Pet Supplies", origin_country: "",
  price_major: 17.95, currency: "USD", price_kind: "listing", price_text: "US$17.95", highlights: [], score: 1,
  default_sku_id: "amazon:us:B000MD58UM", source_region: "US", delivery_zipcode: "11001",
  updated_at: "2026-10-06T08:54:40Z", stock_known: false, match_status: "unverified",
  price_conditions: ["Prime Big Deal", "优惠资格需核实"],
  source_url: "https://www.amazon.com/dp/B000MD58UM?th=1", source_url_status: "observed",
  skus: [{ sku_id: "amazon:us:B000MD58UM", variant_id: "B000MD58UM", spec: "XL Pack of 1", price_major: 17.95, currency: "USD", stock: 0, stock_known: false },
         { sku_id: "amazon:us:B000MD57ZI", variant_id: "B000MD57ZI", spec: "L Pack of 1", price_major: 12.99, currency: "USD", stock: 0, stock_known: false }],
};
let host: HTMLDivElement;
let root: Root;
beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
});
afterEach(async () => { await act(async () => root.unmount()); host.remove(); });

it("中文展示保留可核对的英文标题、规格、优惠条件和实际报价", async () => {
  const localized = readProducts([{ ...card, title: "KONG 狗狗玩具", category: "宠物用品", description: "拉扯互动玩具。",
    source_title: card.title, source_description: "Tug toy", source_highlights: ["Nylon toy"],
    source_price_conditions: card.price_conditions, price_conditions: ["Prime 会员促销，资格待核实"],
    skus: card.skus.map(s => ({ ...s, source_spec: s.spec, spec: "大号 1件装" })) }])[0];
  expect(localized.source_title).toBe(card.title);
  expect(localized.skus[0].source_spec).toBe("XL Pack of 1");
  expect(localized.price_major).toBe(17.95);
  await act(async () => root.render(<ProductDetail product={localized} busy={false} request={vi.fn()}
    onClose={() => {}} onCompare={() => {}} onAsk={() => {}} onPrepare={() => {}} />));
  expect(host.querySelector("h2")?.textContent).toBe("KONG 狗狗玩具");
  expect(host.querySelector(".sku-picker")?.textContent).toContain("大号 1件装");
  const original = host.querySelector("details")!;
  expect(original.querySelector("summary")?.textContent).toBe("Amazon 商品原文");
  expect(original.textContent).toContain("KONG Dog toy");
  expect(original.textContent).toContain("XL Pack of 1");
  expect(original.textContent).toContain("Prime Big Deal");
});

it("保留 Amazon 来源、地区、时间和促销条件，只接受匹配 ASIN 的平台链接", () => {
  const parsed = readProducts([card])[0];
  expect(parsed.delivery_zipcode).toBe("11001");
  expect(parsed.price_conditions).toEqual(card.price_conditions);
  expect(parsed.source_url).toBe(card.source_url);
  expect(parsed.stock_known).toBe(false);
  for (const url of ["https://amazon.com.evil.test/dp/B000MD58UM", "https://user:pass@amazon.com/dp/B000MD58UM",
    "https://amazon.com/dp/B000AAAA01", "javascript:alert(1)"]) {
    expect(readProducts([{ ...card, source_url: url }])[0].source_url).toBeUndefined();
  }
});

it("Amazon 详情跳转原平台，不触发 CJ 试算或站内下单，未知库存明确展示", async () => {
  const request = vi.fn(), onPrepare = vi.fn(), onCompare = vi.fn();
  await act(async () => root.render(<ProductDetail product={card} busy={false} request={request}
    onClose={() => {}} onCompare={onCompare} onAsk={() => {}} onPrepare={onPrepare} />));
  expect(host.textContent).toContain("美国邮编 11001");
  expect(host.textContent).toContain("Prime Big Deal");
  expect(host.textContent).toContain("实时库存未核实");
  expect(host.textContent).not.toContain("准备下单意向");
  expect(host.textContent).not.toContain("查询 CJ 物流试算");
  const radio = [...host.querySelectorAll<HTMLInputElement>('input[type="radio"]')][1];
  await act(async () => radio.click());
  expect(host.querySelector<HTMLAnchorElement>(".cj-purchase-button")?.href).toBe("https://www.amazon.com/dp/B000MD57ZI");
  const compare = [...host.querySelectorAll("button")].find(b => b.textContent?.includes("用当前规格比较"))!;
  await act(async () => compare.click());
  expect(onCompare.mock.calls[0][0]).toMatchObject({ price_major: 12.99, default_sku_id: "amazon:us:B000MD57ZI" });
  expect(onCompare.mock.calls[0][0].price_text).toContain("12.99");
  expect(request).not.toHaveBeenCalled(); expect(onPrepare).not.toHaveBeenCalled();
});

it("缺失报价不显示为免费", async () => {
  await act(async () => root.render(<ProductDetail product={{...card, price_kind: "unknown", price_major: 0, price_text: "报价待核实", skus: [], default_sku_id: undefined}}
    busy={false} request={vi.fn()} onClose={() => {}} onCompare={() => {}} onAsk={() => {}} onPrepare={() => {}} />));
  expect(host.querySelector(".price")?.textContent).toBe("报价待核实");
});

it("比较页保留平台、采集时间和促销条件，并说明未确认同款", async () => {
  await act(async () => root.render(<ProductComparison products={[card]} onClose={() => {}} />));
  expect(host.textContent).toContain("尚未确认同款");
  expect(host.textContent).toContain("Amazon 美国站");
  expect(host.textContent).toContain("11001");
  expect(host.textContent).toContain("Prime Big Deal");
  expect(host.textContent).toContain("到手价");
});

it("平台切换请求正确且展示新平台目录", async () => {
  const request = vi.fn(async (path: string) => ({source: "multi", total: 1, all_count: 1,
    categories: ["Pet Supplies"], source_counts: { amazon: 1 }, products: [card]}));
  await act(async () => root.render(<CjCatalogPage multiPlatform request={request} favoriteIds={new Set()}
    comparedIds={new Set()} onFavorite={() => {}} onCompare={() => {}} onDetail={() => {}} />));
  const amazonButton = [...host.querySelectorAll("button")].find(b => b.textContent === "Amazon 美国站")!;
  await act(async () => amazonButton.click());
  expect(request.mock.calls.at(-1)?.[0]).toContain("platform=amazon");
  expect(host.textContent).toContain("KONG Dog toy");
});
