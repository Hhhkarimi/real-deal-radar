import * as z from 'zod/v4';
import { browseInputSchema, detailSchema, listingSchema, searchInputSchema, sellersInputSchema,
  sellersSchema, suggestionsSchema, type SearchInput } from './contracts.js';
import { TorobError, errorPayload } from './errors.js';
import { categories, normalizePersian, offerCard, positivePrice, productCard, productId,
  specifications, text, type ProductCard } from './normalize.js';
import { TorobClient } from './torob-client.js';

const SORT = { relevance: 'popularity', price_asc: 'price', price_desc: '-price' } as const;
const unique = <T>(values: T[]) => [...new Set(values)];
export class TorobService {
  constructor(private readonly client = new TorobClient()) {}

  private async listing(input: SearchInput | (z.infer<typeof browseInputSchema> & { query?: string })) {
    const query = input.query === undefined ? undefined : normalizePersian(input.query);
    if (input.query !== undefined && !query) throw new TorobError('INVALID_QUERY', 'The normalized query is empty.');
    const search = input as Partial<SearchInput>;
    return this.client.get('/v4/base-product/search/', {
      q: query, category: input.category_id, brand: search.brand_id,
      page: input.page, size: input.limit, sort: SORT[input.sort],
      // Torob's public web client uses __gt / __lt (not __gte / __lte).
      price__gt: search.min_price_toman, price__lt: search.max_price_toman,
    }, listingSchema);
  }

  async search(rawInput: unknown) {
    const input = searchInputSchema.parse(rawInput);
    const { data, provenance } = await this.listing(input);
    let items = data.results.filter(p => input.include_ads || !p.is_adv).map(p => productCard(p, input.query));
    items = items.filter(p => (input.min_price_toman === undefined ||
      p.min_price_toman !== null && p.min_price_toman >= input.min_price_toman) &&
      (input.max_price_toman === undefined || p.min_price_toman !== null && p.min_price_toman <= input.max_price_toman));
    items = this.sort(items, input.sort).slice(0, input.limit);
    return { ...provenance, query: normalizePersian(input.query), page: input.page,
      next_page: data.next ? input.page + 1 : null, total_estimate: data.count ?? null,
      returned: items.length, upstream_returned: data.results.length,
      filtered_out: data.results.length - items.length, coverage: 'single_upstream_page',
      sort: input.sort, spellcheck: data.spellcheck ?? null, items };
  }

  async browse(rawInput: unknown) {
    const input = browseInputSchema.parse(rawInput);
    const { data, provenance } = await this.listing(input);
    const items = this.sort(data.results.filter(p => input.include_ads || !p.is_adv).map(p => productCard(p)), input.sort)
      .slice(0, input.limit);
    return { ...provenance, category_id: input.category_id, page: input.page,
      next_page: data.next ? input.page + 1 : null, total_estimate: data.count ?? null,
      categories: categories(data.categories), parent_categories: categories(data.parent_categories),
      returned: items.length, items };
  }

  async suggest(query: string, limit = 10) {
    const normalized = normalizePersian(query);
    if (!normalized) throw new TorobError('INVALID_QUERY', 'The normalized query is empty.');
    const { data, provenance } = await this.client.get('/suggestion2/', { q: normalized }, suggestionsSchema);
    return { ...provenance, query: normalized, suggestions: unique(data.map(i => i.text)).slice(0, limit) };
  }

  async filters(query: string, categoryId?: number) {
    const { data, provenance } = await this.listing({
      query, category_id: categoryId, page: 0, limit: 1, sort: 'relevance', include_ads: false,
    });
    const attributes = (data.attributes ?? []).slice(0, 30).flatMap(value => {
      if (!value || typeof value !== 'object') return [];
      const item = value as Record<string, unknown>;
      if (typeof item.title !== 'string' || typeof item.slug !== 'string') return [];
      const all = Array.isArray(item.items) ? item.items : [];
      return [{ title: item.title.slice(0, 200), slug: item.slug, type: item.type,
        items: all.slice(0, 60).map(v => {
          const option = v as Record<string, unknown>;
          const scalar = (v: unknown) => typeof v === 'string' ? v.slice(0, 300) : typeof v === 'number' ? v : null;
          return { id: scalar(option.id), value: scalar(option.value), name: scalar(option.name ?? option.name1), name_en: scalar(option.name2) };
        }), truncated: all.length > 60 }];
    });
    return { ...provenance, query: normalizePersian(query),
      price_range_toman: { min: positivePrice(data.min_price), max: positivePrice(data.max_price) },
      categories: categories(data.categories), parent_categories: categories(data.parent_categories), attributes,
      supported_search_filters: ['category_id', 'brand_id', 'min_price_toman', 'max_price_toman'],
      note: 'Other attributes are discovery information; this server does not expose arbitrary upstream filters.' };
  }

