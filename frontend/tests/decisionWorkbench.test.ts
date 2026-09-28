import { describe, expect, it } from "vitest";
import { CommerceClient } from "../src/lib/commerceClient";
import { readDecisionReport } from "../src/lib/decisions";
import type { DecisionRequest } from "../src/types";

const request: DecisionRequest = {
  normalized_query: "旅行背包",
  category: "旅行装备",
  ship_to: "CN",
  target_currency: "CNY",
  price_max_major: 200,
  budget_basis: "landed",
  excluded_material_tags: [],
  required_material_tags: [],
};
const product = {
  product_id: "P1003", title: "旅行背包", brand: "Wanderlite",
  category: "旅行装备", origin_country: "CN", price_major: 129,
  currency: "CNY", score: 0.8, highlights: ["轻便"],
  skus: [{ sku_id: "P1003-S1", spec: "标准", price_major: 129, currency: "CNY", stock: 3 }],
  default_sku_id: "P1003-S1",
};
const report = {
  version: 2, request, status: "ready", generated_at: "2026-09-28T00:00:00Z",
  evidence_refs: ["ctx-1"], excluded: [],
  candidates: [{
    product, sku_id: "P1003-S1", reasons: ["库存可售"], tradeoffs: [], unknowns: [],
    checks: [{ field: "skus.stock", label: "可售库存", status: "pass",
      detail: "快照库存 3", evidence: { kind: "catalog_snapshot", ref: "ctx-1",
        field: "skus.stock", observed_at: "2026-09-28T00:00:00Z" } }],
  }],
};

describe("V2 决策单前端契约", () => {
  it("只接受结构完整的候选，并限制最多五件", () => {
    const parsed = readDecisionReport({ ...report, candidates: Array.from({ length: 6 }, () => report.candidates[0]) });
    expect(parsed?.candidates).toHaveLength(5);
    expect(parsed?.candidates[0].checks[0].evidence.ref).toBe("ctx-1");
    expect(readDecisionReport({ ...report, version: 1 })).toBeNull();
    expect(readDecisionReport({ ...report, candidates: [{ ...report.candidates[0], product: { ...product, price_major: -1 } }] })?.candidates).toEqual([]);
  });

  it("按买家会话提交结构化条件，切换会话后忽略迟到结果", async () => {
    let resolvePreview: ((response: Response) => void) | undefined;
    const calls: Array<{ url: string; body: Record<string, unknown> }> = [];
    const client = new CommerceClient({
      url: "/commerce/ag-ui/run", buyerId: "buyer-a",
      fetch: async (url, init) => {
        calls.push({ url: String(url), body: JSON.parse(String(init?.body)) });
        return new Promise<Response>(resolve => { resolvePreview = resolve; });
      },
    });
    const pending = client.previewDecision(request);
    expect(calls[0].url).toContain("/commerce/decisions/preview?buyer_id=buyer-a");
    expect(calls[0].body).toMatchObject({
      buyer_id: "buyer-a", session_id: client.getSnapshot().sessionId,
      query: "旅行背包", budget_basis: "landed", price_max_major: 200,
    });
    client.reset();
    resolvePreview?.(new Response(JSON.stringify(report), { headers: { "Content-Type": "application/json" } }));
    await pending;
    expect(client.getSnapshot().decisionReport).toBeNull();
    expect(client.getSnapshot().decisionPreviewBusy).toBe(false);
  });

  it("成功预览只展示服务端返回的决策单", async () => {
    const client = new CommerceClient({
      url: "/commerce/ag-ui/run", buyerId: "buyer-a",
      fetch: async () => new Response(JSON.stringify(report), { headers: { "Content-Type": "application/json" } }),
    });
    await client.previewDecision(request);
    expect(client.getSnapshot().decisionReport?.candidates[0].product.product_id).toBe("P1003");
    expect(client.getSnapshot().decisionReport?.request.budget_basis).toBe("landed");
  });

  it("恢复历史时不把较早的预览盖到较新的运行上", async () => {
    const sessionId = "session-a";
    const runUpdatedAt = Date.parse("2026-09-28T00:01:00Z");
    const client = new CommerceClient({
      url: "/commerce/ag-ui/run", buyerId: "buyer-a",
      fetch: async (url) => {
        const path = String(url);
        const data = path.includes("/decisions/preview") ? { report }
          : path.includes("/sessions/session-a") ? {
            run: { status: "completed", messages: [], state: { products: [], searchCompleted: false }, updatedAt: runUpdatedAt },
          } : { sessions: [{ id: sessionId, title: "新的选购", updatedAt: runUpdatedAt }] };
        return new Response(JSON.stringify(data), { headers: { "Content-Type": "application/json" } });
      },
    });
    await client.initialize();
    expect(client.getSnapshot().sessionId).toBe(sessionId);
    expect(client.getSnapshot().decisionReport).toBeNull();
  });
});
