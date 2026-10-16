import type { RawDetail, RawOffer, RawProduct } from './contracts.js';
import { TorobError } from './errors.js';

export function normalizePersian(value: string): string {
  return value.normalize('NFKC').replace(/ي|ى/g, 'ی').replace(/ك/g, 'ک')
    .replace(/[۰-۹]/g, c => String('۰۱۲۳۴۵۶۷۸۹'.indexOf(c)))
    .replace(/[٠-٩]/g, c => String('٠١٢٣٤٥٦٧٨٩'.indexOf(c)))
    .replace(/[\u200c\u200d\u200e\u200f\u202a-\u202e\u2066-\u2069]/g, ' ')
    .replace(/[\u064b-\u065f\u0670]/g, '').replace(/\s+/g, ' ').trim();
}
export const text = (value: string | null | undefined, max = 400) => value?.slice(0, max) ?? null;
export const positivePrice = (value: unknown): number | null =>
  typeof value === 'number' && Number.isFinite(value) && value > 0 && value <= Number.MAX_SAFE_INTEGER ? value : null;
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export function productId(reference: string): string {
  const trimmed = reference.trim();
  if (uuid.test(trimmed)) return trimmed.toLowerCase();
  try {
    const url = new URL(trimmed);
    const parts = url.pathname.split('/');
    if (url.protocol === 'https:' && ['torob.com', 'www.torob.com'].includes(url.hostname) &&
      !url.port && !url.username && !url.password && parts[1] === 'p' && uuid.test(parts[2] ?? '')) {
      return parts[2]!.toLowerCase();
    }
  } catch { /* Return one consistent validation error below. */ }
  throw new TorobError('INVALID_PRODUCT', 'Use a Torob product UUID or an https://torob.com/p/<UUID>/ URL.');
}
export const productUrl = (id: string) => `https://torob.com/p/${id}/`;
export function safeHttpsUrl(value: string | null | undefined): string | null {
  if (!value) return null;
  try { const url = new URL(value); return url.protocol === 'https:' && !url.username && !url.password ? url.href : null; }
  catch { return null; }
}

export function productCard(raw: RawProduct, query?: string) {
  const normalizedTitle = normalizePersian(`${raw.name1} ${raw.name2 ?? ''}`).toLowerCase();
  const terms = query ? normalizePersian(query).toLowerCase().split(' ').filter(t => t.length > 1) : [];
  const unmatched = terms.filter(t => !normalizedTitle.includes(t));
  return {
    id: raw.random_key, title: text(raw.name1), title_en: text(raw.name2),
    min_price_toman: positivePrice(raw.price), price_text: text(raw.price_text),
    availability: raw.availability ?? null, condition_text: text(raw.stock_status),
    seller_summary: text(raw.shop_text), is_ad: raw.is_adv ?? false,
    torob_is_authentic: raw.is_authentic ?? null,
    image_url: safeHttpsUrl(raw.image_url), url: productUrl(raw.random_key),
    ...(query ? { low_confidence: unmatched.length > 0, unmatched_terms: unmatched } : {}),
  };
}
export type ProductCard = ReturnType<typeof productCard>;

export function offerCard(raw: RawOffer, type: 'online' | 'offline') {
  return {
    offer_id: raw.prk, shop_id: raw.shop_id ?? null, shop_name: text(raw.shop_name),
    city: text(raw.shop_name2), title: text(raw.name1), warranty_or_description: text(raw.name2),
    price_toman: positivePrice(raw.price), available: raw.availability ?? null,
    shop_type: type, is_ad: raw.is_adv ?? false,
    price_unreliable: raw.is_price_unreliable ?? false,
    shop_score: raw.score_info?.score ?? raw.shop_score ?? null,
    shop_score_text: text(raw.score_info?.score_text),
    shop_performance_notes: raw.score_info?.complaints_info?.summary?.slice(0, 5).map(v => text(v)) ?? [],
    torob_guarantee_status: text(raw.guarantee_info?.status),
    last_price_change_text: text(raw.last_price_change_date),
    shipping_fee_text: text(raw.postage_fee), purchase_warning: raw.show_purchase_warning ?? false,
    // Links are returned for the user; the server never follows buy/contact redirects.
    offer_url: type === 'online' ? safeHttpsUrl(raw.page_url) : null,
  };
}

export function specifications(raw: RawDetail, limit = 60) {
  const entries: { section: string | null; name: string; value: string; source: string }[] = [];
  for (const group of raw.key_specs ?? []) for (const item of group.items) {
    entries.push({ section: text(group.header), name: item.key.slice(0, 150),
      value: item.value.join('، ').slice(0, 1000), source: 'key_specs' });
  }
  for (const group of raw.structural_specs?.headers ?? []) for (const [name, value] of Object.entries(group.specs)) {
    const display = typeof value === 'string' || typeof value === 'number' ? String(value) :
      Array.isArray(value) ? value.filter(v => typeof v === 'string').join('، ') : '';
    if (display) entries.push({ section: text(group.header), name: name.slice(0, 150),
      value: display.slice(0, 1000), source: 'structural_specs' });
  }
  return { items: entries.slice(0, limit), truncated: entries.length > limit };
}

export function categories(values: unknown[] = []) {
  return values.slice(0, 60).flatMap(value => {
    if (!value || typeof value !== 'object') return [];
    const item = value as Record<string, unknown>;
    const id = Number(item.cat_id ?? item.id);
    if (!Number.isSafeInteger(id) || id <= 0 || typeof item.title !== 'string') return [];
    return [{ id, title: item.title.slice(0, 200), url: `https://torob.com/browse/${id}/` }];
  });
}
