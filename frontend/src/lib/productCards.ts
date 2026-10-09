import type { ProductCard } from "../types";
import { productPurchaseUrl } from "./productPurchaseUrl";

const isRecord = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
const isAmount = (value: unknown): value is number =>
  typeof value === "number" && Number.isFinite(value) && value >= 0;
const isCount = (value: unknown): value is number =>
  isAmount(value) && Number.isSafeInteger(value);
const isStringArray = (value: unknown): value is string[] =>
  Array.isArray(value) && value.every((entry) => typeof entry === "string");

function isCurrency(value: unknown): value is string {
  if (typeof value !== "string" || !/^[A-Z]{3}$/.test(value)) return false;
  try {
    new Intl.NumberFormat("zh-CN", {
      style: "currency",
      currency: value,
    }).format(0);
    return true;
  } catch {
    return false;
  }
}

function readLandedPrice(
  value: unknown,
  currency: string,
): ProductCard["landed_price"] {
  if (!isRecord(value)) return undefined;
  // 服务端报价失败时只返回原因；保留该真实状态，不补造金额。
  if (
    typeof value.unavailable_reason === "string" &&
    value.unavailable_reason.trim()
  ) {
    return {
      unavailable_reason: value.unavailable_reason,
    } as ProductCard["landed_price"];
  }
  if (
    typeof value.ship_to !== "string" ||
    !value.ship_to.trim() ||
    !isCurrency(value.currency) ||
    value.currency !== currency ||
    !isAmount(value.subtotal_major) ||
    !isAmount(value.freight_major) ||
    !isAmount(value.tariff_major) ||
    !isAmount(value.landed_total_major) ||
    !isAmount(value.tariff_rate) ||
    typeof value.de_minimis_applied !== "boolean"
  )
    return undefined;
  return {
    ship_to: value.ship_to,
    currency: value.currency,
    subtotal_major: value.subtotal_major,
    freight_major: value.freight_major,
    tariff_major: value.tariff_major,
    landed_total_major: value.landed_total_major,
    tariff_rate: value.tariff_rate,
    de_minimis_applied: value.de_minimis_applied,
  };
}

