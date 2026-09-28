import type { DecisionCheck, DecisionReport, DecisionRequest } from "../types";
import { readProducts } from "./commerceClient";

const record = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value);
const strings = (value: unknown): string[] =>
  Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
const nullableString = (value: unknown): value is string | null =>
  value === null || typeof value === "string";

function readRequest(value: unknown): DecisionRequest | null {
  if (!record(value) || typeof value.normalized_query !== "string" ||
      !nullableString(value.category) || !nullableString(value.ship_to) ||
      typeof value.target_currency !== "string" ||
      !/^[A-Z]{3}$/.test(value.target_currency) ||
      !(value.price_max_major === null ||
        (typeof value.price_max_major === "number" && Number.isFinite(value.price_max_major) && value.price_max_major >= 0)) ||
      (value.budget_basis !== "product" && value.budget_basis !== "landed")) return null;
  return {
    normalized_query: value.normalized_query,
    category: value.category,
    ship_to: value.ship_to,
    target_currency: value.target_currency,
    price_max_major: value.price_max_major,
    budget_basis: value.budget_basis,
    excluded_material_tags: strings(value.excluded_material_tags),
    required_material_tags: strings(value.required_material_tags),
  };
}

function readCheck(value: unknown): DecisionCheck | null {
  if (!record(value) || typeof value.field !== "string" ||
      typeof value.label !== "string" || typeof value.detail !== "string" ||
      (value.status !== "pass" && value.status !== "unknown") ||
      !record(value.evidence) || typeof value.evidence.kind !== "string" ||
      !nullableString(value.evidence.ref) || typeof value.evidence.field !== "string" ||
      !nullableString(value.evidence.observed_at)) return null;
  return {
    field: value.field,
    label: value.label,
    status: value.status,
    detail: value.detail,
    evidence: {
      kind: value.evidence.kind,
      ref: value.evidence.ref,
      field: value.evidence.field,
      observed_at: value.evidence.observed_at,
    },
  };
}

/** Treat the decision sheet as server data; malformed items never become product claims. */
export function readDecisionReport(value: unknown): DecisionReport | null {
  if (!record(value) || value.version !== 2 ||
      (value.status !== "ready" && value.status !== "no_match") ||
      typeof value.generated_at !== "string" ||
      !Array.isArray(value.candidates) || !Array.isArray(value.excluded)) return null;
  const request = readRequest(value.request);
  if (!request) return null;
  const candidates = value.candidates.flatMap((item) => {
    if (!record(item) || typeof item.sku_id !== "string") return [];
    const product = readProducts([item.product])[0];
    if (!product) return [];
    return [{
      product,
      sku_id: item.sku_id,
      checks: Array.isArray(item.checks) ? item.checks.flatMap((check) => {
        const parsed = readCheck(check);
        return parsed ? [parsed] : [];
      }) : [],
      reasons: strings(item.reasons),
      tradeoffs: strings(item.tradeoffs),
      unknowns: strings(item.unknowns),
    }];
  }).slice(0, 5);
  const excluded = value.excluded.flatMap((item) =>
    record(item) && typeof item.product_id === "string" &&
    typeof item.title === "string" && typeof item.reason === "string"
      ? [{ product_id: item.product_id, title: item.title, reason: item.reason }]
      : []);
  return {
    version: 2,
    catalog_source: value.catalog_source === "cj" ? "cj" : "fixture",
    request,
    status: value.status,
    candidates,
    excluded,
    evidence_refs: strings(value.evidence_refs),
    generated_at: value.generated_at,
  };
}
