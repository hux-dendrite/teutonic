"use strict";

const assert = require("assert");
const dashboard = require("../../website/dashboard-v1.js");

function base() {
    return {
        schema_version: 1,
        publication_id: "11111111-1111-4111-8111-111111111111",
        generated_at: "2026-08-18T12:00:00Z",
        updated_at: "2026-08-18T12:00:00Z",
        source_watermark: 1,
        chain: {
            name: "Teutonic", netuid: 306, generation: "test", competition: "quasar",
            seed_repo: "owner/genesis", seed_digest: "hf:" + "b".repeat(40), seed_repo_backend: "hf"
        },
        king: null,
        king_payout: { weight: null, alpha_per_hour: null, usd_per_hour: null },
        king_chain: [],
        stats: {},
        current_eval: null,
        queue: [],
        history: [],
        weight_status: {},
        service_status: { overall: "healthy" },
        market: null
    };
}

function hiddenRecord() {
    return { model_identity: "hidden_until_promotion", challenger_repo: null };
}

const idle = dashboard.presentation(base());
assert.strictEqual(idle.idle, true);
assert.strictEqual(idle.historyEmpty, true);

const queuedPayload = base();
queuedPayload.queue = [hiddenRecord()];
const queued = dashboard.presentation(queuedPayload);
assert.strictEqual(queued.idle, false);
assert.strictEqual(queued.duelIdentity, dashboard.HIDDEN);

const evaluatingPayload = base();
evaluatingPayload.current_eval = hiddenRecord();
assert.strictEqual(dashboard.presentation(evaluatingPayload).duelIdentity, dashboard.HIDDEN);
assert.deepStrictEqual(
    dashboard.currentEvaluationPresentation({
        provisional_mu_hat: 0.72,
        provisional_lcb: 0.61,
        delta_threshold: 0.5,
        provisional_n_sequences: 400,
        provisional_n_bootstrap: 1000
    }),
    {
        available: true,
        lcb: 0.61,
        muHat: 0.72,
        threshold: 0.5,
        sequences: 400,
        bootstraps: 1000,
        clearsThreshold: true
    }
);
assert.strictEqual(dashboard.currentEvaluationPresentation({}).available, false);
assert.strictEqual(
    dashboard.currentEvaluationPresentation({ provisional_lcb: null }).available,
    false
);

const rejectedPayload = base();
rejectedPayload.history = [hiddenRecord()];
assert.strictEqual(dashboard.presentation(rejectedPayload).historyIdentity[0], dashboard.HIDDEN);

const winnerPayload = base();
winnerPayload.history = [{ model_identity: "public", challenger_repo: "owner/winner" }];
assert.strictEqual(dashboard.presentation(winnerPayload).historyIdentity[0], "owner/winner");

const nonWinnerPayload = base();
nonWinnerPayload.history = [{ model_identity: "public", challenger_repo: "owner/non-winner" }];
assert.strictEqual(dashboard.presentation(nonWinnerPayload).historyIdentity[0], "owner/non-winner");

const degradedPayload = base();
degradedPayload.service_status.overall = "degraded";
assert.strictEqual(dashboard.presentation(degradedPayload).degraded, true);

const staleMarketPayload = base();
staleMarketPayload.market = { stale: true };
assert.strictEqual(dashboard.presentation(staleMarketPayload).marketStale, true);

const historyRows = [
    { verdict: "accepted", challenge_id: "accepted" },
    { verdict: "error", challenge_id: "failed" },
    { verdict: "rejected", challenge_id: "rejected" }
];
const hiddenErrors = dashboard.historyPresentation(historyRows, false);
assert.deepStrictEqual(hiddenErrors.rows.map((row) => row.challenge_id), ["accepted", "rejected"]);
assert.strictEqual(hiddenErrors.errorCount, 1);
assert.strictEqual(dashboard.historyPresentation(historyRows, true).rows.length, 3);

