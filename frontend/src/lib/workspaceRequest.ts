import type { HttpAgentConfig } from "@ag-ui/client";

interface WorkspaceOptions {
  url: string;
  buyerId: string;
  fetch?: HttpAgentConfig["fetch"];
  headers: Record<string, string>;
}
const isRecord = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);

/** Buyer-owned ordinary HTTP requests; independent of SDK run and session state. */
export async function requestWorkspace(options: WorkspaceOptions, path: string, method = "GET",
  body?: Record<string, unknown>): Promise<Record<string, unknown>> {
    const base = options.url.replace(/\/ag-ui\/run\/?$/, "");
    const separator = path.includes("?") ? "&" : "?";
    const response = await (options.fetch ?? globalThis.fetch)(`${base}${path}${separator}buyer_id=${encodeURIComponent(options.buyerId)}`, {
      method, headers: { ...options.headers, ...(body ? { "Content-Type": "application/json" } : {}) },
      body: body ? JSON.stringify(path === "/context/compact" ? {...body,buyer_id:options.buyerId} : body) : undefined,
    });
    const data: unknown = await response.json().catch(() => { throw new Error("服务暂时不可用，请稍后重试。输入已保留。"); });
    if (!response.ok) {
      const detail = isRecord(data) ? data.detail : null;
      throw Object.assign(new Error(typeof detail === "string" ? detail : isRecord(detail) && typeof detail.message === "string" ? detail.message : response.status === 401 || response.status === 403
        ? "无法访问个人资料，请检查当前登录身份。" : "未能保存或读取，请刷新后重试。输入已保留。"),{status:response.status});
    }
    if (!isRecord(data)) throw new Error("个人资料服务返回格式无效");
    return data;
}
