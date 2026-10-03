import { useEffect, useMemo, useState, type FormEvent } from "react";
import type { DecisionReport, DecisionRequest, ProductCard } from "../types";
import { money, ProductImage } from "./ProductCards";
import "./decisionWorkbench.css";

type Draft = {
  query: string;
  category: string;
  shipTo: string;
  currency: string;
  budget: string;
  basis: DecisionRequest["budget_basis"];
  excluded: string;
  required: string;
};

const joinTags = (tags: string[]) => tags.join("、");
const splitTags = (value: string) => [...new Set(value.split(/[,，、;；\n]+/).map(tag => tag.trim()).filter(Boolean))];
const draftFrom = (request: DecisionRequest): Draft => ({
  query: request.normalized_query,
  category: request.category ?? "",
  shipTo: request.ship_to ?? "",
  currency: request.target_currency,
  budget: request.price_max_major === null ? "" : String(request.price_max_major),
  basis: request.budget_basis,
  excluded: joinTags(request.excluded_material_tags),
  required: joinTags(request.required_material_tags),
});
const requestFrom = (draft: Draft): DecisionRequest => ({
  normalized_query: draft.query.trim(),
  category: draft.category.trim() || null,
  ship_to: draft.shipTo.trim().toUpperCase() || null,
  target_currency: draft.currency.trim().toUpperCase(),
  price_max_major: draft.budget.trim() ? Number(draft.budget) : null,
  budget_basis: draft.basis,
  excluded_material_tags: splitTags(draft.excluded),
  required_material_tags: splitTags(draft.required),
});
const timeLabel = (value: string | null) => {
  if (!value) return "时间未记录";
  const time = Date.parse(value);
  return Number.isFinite(time) ? new Date(time).toLocaleString("zh-CN") : value || "时间未记录";
};
const evidenceName = (kind: string) => ({
  catalog: "商品目录",
  catalog_snapshot: "商品目录快照",
  tariff: "估算规则",
  tariff_schedule: "估算规则快照",
  rule_estimate: "规则估算",
}[kind] ?? (kind || "来源未标注"));

function Candidate({ candidate, index, onDetail }: {
  candidate: DecisionReport["candidates"][number];
  index: number;
  onDetail: (product: ProductCard) => void;
}) {
  const { product } = candidate;
  const sku = product.skus.find(item => item.sku_id === candidate.sku_id);
  const originalPrice = product.source_price_major !== undefined && product.source_currency
    ? { amount: product.source_price_major, currency: product.source_currency }
    : sku && sku.currency !== product.currency
      ? { amount: sku.price_major, currency: sku.currency }
      : null;
  const landed = product.landed_price;
  const quoteMatchesSku = !product.default_sku_id || product.default_sku_id === candidate.sku_id;
  const quoteChecked = candidate.checks.some(check =>
    check.field === "landed_price.landed_total_major" && check.label === "估算到手价" && check.status === "pass");
  const estimate = quoteChecked && quoteMatchesSku && landed && !landed.unavailable_reason && Number.isFinite(landed.landed_total_major) ? landed : null;
  return <article className="decision-candidate" style={{ animationDelay: `${index * 70}ms` }}>
    <div className="decision-candidate-lead">
      <div className="decision-candidate-image"><ProductImage product={product} /></div>
      <div className="decision-candidate-heading">
        <div className="decision-candidate-index"><span>候选 {String(index + 1).padStart(2, "0")}</span><span>{product.brand || product.category}</span></div>
        <h3>{product.title}</h3>
        <p>{candidate.sku_id ? `${sku?.spec || candidate.sku_id} · SKU ${candidate.sku_id}` : "规格待获取"}</p>
      </div>
      <button className="decision-detail-button" type="button" onClick={() => onDetail(product)}>查看商品详情 <span aria-hidden="true">↗</span></button>
    </div>
    <div className="decision-candidate-body">
      <div className="decision-price-strip">
        <div><small>{product.source_platform === "CJdropshipping" ? "CJ 列表参考价 · USD" : "目录商品价 · 目标币种"}</small><strong>{product.price_text || money(product.price_major, product.currency)}</strong>
          {originalPrice && originalPrice.currency !== product.currency && <small>原币参考 {money(originalPrice.amount, originalPrice.currency)}</small>}
        </div>
        <div><small>估算到手价 · 默认规格</small><strong>{estimate ? money(estimate.landed_total_major, estimate.currency) : "未知"}</strong></div>
      </div>
      {estimate && <p className="decision-price-detail">
        商品 {money(estimate.subtotal_major, estimate.currency)} ＋ 运费估算 {money(estimate.freight_major, estimate.currency)} ＋ 规则估算费用 {money(estimate.tariff_major, estimate.currency)}
      </p>}
      <div className="decision-narrative">
        <div><h4>入选理由</h4>{candidate.reasons.length ? <ul>{candidate.reasons.map((reason, i) => <li key={i}>{reason}</li>)}</ul> : <p>暂无可核验的入选理由。</p>}</div>
        <div><h4>需要权衡</h4>{candidate.tradeoffs.length ? <ul>{candidate.tradeoffs.map((tradeoff, i) => <li key={i}>{tradeoff}</li>)}</ul> : <p>暂无额外取舍记录。</p>}</div>
      </div>
      {candidate.unknowns.length > 0 && <div className="decision-unknowns"><strong>仍待确认</strong><ul>{candidate.unknowns.map((unknown, i) => <li key={i}>{unknown}</li>)}</ul></div>}
      <details className="decision-evidence">
        <summary>逐项核验与来源 <span>{candidate.checks.length} 项</span></summary>
        {candidate.checks.length ? <div className="decision-checks">{candidate.checks.map((check, i) => <div className="decision-check" key={`${check.field}-${i}`}>
          <div className="decision-check-head"><strong>{check.label}</strong><span className={check.status === "pass" ? "pass" : "unknown"}>{check.status === "pass" ? "已核验" : "未知"}</span></div>
          <p>{check.detail}</p>
          <small>{evidenceName(check.evidence.kind)} · {check.evidence.field || check.field} · {timeLabel(check.evidence.observed_at)}</small>
          <code title={check.evidence.ref ?? undefined}>{check.evidence.ref || "未提供引用"}</code>
        </div>)}</div> : <p className="decision-check-empty">暂无逐项核验记录。</p>}
      </details>
    </div>
  </article>;
}

