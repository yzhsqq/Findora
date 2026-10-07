import type { ProductCard } from "../types";

const hosts = new Set(["cjdropshipping.com", "www.cjdropshipping.com", "eur.cjdropshipping.com"]);

export function cjPurchaseUrl(product: Pick<ProductCard, "product_id" | "source_platform" | "source_url" | "source_url_status">): string | undefined {
  if (product.source_platform !== "CJdropshipping" ||
      !["observed", "page_verified"].includes(product.source_url_status || "") || !product.source_url ||
      !/^(?:\d+|[\da-f]{8}(?:-[\da-f]{4}){3}-[\da-f]{12})$/i.test(product.product_id)) return;
  try {
    const url = new URL(product.source_url);
    const suffix = `-p-${product.product_id}.html`.toLowerCase();
    if (url.protocol !== "https:" || !hosts.has(url.hostname) || url.username || url.password ||
        url.port || url.search || url.hash || /\s/.test(product.source_url) ||
        !/^\/product\/[a-z0-9._%-]+$/i.test(url.pathname) || !url.pathname.toLowerCase().endsWith(suffix)) return;
    return product.source_url;
  } catch { return; }
}
