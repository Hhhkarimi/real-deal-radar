import * as z from 'zod/v4';

const optionalText = z.string().nullish();
const optionalNumber = z.number().finite().nullish();
export const productSchema = z.looseObject({
  random_key: z.uuid(),
  name1: z.string().min(1),
  name2: optionalText,
  price: optionalNumber,
  price_text: optionalText,
  price_text_mode: optionalText,
  shop_text: optionalText,
  stock_status: optionalText,
  is_adv: z.boolean().optional(),
  availability: z.boolean().optional(),
  image_url: optionalText,
  is_authentic: z.boolean().optional(),
});
export const offerSchema = z.looseObject({
  prk: z.uuid(),
  shop_id: optionalNumber,
  shop_name: z.string().min(1),
  shop_name2: optionalText,
  name1: optionalText,
  name2: optionalText,
  price: optionalNumber,
  price_text_mode: optionalText,
  availability: z.boolean().optional(),
  is_adv: z.boolean().optional(),
  is_price_unreliable: z.boolean().optional(),
  shop_score: optionalNumber,
  page_url: optionalText,
  last_price_change_date: optionalText,
  postage_fee: optionalText,
  show_purchase_warning: z.boolean().optional(),
  score_info: z.looseObject({
    score: optionalNumber,
    score_text: optionalText,
    complaints_info: z.looseObject({ summary: z.array(z.string()).optional() }).nullish(),
  }).nullish(),
  guarantee_info: z.looseObject({ status: optionalText }).nullish(),
});
export const listingSchema = z.looseObject({
  results: z.array(productSchema),
  count: optionalNumber,
  next: optionalText,
  previous: optionalText,
  min_price: optionalNumber,
  max_price: optionalNumber,
  categories: z.array(z.unknown()).optional(),
  parent_categories: z.array(z.unknown()).optional(),
  attributes: z.array(z.unknown()).optional(),
  spellcheck: z.object({
    is_spellchecked: z.boolean().optional(), initial_query: z.string().max(1000).optional(),
    corrected_query: z.string().max(1000).optional(),
  }).nullish(),
});
const sellerGroupSchema = z.looseObject({
  result: z.array(offerSchema),
  count: optionalNumber,
  sellers_url: optionalText,
});
export const detailSchema = productSchema.extend({
  min_price: optionalNumber,
  max_price: optionalNumber,
  products_info: sellerGroupSchema.nullish(),
  products_in_store_info: sellerGroupSchema.nullish(),
  torob_category: optionalNumber,
  seo_description: optionalText,
  key_specs: z.array(z.looseObject({
    header: optionalText,
    items: z.array(z.looseObject({ key: z.string(), value: z.array(z.string()) })),
  })).optional(),
  structural_specs: z.looseObject({
    source: optionalText,
    headers: z.array(z.looseObject({
      header: optionalText,
      specs: z.record(z.string(), z.unknown()),
    })),
  }).nullish(),
  breadcrumbs: z.array(z.unknown()).optional(),
});
export const sellersSchema = z.looseObject({ results: z.array(offerSchema), count: optionalNumber });
export const suggestionsSchema = z.array(z.looseObject({ text: z.string() }));
export type RawProduct = z.infer<typeof productSchema>;
export type RawOffer = z.infer<typeof offerSchema>;
export type RawDetail = z.infer<typeof detailSchema>;

export const searchInputSchema = z.strictObject({
  query: z.string().trim().min(1).max(200).describe('Persian or English product search.'),
  page: z.number().int().min(0).max(99).default(0).describe('Zero-based upstream page.'),
  limit: z.number().int().min(1).max(30).default(10),
  sort: z.enum(['relevance', 'price_asc', 'price_desc']).default('relevance'),
  min_price_toman: z.number().int().min(0).max(Number.MAX_SAFE_INTEGER).optional(),
  max_price_toman: z.number().int().min(0).max(Number.MAX_SAFE_INTEGER).optional(),
  category_id: z.number().int().positive().optional(),
  brand_id: z.number().int().positive().optional(),
  include_ads: z.boolean().default(false),
}).refine(v => v.min_price_toman === undefined || v.max_price_toman === undefined ||
  v.min_price_toman <= v.max_price_toman, { message: 'min_price_toman must not exceed max_price_toman.' });
export type SearchInput = z.infer<typeof searchInputSchema>;
export const productRefSchema = z.string().trim().min(1).max(1200)
  .describe('Torob product UUID from search, or an https://torob.com/p/<UUID>/ URL.');
export const productInputSchema = z.strictObject({ product: productRefSchema });
export const sellersInputSchema = z.strictObject({
  product: productRefSchema,
  limit: z.number().int().min(1).max(50).default(15),
  shop_type: z.enum(['online', 'offline']).default('online'),
  include_ads: z.boolean().default(false),
  available_only: z.boolean().default(true),
});
export const browseInputSchema = z.strictObject({
  category_id: z.number().int().positive(),
  page: z.number().int().min(0).max(99).default(0),
  limit: z.number().int().min(1).max(30).default(10),
  sort: z.enum(['relevance', 'price_asc', 'price_desc']).default('relevance'),
  include_ads: z.boolean().default(false),
});