export default function DecisionWorkbench({ report, busy, previewBusy, previewError, onPreview, onDetail }: {
  report: DecisionReport;
  busy: boolean;
  previewBusy: boolean;
  previewError: string | null;
  onPreview: (request: DecisionRequest) => void;
  onDetail: (product: ProductCard) => void;
}) {
  const [draft, setDraft] = useState<Draft>(() => draftFrom(report.request));
  useEffect(() => setDraft(draftFrom(report.request)), [report]);
  const nextRequest = useMemo(() => requestFrom(draft), [draft]);
  const changed = JSON.stringify(nextRequest) !== JSON.stringify(report.request);
  const budgetValid = nextRequest.price_max_major === null || Number.isFinite(nextRequest.price_max_major);
  const valid = nextRequest.normalized_query.length > 0 && nextRequest.normalized_query.length <= 500 && /^[A-Z]{3}$/.test(nextRequest.target_currency) &&
    (!nextRequest.ship_to || /^[A-Z]{2}$/.test(nextRequest.ship_to)) && budgetValid &&
    (nextRequest.price_max_major === null || nextRequest.price_max_major >= 0) &&
    (nextRequest.budget_basis !== "landed" || !!nextRequest.ship_to) &&
    nextRequest.excluded_material_tags.length <= 12 && nextRequest.required_material_tags.length <= 12;
  const set = (key: keyof Draft, value: string) => setDraft(current => ({ ...current, [key]: value }));
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (valid && changed && !busy && !previewBusy) onPreview(nextRequest);
  };
  return <section className="decision-workbench" aria-labelledby="decision-title">
    <div className="decision-head">
      <div><span className="decision-kicker">FINDORA / DECISION NOTE 02</span><h2 id="decision-title">这份选择，<em>有据可查。</em></h2></div>
      <div className="decision-head-meta"><span>{report.catalog_source === "cj" ? "CJ 商品快照" : "商品目录快照"}</span><span>生成于 {timeLabel(report.generated_at)}</span></div>
    </div>
    <div className="decision-layout">
      <form className="decision-controls" onSubmit={submit} aria-label="调整选购条件">
        <div className="decision-controls-top"><span>01 / 选购条件</span><strong>{changed ? "有待应用的修改" : "当前已应用"}</strong></div>
        <label>寻找什么<textarea rows={3} maxLength={500} value={draft.query} disabled={busy || previewBusy} onChange={event => set("query", event.target.value)} /></label>
        <div className="decision-fields-two">
          <label>品类<input maxLength={80} value={draft.category} disabled={busy || previewBusy} onChange={event => set("category", event.target.value)} placeholder="不限" /></label>
          <label>配送国家代码<input maxLength={2} value={draft.shipTo} disabled={busy || previewBusy} onChange={event => set("shipTo", event.target.value)} placeholder="如 CN" /></label>
        </div>
        <div className="decision-fields-two">
          <label>预算上限<input type="number" inputMode="decimal" min="0" step="0.01" value={draft.budget} disabled={busy || previewBusy} onChange={event => set("budget", event.target.value)} placeholder="不限" /></label>
          <label>币种<input maxLength={3} value={draft.currency} disabled={busy || previewBusy} onChange={event => set("currency", event.target.value)} aria-label="目标币种三字母代码" /></label>
        </div>
        <fieldset><legend>预算按什么计算</legend><label><input type="radio" name="decision-budget-basis" checked={draft.basis === "product"} disabled={busy || previewBusy} onChange={() => set("basis", "product")} />商品价</label><label><input type="radio" name="decision-budget-basis" checked={draft.basis === "landed"} disabled={busy || previewBusy} onChange={() => set("basis", "landed")} />估算到手价</label></fieldset>
        <label>必须包含的材质<input value={draft.required} disabled={busy || previewBusy} onChange={event => set("required", event.target.value)} placeholder="用逗号分隔；可留空" /></label>
        <label>需要排除的材质<input value={draft.excluded} disabled={busy || previewBusy} onChange={event => set("excluded", event.target.value)} placeholder="用逗号分隔；可留空" /></label>
        {!valid && <p className="decision-form-hint" role="alert">请填写需求、三字母币种和两字母配送国家代码；到手价预算需填写目的地，预算须非负，每类材质最多 12 项。</p>}
        {previewError && <p className="decision-form-error" role="alert">调整未生效：{previewError}。下方仍为上次条件的结果。</p>}
        <button className="decision-submit" type="submit" disabled={!changed || !valid || busy || previewBusy}>{previewBusy ? "正在重新核验…" : "按新条件重新核验"}<span aria-hidden="true">↗</span></button>
        <p className="decision-controls-note">调整条件会重新计算候选；未知信息仍保持未知。</p>
      </form>
      <div className="decision-results" aria-live="polite" aria-busy={previewBusy}>
        <div className="decision-results-heading"><div><span>02 / 候选结果</span><h3>{report.candidates.length ? `${report.candidates.length} 件值得继续看` : "暂未找到符合条件的商品"}</h3></div><span className="decision-limit">最多展示 5 件</span></div>
        {previewBusy ? <div className="decision-loading" role="status">正在根据新条件重新核验商品与价格…</div> : <>
          {changed && <p className="decision-pending" role="status">下方结果仍按上次已应用条件生成。点击“重新核验”后更新。</p>}
          {report.candidates.length ? <div className="decision-candidates">{report.candidates.slice(0, 5).map((candidate, index) => <Candidate key={`${candidate.product.product_id}-${candidate.sku_id}`} candidate={candidate} index={index} onDetail={onDetail} />)}</div>
            : <div className="decision-no-match"><strong>条件需要再放宽一些</strong><p>当前没有可核验的合格候选。试着调整预算、材质或配送国家后重新核验。</p></div>}
          {report.excluded.length > 0 && <details className="decision-excluded"><summary>查看未入选商品与原因 <span>{report.excluded.length} 件</span></summary><ul>{report.excluded.map((item, i) => <li key={`${item.product_id}-${i}`}><strong>{item.title}</strong><span>{item.reason}</span></li>)}</ul></details>}
        </>}
      </div>
    </div>
    <p className="decision-footnote">{report.catalog_source === "cj" ? "商品来自 CJ 快照；列表报价不代表最终结算价，库存、运费与配送范围须另行核验。" : "商品信息来自目录快照；到手价为规则估算。价格、库存与配送信息以实际确认时为准。"}</p>
  </section>;
}