  private detail(reference: string) {
    return this.client.get('/v4/base-product/details/', { prk: productId(reference) }, detailSchema);
  }

  async details(reference: string) {
    const { data, provenance } = await this.detail(reference);
    return { ...provenance, ...productCard(data), category_id: data.torob_category ?? null,
      max_price_toman: positivePrice(data.max_price), description: text(data.seo_description, 1500),
      breadcrumbs: categories(data.breadcrumbs), specifications: specifications(data),
      online_offers_preview: (data.products_info?.result ?? []).filter(o => !o.is_adv)
        .map(o => offerCard(o, 'online')).sort((a, b) => (a.price_toman ?? Infinity) - (b.price_toman ?? Infinity)).slice(0, 5),
      offers_preview_is_complete: false,
      note: 'Use product_sellers for the loaded seller list. Offers may differ in condition, warranty, registration and shipping.' };
  }

  async sellers(rawInput: unknown) {
    const input = sellersInputSchema.parse(rawInput);
    const { data, provenance: detailProvenance } = await this.detail(input.product);
    const group = input.shop_type === 'online' ? data.products_info : data.products_in_store_info;
    let offers = group?.result ?? [], total = group?.count ?? null, provenance = detailProvenance;
    let complete = group ? (group.count !== null && group.count !== undefined && offers.length >= group.count) : false;
    if (group?.sellers_url) {
      // Only follow the exact sellers path for this product; never follow arbitrary links from upstream data.
      const url = new URL(group.sellers_url);
      if (url.origin !== 'https://api.torob.com' || url.pathname !== '/v4/base-product/sellers/' ||
        url.searchParams.get('prk') !== data.random_key || url.username || url.password) {
        throw new TorobError('UPSTREAM_SCHEMA_CHANGED', 'Torob returned an unexpected seller-list URL.');
      }
      const params = Object.fromEntries(url.searchParams);
      const loaded = await this.client.get('/v4/base-product/sellers/', params, sellersSchema);
      offers = loaded.data.results;
      total = loaded.data.count ?? total;
      provenance = loaded.provenance;
      complete = total !== null && offers.length >= total;
    }
    const deduplicated = [...new Map(offers.map(o => [o.prk, o])).values()];
    const filtered = deduplicated.filter(o => (input.include_ads || !o.is_adv) &&
      (!input.available_only || o.availability !== false && positivePrice(o.price) !== null));
    const items = filtered.map(o => offerCard(o, input.shop_type))
      .sort((a, b) => (a.price_toman ?? Infinity) - (b.price_toman ?? Infinity));
    return { ...provenance, product: productCard(data), shop_type: input.shop_type,
      upstream_offer_count_estimate: total, loaded_offers: deduplicated.length,
      loaded_list_complete: complete, matching_offers: items.length,
      unique_shops_in_matching_offers: new Set(filtered.map(o => o.shop_id ?? o.shop_name)).size,
      returned: Math.min(items.length, input.limit), truncated: items.length > input.limit,
      items: items.slice(0, input.limit),
      note: 'Sorted by listed price. Shipping is excluded. Several offers can belong to the same shop; verify condition and warranty.' };
  }

  async similar(reference: string, limit = 10, includeAds = false) {
    const id = productId(reference);
    const { data, provenance } = await this.client.get('/v4/base-product/similar-base-product/',
      { prk: id, limit }, listingSchema);
    const items = data.results.filter(p => includeAds || !p.is_adv).slice(0, limit).map(p => productCard(p));
    return { ...provenance, product_id: id, returned: items.length, items };
  }

