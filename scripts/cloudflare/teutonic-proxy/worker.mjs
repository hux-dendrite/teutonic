// Cloudflare Worker bound to teutonic.ai and www.teutonic.ai.
// Reverse-proxies dashboard assets to their Cloudflare R2 bucket. The dataset
// manifest route is resolved from the active dashboard bucket.
//
// Account: 00523074f51300584834607253cae0fa
// Zone:    1075a976f65a8acdfeb5109615bb5906 (teutonic.ai)
// Worker:  teutonic-proxy
// Deploy:  scripts/cloudflare/deploy.sh
//
// For HTML/JSON/markdown we strip conditional-request headers on the way out,
// drop Last-Modified on the way back, and disable caching so live dashboard
// state is never hidden by stale browser or intermediary responses.

const ORIGIN = "https://pub-e2009eec1ca9488699de6263f40bb7e7.r2.dev";
const DATASET_ORIGIN = "https://pub-fedac496355c4edc9aed57189e6e190f.r2.dev";
const DATASET_MANIFEST_PATH = "/datasets/manifest.json";

// Content types that drive the live dashboard. These must always reflect
// the current bytes in the bucket, so we disable every layer of caching.
const NO_CACHE_TYPES = [
  /^text\/html/i,
  /^application\/json/i,
  /^text\/markdown/i,
  /^text\/plain/i,
];

// Conditional-request headers are not forwarded for live dashboard assets.
const REQ_STRIP = ["if-modified-since", "if-none-match"];

// Upstream noise we don't need to expose.
const RESP_STRIP = ["server"];

export default {
  async fetch(request) {
    const url = new URL(request.url);
    const path = url.pathname === "" || url.pathname === "/" ? "/index.html" : url.pathname;

    if (path === DATASET_MANIFEST_PATH && request.method !== "GET" && request.method !== "HEAD") {
      return new Response("Method not allowed", { status: 405, headers: { Allow: "GET, HEAD" } });
    }

    const headers = new Headers(request.headers);
    for (const h of REQ_STRIP) headers.delete(h);

    let target;
    if (path === DATASET_MANIFEST_PATH) {
      target = DATASET_ORIGIN + DATASET_MANIFEST_PATH + (url.search || "");
    } else {
      target = ORIGIN + path + (url.search || "");
    }
    const upstream = await fetch(target, {
      method: request.method,
      headers,
      body: request.method === "GET" || request.method === "HEAD" ? undefined : request.body,
      redirect: "follow",
      // Bypass the Cloudflare edge cache entirely for this fetch. We make
      // per-request caching decisions on the response below.
      cf: { cacheTtl: 0, cacheEverything: false },
    });

    const r = new Response(upstream.body, upstream);
    for (const h of RESP_STRIP) r.headers.delete(h);
    r.headers.set("x-served-by", "teutonic.ai/worker");

    const ct = r.headers.get("content-type") || "";
    if (NO_CACHE_TYPES.some((re) => re.test(ct))) {
      // Force browsers to revalidate every load. ETag is a real content
      // hash from S3, so 304s remain possible and cheap; the bogus
      // Last-Modified gets dropped so it can't cause a stale 304.
      r.headers.set("Cache-Control", "no-cache, must-revalidate");
      r.headers.delete("Last-Modified");
      // Belt-and-suspenders for any intermediary that ignores no-cache.
      r.headers.set("Pragma", "no-cache");
    }

    return r;
  },
};