/** 目录卡片是服务端结构化结果，不从模型 Markdown 中猜价格或图片。 */
export function readProducts(value: unknown): ProductCard[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item): ProductCard[] => {
    if (
      !isRecord(item) ||
      typeof item.product_id !== "string" ||
      !item.product_id.trim() ||
      typeof item.title !== "string" ||
      typeof item.brand !== "string" ||
      typeof item.category !== "string" ||
      typeof item.origin_country !== "string" ||
      !isAmount(item.price_major) ||
      !isCurrency(item.currency) ||
      typeof item.score !== "number" ||
      !Number.isFinite(item.score) ||
      !isStringArray(item.highlights) ||
      !Array.isArray(item.skus)
    )
      return [];
    const skus: ProductCard["skus"] = [];
    for (const sku of item.skus) {
      if (
        !isRecord(sku) ||
        typeof sku.sku_id !== "string" ||
        !sku.sku_id.trim() ||
        typeof sku.spec !== "string" ||
        !isAmount(sku.price_major) ||
        !isCurrency(sku.currency) ||
        !isCount(sku.stock)
      )
        return [];
      skus.push({
        sku_id: sku.sku_id,
        ...(typeof sku.variant_id === "string" ? { variant_id: sku.variant_id } : {}),
        spec: sku.spec,
        ...(typeof sku.source_spec === "string" ? { source_spec: sku.source_spec } : {}),
        price_major: sku.price_major,
        currency: sku.currency,
        stock: sku.stock,
        ...(typeof sku.stock_known === "boolean" ? { stock_known: sku.stock_known } : {}),
        ...(isCount(sku.cj_stock) ? { cj_stock: sku.cj_stock } : {}),
        ...(isCount(sku.factory_stock) ? { factory_stock: sku.factory_stock } : {}),
      });
    }
    const card: ProductCard = {
      product_id: item.product_id,
      title: item.title,
      brand: item.brand,
      category: item.category,
      origin_country: item.origin_country,
      price_major: item.price_major,
      currency: item.currency,
      highlights: [...item.highlights],
      score: item.score,
      skus,
    };
    // 可选展示字段单独清洗，坏的评分/图片信息不能拖垮仍可展示的有效商品。
    for (const key of [
      "description",
      "source_description",
      "source_title",
      "source_category",
      "updated_at",
      "image_alt",
      "source_platform",
      "canonical_product_id",
      "price_text",
      "supplier_name",
      "external_product_id",
      "source_region",
      "delivery_zipcode",
      "seller_name",
      "availability_text",
      "condition",
    ] as const) {
      if (typeof item[key] === "string") card[key] = item[key];
    }
    if (item.image_url === null || typeof item.image_url === "string")
      card.image_url = item.image_url;
    if (item.image_kind === "illustration" || item.image_kind === "placeholder" || item.image_kind === "source")
      card.image_kind = item.image_kind;
    if (item.price_kind === "range" || item.price_kind === "listing" || item.price_kind === "unknown") card.price_kind = item.price_kind;
    if (typeof item.stock_known === "boolean") card.stock_known = item.stock_known;
    if (typeof item.snapshot_available === "boolean") card.snapshot_available = item.snapshot_available;
    if (item.match_status === "unverified") card.match_status = item.match_status;
    if (typeof item.detail_available === "boolean") card.detail_available = item.detail_available;
    if (item.source_url_status === "observed" || item.source_url_status === "page_verified") {
      const link = productPurchaseUrl({ ...card, source_url_status: item.source_url_status,
        source_url: typeof item.source_url === "string" ? item.source_url : undefined });
      if (link) {
        card.source_url = link;
        card.source_url_status = item.source_url_status;
        if (typeof item.source_url_checked_at === "string") card.source_url_checked_at = item.source_url_checked_at;
      }
    }
    if (typeof item.inventory_checked_at === "string" || item.inventory_checked_at === null) card.inventory_checked_at = item.inventory_checked_at;
    if (typeof item.rating_is_live === "boolean")
      card.rating_is_live = item.rating_is_live;
    if (item.rating_summary === null) card.rating_summary = null;
    else if (
      isRecord(item.rating_summary) &&
      isAmount(item.rating_summary.average) &&
      item.rating_summary.average <= 5 &&
      isCount(item.rating_summary.review_count)
    ) {
      card.rating_summary = {
        average: item.rating_summary.average,
        review_count: item.rating_summary.review_count,
      };
    }
    for (const key of ["ships_to", "material_tags", "price_conditions", "source_highlights", "source_price_conditions"] as const) {
      if (isStringArray(item[key])) card[key] = [...item[key]];
    }
    if (isStringArray(item.ship_from_warehouses)) card.ship_from_warehouses = [...item.ship_from_warehouses];
    if (isStringArray(item.factory_inventory_countries)) card.factory_inventory_countries = [...item.factory_inventory_countries];
    if (isRecord(item.dimensions_cm)) {
      const dimensions: NonNullable<ProductCard["dimensions_cm"]> = {};
      let valid = true;
      for (const key of ["length", "width", "height"] as const) {
        const dimension = item.dimensions_cm[key];
        if (dimension === undefined) continue;
        if (!isAmount(dimension)) {
          valid = false;
          break;
        }
        dimensions[key] = dimension;
      }
      if (valid) card.dimensions_cm = dimensions;
    }
    if (isAmount(item.weight_kg)) card.weight_kg = item.weight_kg;
    if (
      typeof item.default_sku_id === "string" &&
      skus.some((sku) => sku.sku_id === item.default_sku_id)
    ) {
      card.default_sku_id = item.default_sku_id;
    }
    if (typeof item.quote_sku_id === "string" && skus.some(sku => sku.sku_id === item.quote_sku_id)) card.quote_sku_id = item.quote_sku_id;
    if (isAmount(item.source_price_major) && isCurrency(item.source_currency)) {
      card.source_price_major = item.source_price_major;
      card.source_currency = item.source_currency;
    }
    const landed = readLandedPrice(item.landed_price, card.currency);
    if (landed) card.landed_price = landed;
    return [card];
  });
}

