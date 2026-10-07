import type { ProductCard } from "../types";

/** Display names of every snapshot platform that can appear in a product card. */
export const PLATFORM_LABELS: Record<string, string> = {
  CJdropshipping: "CJ",
  Amazon: "Amazon 美国站",
  eBay: "eBay 美国站",
};
/** Compact names for image captions and dense lists. */
const SHORT_LABELS: Record<string, string> = { CJdropshipping: "CJ", Amazon: "Amazon", eBay: "eBay" };
/** Backend source_status keys (cj / amazon / ebay) to display names. */
export const SOURCE_STATUS_LABELS: Record<string, string> = { cj: "CJ", amazon: "Amazon", ebay: "eBay" };

export function platformLabel(product: Pick<ProductCard, "source_platform">): string {
  const platform = product.source_platform;
  return (platform && PLATFORM_LABELS[platform]) || platform || "商品目录";
}

export function shortPlatformLabel(product: Pick<ProductCard, "source_platform">): string {
  const platform = product.source_platform;
  return (platform && SHORT_LABELS[platform]) || platform || "平台";
}

/** Marketplace snapshots quote a US listing price and are never purchased through CJ. */
export function isMarketplaceSnapshot(product: Pick<ProductCard, "source_platform">): boolean {
  return product.source_platform === "Amazon" || product.source_platform === "eBay";
}