assert.strictEqual(
    dashboard.taoMarketCapHotkeyUrl("5MinerHotkey"),
    "https://taomarketcap.com/hotkey/5MinerHotkey/metagraph"
);
assert.strictEqual(
    dashboard.taoMarketCapHotkeyUrl("5Miner Hotkey"),
    "https://taomarketcap.com/hotkey/5Miner%20Hotkey/metagraph"
);
assert.strictEqual(dashboard.taoMarketCapHotkeyUrl(""), "");

const shards = dashboard.shardPresentation({
    shards_used: [
        { source: "finewebedu", names: ["part-001.npy", "part-002.npy", "part-001.npy"] },
        { source: " ", names: ["part-003.npy", ""] },
        { source: "ignored", names: [] }
    ]
});
assert.strictEqual(shards.count, 3);
assert.deepStrictEqual(shards.groups, [
    { source: "finewebedu", names: ["part-001.npy", "part-002.npy"] },
    { source: "dataset", names: ["part-003.npy"] }
]);
assert.deepStrictEqual(dashboard.shardPresentation({}).groups, []);

assert.deepStrictEqual(
    dashboard.uploadFailurePresentation({
        uid: 170,
        registration_state: "active",
        upload_id: "9452d08b-b3bb-4bc9-9c45-01889fff6fa8",
        upload_state: "verification_failed",
        error_code: "ArtifactIntegrityError"
    }),
    {
        registration: "UID 170 · ACTIVE",
        uploadId: "9452d08b-b3bb-4bc9-9c45-01889fff6fa8",
        uploadState: "verification_failed",
        failureCode: "ArtifactIntegrityError"
    }
);
assert.strictEqual(dashboard.uploadFailurePresentation({ verdict: "error" }), null);

assert.deepStrictEqual(
    dashboard.decisionPresentation({ verdict: "accepted", lcb: 0.64, delta: 0.5 }),
    {
        kind: "win",
        label: "WIN REASON",
        summary: "LCB 0.640000 > REQUIRED 0.500000 · MARGIN +0.140000",
        detail: "The confidence-adjusted improvement was high enough to replace the king."
    }
);
assert.deepStrictEqual(
    dashboard.decisionPresentation({ verdict: "rejected", lcb: 0.38, delta_threshold: 0.5 }),
    {
        kind: "loss",
        label: "LOSS REASON",
        summary: "LCB 0.380000 ≤ REQUIRED 0.500000 · SHORTFALL 0.120000",
        detail: "The measured improvement was not confident enough to replace the king."
    }
);
assert.strictEqual(dashboard.decisionPresentation({ verdict: "error" }).kind, "error");

const invalid = base();
invalid.schema_version = 2;
assert.throws(() => dashboard.presentation(invalid), /unsupported dashboard schema/);

const datasetManifest = {
    schema_version: 1,
    dataset_label: "fixture-mix",
    eval_n: 300,
    sources: [{
        name: "fixture",
        proportion: 1,
        manifest_url: "https://datasets.example/fixture/manifest.json",
        manifest_sha256: "b".repeat(64),
        source_repo: "owner/dataset",
        tokenizer: "owner/tokenizer",
        dtype: "uint32",
        tokenization_mode: "seq_packed_shards",
        sequence_length: 2048,
        total_tokens: 2_048_000,
        total_shards: 4,
        estimated_sequences: 1000
    }]
};
const dataset = dashboard.datasetPresentation(datasetManifest);
assert.strictEqual(dataset.rows.length, 1);
assert.strictEqual(dataset.totalTokens, 2_048_000);
assert.strictEqual(dataset.totalSequences, 1000);
assert.strictEqual(dataset.evalTokens, 614_400);
assert.strictEqual(dataset.rows[0].normalizedWeight, 1);
assert.strictEqual(dataset.rows[0].source, "owner/dataset");
assert.strictEqual(dataset.rows[0].metadataLoaded, true);
assert.throws(() => dashboard.datasetPresentation({}, {}), /sources must be an array/);
console.log("dashboard-v1 representative render states passed");