  async batch(references: string[]) {
    const ids = unique(references.map(productId));
    const items = [];
    for (const id of ids) {
      try { items.push({ id, ok: true, product: await this.details(id) }); }
      catch (error) {
        if (!(error instanceof TorobError)) throw error;
        items.push({ id, ok: false, ...errorPayload(error) });
        // A block applies to the whole origin; stop issuing requests after the first one.
        if (error.code === 'UPSTREAM_BLOCKED') {
          for (const remaining of ids.slice(items.length)) items.push({ id: remaining, ok: false,
            error: { code: 'SKIPPED_UPSTREAM_BLOCKED', message: 'Skipped because Torob asked the server to wait.' } });
          break;
        }
      }
    }
    return { requested: ids.length, succeeded: items.filter(i => i.ok).length, items };
  }

  async compare(references: string[]) {
    const ids = unique(references.map(productId));
    if (ids.length < 2) throw new TorobError('INVALID_PRODUCTS', 'Provide at least two distinct products.');
    const products: Awaited<ReturnType<TorobService['details']>>[] = [];
    for (const id of ids) products.push(await this.details(id));
    const keys = unique(products.flatMap(p => p.specifications.items.map(s => `${s.source}|${s.section}|${s.name}`)));
    const differences = keys.map(key => {
      const specs = products.map(p => p.specifications.items.find(s => `${s.source}|${s.section}|${s.name}` === key));
      const first = specs.find(s => s !== undefined)!;
      return { section: first.section, name: first.name, source: first.source,
        values: products.map((p, i) => ({ product_id: p.id, value: specs[i]?.value ?? null })) };
    }).filter(row => new Set(row.values.map(v => v.value)).size > 1);
    return { source: 'torob.com', currency: 'IRT', price_unit: 'toman', products: products.map(p => ({
      id: p.id, title: p.title, title_en: p.title_en, min_price_toman: p.min_price_toman,
      max_price_toman: p.max_price_toman, availability: p.availability, condition_text: p.condition_text,
      seller_summary: p.seller_summary, is_ad: p.is_ad, torob_is_authentic: p.torob_is_authentic,
      image_url: p.image_url, url: p.url,
      fetched_at: p.fetched_at, cache_expires_at: p.cache_expires_at, cached: p.cached,
      specifications_truncated: p.specifications.truncated,
    })), differing_specifications: differences.slice(0, 60),
      differences_truncated: differences.length > 60,
      note: 'Missing specifications are null. Listing prices alone do not establish product quality or seller reliability.' };
  }

  async cheapest(rawInput: unknown, pages = 2) {
    const input = searchInputSchema.parse(rawInput);
    const items = new Map<string, ProductCard>();
    const fetched = [];
    let pagesRead = 0, nextPage: number | null = 0;
    for (let page = 0; page < pages && nextPage !== null; page++) {
      const result = await this.search({ ...input, sort: 'price_asc', page, limit: 30 });
      pagesRead++;
      fetched.push({ page, fetched_at: result.fetched_at, cache_expires_at: result.cache_expires_at });
      for (const item of result.items) if (item.min_price_toman !== null) items.set(item.id, item);
      nextPage = result.next_page;
    }
    const ranked = this.sort([...items.values()], 'price_asc').slice(0, input.limit);
    return { source: 'torob.com', currency: 'IRT', price_unit: 'toman', query: normalizePersian(input.query),
      pages_read: pagesRead, coverage: 'bounded_search_pages', more_pages_available: nextPage !== null,
      fetched_pages: fetched, unique_candidates: items.size, returned: ranked.length, items: ranked,
      note: 'Lowest listed prices among the scanned pages; no guarantee of the market minimum. Check low_confidence, condition and shipping.' };
  }

  private sort(items: ProductCard[], sort: SearchInput['sort']) {
    if (sort === 'relevance') return items;
    return items.sort((a, b) => {
      if (a.min_price_toman === null) return b.min_price_toman === null ? 0 : 1;
      if (b.min_price_toman === null) return -1;
      return sort === 'price_asc' ? a.min_price_toman - b.min_price_toman : b.min_price_toman - a.min_price_toman;
    });
  }
}
