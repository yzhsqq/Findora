// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import App from "../src/App";
import CjCatalogPage from "../src/components/CjCatalogPage";

let host: HTMLDivElement, root: Root;
beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  localStorage.clear(); sessionStorage.clear();
  vi.stubGlobal("scrollTo", vi.fn());
  Element.prototype.scrollIntoView = vi.fn();
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove(); vi.unstubAllGlobals();
});

it("能力请求失败保留目录入口，重试后恢复真实平台模式", async () => {
  let attempts = 0;
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    if (String(url).includes("/catalog/capabilities")) {
      attempts++;
      return attempts === 1 ? Response.json({detail: "unavailable"}, {status: 503}) : Response.json({source: "multi"});
    }
    if (String(url).includes("/catalog?")) return Response.json({source: "multi", products: [], total: 0});
    return Response.json({sessions: [], confirmations: [], skills: [], products: []});
  }));
  await act(async () => root.render(<App />));
  expect(host.querySelector('[role="alert"]')?.textContent).toContain("商品库暂时无法连接");
  expect([...host.querySelectorAll("button")].some(b => b.textContent === "商品库")).toBe(true);
  const retry = [...host.querySelectorAll("button")].find(b => b.textContent === "重试连接商品库")!;
  await act(async () => retry.click());
  expect(attempts).toBe(2);
  expect(host.textContent).toContain("多平台商品库");
  expect(host.textContent).not.toContain("商品库暂时无法连接");
});

it("局部失败明确说明统计范围并可重试平台", async () => {
  const request = vi.fn().mockResolvedValue({products: [], total: 0, source_status: {cj: "ok", amazon: "unavailable"}});
  await act(async () => root.render(<CjCatalogPage multiPlatform request={request} favoriteIds={new Set()}
    comparedIds={new Set()} onFavorite={() => {}} onCompare={() => {}} onDetail={() => {}} />));
  expect(host.textContent).toContain("Amazon 暂时无法读取");
  expect(host.textContent).toContain("统计仅包含可用平台");
  const retry = [...host.querySelectorAll("button")].find(b => b.textContent === "重试全部平台")!;
  request.mockResolvedValue({products: [], total: 0, source_status: {cj: "ok", amazon: "ok"}});
  await act(async () => retry.click());
  expect(request).toHaveBeenCalledTimes(2);
  expect(host.textContent).not.toContain("Amazon 暂时无法读取");
});
