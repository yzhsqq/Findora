import type { ProductCard } from "../types";
import { cjPurchaseUrl } from "./cjProductLink";

const EBAY_HOSTS = new Set(["ebay.com", "www.ebay.com"]);

function ebayPurchaseUrl(product: Pick<ProductCard, "product_id" | "source_url" | "source_url_status">): string | undefined {
  const itemId = /^ebay:us:(\d{9,15})$/.exec(product.product_id)?.[1];
  if (!itemId || !product.source_url || !["observed", "page_verified"].includes(product.source_url_status || "")) return;
  try {
    const url = new URL(product.source_url);
    if (url.protocol !== "https:" || !EBAY_HOSTS.has(url.hostname) ||
        url.username || url.password || url.port || /\s/.test(product.source_url)) return;
    // The listing url must point at the same item; a variation query is allowed.
    if (new RegExp(`/itm/${itemId}(?:/|$)`).test(url.pathname)) return product.source_url;
  } catch { return; }
}

export function productPurchaseUrl(product: Pick<ProductCard, "product_id" | "source_platform" | "source_url" | "source_url_status">): string | undefined {
  if (product.source_platform === "eBay") return ebayPurchaseUrl(product);
  if (product.source_platform !== "Amazon") return cjPurchaseUrl(product);
  const asin = /^amazon:us:([A-Z0-9]{10})$/.exec(product.product_id)?.[1];
  if (!asin || !product.source_url || !["observed", "page_verified"].includes(product.source_url_status || "")) return;
  try {
    const url = new URL(product.source_url);
    if (url.protocol !== "https:" || !["amazon.com", "www.amazon.com"].includes(url.hostname) ||
        url.username || url.password || url.port || /\s/.test(product.source_url)) return;
    const sourceAsin = /\/(?:dp|gp\/product)\/([A-Z0-9]{10})(?:\/|$)/.exec(url.pathname)?.[1];
    if (sourceAsin === asin) return product.source_url;
  } catch { return; }
}
